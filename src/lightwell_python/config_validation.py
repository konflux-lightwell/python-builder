"""Validate the full build-config schema inherited from calunga-python.

Checks field types, canonical package identity, PEP 440 version coverage,
backend environment tokens, and explicit source-relative rewrite rules.
Fromager settings remain opaque and are validated by Fromager at build time.
See NOTICE for source provenance. Runtime file containment is enforced by
lightwell_python.versioning in addition to these static syntax checks.
"""

import json
import re
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from lightwell_python.runtime import read_json

REQUIRED_TOP_LEVEL_FIELDS = (
    "package",
    "canonical_name",
    "upstream_registry",
    "lightwell_builds_repo",
    "versions",
)
REQUIRED_STRING_FIELDS = (
    "package",
    "canonical_name",
    "upstream_registry",
    "lightwell_builds_repo",
)
OPTIONAL_TOP_LEVEL_FIELDS = (
    "version_configs",
    "allowed_artifacts",
)
TOP_LEVEL_FIELDS = REQUIRED_TOP_LEVEL_FIELDS + OPTIONAL_TOP_LEVEL_FIELDS
VERSION_CONFIG_FIELDS = (
    "fromager_settings",
    "tests",
    "allowed_artifacts",
    "env",
    "version_rewrite",
)
TESTS_FIELDS = ("dependencies", "commands")
VERSION_REWRITE_FIELDS = ("files",)
REWRITE_FILE_FIELDS = ("path", "pattern", "replacement")

# Runtime tokens resolved at pipeline execution time (ADR-0020). This validator only checks
# that referenced token names are among these — it cannot resolve them, since COMPUTED_VERSION
# does not exist until the pipeline actually computes it.
SUPPORTED_TOKENS = {"COMPUTED_VERSION", "PACKAGE", "VERSION"}
TOKEN_PATTERN = re.compile(r"\{([A-Za-z0-9_]+)\}")

# Retain the existing POSIX environment identifier contract for JSON handoffs.
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _require_nonempty_str(value, label: str, errors: list[str]) -> None:
    """Append a type or empty-string error without interrupting validation."""
    if not isinstance(value, str):
        errors.append(f"{label} must be a string, got {type(value).__name__}")
    elif not value.strip():
        errors.append(f"{label} must be a non-empty string")


def _load_config(path: Path) -> tuple[dict | None, list[str]]:
    """Read and parse the JSON file.

    Returns (config, errors). On a fatal error (invalid JSON or a non-object
    top level), config is None and errors is non-empty.
    """
    try:
        config = read_json(path)
    except json.JSONDecodeError as e:
        return None, [f"Invalid JSON: {e}"]

    if not isinstance(config, dict):
        return None, [f"Top-level value must be an object, got {type(config).__name__}"]

    return config, []


def _validate_top_level(config: dict, path: Path, errors: list[str]) -> None:
    """Validate field shapes and package/canonical-name/filename consistency."""
    # Required top-level fields
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in config:
            errors.append(f"Missing required field: '{field}'")

    # Unknown top-level fields (strict schema)
    for field in config:
        if field not in TOP_LEVEL_FIELDS:
            errors.append(f"Unknown top-level field: '{field}'")

    # String fields
    package = config.get("package")
    canonical_name = config.get("canonical_name")
    for field in REQUIRED_STRING_FIELDS:
        if field in config:
            _require_nonempty_str(config[field], f"'{field}'", errors)

    # canonical_name must be the PEP 503 canonicalized package name
    if isinstance(package, str) and isinstance(canonical_name, str):
        expected = canonicalize_name(package)
        if canonical_name != expected:
            errors.append(
                f"'canonical_name' ('{canonical_name}') is not the canonical "
                f"form of 'package' ('{package}') — expected '{expected}'"
            )

    # Filename must match canonical_name
    if isinstance(canonical_name, str) and canonical_name and path.stem != canonical_name:
        errors.append(
            f"Filename stem '{path.stem}' does not match 'canonical_name' ('{canonical_name}')"
        )

    if "allowed_artifacts" in config:
        _validate_allowed_artifacts(config["allowed_artifacts"], "allowed_artifacts", errors)


