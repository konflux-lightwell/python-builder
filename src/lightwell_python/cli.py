"""Task-specific command-line interfaces and shared execution setup."""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from lightwell_python.registry import Registry
from lightwell_python.runtime import BuildType, Package, Runner, TaskError
from lightwell_python.tasks import sdist, source, wheels


class TaskContext(TypedDict):
    """Validated runtime inputs shared by the task handlers.

    Attributes:
        workdir: Isolated scratch directory retained for diagnosis.
        results: Absolute directory receiving successful task results.
        package: Validated distribution name and upstream version.
        runner: Process adapter configured with timeout and certificate trust.
    """

    workdir: Path
    results: Path
    package: Package
    runner: Runner


TaskHandler = Callable[[argparse.Namespace, TaskContext], None]


def _option(
    target: argparse.ArgumentParser,
    name: str,
    *,
    default: Any = None,
    required: bool = False,
    **kwargs: Any,
) -> None:
    """Register an option with its uppercase environment-variable fallback.

    The environment key follows the public option name, even when ``dest`` maps
    the parsed value to a different task argument name.
    """
    value = os.environ.get(name.upper().replace("-", "_"), default)
    target.add_argument("--" + name, default=value, required=required and value is None, **kwargs)


def _common_parser(task: str, handler: TaskHandler) -> argparse.ArgumentParser:
    """Create shared options and bind the task's argparse dispatch target."""
    result = argparse.ArgumentParser(
        description=f"Run Lightwell {task} using locally restored inputs."
    )
    result.set_defaults(func=handler)

    _option(result, "package", required=True)
    _option(result, "version", required=True)
    _option(result, "workdir", type=Path, default=Path("/var/workdir"))
    _option(result, "results-dir", dest="results", type=Path, default=Path("/tekton/results"))
    _option(result, "ca-bundle", type=Path, default=Path("/mnt/trusted-ca/ca-bundle.crt"))
    _option(result, "auth-file", type=Path)
    _option(result, "command-timeout", type=float, default=7200)
    result.add_argument("--verbose", action="store_true")
    return result


def _publication_options(target: argparse.ArgumentParser) -> None:
    """Add the restored input and registry options shared by wheel and sdist builds."""
    _option(target, "sdists-dir", type=Path, required=True)
    _option(target, "image", required=True)
    _option(target, "image-expires-after", dest="image_expires", default="")


def source_parser() -> argparse.ArgumentParser:
    """Define source preparation arguments and select its handler.

    Returns:
        A parser for the source-to-sdist entrypoint, including environment defaults.
    """
    result = _common_parser("source-to-sdist", _run_source)
    _option(result, "build-type", choices=[mode.value for mode in BuildType], required=True)
    _option(result, "source-dir", type=Path, required=True)
    _option(result, "sdists-output", type=Path, required=True)
    _option(result, "build-configs-repo-url", dest="config_url", required=True)
    _option(result, "build-configs-revision", dest="config_revision", required=True)
    _option(result, "git-auth", type=Path)
    return result


def wheels_parser() -> argparse.ArgumentParser:
    """Define wheel build arguments and select its handler.

    Returns:
        A parser for the build-wheels entrypoint, including environment defaults.
    """
    result = _common_parser("build-wheels", _run_wheels)
    _publication_options(result)
    _option(result, "build-type", choices=[mode.value for mode in BuildType], required=True)
    _option(result, "computed-version", dest="computed", required=True)
    _option(result, "manifest-output", type=Path, required=True)
    _option(result, "wheel-server-url", dest="wheel_server", default="")
    _option(result, "pulp-secret", type=Path, default=Path("/mnt/pulp-secret"))
    _option(result, "overrides", type=Path, default=Path("/usr/local/share/fromager/overrides"))
    _option(result, "jobs", type=int)
    return result


def sdist_parser() -> argparse.ArgumentParser:
    """Define sdist normalization arguments and select its handler.

    Returns:
        A parser for the build-sdist entrypoint, including environment defaults.
    """
    result = _common_parser("build-sdist", _run_sdist)
    _publication_options(result)
    _option(result, "git-url", required=True)
    _option(result, "revision", required=True)
    return result


def _run_source(args: argparse.Namespace, context: TaskContext) -> None:
    """Pass parsed source preparation inputs to the task implementation."""
    source.run(
        **context,
        build_type=BuildType(args.build_type),
        source_dir=args.source_dir.resolve(),
        sdists_output=args.sdists_output.resolve(),
        config_url=args.config_url,
        config_revision=args.config_revision,
        git_auth=args.git_auth,
    )


def _run_wheels(args: argparse.Namespace, context: TaskContext) -> None:
    """Pass parsed wheel build inputs and registry credentials to the task."""
    wheels.run(
        **context,
        build_type=BuildType(args.build_type),
        computed=args.computed,
        sdists_dir=args.sdists_dir.resolve(),
        registry=Registry(context["runner"], args.auth_file),
        image=args.image,
        manifest_output=args.manifest_output.resolve(),
        wheel_server=args.wheel_server,
        pulp_secret=args.pulp_secret,
        image_expires=args.image_expires,
        overrides=args.overrides,
        jobs=args.jobs,
    )


def _run_sdist(args: argparse.Namespace, context: TaskContext) -> None:
    """Pass parsed sdist normalization inputs and registry credentials to the task."""
    sdist.run(
        **context,
        sdists_dir=args.sdists_dir.resolve(),
        registry=Registry(context["runner"], args.auth_file),
        image=args.image,
        git_url=args.git_url,
        revision=args.revision,
        image_expires=args.image_expires,
    )


PARSER_FACTORIES: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "source-to-sdist": source_parser,
    "build-wheels": wheels_parser,
    "build-sdist": sdist_parser,
}


def parser(task: str) -> argparse.ArgumentParser:
    """Select the independent argument definition for a task.

    Args:
        task: One of source-to-sdist, build-wheels, or build-sdist.

    Returns:
        A fresh parser with environment defaults and its bound execution target.

    Raises:
        ValueError: The task name is not supported.
    """
    try:
        factory = PARSER_FACTORIES[task]
    except KeyError as exc:
        raise ValueError(f"Unknown task: {task}") from exc
    return factory()


def execute(task: str, argv: list[str] | None = None) -> int:
    """Parse inputs, prepare scratch space, and invoke the argparse dispatch target.

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

        args.workdir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"{task}-", dir=args.workdir)).resolve()
        logging.getLogger(__name__).info("Task workspace: %s", workdir)
        context = TaskContext(
            workdir=workdir,
            results=args.results.resolve(),
            package=package,
            runner=runner,
        )
        args.func(args, context)
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
