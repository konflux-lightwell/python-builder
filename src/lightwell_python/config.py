"""Resolve one immutable build-config snapshot for all stages of a build."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from lightwell_python.config_validation import validate_file
from lightwell_python.runtime import Package, TaskError, read_json


@dataclass(frozen=True)
class BuildConfig:
    """Package settings selected from a single build-config snapshot.

    Attributes:
        settings: Fromager settings before runtime PURL and changelog additions.
        env: Backend environment templates containing supported runtime tokens.
        rewrites: Explicit path/pattern/replacement rules; empty means use defaults.
        allowed: Canonical names permitted to be built from source in remediation.
        canonical_source: Source type and configuration for this package version.
    """

    settings: dict[str, Any]
    env: dict[str, str]
    rewrites: list[dict[str, str]]
    allowed: frozenset[str]
    canonical_source: dict[str, Any]

    @classmethod
    def load(cls, path: Path, package: Package) -> BuildConfig:
        """Validate the package config and select its matching version rule.

        Args:
            path: Package JSON file whose stem matches its canonical name.
            package: Distribution and concrete upstream version being built.

        Returns:
            Resolved settings, environment templates, rewrite rules, and allowlist.
            Configs without version rules use empty settings and the package allowlist.

        Raises:
            TaskError: Schema validation fails, the package identity differs, or version
                rules are present but do not match exactly once.
        """
        errors = validate_file(path)
        if errors:
            raise TaskError("Invalid build config: " + "; ".join(errors))
        config = read_json(path)
        if config["canonical_name"] != package.name:
            raise TaskError("Build config belongs to a different package")
        matches = []
        rules = config.get("version_configs", {})
        for key, body in rules.items():
            try:
                spec = SpecifierSet(key, prereleases=True)
            except InvalidSpecifier:
                spec = SpecifierSet(f"=={key}", prereleases=True)
            if Version(package.version) in spec:
                matches.append(body)
        if rules and len(matches) != 1:
            raise TaskError(
                f"Expected one version rule for {package.requirement}; got {len(matches)}"
            )
        selected = matches[0] if matches else {}
        canonical_source = (
            selected.get("canonical_source")
            or config.get("canonical_source")
            or {"type": "pypi-sdist"}
        )
        return cls(
            settings=selected.get("fromager_settings", {}),
            env=selected.get("env", {}),
            rewrites=selected.get("version_rewrite", {}).get("files", []),
            allowed=frozenset(
                selected.get("allowed_artifacts", config.get("allowed_artifacts", [package.name]))
            ),
            canonical_source=canonical_source,
        )

    def environment(self, package: Package, computed: str) -> dict[str, str]:
        """Expand backend environment templates into literal process values.

        Args:
            package: Distribution identity supplying PACKAGE and VERSION tokens.
            computed: Full build version supplying COMPUTED_VERSION.

        Returns:
            A new environment mapping. Values are never evaluated as shell code.

        Raises:
            TaskError: A template contains unsupported or malformed tokens.
        """
        return {key: expand(value, package, computed) for key, value in self.env.items()}

    def render_settings(self, path: Path, package: Package, computed: str) -> None:
        """Write per-package Fromager YAML for this build.

        PURL metadata records both upstream and computed versions. A changelog entry
        forces a rebuild. The stored config settings are copied before modification.

        Args:
            path: YAML destination; missing parent directories are created.
            package: Primary distribution and its upstream version.
            computed: Expected version of the built wheel.

        Raises:
            TaskError: PURL/changelog settings have incompatible types.
        """
        settings = copy.deepcopy(self.settings)
        purl = settings.setdefault("purl", {})
        changelog = settings.setdefault("changelog", {})
        if not isinstance(purl, dict) or not isinstance(changelog, dict):
            raise TaskError("Fromager purl and changelog settings must be mappings")
        purl.update(
            version=computed, upstream=f"pkg:pypi/{package.name}@{quote(package.version, safe='')}"
        )
        entries = changelog.setdefault(package.version, [])
        if not isinstance(entries, list):
            raise TaskError("Fromager changelog entries must be lists")
        entries.append(f"Lightwell build for {package.requirement}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")


def expand(value: str, package: Package, computed: str) -> str:
    """Expand the PACKAGE, VERSION, and COMPUTED_VERSION template tokens.

    Args:
        value: Template string from a backend environment or rewrite rule.
        package: Distribution identity supplying the upstream tokens.
        computed: Full version to substitute for COMPUTED_VERSION.

    Returns:
        The substituted string, with unrelated characters preserved literally.

    Raises:
        TaskError: A token is unknown, braces are malformed, or a NUL is present.
    """
    tokens = {"PACKAGE": package.name, "VERSION": package.version, "COMPUTED_VERSION": computed}

    def replace(match: re.Match[str]) -> str:
        """Resolve a recognized token or report its unsupported name."""
        try:
            return tokens[match[1]]
        except KeyError as exc:
            raise TaskError(f"Unsupported template token: {match[1]}") from exc

    resolved = re.sub(r"\{([^{}]*)\}", replace, value)
    if "{" in resolved or "}" in resolved or "\x00" in resolved:
        raise TaskError("Malformed template value")
    return resolved