def _validate_versions(config: dict, errors: list[str]) -> None:
    """Validate a nonempty list of unique PEP 440 version strings."""
    if "versions" not in config:
        return
    versions = config["versions"]
    if not isinstance(versions, list):
        errors.append(f"'versions' must be a list, got {type(versions).__name__}")
        return
    if not versions:
        errors.append("'versions' must be a non-empty list")
        return

    seen: set[Version] = set()
    for i, v in enumerate(versions):
        loc = f"versions[{i}]"
        if not isinstance(v, str):
            errors.append(f"{loc} must be a string, got {type(v).__name__}")
            continue
        if not v.strip():
            errors.append(f"{loc} must be a non-empty string")
            continue
        try:
            parsed = Version(v)
        except InvalidVersion:
            errors.append(f"{loc}: '{v}' is not a valid PEP 440 version")
            continue
        if parsed in seen:
            errors.append(f"{loc}: duplicate version '{v}'")
        else:
            seen.add(parsed)


def _parse_specifier(key: str, loc: str, errors: list[str]) -> SpecifierSet | None:
    """Parse a PEP 440 specifier, treating bare versions as exact matches."""
    try:
        return SpecifierSet(key, prereleases=True)
    except InvalidSpecifier:
        pass
    try:
        Version(key)
    except InvalidVersion:
        errors.append(f"{loc}: key '{key}' is not a valid PEP 440 specifier or version")
        return None
    return SpecifierSet(f"=={key}", prereleases=True)


def _validate_tests(tests, loc: str, errors: list[str]) -> None:
    """Validate optional test commands and PEP 508 dependency declarations."""
    if not isinstance(tests, dict):
        errors.append(f"{loc} must be an object, got {type(tests).__name__}")
        return
    for field in tests:
        if field not in TESTS_FIELDS:
            errors.append(f"{loc}: unknown field '{field}'")

    deps = tests.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            errors.append(f"{loc}.dependencies must be a list, got {type(deps).__name__}")
        else:
            for i, dep in enumerate(deps):
                dloc = f"{loc}.dependencies[{i}]"
                if not isinstance(dep, str) or not dep.strip():
                    errors.append(f"{dloc} must be a non-empty string")
                    continue
                try:
                    Requirement(dep)
                except InvalidRequirement as e:
                    errors.append(f"{dloc}: invalid PEP 508 requirement '{dep}': {e}")

    cmds = tests.get("commands")
    if cmds is not None:
        if not isinstance(cmds, list):
            errors.append(f"{loc}.commands must be a list, got {type(cmds).__name__}")
        else:
            for i, cmd in enumerate(cmds):
                if not isinstance(cmd, str) or not cmd.strip():
                    errors.append(f"{loc}.commands[{i}] must be a non-empty string")


def _validate_allowed_artifacts(allowed, loc: str, errors: list[str]) -> None:
    """Require unique canonical package names in the source-build allowlist."""
    if not isinstance(allowed, list):
        errors.append(f"{loc} must be a list, got {type(allowed).__name__}")
        return
    if not allowed:
        errors.append(f"{loc} must be a non-empty list")
        return
    seen: set[str] = set()
    for i, item in enumerate(allowed):
        iloc = f"{loc}[{i}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{iloc} must be a non-empty string")
            continue
        expected = canonicalize_name(item)
        if item != expected:
            errors.append(f"{iloc} ('{item}') is not PEP 503 canonicalized — expected '{expected}'")
            continue
        if item in seen:
            errors.append(f"{loc}: duplicate entry '{item}'")
        else:
            seen.add(item)


