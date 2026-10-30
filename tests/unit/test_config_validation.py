"""Build-config compatibility tests adapted from calunga-python."""

import json
from pathlib import Path

import pytest

from lightwell_python import config_validation as vbc


def baseline() -> dict:
    """A fresh, fully valid config (pillow-shaped) for each test to mutate."""
    return {
        "package": "pillow",
        "canonical_name": "pillow",
        "upstream_registry": "pypi.org",
        "lightwell_builds_repo": "pypi.org-pillow",
        "versions": ["12.3.0"],
        "version_configs": {
            "<12.3.0": {
                "fromager_settings": {},
                "tests": {"dependencies": ["pytest"], "commands": ["pytest"]},
            },
            ">=12.3.0": {
                "fromager_settings": {"env": {"MAX_CONCURRENCY": "4"}},
            },
        },
    }


def run(tmp_path: Path, config, filename: str | None = None) -> list[str]:
    """Write `config` to a temp file and return validate_file's error list.

    `config` may be a dict (JSON-encoded) or a raw string (written verbatim,
    for malformed-JSON cases). Filename defaults to <canonical_name>.json so
    the filename check passes unless a test overrides it.
    """
    if isinstance(config, dict):
        name = filename or f"{config.get('canonical_name', 'pkg')}.json"
        payload = json.dumps(config)
    else:
        name = filename or "pkg.json"
        payload = config
    p = tmp_path / name
    p.write_text(payload)
    return vbc.validate_file(p)


def has(errors: list[str], substr: str) -> bool:
    return any(substr in e for e in errors)


# --- baseline -------------------------------------------------------------


def test_valid_baseline(tmp_path):
    assert run(tmp_path, baseline()) == []


# --- loading --------------------------------------------------------------


def test_invalid_json(tmp_path):
    errors = run(tmp_path, "{not valid json", filename="pillow.json")
    assert len(errors) == 1 and has(errors, "Invalid JSON")


def test_non_object_top_level(tmp_path):
    errors = run(tmp_path, "[]", filename="pillow.json")
    assert errors == ["Top-level value must be an object, got list"]


# --- top-level fields -----------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["package", "canonical_name", "upstream_registry", "lightwell_builds_repo", "versions"],
)
def test_missing_required_top_level(tmp_path, field):
    config = baseline()
    del config[field]
    errors = run(tmp_path, config)
    assert has(errors, f"Missing required field: '{field}'")


def test_valid_versions_only(tmp_path):
    config = baseline()
    del config["version_configs"]
    config["versions"] = ["1.0.0", "1.1.0"]
    assert run(tmp_path, config) == []


def test_valid_versions_and_version_configs(tmp_path):
    config = baseline()
    config["versions"] = ["12.3.0"]
    assert run(tmp_path, config) == []


def test_versions_not_a_list(tmp_path):
    config = baseline()
    config["versions"] = "1.0.0"
    errors = run(tmp_path, config)
    assert has(errors, "'versions' must be a list")


def test_versions_empty(tmp_path):
    config = baseline()
    config["versions"] = []
    errors = run(tmp_path, config)
    assert has(errors, "'versions' must be a non-empty list")


def test_versions_invalid_pep440(tmp_path):
    config = baseline()
    config["versions"] = ["1.0.0", "not-a-version"]
    errors = run(tmp_path, config)
    assert has(errors, "not a valid PEP 440 version")


def test_versions_duplicate(tmp_path):
    config = baseline()
    config["versions"] = ["1.0.0", "1.0.0"]
    errors = run(tmp_path, config)
    assert has(errors, "duplicate version '1.0.0'")


def test_unknown_top_level_field(tmp_path):
    config = baseline()
    config["surprise"] = 1
    errors = run(tmp_path, config)
    assert has(errors, "Unknown top-level field: 'surprise'")


@pytest.mark.parametrize(
    "field", ["package", "canonical_name", "upstream_registry", "lightwell_builds_repo"]
)
def test_empty_string_fields(tmp_path, field):
    config = baseline()
    config[field] = "   "
    errors = run(tmp_path, config)
    assert has(errors, f"'{field}' must be a non-empty string")


def test_non_string_field(tmp_path):
    config = baseline()
    config["upstream_registry"] = 5
    errors = run(tmp_path, config)
    assert has(errors, "'upstream_registry' must be a string, got int")


def test_canonical_name_mismatch(tmp_path):
    config = baseline()
    config["package"] = "Foo.Bar"
    config["canonical_name"] = "Foo.Bar"
    errors = run(tmp_path, config, filename="Foo.Bar.json")
    assert has(errors, "is not the canonical form of 'package'")
    assert has(errors, "expected 'foo-bar'")


def test_filename_mismatch(tmp_path):
    errors = run(tmp_path, baseline(), filename="not-pillow.json")
    assert has(errors, "Filename stem 'not-pillow' does not match")


