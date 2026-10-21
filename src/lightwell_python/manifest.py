"""Build provenance policy and disjoint produced/cached manifest entries."""

from __future__ import annotations

from pathlib import Path

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
