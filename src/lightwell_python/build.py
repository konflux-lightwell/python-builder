"""Fromager execution, auditwheel repair, and artifact collection in Python."""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from packaging.utils import parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

from lightwell_python.runtime import Package, Runner, TaskError, log_stage, sha256
from lightwell_python.wheel_metadata import patch_wheel, system_libraries

LOG = logging.getLogger(__name__)


@contextmanager
def backend_environment(
    base: dict[str, str], secret: Path | None
) -> Generator[dict[str, str], None, None]:
    """Prepare a build environment with optional temporary Pulp credentials.

    Args:
        base: Existing environment to copy; it is never mutated.
        secret: Optional mounted secret directory. SERVICE_ACCOUNT_* files take
            precedence over username/password files and inherited values.

    Yields:
        A copy of the environment with NETRC pointing to a private temporary file
        when both credentials are available. The file is removed on context exit,
        including when the build raises an exception.
    """
    environment = dict(base)
    credentials = {}
    for key, alternate in (
        ("SERVICE_ACCOUNT_USERNAME", "username"),
        ("SERVICE_ACCOUNT_PASSWORD", "password"),
    ):
        value = base.get(key, "")
        if secret:
            for name in (key, alternate):
                path = secret / name
                if path.is_file():
                    value = path.read_text(encoding="utf-8").rstrip("\n")
                    break
        credentials[key] = value
    with tempfile.TemporaryDirectory(prefix="lightwell-netrc-") as directory:
        username = credentials["SERVICE_ACCOUNT_USERNAME"]
        password = credentials["SERVICE_ACCOUNT_PASSWORD"]
        if username and password:
            netrc = Path(directory) / "netrc"
            netrc.touch(mode=0o600)
            netrc.write_text(
                f"machine packages.redhat.com login {json.dumps(username)} "
                f"password {json.dumps(password)}\n",
                encoding="utf-8",
            )
            environment["NETRC"] = str(netrc)
        yield environment


def run_fromager(
    runner: Runner,
    output: Path,
    sdists: Path,
    settings: Path,
    package: Package,
    environment: dict[str, str],
    wheel_server: str = "",
    *,
    overrides: Path = Path("/usr/local/share/fromager/overrides"),
    jobs: int | None = None,
) -> None:
    """Bootstrap dependencies, build the resolved order, and repair wheels.

    Args:
        runner: Adapter used to execute Fromager and auditwheel.
        output: New build output directory; it must not already exist.
        sdists: Writable sdist cache with the pre-placed primary source archive.
        settings: Directory containing resolved per-package Fromager YAML.
        package: Primary distribution and exact upstream version to build.
        environment: Backend environment, including any temporary credentials.
        wheel_server: Optional cache URL; empty disables prebuilt-wheel lookup.
        overrides: Builder-provided directory containing settings.yaml and patches.
        jobs: Optional Fromager parallelism limit.

    Raises:
        TaskError: A tool fails, the build summary is absent, or wheel repair fails.
        FileExistsError: The output directory already exists.
    """
    output.mkdir(parents=True, exist_ok=False)
    common: list[str | Path] = [
        "fromager",
        "--settings-file",
        overrides / "settings.yaml",
        "--settings-dir",
        settings,
    ]
    if (overrides / "patches").is_dir():
        common += ["--patches-dir", overrides / "patches"]
    if jobs:
        common += ["--jobs", str(jobs)]
    cache = ["--cache-wheel-server-url", wheel_server] if wheel_server else []
    env = environment | {"FROMAGER_SDISTS_REPO": str(sdists), "SOURCE_DATE_EPOCH": "0"}
    with log_stage("Fromager bootstrap and dependency resolution"):
        runner.run([*common, "bootstrap", package.requirement, *cache], cwd=output, env=env)
    with log_stage("Fromager build sequence"):
        runner.run(
            [*common, "build-sequence", "work-dir/build-order.json", *cache], cwd=output, env=env
        )
    summary = output / "work-dir/build-sequence-summary.json"
    if not summary.is_file():
        raise TaskError("Fromager did not produce a build sequence summary")
    shutil.copyfile(summary, output / f"build-sequence-summary-{package.name}.json")
    with log_stage("Repair native wheels and update embedded SBOMs"):
        repair_wheels(runner, output / "wheels-repo/downloads", env)