# --- version_configs keys & body ------------------------------------------


def test_version_configs_not_object(tmp_path):
    config = baseline()
    config["version_configs"] = []
    errors = run(tmp_path, config)
    assert has(errors, "'version_configs' must be an object, got list")


def test_version_configs_empty_accepted(tmp_path):
    config = baseline()
    config["version_configs"] = {}
    errors = run(tmp_path, config)
    assert errors == []


def test_bare_version_key_accepted(tmp_path):
    # ntplib-style: an exact version as key, treated as '=='.
    config = {
        "package": "ntplib",
        "canonical_name": "ntplib",
        "upstream_registry": "pypi.org",
        "lightwell_builds_repo": "pypi.org-ntplib",
        "versions": ["0.4.0"],
        "version_configs": {"0.4.0": {"fromager_settings": {}}},
    }
    assert run(tmp_path, config) == []


def test_invalid_specifier_key(tmp_path):
    config = baseline()
    config["version_configs"]["garbage-key"] = {"fromager_settings": {}}
    errors = run(tmp_path, config)
    assert has(errors, "key 'garbage-key' is not a valid PEP 440 specifier or version")


def test_duplicate_specifier_keys(tmp_path):
    # '0.4.0' and '==0.4.0' resolve to the same specifier.
    config = baseline()
    config["version_configs"] = {
        "0.4.0": {"fromager_settings": {}},
        "==0.4.0": {"fromager_settings": {}},
    }
    errors = run(tmp_path, config)
    assert has(errors, "duplicate specifier")


def test_config_body_all_fields_optional(tmp_path):
    # An empty config body is valid: both fromager_settings and tests are optional.
    config = baseline()
    config["version_configs"]["<12.3.0"] = {}
    assert run(tmp_path, config) == []


