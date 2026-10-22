"""Check the installed builder tools without network access or registry writes.

Run this with the task interpreter, /opt/lightwell/bin/python. The
Containerfile runs it after assembly so missing tools, plugins, shared libraries,
or CLI options fail the image build before the image can be used by Tekton.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CLI_COMMANDS = (
    "lightwell-source-to-sdist",
    "lightwell-build-wheels",
    "lightwell-build-sdist",
)


def run(command: list[str], required_options: tuple[str, ...] = ()) -> None:
    """Execute a local probe and verify options used by the task package.

    Args:
        command: Argument vector for a help or version probe.
        required_options: CLI flags that must occur in its output.

    Raises:
        RuntimeError: An executable or required option is missing, or a probe fails.
        subprocess.TimeoutExpired: The command does not finish within one minute.
    """
    if shutil.which(command[0]) is None:
        raise RuntimeError(f"Required executable is missing from PATH: {command[0]}")
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Probe failed: {' '.join(command)}\n{error.stdout}") from error
    for option in required_options:
        if option not in result.stdout:
            raise RuntimeError(f"{' '.join(command)} does not expose required option {option}")
    print(f"OK: {' '.join(command)}", flush=True)


def main() -> None:
    """Verify task dependencies and native builder tools."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("builder", "plugins"), required=True)
    args = parser.parse_args()

    if args.profile == "plugins":
        check_fromager_plugins()
        return

    for distribution in ("lightwell-python", "packaging", "PyYAML"):
        print(f"{distribution}=={importlib.metadata.version(distribution)}", flush=True)

    for command in CLI_COMMANDS:
        run([command, "--help"])

    common_probes = (
        (["git", "--version"], ()),
        (["oras", "push", "--help"], ("--export-manifest", "--registry-config")),
        (["oras", "tag", "--help"], ("--registry-config",)),
    )
    for command, options in common_probes:
        run(command, options)

    check_registry_auth()
    check_builder()

    for parent in (Path.cwd(), Path.home()):
        with tempfile.TemporaryDirectory(dir=parent, prefix="builder-smoke-") as directory:
            Path(directory, "write-check").write_text("ok\n")
    print(f"{args.profile} runtime checks passed as UID {os.getuid()}", flush=True)


def check_registry_auth() -> None:
    """Exercise select-oci-auth with synthetic credentials without contacting a registry."""
    with tempfile.TemporaryDirectory(prefix="builder-auth-smoke-") as directory:
        auth = Path(directory) / ".docker/config.json"
        auth.parent.mkdir()
        auth.write_text(json.dumps({"auths": {"registry.test/team": {"auth": "smoke-only"}}}))
        # The pinned helper reads ~/.docker/config.json. Isolate the child process
        # from developer credentials, including versions that support env overrides.
        environment = dict(os.environ) | {
            "HOME": directory,
            "AUTHFILE": str(auth),
            "DOCKER_CONFIG": str(auth.parent),
        }
        try:
            result = subprocess.run(
                ["select-oci-auth", "registry.test/team/image:tag"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            # This probe uses only synthetic credentials, so stderr is safe to show.
            raise RuntimeError(
                f"select-oci-auth smoke check failed (exit {error.returncode}): {error.stderr}"
            ) from error
        expected = {"auths": {"registry.test": {"auth": "smoke-only"}}}
        if json.loads(result.stdout) != expected:
            raise RuntimeError("select-oci-auth did not select the repository credential")
    print("OK: select-oci-auth credential selection", flush=True)


def check_builder() -> None:
    """Verify the native build tools and load the Fromager plugin entrypoints."""
    probes = (
        (["fromager", "--help"], ("--settings-file", "--settings-dir", "--patches-dir")),
        (["fromager", "bootstrap", "--help"], ("--cache-wheel-server-url",)),
        (["fromager", "build-sequence", "--help"], ()),
        (["fromager", "step", "prepare-source", "--help"], ()),
        (["fromager", "step", "build-sdist", "--help"], ()),
        (["auditwheel", "repair", "--help"], ()),
        (["patchelf", "--version"], ()),
        (["cosign", "attach", "sbom", "--help"], ("--sbom", "--type")),
        (["gcc", "--version"], ()),
        (["g++", "--version"], ()),
        (["gfortran", "--version"], ()),
        (["make", "--version"], ()),
        (["cmake", "--version"], ()),
        (["pkg-config", "--version"], ()),
        (["cargo", "--version"], ()),
        (["rustc", "--version"], ()),
    )
    for command, options in probes:
        run(command, options)

    overrides = Path("/usr/local/share/fromager/overrides")
    if not (overrides / "settings.yaml").is_file() or not (overrides / "patches").is_dir():
        raise RuntimeError("Fromager settings or patches are missing")

    # The plugin belongs to Fromager's environment, not the isolated task venv.
    run(
        [
            "/src/venv/bin/python",
            str(Path(__file__).resolve()),
            "--profile",
            "plugins",
        ]
    )


def check_fromager_plugins() -> None:
    """Load the package overrides using Fromager's own Python environment."""
    print(f"fromager=={importlib.metadata.version('fromager')}", flush=True)
    plugins = importlib.metadata.entry_points(group="fromager.project_overrides")
    if not any(plugin.name == "faiss_cpu" for plugin in plugins):
        raise RuntimeError("Fromager package override is missing: faiss_cpu")

    # Load every installed override, including any supplied by a newer base.
    for plugin in sorted(plugins, key=lambda entry: entry.name):
        plugin.load()
        print(f"OK: Fromager override {plugin.name}", flush=True)


if __name__ == "__main__":
    main()