def repair_wheels(runner: Runner, wheels: Path, environment: dict[str, str]) -> None:
    """Repair native Linux wheels and update publishable wheel SBOMs.

    Args:
        runner: Adapter for auditwheel repair/show commands.
        wheels: Fromager wheel download directory, modified in place.
        environment: Backend environment inherited by auditwheel.

    Raises:
        TaskError: Shared-library basenames conflict, auditwheel fails, or wheel
            metadata cannot be rewritten safely.
    """
    native = sorted(wheels.glob("*linux_x86_64.whl"))
    LOG.info("Native wheels requiring repair: %d", len(native))
    if native:
        with tempfile.TemporaryDirectory(prefix="lightwell-libs-") as directory:
            libraries = Path(directory)
            for wheel in native:
                _extract_shared_libraries(wheel, libraries)
            inherited = environment.get(
                "LD_LIBRARY_PATH", runner.environment.get("LD_LIBRARY_PATH", "")
            )
            env = environment | {
                "LD_LIBRARY_PATH": directory + (":" + inherited if inherited else "")
            }
            runner.run(["auditwheel", "repair", *native, "-w", wheels], env=env)
    publishable_wheels = [wheel for wheel in sorted(wheels.glob("*.whl")) if publishable(wheel)]
    LOG.info("Wheels to process for publication: %d", len(publishable_wheels))
    for index, wheel in enumerate(publishable_wheels, start=1):
        LOG.info("Processing wheel %d/%d: %s", index, len(publishable_wheels), wheel.name)
        libraries_by_name = {}
        if "manylinux" in wheel.name:
            report = runner.run(["auditwheel", "show", wheel], env=environment, capture=True)
            libraries_by_name = system_libraries(report)
        patch_wheel(wheel, libraries_by_name)


def _extract_shared_libraries(wheel: Path, destination: Path) -> None:
    """Flatten a wheel's shared libraries for auditwheel, rejecting conflicting names."""
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith("/") or not re.search(r"\.so(?:\.|$)", name):
                continue

            target = destination / Path(name).name
            with archive.open(name) as source:
                if target.exists():
                    if source.read() != target.read_bytes():
                        raise TaskError(f"Conflicting wheel library: {target.name}")
                    continue
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def publishable(wheel: Path) -> bool:
    """Check whether a wheel has an accepted publication platform tag.

    Args:
        wheel: Wheel path; only its filename is inspected.

    Returns:
        True for pure Python or manylinux wheels. Unrepaired Linux wheels are false.
    """
    _, _, _, tags = parse_wheel_filename(wheel.name)
    return any(
        (tag.abi == "none" and tag.platform == "any") or tag.platform.startswith("manylinux")
        for tag in tags
    )


def sync_sdists(source: Path, destination: Path, package: Package, computed: str) -> None:
    """Copy built source archives into the collection directory.

    Args:
        source: Directory searched recursively for built tar.gz archives.
        destination: Directory receiving the flattened archive filenames.
        package: Primary distribution whose upstream filename may need renaming.
        computed: Full version to use in the primary archive's destination filename.

    Raises:
        TaskError: Two different archives would occupy the same destination path.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*.tar.gz")):
        name, version = parse_sdist_filename(path.name)
        target_name = (
            f"{package.name}-{computed}.tar.gz"
            if name == package.name and version == Version(package.version)
            else path.name
        )
        target = destination / target_name
        if target.exists() and sha256(path) != sha256(target):
            raise TaskError(f"Conflicting built sdists: {target_name}")
        if path.resolve() != target.resolve():
            shutil.copyfile(path, target)


def collect_artifacts(output: Path, destination: Path) -> None:
    """Collect publishable wheels, built sdists, and build summaries.

    Args:
        output: Fromager output tree containing wheels-repo and sdists-repo.
        destination: New directory to stage the final OCI image contents.

    Raises:
        FileExistsError: The staging directory already exists.
        TaskError: No artifacts exist or differing files share a destination basename.
    """
    destination.mkdir(parents=True, exist_ok=False)
    files = [p for p in sorted((output / "wheels-repo/downloads").glob("*.whl")) if publishable(p)]
    files += sorted((output / "sdists-repo/builds").rglob("*.tar.gz"))
    files += sorted(output.glob("build-sequence-summary-*.json"))
    for path in files:
        target = destination / path.name
        if target.exists() and sha256(path) != sha256(target):
            raise TaskError(f"Artifact basename collision: {path.name}")
        shutil.copyfile(path, target)
    if not files:
        raise TaskError("No build artifacts were produced")