def test_config_fromager_settings_wrong_type(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["fromager_settings"] = []
    errors = run(tmp_path, config)
    assert has(errors, "version_configs['<12.3.0'].fromager_settings must be an object, got list")


def test_config_unknown_field(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["mystery"] = 1
    errors = run(tmp_path, config)
    assert has(errors, "version_configs['<12.3.0']: unknown field 'mystery'")


# --- tests ----------------------------------------------------------------


def test_tests_invalid_requirement(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["tests"]["dependencies"] = ["!!bad"]
    errors = run(tmp_path, config)
    assert has(errors, "invalid PEP 508 requirement '!!bad'")


def test_tests_empty_command(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["tests"]["commands"] = [""]
    errors = run(tmp_path, config)
    assert has(errors, "tests.commands[0] must be a non-empty string")


def test_tests_dependencies_not_list(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["tests"]["dependencies"] = "pytest"
    errors = run(tmp_path, config)
    assert has(errors, "tests.dependencies must be a list, got str")


def test_tests_unknown_field(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["tests"]["extra"] = 1
    errors = run(tmp_path, config)
    assert has(errors, "tests: unknown field 'extra'")


# --- cross-validation & non-overlapping -----------------------------------


def test_version_uncovered_by_version_configs(tmp_path):
    config = baseline()
    config["versions"] = ["10.0.0"]
    config["version_configs"] = {">=12.0.0": {"fromager_settings": {}}}
    errors = run(tmp_path, config)
    assert has(
        errors, "Version '10.0.0' in 'versions' does not match any entry in 'version_configs'"
    )


def test_version_matching_multiple_overlapping_configs(tmp_path):
    config = baseline()
    config["versions"] = ["12.1.0"]
    config["version_configs"] = {
        "<12.3.0": {"fromager_settings": {}},
        ">=12.0.0": {"fromager_settings": {}},
    }
    errors = run(tmp_path, config)
    assert has(errors, "matches multiple overlapping 'version_configs' entries")


def test_version_configs_exact_boundary_non_overlapping(tmp_path):
    config = baseline()
    config["versions"] = ["12.3.0"]
    config["version_configs"] = {
        "<12.3.0": {"fromager_settings": {}},
        ">=12.3.0": {"fromager_settings": {}},
    }
    assert run(tmp_path, config) == []


def test_version_configs_prerelease_matched(tmp_path):
    config = baseline()
    config["versions"] = ["12.3.0a1"]
    config["version_configs"] = {
        ">=12.0.0": {"fromager_settings": {}},
    }
    assert run(tmp_path, config) == []


# --- allowed_artifacts ---------------------------------------------------


def test_allowed_artifacts_top_level_valid(tmp_path):
    config = baseline()
    config["allowed_artifacts"] = ["pillow", "pillow-tools"]
    assert run(tmp_path, config) == []


def test_allowed_artifacts_version_configs_valid(tmp_path):
    config = baseline()
    config["version_configs"] = {
        ">=12.0.0": {"allowed_artifacts": ["pillow"]},
    }
    assert run(tmp_path, config) == []


def test_allowed_artifacts_must_be_list(tmp_path):
    config = baseline()
    config["allowed_artifacts"] = "pillow"
    errors = run(tmp_path, config)
    assert has(errors, "allowed_artifacts must be a list")


def test_allowed_artifacts_must_be_non_empty(tmp_path):
    config = baseline()
    config["allowed_artifacts"] = []
    errors = run(tmp_path, config)
    assert has(errors, "allowed_artifacts must be a non-empty list")


def test_allowed_artifacts_canonical_check(tmp_path):
    config = baseline()
    config["allowed_artifacts"] = ["Pillow_Tools"]
    errors = run(tmp_path, config)
    assert has(errors, "is not PEP 503 canonicalized")


def test_allowed_artifacts_no_duplicates(tmp_path):
    config = baseline()
    config["allowed_artifacts"] = ["pillow", "pillow"]
    errors = run(tmp_path, config)
    assert has(errors, "duplicate entry 'pillow'")


# --- env (ADR-0020) ---------------------------------------------------------


def test_env_valid(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {
        "SETUPTOOLS_SCM_PRETEND_VERSION": "{COMPUTED_VERSION}",
        "STATIC_VAR": "no-tokens-here",
    }
    assert run(tmp_path, config) == []


def test_env_not_object(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = ["not", "an", "object"]
    errors = run(tmp_path, config)
    assert has(errors, "env must be an object, got list")


def test_env_value_not_string(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {"FOO": 4}
    errors = run(tmp_path, config)
    assert has(errors, "env.FOO must be a non-empty string")


def test_env_value_empty_string(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {"FOO": "   "}
    errors = run(tmp_path, config)
    assert has(errors, "env.FOO must be a non-empty string")


def test_env_unknown_token(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {"FOO": "{NOT_A_TOKEN}"}
    errors = run(tmp_path, config)
    assert has(errors, "unknown token '{NOT_A_TOKEN}'")


def test_env_malformed_token_syntax(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {"FOO": "v{COMPUTED_VERSION"}
    errors = run(tmp_path, config)
    assert has(errors, "malformed token syntax")


def test_env_empty_braces_is_malformed_not_unknown_token(tmp_path):
    # '{}' has no token name at all -- it's malformed syntax, not a
    # (falsely) named-but-unrecognized token like '{BOGUS}'.
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {"FOO": "{}"}
    errors = run(tmp_path, config)
    assert has(errors, "malformed token syntax")
    assert not has(errors, "unknown token")


@pytest.mark.parametrize("bad_key", ["MY VAR", "MY-VAR", "FOO=BAR", "1LEADING_DIGIT", ""])
def test_env_key_invalid_identifier(tmp_path, bad_key):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {bad_key: "value"}
    errors = run(tmp_path, config)
    assert has(errors, "is not a valid environment variable identifier")


def test_env_key_valid_identifier_with_underscore_and_digits(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["env"] = {"_FOO_2": "value"}
    assert run(tmp_path, config) == []


def test_env_distinct_from_nested_fromager_settings_env(tmp_path):
    # fromager_settings.env (pre-existing, opaque, consumed by fromager itself)
    # is a different field from the new top-level env (this ticket, consumed
    # by the build-backend-invocation process). Both may coexist; neither the
    # nested one nor the combination should trip the new checks.
    config = baseline()
    config["version_configs"][">=12.3.0"]["fromager_settings"]["env"] = {"MAX_CONCURRENCY": "4"}
    config["version_configs"][">=12.3.0"]["env"] = {
        "SETUPTOOLS_SCM_PRETEND_VERSION": "{COMPUTED_VERSION}"
    }
    assert run(tmp_path, config) == []


# --- version_rewrite (ADR-0020) ---------------------------------------------


def version_rewrite_baseline() -> dict:
    return {
        "files": [
            {
                "path": "src/PIL/version.py",
                "pattern": r"^__version__\s*=\s*.*$",
                "replacement": '__version__ = "{COMPUTED_VERSION}"',
            }
        ]
    }


def test_version_rewrite_valid(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["version_rewrite"] = version_rewrite_baseline()
    assert run(tmp_path, config) == []


def test_version_rewrite_missing_files_rejected(tmp_path):
    # An empty 'version_rewrite: {}' is not a valid no-op -- 'files' is
    # required whenever 'version_rewrite' is present at all, so a config
    # can't silently resolve to a missing 'files' key downstream.
    config = baseline()
    config["version_configs"]["<12.3.0"]["version_rewrite"] = {}
    errors = run(tmp_path, config)
    assert has(errors, "version_rewrite: missing required field 'files'")


def test_version_rewrite_not_object(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["version_rewrite"] = []
    errors = run(tmp_path, config)
    assert has(errors, "version_rewrite must be an object, got list")


def test_version_rewrite_unknown_field(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["mystery"] = 1
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "version_rewrite: unknown field 'mystery'")


def test_version_rewrite_files_not_list(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["version_rewrite"] = {"files": {}}
    errors = run(tmp_path, config)
    assert has(errors, "version_rewrite.files must be a list, got dict")


def test_version_rewrite_files_empty(tmp_path):
    config = baseline()
    config["version_configs"]["<12.3.0"]["version_rewrite"] = {"files": []}
    errors = run(tmp_path, config)
    assert has(errors, "version_rewrite.files must be a non-empty list")


def test_version_rewrite_file_missing_field(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    del vr["files"][0]["pattern"]
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "files[0]: missing required field 'pattern'")


def test_version_rewrite_file_unknown_field(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["extra"] = 1
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "files[0]: unknown field 'extra'")


@pytest.mark.parametrize("bad_path", ["../etc/passwd", "a/../../b", "/etc/passwd"])
def test_version_rewrite_file_path_escapes(tmp_path, bad_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["path"] = bad_path
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "must be a file path relative to the package source root")


def test_version_rewrite_file_path_empty(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["path"] = "   "
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "files[0].path must be a non-empty string")


@pytest.mark.parametrize("bad_path", [r"..\etc\passwd", r"src\version.py"])
def test_version_rewrite_file_path_backslash_rejected(tmp_path, bad_path):
    # PurePosixPath treats '\' as a literal filename character, not a
    # separator, so a '..'-segment check alone would miss this traversal
    # attempt on POSIX. Backslashes are rejected outright instead.
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["path"] = bad_path
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "must use forward slashes, not backslashes")


@pytest.mark.parametrize("bad_path", ["src/", "."])
def test_version_rewrite_file_path_directory_shaped_rejected(tmp_path, bad_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["path"] = bad_path
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "must be a file path relative to the package source root")


def test_version_rewrite_file_invalid_regex(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["pattern"] = "["
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "invalid regex")


def test_version_rewrite_file_replacement_unknown_token(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["replacement"] = "{BOGUS}"
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "unknown token '{BOGUS}'")


def test_version_rewrite_file_replacement_malformed_token(tmp_path):
    config = baseline()
    vr = version_rewrite_baseline()
    vr["files"][0]["replacement"] = "v{COMPUTED_VERSION"
    config["version_configs"]["<12.3.0"]["version_rewrite"] = vr
    errors = run(tmp_path, config)
    assert has(errors, "malformed token syntax")


# --- canonical_source -------------------------------------------------------


def test_canonical_source_pypi_sdist_top_level_valid(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {"type": "pypi-sdist"}
    assert run(tmp_path, cfg) == []


def test_canonical_source_git_valid(tmp_path):
    cfg = baseline()
    cfg["version_configs"][">=12.3.0"]["canonical_source"] = {
        "type": "git",
        "upstream_url": "https://github.com/python-pillow/Pillow",
        "ref": "12.3.0",
    }
    assert run(tmp_path, cfg) == []


def test_canonical_source_git_with_internal_url_valid(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {
        "type": "git",
        "upstream_url": "https://github.com/python-pillow/Pillow",
        "internal_url": "https://internal.example.test/mirror/pillow.git",
        "ref": "12.3.0",
    }
    assert run(tmp_path, cfg) == []


def test_canonical_source_git_missing_upstream_url(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {"type": "git", "ref": "12.3.0"}
    assert has(run(tmp_path, cfg), "missing required field 'upstream_url'")


def test_canonical_source_git_missing_ref(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {"type": "git", "upstream_url": "https://github.com/x/y"}
    assert has(run(tmp_path, cfg), "missing required field 'ref'")


def test_canonical_source_pypi_sdist_rejects_extra_fields(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {"type": "pypi-sdist", "ref": "x"}
    assert has(run(tmp_path, cfg), "is not allowed for type 'pypi-sdist'")


def test_canonical_source_unknown_type(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {"type": "svn"}
    assert has(run(tmp_path, cfg), "must be one of")


def test_canonical_source_non_https_upstream_url(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {"type": "git", "upstream_url": "git://x/y", "ref": "1.0"}
    assert has(run(tmp_path, cfg), "must start with 'https://'")


def test_canonical_source_ref_rejects_unsafe_characters(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = {
        "type": "git",
        "upstream_url": "https://github.com/x/y",
        "ref": "-rf",
    }
    assert has(run(tmp_path, cfg), "must not start with '-'")


def test_canonical_source_not_an_object(tmp_path):
    cfg = baseline()
    cfg["canonical_source"] = "git"
    assert has(run(tmp_path, cfg), "must be an object")
