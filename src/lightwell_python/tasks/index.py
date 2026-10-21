"""Generate a build index from source history and attach the exact content digest."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from lightwell_python.manifest import manifest_arguments
from lightwell_python.registry import Registry, pinned
from lightwell_python.runtime import (
    BuildType,
    Package,
    Runner,
    TaskError,
    read_json,
    sha256,
    write_results,
)
from lightwell_python.versioning import version_counts

MEDIA_TYPE = "application/vnd.lightwell.build-index.v1+json"


def run(
    *,
    workdir: Path,
    results: Path,
    package: Package,
    computed: str,
    build_type: BuildType,
    source_dir: Path,
    image_ref: str,
    registry: Registry,
    runner: Runner,
    manifest_path: Path | None = None,
) -> None:
    """Generate a build index from restored history and attach it to the final image.

    Fath-cuan interprets Lightwell-Fix trailers. Remediated builds pass --require-vuln;
    Validated builds skip that requirement. Optional companion entries come from a
    manifest restored separately by Tekton, never from a Python transport operation.

    Args:
        workdir: Existing scratch directory receiving build-index.json.
        results: Tekton mount receiving BUILD_INDEX_ATTACHED_REF and BUILD_INDEX_DIGEST.
        package: Canonical primary distribution and upstream version.
        computed: Full wheel version recorded in the primary PURL and version fields.
        build_type: Policy controlling version validation and vulnerability checks.
        source_dir: Restored repository with its .git history available for reading.
        image_ref: Digest-pinned final wheels image receiving the index referrer.
        registry: Adapter for attaching the generated index.
        runner: Adapter for fath-cuan execution.
        manifest_path: Optional restored build-manifest.json file for companion PURLs.

    Raises:
        TaskError: The image/version is invalid, source history or manifest is
            unavailable, generation fails, or attachment fails. Results are emitted
            only after attachment succeeds.
    """
    image_ref = pinned(image_ref)
    b, n = version_counts(package, computed, build_type)
    source = source_dir.resolve()
    if not (source / ".git").is_dir():
        raise TaskError("Source artifact has no .git directory for Lightwell-Fix trailers")

    extra = []
    if manifest_path is not None:
        if not manifest_path.is_file():
            raise TaskError("Manifest artifact has no build-manifest.json")
        extra = manifest_arguments(read_json(manifest_path), package.name)

    target = workdir / "build-index.json"
    command: list[str | Path] = [
        "fath-cuan",
        "index",
        "create",
        "--ecosystem",
        "pypi",
        "--purl",
        f"pkg:pypi/{package.name}@{quote(computed, safe='')}",
        "--version-upstream",
        package.version,
        "--version-local",
        computed,
        "--b",
        str(b),
        "--n",
        str(n),
        "--git-dir",
        source,
    ]
    if build_type != BuildType.VALIDATED:
        command.append("--require-vuln")

    # Scope safe.directory to this process (including fath-cuan's git children).
    runner.run(
        [*command, *extra, "--output", target],
        env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(source),
        },
    )

    data = read_json(target)
    if not isinstance(data, dict) or not data:
        raise TaskError("fath-cuan did not produce a build index object")
    digest = "sha256:" + sha256(target)

    registry.attach(image_ref, target, MEDIA_TYPE)

    write_results(results, {"BUILD_INDEX_ATTACHED_REF": image_ref, "BUILD_INDEX_DIGEST": digest})
