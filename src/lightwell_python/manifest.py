"""Build provenance policy and disjoint produced/cached manifest entries."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from packaging.utils import canonicalize_name
from packaging.version import Version

from lightwell_python.runtime import BuildType, TaskError, read_json


def build_manifest(output: Path, build_type: BuildType, allowed: frozenset[str]) -> dict:
    """Classify built and cached distributions from Fromager summaries.

    Args:
        output: Build directory containing build-sequence-summary-*.json files.
        build_type: Validated permits the dependency closure; remediation enforces
            the source-build allowlist.
        allowed: Canonical distribution names allowed to be built from source.

    Returns:
        A manifest with sorted, deduplicated produced and cached entries. A
        name/version pair built from source takes precedence over a cached entry.

    Raises:
        TaskError: Summaries are missing, empty, malformed, or contain an
            unauthorized source build.
    """
    paths = sorted(output.glob("build-sequence-summary-*.json"))
    if not paths:
        raise TaskError("No build sequence summaries were produced")

    produced: set[tuple[str, str]] = set()
    cached: set[tuple[str, str]] = set()
    for path in paths:
        entries = read_json(path)
        if not isinstance(entries, list) or not entries:
            raise TaskError(f"Build summary must be a nonempty list: {path.name}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise TaskError("Build summary entry must be an object")
            name = entry.get("name") or entry.get("dist") or entry.get("dist_name")
            version = str(entry.get("version") or entry.get("req_version") or "").strip()
            if not isinstance(name, str) or not name or not version:
                raise TaskError("Build summary entry is missing identity/version")
            name = canonicalize_name(name, validate=True)
            Version(version)
            skipped = entry.get("skipped", False)
            prebuilt = entry.get("prebuilt", False)
            if not isinstance(skipped, bool) or not isinstance(prebuilt, bool):
                raise TaskError("Build summary skipped/prebuilt values must be booleans")
            if build_type == BuildType.VALIDATED:
                if not skipped:
                    produced.add((name, version))
            elif not skipped and not prebuilt:
                if name not in allowed:
                    raise TaskError(f"Unauthorized source build: {name}")
                produced.add((name, version))
            else:
                cached.add((name, version))
    return {
        "produced": [{"name": name, "version": version} for name, version in sorted(produced)],
        "cached": [
            {"name": name, "version": version} for name, version in sorted(cached - produced)
        ],
    }


def manifest_arguments(manifest: dict, primary: str) -> list[str]:
    """Translate companion manifest entries into fath-cuan arguments.

    Args:
        manifest: Build manifest containing produced and cached distribution lists.
        primary: Primary distribution, already supplied through fath-cuan's --purl.

    Returns:
        A flat argument vector using --produced-purl and --extra-purl. The primary
        distribution is omitted and produced PURLs take precedence over cached ones.

    Raises:
        TaskError: A category or entry does not follow the manifest schema.
    """
    produced = _manifest_purls(manifest, "produced", primary)
    cached = _manifest_purls(manifest, "cached", primary)

    arguments = []
    for purl in sorted(produced):
        arguments.extend(["--produced-purl", purl])
    for purl in sorted(cached - produced):
        arguments.extend(["--extra-purl", purl])
    return arguments


def _manifest_purls(manifest: dict, category: str, primary: str) -> set[str]:
    """Read one manifest category, excluding the primary distribution."""
    entries = manifest.get(category, [])
    if not isinstance(entries, list):
        raise TaskError(f"Manifest {category} must be a list")

    primary_name = canonicalize_name(primary)
    result = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("version"):
            raise TaskError("Manifest entry is missing identity/version")
        name = canonicalize_name(entry["name"], validate=True)
        version = str(entry["version"])
        Version(version)
        if name != primary_name:
            result.add(f"pkg:pypi/{name}@{quote(version, safe='')}")
    return result
