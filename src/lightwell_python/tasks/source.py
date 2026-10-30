"""Prepare an sdist tree for the following build-trusted-artifacts create step."""

from __future__ import annotations

import base64
import logging
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from lightwell_python import git_checkout
from lightwell_python.archives import create_sdist
from lightwell_python.config import BuildConfig
from lightwell_python.runtime import (
    BuildType,
    Package,
    Runner,
    TaskError,
    copy_input_tree,
    log_stage,
    write_json,
    write_results,
)
from lightwell_python.versioning import compute_version, rewrite_metadata

LOG = logging.getLogger(__name__)


def clone_config(
    runner: Runner, url: str, revision: str, destination: Path, credentials: Path | None
) -> str:
    """Check out a build-config revision with optional scoped HTTPS credentials.

    Args:
        runner: Adapter for Git commands.
        url: Config repository URL, without embedded credentials.
        revision: Git revision to check out in detached HEAD mode.
        destination: New local checkout directory.
        credentials: Optional mounted directory containing username/password files.

    Returns:
        The resolved HEAD commit ID, recorded alongside the selected config snapshot.

    Raises:
        TaskError: Credentialed cloning uses a non-HTTPS URL, the revision is unsafe,
            or a Git command fails. Credentials are passed in the subprocess
            environment rather than command arguments or global Git configuration.
    """
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if credentials:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise TaskError(
                "Credentialed build-config clones require HTTPS without URL credentials"
            )
        user = (credentials / "username").read_text().rstrip("\n")
        password = (credentials / "password").read_text().rstrip("\n")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{url}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
        }
    if revision.startswith("-") or not revision:
        raise TaskError("Invalid build-config revision")
    runner.run(["git", "clone", "--", url, destination], env=env)
    runner.run(["git", "-C", destination, "checkout", "--detach", revision], env=env)
    return runner.run(["git", "-C", destination, "rev-parse", "HEAD"], capture=True).strip()


def run(
    *,
    workdir: Path,
    results: Path,
    package: Package,
    build_type: BuildType,
    source_dir: Path,
    sdists_output: Path,
    config_url: str,
    config_revision: str,
    runner: Runner,
    git_auth: Path | None = None,
) -> None:
    """Prepare the sdist handoff from a restored source repository.

    The source tree is copied into scratch space before rewriting. The selected
    build config, backend environment JSON, and deterministic sdist aliases are
    staged together. Tekton's subsequent create step publishes that directory.

    Args:
        workdir: Existing, isolated scratch directory for this invocation.
        results: Tekton mount receiving COMPUTED_VERSION, B_VALUE, and N_VALUE.
        package: Canonical primary distribution and upstream version.
        build_type: Policy governing Git lineage and version rewriting.
        source_dir: Repository already restored by build-trusted-artifacts use.
        sdists_output: New, stable handoff directory for the following create step.
        config_url: Repository containing package build configs.
        config_revision: Revision to resolve once for the whole build.
        runner: Adapter for Git execution.
        git_auth: Optional mounted username/password credentials for config cloning.

    Raises:
        TaskError: Inputs, version lineage, config, or rewrites are invalid, or the
            handoff already exists. Handoff publication is left to Tekton; Python
            does not write SDISTS_ARTIFACT.
    """
    if sdists_output.exists():
        raise TaskError(f"Sdist output already exists: {sdists_output}")

    LOG.info("Preparing source for %s; build type: %s", package.requirement, build_type.value)
    repo = workdir / "source"
    with log_stage("Copy restored source into scratch space"):
        copy_input_tree(source_dir, repo)
        LOG.info("Source copy: %s", repo)

    with log_stage("Resolve build version"):
        version = compute_version(repo, package, build_type, runner)
        LOG.info("Computed version: %s (B=%d, N=%d)", version.full, version.b, version.n)

    config_repo = workdir / "build-configs"
    with log_stage("Clone and validate build configuration"):
        commit = clone_config(runner, config_url, config_revision, config_repo, git_auth)
        config_path = config_repo / "packages" / f"{package.name}.json"
        config = BuildConfig.load(config_path, package)
        LOG.info("Build configuration: %s; resolved commit: %s", config_path, commit)

    staging = workdir / "sdists-repo"
    with log_stage("Stage configuration snapshot and backend environment"):
        snapshot = staging / "build-configs/packages"
        snapshot.mkdir(parents=True)
        shutil.copyfile(config_path, snapshot / config_path.name)
        (snapshot.parent / ".commit").write_text(commit + "\n")
        write_json(staging / "backend-env.json", config.environment(package, version.full))
        write_json(
            staging / "version-overrides.json",
            {"env": config.env, "version_rewrite": {"files": config.rewrites}},
        )

    with log_stage("Rewrite source version metadata"):
        rewritten = rewrite_metadata(repo / "source", package, version.full, config.rewrites)

    if config.canonical_source["type"] == "git":
        with log_stage("Stage verified git checkout"):
            head_sha = git_checkout.retag(repo, version.full, runner)
            git_checkout.verify_clean(repo, rewritten, runner)
            base = f"{package.name}-{version.full}"
            checkout = staging / "checkout" / base
            shutil.copytree(repo / "source", checkout / base, symlinks=True)
            shutil.copytree(repo / ".git", checkout / ".git", symlinks=True)
            LOG.info("Staged git checkout: %s (HEAD %s)", checkout, head_sha)
            if version.commit:
                (staging / ".commit").write_text(version.commit)
    else:
        with log_stage("Create deterministic sdist and cache aliases"):
            archive = create_sdist(repo / "source", staging, package, version.full)
            LOG.info("Created sdist: %s (%d bytes)", archive.name, archive.stat().st_size)
            if version.commit:
                (staging / ".commit").write_text(version.commit)

    with log_stage("Finalize sdist handoff and task results"):
        sdists_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), sdists_output)
        write_results(
            results,
            {"COMPUTED_VERSION": version.full, "B_VALUE": version.b, "N_VALUE": version.n},
        )
    LOG.info("Sdist handoff ready: %s; task results: %s", sdists_output, results)