def _check_tokens_only(value: str, loc: str, errors: list[str]) -> None:
    """Check supported tokens and reject malformed brace syntax.

    This is a static, name-only check: it cannot verify a token will actually
    resolve at runtime (there is no COMPUTED_VERSION at validation time).
    """
    for match in TOKEN_PATTERN.finditer(value):
        name = match.group(1)
        if name not in SUPPORTED_TOKENS:
            errors.append(f"{loc}: unknown token '{{{name}}}' in {value!r}")

    # Anything left over after stripping recognized {TOKEN} forms indicates a
    # stray/malformed brace (e.g. '{' with no closing '}', or an empty '{}').
    stripped = TOKEN_PATTERN.sub("", value)
    if "{" in stripped or "}" in stripped:
        errors.append(f"{loc}: malformed token syntax in {value!r}")


def _validate_env(env, loc: str, errors: list[str]) -> None:
    """Validate environment names and nonempty strings with supported tokens."""
    if not isinstance(env, dict):
        errors.append(f"{loc} must be an object, got {type(env).__name__}")
        return
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_KEY_PATTERN.match(key):
            errors.append(f"{loc}: key {key!r} is not a valid environment variable identifier")
            continue
        vloc = f"{loc}.{key}"
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{vloc} must be a non-empty string")
            continue
        _check_tokens_only(value, vloc, errors)


def _validate_rewrite_file(entry, loc: str, errors: list[str]) -> None:
    """Validate one version_rewrite.files[] entry: {path, pattern, replacement}."""
    if not isinstance(entry, dict):
        errors.append(f"{loc} must be an object, got {type(entry).__name__}")
        return

    for field in entry:
        if field not in REWRITE_FILE_FIELDS:
            errors.append(f"{loc}: unknown field '{field}'")
    for field in REWRITE_FILE_FIELDS:
        if field not in entry:
            errors.append(f"{loc}: missing required field '{field}'")

    path = entry.get("path")
    if "path" in entry:
        if not isinstance(path, str) or not path.strip():
            errors.append(f"{loc}.path must be a non-empty string")
        elif "\\" in path:
            # PurePosixPath treats '\' as a literal filename character, not a
            # separator, so a payload like '..\\etc\\passwd' would otherwise
            # silently bypass the '..'-segment check below.
            errors.append(f"{loc}.path must use forward slashes, not backslashes: {path!r}")
        else:
            # Static syntax check only: no absolute paths, no '..' segments,
            # and no directory-shaped paths (a trailing '/' or bare '.') --
            # this must be a file path. Whether the path actually lands
            # inside the checked-out source/ tree can only be confirmed at
            # pipeline runtime.
            p = PurePosixPath(path)
            if p.is_absolute() or ".." in p.parts or path.endswith("/") or str(p) == ".":
                errors.append(
                    f"{loc}.path must be a file path relative to the package source root: {path!r}"
                )

    pattern = entry.get("pattern")
    if "pattern" in entry:
        if not isinstance(pattern, str) or not pattern.strip():
            errors.append(f"{loc}.pattern must be a non-empty string")
        else:
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"{loc}.pattern: invalid regex {pattern!r}: {e}")

    replacement = entry.get("replacement")
    if "replacement" in entry:
        if not isinstance(replacement, str) or not replacement.strip():
            errors.append(f"{loc}.replacement must be a non-empty string")
        else:
            _check_tokens_only(replacement, f"{loc}.replacement", errors)


def _validate_version_rewrite(vr, loc: str, errors: list[str]) -> None:
    """Require a nonempty list of valid path/pattern/replacement rewrite rules."""
    if not isinstance(vr, dict):
        errors.append(f"{loc} must be an object, got {type(vr).__name__}")
        return
    for field in vr:
        if field not in VERSION_REWRITE_FIELDS:
            errors.append(f"{loc}: unknown field '{field}'")

    if "files" not in vr:
        errors.append(f"{loc}: missing required field 'files'")
        return

    files = vr["files"]
    if not isinstance(files, list):
        errors.append(f"{loc}.files must be a list, got {type(files).__name__}")
    elif not files:
        errors.append(f"{loc}.files must be a non-empty list")
    else:
        for i, entry in enumerate(files):
            _validate_rewrite_file(entry, f"{loc}.files[{i}]", errors)


