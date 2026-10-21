"""Python task logic operating between Tekton trusted-artifact use/create steps."""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from lightwell_python.registry import Registry
from lightwell_python.runtime import BuildType, Package, Runner, TaskError
from lightwell_python.tasks import sdist, source, wheels


def parser(task: str) -> argparse.ArgumentParser:
    """Build the command-line interface for a single pipeline task.

    Args:
        task: One of source-to-sdist, build-wheels, or build-sdist.

    Returns:
        A parser whose options also accept uppercase underscore environment defaults.
        Inputs are local paths restored by Tekton's trusted-artifact steps.

    Raises:
        ValueError: The task name is not supported.
    """
    result = argparse.ArgumentParser(
        description=f"Run Lightwell {task} using locally restored inputs."
    )

    def option(name: str, *, default: Any = None, required: bool = False, **kwargs: Any) -> None:
        """Register one CLI option with its matching environment fallback."""
        value = os.environ.get(name.upper().replace("-", "_"), default)
        result.add_argument(
            "--" + name, default=value, required=required and value is None, **kwargs
        )

    option("package", required=True)
    option("version", required=True)
    option("workdir", type=Path, default=Path("/var/workdir"))
    option("results-dir", type=Path, default=Path("/tekton/results"))
    option("ca-bundle", type=Path, default=Path("/mnt/trusted-ca/ca-bundle.crt"))
    option("auth-file", type=Path)
    option("command-timeout", type=float, default=7200)
    result.add_argument("--verbose", action="store_true")
    if task != "build-sdist":
        option("build-type", choices=[mode.value for mode in BuildType], required=True)
    if task == "source-to-sdist":
        option("source-dir", type=Path, required=True)
    else:
        option("sdists-dir", type=Path, required=True)
        option("image", required=True)
        option("image-expires-after", default="")
    if task == "build-wheels":
        option("computed-version", required=True)
    if task == "source-to-sdist":
        option("sdists-output", type=Path, required=True)
        option("build-configs-repo-url", required=True)
        option("build-configs-revision", required=True)
        option("git-auth", type=Path)
    elif task == "build-wheels":
        option("manifest-output", type=Path, required=True)
        option("wheel-server-url", default="")
        option("pulp-secret", type=Path, default=Path("/mnt/pulp-secret"))
        option("overrides", type=Path, default=Path("/usr/local/share/fromager/overrides"))
        option("jobs", type=int)
    elif task == "build-sdist":
        option("git-url", required=True)
        option("revision", required=True)
    else:
        raise ValueError(f"Unknown task: {task}")
    return result


def execute(task: str, argv: list[str] | None = None) -> int:
    """Parse inputs, prepare scratch space, and dispatch the task implementation.

    Args:
        task: Task name selecting the parser and implementation.
        argv: Explicit CLI arguments, or None to use the process arguments.

    Returns:
        Zero on success or one for an expected task, filesystem, or value error.
        Argparse handles help and invalid CLI syntax through SystemExit.

    Notes:
        Each invocation retains a unique scratch directory under --workdir for
        diagnosis. Trusted-artifact outputs use explicit handoff paths outside that
        directory; this function never invokes trusted-artifact transport.
    """
    args = parser(task).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.command_timeout <= 0 or (
            getattr(args, "jobs", None) is not None and args.jobs <= 0
        ):
            raise TaskError("Timeout and jobs must be positive")
        package = Package(args.package, args.version)
        runner = Runner(timeout=args.command_timeout)
        if args.ca_bundle.is_file():
            for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"):
                runner.environment[key] = str(args.ca_bundle.resolve())
        registry = Registry(runner, args.auth_file)
        args.workdir.mkdir(parents=True, exist_ok=True)
        # Keep intermediate outputs for diagnosis; each invocation is isolated.
        workdir = Path(tempfile.mkdtemp(prefix=f"{task}-", dir=args.workdir)).resolve()
        logging.getLogger(__name__).info("Task workspace: %s", workdir)
        common = {
            "workdir": workdir,
            "results": args.results_dir.resolve(),
            "package": package,
            "runner": runner,
        }
        if task == "source-to-sdist":
            source.run(
                **common,
                build_type=BuildType(args.build_type),
                source_dir=args.source_dir.resolve(),
                sdists_output=args.sdists_output.resolve(),
                config_url=args.build_configs_repo_url,
                config_revision=args.build_configs_revision,
                git_auth=args.git_auth,
            )
        elif task == "build-wheels":
            wheels.run(
                **common,
                build_type=BuildType(args.build_type),
                computed=args.computed_version,
                sdists_dir=args.sdists_dir.resolve(),
                registry=registry,
                image=args.image,
                manifest_output=args.manifest_output.resolve(),
                wheel_server=args.wheel_server_url,
                pulp_secret=args.pulp_secret,
                image_expires=args.image_expires_after,
                overrides=args.overrides,
                jobs=args.jobs,
            )
        elif task == "build-sdist":
            sdist.run(
                **common,
                sdists_dir=args.sdists_dir.resolve(),
                registry=registry,
                image=args.image,
                git_url=args.git_url,
                revision=args.revision,
                image_expires=args.image_expires_after,
            )
    except (TaskError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    return 0


def source_to_sdist() -> None:
    """Run source-to-sdist from the installed console script."""
    raise SystemExit(execute("source-to-sdist"))


def build_wheels() -> None:
    """Run python-fromager-build-wheels from the installed console script."""
    raise SystemExit(execute("build-wheels"))


def build_sdist() -> None:
    """Run python-fromager-build-sdist from the installed console script."""
    raise SystemExit(execute("build-sdist"))
