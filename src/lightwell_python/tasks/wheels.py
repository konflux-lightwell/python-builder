"""Implement the complete Fromager wheel task with verified provenance and SBOMs."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from lightwell_python.archives import find_sdist
from lightwell_python.build import (
    backend_environment,
    collect_artifacts,
    run_fromager,
    run_fromager_checkout,
    sync_sdists,
)
from lightwell_python.config import BuildConfig
from lightwell_python.manifest import build_manifest
from lightwell_python.registry import Registry, repository
from lightwell_python.runtime import (
    BuildType,
    Package,
    Runner,
    TaskError,
    copy_input_tree,
    log_stage,
    read_json,
    write_json,
    write_results,
)
from lightwell_python.versioning import version_counts
from lightwell_python.wheel_metadata import (
    annotate_ai_attribution,
    extract_sbom,
    validate_ai_models,
    verify_primary_wheels,
)

LOG = logging.getLogger(__name__)


def run(
    *,
    workdir: Path,
    results: Path,
    package: Package,
    computed: str,
    build_type: BuildType,
    sdists_dir: Path,
    manifest_output: Path,
    image: str,
    registry: Registry,
    runner: Runner,
    wheel_server: str = "",
    pulp_secret: Path | None = None,
    image_expires: str = "",
    overrides: Path = Path("/usr/local/share/fromager/overrides"),
    jobs: int | None = None,
    ai_assisted: bool = False,
    ai_models: str = "",
) -> None:
    """Build and publish wheels, then stage their manifest for trusted-artifact creation.

    A writable copy of the restored sdist tree supplies the exact config snapshot.
    Build summaries and primary wheel metadata are verified before publication.
    The manifest handoff becomes available only after image and SBOM upload succeed.
    When AI-assisted, only the extracted SBOM copy receives an attribution
    annotation before publication; the embedded wheel SBOM is left unchanged.

    Args:
        workdir: Existing, isolated scratch directory for build outputs.
        results: Tekton mount receiving IMAGE_URL, IMAGE_DIGEST, IMAGE_REF, and
            SBOM_BLOB_URL. The create step owns MANIFEST_ARTIFACT.
        package: Canonical primary distribution and upstream version.
        computed: Expected full version of the primary wheel.
        build_type: Version and dependency provenance policy.
        sdists_dir: Sdist/config tree restored by the preceding use step.
        manifest_output: New, stable build-manifest.json handoff path for create.
        image: Destination reference for the final wheels OCI image.
        registry: Adapter for final-image and SBOM publication.
        runner: Adapter for Fromager and auditwheel execution.
        wheel_server: Optional prebuilt-wheel cache URL; forbidden for Validated.
        pulp_secret: Optional mounted Pulp credentials for the build backend.
        image_expires: Optional Quay expiration annotation for the final image.
        overrides: Builder-provided Fromager settings and patch directory.
        jobs: Optional Fromager parallelism limit.
        ai_assisted: Add AI attribution to the attached SBOM when True.
        ai_models: Comma-separated AI model IDs recorded with the attribution.
            Used only when ai_assisted is True.

    Raises:
        TaskError: Configuration, provenance, metadata, tools, or publication fail.
            Unsafe ai_models values fail before the build starts.
            Uploads are not transactional: a failed SBOM attachment may leave the
            image uploaded, but no manifest handoff or success results are emitted.
    """
    if manifest_output.exists():
        raise TaskError(f"Manifest output already exists: {manifest_output}")
    version_counts(package, computed, build_type)
    if build_type == BuildType.VALIDATED and wheel_server:
        raise TaskError("Validated builds cannot use a prebuilt wheel server")
    if ai_assisted:
        validate_ai_models(ai_models)

    LOG.info(
        "Building wheels for %s; computed version: %s; build type: %s; wheel cache: %s",
        package.requirement,
        computed,
        build_type.value,
        "enabled" if wheel_server else "disabled",
    )
    sdists = workdir / "sdists-repo"
    with log_stage("Copy restored build inputs"):
        copy_input_tree(sdists_dir, sdists)

    with log_stage("Validate configuration snapshot and render Fromager settings"):
        config = BuildConfig.load(
            sdists / "build-configs/packages" / f"{package.name}.json", package
        )
        expected = config.environment(package, computed)
        environment = read_json(sdists / "backend-env.json")
        if environment != expected:
            raise TaskError("Backend environment does not match the build-config snapshot")
        settings = workdir / "fromager-settings"
        config.render_settings(settings / f"{package.name}.yaml", package, computed)

    output = workdir / "output"
    with backend_environment(runner.environment | environment, pulp_secret) as env:
        if config.canonical_source["type"] == "git":
            run_fromager_checkout(
                runner,
                output,
                sdists / "checkout" / f"{package.name}-{computed}" / f"{package.name}-{computed}",
                settings,
                package,
                computed,
                env,
                wheel_server,
                overrides=overrides,
                jobs=jobs,
            )
        else:
            with log_stage("Locate restored sdist archive"):
                archive = find_sdist(sdists, package, computed)
                LOG.info("Using sdist: %s", archive)
            run_fromager(
                runner,
                output,
                sdists,
                settings,
                package,
                env,
                wheel_server,
                overrides=overrides,
                jobs=jobs,
            )

    artifact = workdir / "artifact"
    with log_stage("Collect built wheels, sdists, and summaries"):
        sync_sdists(sdists / "builds", output / "sdists-repo/builds", package, computed)
        collect_artifacts(output, artifact)
        LOG.info("Collected %d artifact files in %s", len(list(artifact.iterdir())), artifact)

    with log_stage("Verify wheel provenance, versions, and primary SBOM"):
        manifest = build_manifest(output, build_type, config.allowed)
        primary = verify_primary_wheels(artifact, package, computed)
        manifest_path = workdir / "build-manifest.json"
        write_json(manifest_path, manifest)
        sbom = workdir / "sbom.spdx.json"
        extract_sbom(primary[0], sbom)
        if ai_assisted:
            annotate_ai_attribution(sbom, ai_models)
        LOG.info(
            "Verified %d primary wheels; manifest: %d produced, %d cached packages",
            len(primary),
            len(manifest["produced"]),
            len(manifest["cached"]),
        )

    annotations = {
        "dev.lightwell.package": package.name,
        "dev.lightwell.version": computed,
        "dev.lightwell.upstream-version": package.version,
        "dev.lightwell.pipeline": "python-build",
        "dev.lightwell.distribution-target": build_type.distribution_target,
    }
    if image_expires:
        annotations["quay.expires-after"] = image_expires

    tag = f"{package.name}-{computed.replace('+', '-')}"
    tags = [tag] if f"{repository(image)}:{tag}" != image else []
    commit_path = sdists / ".commit"
    if commit_path.is_file():
        commit = commit_path.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            raise TaskError("Invalid source commit in sdist artifact")
        tags.append(f"{tag}-{commit}")
    if any(not re.fullmatch(r"[\w][\w.-]{0,127}", t, flags=re.ASCII) for t in [tag, *tags]):
        raise TaskError("Computed image tag is not a valid OCI tag")

    with log_stage("Publish wheels OCI artifact"):
        reference = registry.push(
            image, artifact, "application/vnd.python.wheels", annotations, tags
        )
        LOG.info("Published wheels digest: %s", reference.rsplit("@", 1)[-1])
    with log_stage("Attach primary SPDX SBOM"):
        sbom_ref = registry.attach_sbom(reference, sbom)

    with log_stage("Finalize build manifest handoff and task results"):
        write_json(manifest_output, manifest)
        write_results(
            results,
            {
                "IMAGE_URL": image,
                "IMAGE_DIGEST": reference.rsplit("@", 1)[1],
                "IMAGE_REF": reference,
                "SBOM_BLOB_URL": sbom_ref,
            },
        )
    LOG.info("Build manifest ready: %s; task results: %s", manifest_output, results)