def _validate_config_body(config, loc: str, errors: list[str]) -> None:
    """Validate the supported fields of a per-version configuration object."""
    if not isinstance(config, dict):
        errors.append(f"{loc} must be an object, got {type(config).__name__}")
        return

    for field in config:
        if field not in VERSION_CONFIG_FIELDS:
            errors.append(f"{loc}: unknown field '{field}'")

    # fromager_settings type (optional; contents intentionally not validated,
    # including any nested fromager_settings.env — that is a distinct field
    # from the top-level 'env' below, opaque to this validator, and consumed
    # by fromager itself rather than the build-backend-invocation process).
    if "fromager_settings" in config and not isinstance(config["fromager_settings"], dict):
        errors.append(
            f"{loc}.fromager_settings must be an object, "
            f"got {type(config['fromager_settings']).__name__}"
        )
    if "tests" in config:
        _validate_tests(config["tests"], f"{loc}.tests", errors)
    if "allowed_artifacts" in config:
        _validate_allowed_artifacts(config["allowed_artifacts"], f"{loc}.allowed_artifacts", errors)
    if "env" in config:
        _validate_env(config["env"], f"{loc}.env", errors)
    if "version_rewrite" in config:
        _validate_version_rewrite(config["version_rewrite"], f"{loc}.version_rewrite", errors)


def _validate_version_configs(config: dict, errors: list[str]) -> None:
    """Validate unique PEP 440 specifiers and their configuration bodies."""
    if "version_configs" not in config:
        return
    vcs = config["version_configs"]
    if not isinstance(vcs, dict):
        errors.append(f"'version_configs' must be an object, got {type(vcs).__name__}")
        return

    # Detect distinct keys that resolve to the same specifier, e.g. '0.4.0'
    # and '==0.4.0'. Normalized via str(SpecifierSet).
    seen: dict[str, str] = {}
    for key, cfg in vcs.items():
        loc = f"version_configs['{key}']"
        spec = _parse_specifier(key, loc, errors)
        if spec is not None:
            norm = str(spec)
            if norm in seen:
                errors.append(
                    f"{loc}: duplicate specifier (same as version_configs['{seen[norm]}'])"
                )
            else:
                seen[norm] = key
        _validate_config_body(cfg, loc, errors)


def _validate_cross_references(config: dict, errors: list[str]) -> None:
    """Require each declared version to match exactly one configured range."""
    vcs = config.get("version_configs")
    if not vcs or not isinstance(vcs, dict):
        return

    versions = config.get("versions")
    if not isinstance(versions, list):
        return

    rules: list[tuple[str, SpecifierSet]] = []
    for key in vcs:
        spec = _parse_specifier(key, "", [])
        if spec is not None:
            rules.append((key, spec))

    # Every version in versions must match exactly one rule
    for v_str in versions:
        if not isinstance(v_str, str):
            continue
        try:
            v_obj = Version(v_str)
        except InvalidVersion:
            continue
        matched = [k for k, spec in rules if v_obj in spec]
        if len(matched) == 0:
            errors.append(
                f"Version '{v_str}' in 'versions' does not match any entry in 'version_configs'"
            )
        elif len(matched) > 1:
            errors.append(
                f"Version '{v_str}' in 'versions' matches multiple overlapping "
                f"'version_configs' entries: {matched}"
            )


def validate_file(path: Path) -> list[str]:
    """Validate a complete package build-config document.

    Args:
        path: JSON package config whose filename stem must match canonical_name.

    Returns:
        Human-readable schema errors, or an empty list for a valid document. Checks
        include required fields, version-range coverage, backend tokens, rewrite
        rules, test settings, and canonical artifact names.

    Raises:
        TaskError: JSON contains duplicate keys.
        OSError: The file cannot be read.
    """
    config, errors = _load_config(path)
    if config is None:
        return errors

    _validate_top_level(config, path, errors)
    _validate_versions(config, errors)
    _validate_version_configs(config, errors)
    _validate_cross_references(config, errors)
    return errors
