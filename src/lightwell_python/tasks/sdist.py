"""Normalize an acquired source archive while preserving opaque origin evidence."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from packaging.utils import parse_sdist_filename
from packaging.version import Version

from lightwell_python.archives import find_sdist
from lightwell_python.registry import Registry
from lightwell_python.runtime import (
    Package,
    Runner,
    TaskError,
    copy_input_tree,
    log_stage,
    sha256,
    write_json,
    write_results,
)

LOG = logging.getLogger(__name__)


def run(
    *,
    workdir: Path,
    results: Path,
    package: Package,
    sdists_dir: Path,
    image: str,
    git_url: str,
    revision: str,
    registry: Registry,
    runner: Runner,
    image_expires: str = "",
) -> None:
    """Normalize a restored upstream sdist and publish its evidence-bearing OCI image.

    The acquired evidence is copied unchanged. Fromager creates a new primary sdist;
    byte digests bind that output to the acquired archive and source-origin.json.

    Args:
        workdir: Existing, isolated scratch directory for normalization and staging.
        results: Tekton mount receiving image results and CHAINS-GIT coordinates.
        package: Canonical primary distribution and upstream version.
        sdists_dir: Acquired archive/evidence tree restored by the preceding use step.
        image: Destination OCI reference for the normalized sdist and evidence.
        git_url: Source URL to report in CHAINS-GIT_URL.
        revision: Source commit to report in CHAINS-GIT_COMMIT.
        registry: Adapter for final OCI image publication.
        runner: Adapter for Fromager prepare-source/build-sdist execution.
        image_expires: Optional Quay expiration annotation.

    Raises:
        TaskError: Origin evidence is missing, Fromager fails, normalization does not
            produce exactly one matching sdist, or publication fails.
    """
    LOG.info("Normalizing sdist for %s", package.requirement)
    acquired = workdir / "sdists-repo"
    with log_stage("Copy acquired sdist and validate origin evidence"):
        copy_input_tree(sdists_dir, acquired)
        original = find_sdist(acquired, package)
        origin = acquired / "source-origin.json"
        if not origin.is_file():
            raise TaskError("Acquired sdist is missing source-origin.json")
        input_digest, origin_digest = sha256(original), sha256(origin)
        LOG.info("Acquired sdist: %s; SHA-256: %s", original.name, input_digest)

    output = workdir / "output"
    artifact = workdir / "artifact"
    builds = output / "sdists-repo/builds"
    with log_stage("Stage original evidence and prepare normalization workspace"):
        shutil.copytree(acquired, output / "sdists-repo", symlinks=True)
        shutil.copytree(acquired, artifact, symlinks=True)
        # Preserve acquired evidence, but exclude stale normalization outputs.
        if builds.exists():
            shutil.rmtree(builds)

    env = {"FROMAGER_SDISTS_REPO": str(output / "sdists-repo")}
    for step in ("prepare-source", "build-sdist"):
        with log_stage(f"Fromager {step}"):
            runner.run(
                ["fromager", "step", step, package.name, package.version], cwd=output, env=env
            )

    with log_stage("Verify normalized sdist and record transformation digests"):
        candidates = []
        for path in sorted(builds.glob("*.tar.gz")):
            name, version = parse_sdist_filename(path.name)
            if name == package.name and version == Version(package.version):
                candidates.append(path)
        if len(candidates) != 1:
            raise TaskError(f"Expected exactly one normalized primary sdist; got {len(candidates)}")

        normalized = artifact / candidates[0].name
        shutil.copyfile(candidates[0], normalized)
        output_digest = sha256(normalized)
        write_json(
            artifact / "sdist-transformation.json",
            {
                "schema": "lightwell.sdist-transformation",
                "version": 1,
                "transformation": "fromager-normalize-sdist",
                "input": {"sha256": input_digest},
                "output": {"path": normalized.name, "sha256": output_digest},
                "source_origin": {"path": "source-origin.json", "sha256": origin_digest},
            },
        )
        LOG.info("Normalized sdist: %s; SHA-256: %s", normalized.name, output_digest)

    annotations = {
        "dev.lightwell.package": package.name,
        "dev.lightwell.version": package.version,
        "dev.lightwell.pipeline": "python-sdist-ingest",
    }
    if image_expires:
        annotations["quay.expires-after"] = image_expires

    with log_stage("Publish normalized sdist and evidence OCI artifact"):
        reference = registry.push(image, artifact, "application/vnd.python.sdist", annotations)
        LOG.info("Published sdist digest: %s", reference.rsplit("@", 1)[-1])

    with log_stage("Write sdist task results"):
        write_results(
            results,
            {
                "IMAGE_URL": image,
                "IMAGE_DIGEST": reference.rsplit("@", 1)[1],
                "IMAGE_REF": reference,
                "CHAINS-GIT_URL": git_url,
                "CHAINS-GIT_COMMIT": revision,
            },
        )
    LOG.info("Normalized sdist task results ready: %s", results)
