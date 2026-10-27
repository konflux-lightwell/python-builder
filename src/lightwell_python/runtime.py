"""Explicit process execution, task configuration, and filesystem primitives."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import Version

LOG = logging.getLogger(__name__)


class TaskError(RuntimeError):
    """A task cannot satisfy its input or output contract."""


@contextmanager
def log_stage(description: str) -> Generator[None, None, None]:
    """Log a stage's start, outcome, and elapsed time without exposing exception data.

    Args:
        description: Human-readable operation label. Never include credentials,
            environment values, or unfiltered command arguments.

    Yields:
        Control to the operation; failures propagate unchanged to the caller.
    """
    started = time.monotonic()
    LOG.info("Starting: %s", description)
    try:
        yield
    except Exception as exc:
        LOG.error(
            "Failed: %s after %.1fs (%s)",
            description,
            time.monotonic() - started,
            type(exc).__name__,
        )
        raise
    else:
        LOG.info("Completed: %s in %.1fs", description, time.monotonic() - started)


class BuildType(StrEnum):
    """Supported version and dependency provenance policies."""

    BACKPORT = "Backport"
    NOVEL = "Novel"
    VALIDATED = "Validated"

    @property
    def distribution_target(self) -> str:
        """dev.lightwell.distribution-target annotation value for this build type.

        Backport and Novel both ship to the "remediated" repo/stream; the
        vuln-class (CVE vs LW-) distinction is derived separately from the
        vuln-id prefix, not from this annotation.
        """
        if self is BuildType.VALIDATED:
            return "validated"
        return "remediated"


@dataclass(frozen=True)
class Package:
    """A validated Python distribution name and upstream version.

    Attributes:
        name: PEP 503 canonical name, normalized during construction.
        version: Upstream PEP 440 version with a filename-safe spelling.
    """

    name: str
    version: str

    def __post_init__(self) -> None:
        """Normalize the package name and reject unsafe version spellings."""
        object.__setattr__(self, "name", canonicalize_name(self.name, validate=True))
        Version(self.version)
        if not re.fullmatch(r"[A-Za-z0-9.!+_-]+", self.version):
            raise TaskError("Version contains unsafe filename characters")

    @property
    def requirement(self) -> str:
        """Return the exact Fromager requirement."""
        return f"{self.name}=={self.version}"


@dataclass
class Runner:
    """Run argument vectors without a shell; retry only explicitly opted-in operations.

    Build output is streamed by default. Captured output is reserved for small
    machine-readable responses. Environment values and command arguments are
    deliberately omitted from failure messages because they may hold credentials.
    """

    environment: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    timeout: float = 7200

    def run(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = False,
        attempts: int = 1,
    ) -> str:
        """Execute an argument vector with an optional bounded retry policy.

        Args:
            args: Executable followed by arguments; no shell expansion is performed.
            cwd: Working directory for this command, without changing the parent process.
            env: Overrides merged into this runner's base environment.
            capture: Capture stdout for small machine-readable responses. Otherwise,
                inherit stdout so long-running builds remain visible.
            attempts: Maximum executions after nonzero exits. Build commands should use
                the default of one; registry operations can opt into retries.

        Returns:
            Captured stdout, or an empty string when output is streamed.

        Raises:
            ValueError: The requested number of attempts is less than one.
            TaskError: Execution fails, times out, or exhausts its retries. Timeouts and
                launch errors are not retried. Command arguments are omitted from errors
                because they may contain credentials.
        """
        if attempts < 1:
            raise ValueError("attempts must be positive")
        command = [str(arg) for arg in args]
        for attempt in range(attempts):
            executable = Path(command[0]).name
            started = time.monotonic()
            LOG.info("Running %s (attempt %d/%d)", executable, attempt + 1, attempts)
            try:
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=self.environment | dict(env or {}),
                    text=True,
                    stdout=subprocess.PIPE if capture else None,
                    check=False,
                    timeout=self.timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TaskError(
                    f"Unable to execute {Path(command[0]).name}: {type(exc).__name__}"
                ) from exc
            if result.returncode == 0:
                LOG.info("%s completed in %.1fs", executable, time.monotonic() - started)
                return result.stdout or ""
            if attempt + 1 < attempts:
                delay = min(2**attempt, 10)
                LOG.warning(
                    "%s exited with status %d after %.1fs; retrying in %ds",
                    executable,
                    result.returncode,
                    time.monotonic() - started,
                    delay,
                )
                time.sleep(delay)
        raise TaskError(f"{Path(command[0]).name} exited with status {result.returncode}")


def read_json(path: Path) -> Any:
    """Read UTF-8 JSON while rejecting duplicate object keys.

    Args:
        path: JSON file to read.

    Returns:
        The decoded JSON value; callers validate the expected schema.

    Raises:
        TaskError: An object contains the same key more than once.
        json.JSONDecodeError: The file does not contain valid JSON.
        OSError: The file cannot be read.
    """

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Build a decoded JSON object without discarding duplicate keys."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TaskError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def write_json(path: Path, data: Any) -> None:
    """Write indented UTF-8 JSON with a terminating newline.

    Args:
        path: Destination file. Missing parent directories are created.
        data: JSON-serializable content. An existing file is overwritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    """Compute a file's SHA-256 digest without buffering the entire file.

    Args:
        path: File to hash.

    Returns:
        Lowercase hexadecimal digest, without the ``sha256:`` prefix.
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_results(directory: Path, results: Mapping[str, str | int]) -> None:
    """Write Python-owned Tekton results after successful processing.

    Args:
        directory: Results mount, normally ``/tekton/results``.
        results: Result names and values to write without terminating newlines.
            Trusted-artifact URI results are owned by the separate create steps.

    Raises:
        TaskError: A result name contains characters outside the accepted format.
        OSError: A result file cannot be written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in results.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]*", name):
            raise TaskError(f"Invalid result name: {name}")
        (directory / name).write_text(str(value), encoding="utf-8")


def contained_file(root: Path, relative: str) -> Path:
    """Resolve a source-relative file and check that it stays inside the root.

    Args:
        root: Directory that owns the file, such as the package source tree.
        relative: Relative filename from a rewrite rule or source scan.

    Returns:
        The resolved absolute path to an existing regular file.

    Raises:
        TaskError: The path is absolute, contains traversal, escapes through a
            symlink, or does not resolve to a file.
    """
    path = Path(relative)
    target = (root / path).resolve()
    if path.is_absolute() or ".." in path.parts or not target.is_relative_to(root.resolve()):
        raise TaskError(f"Path escapes source directory: {relative}")
    if not target.is_file():
        raise TaskError(f"Required file missing: {relative}")
    return target


def copy_input_tree(source: Path, destination: Path) -> None:
    """Copy a restored input into writable scratch space.

    The restored input is left intact. Symlinks are preserved, and owner read/write
    permissions are added to copied files; copied directories also gain traversal.

    Args:
        source: Existing directory restored by the trusted-artifact use step.
        destination: New scratch directory outside the input tree.

    Raises:
        TaskError: The input is missing or the destination is inside it.
        FileExistsError: The destination already exists.
    """
    if not source.is_dir():
        raise TaskError(f"Restored input directory is missing: {source}")
    if destination.resolve().is_relative_to(source.resolve()):
        raise TaskError("Scratch directory must be outside the restored input tree")
    shutil.copytree(source, destination, symlinks=True)
    for path in [destination, *destination.rglob("*")]:
        if not path.is_symlink():
            access = stat.S_IRUSR | stat.S_IWUSR
            if path.is_dir():
                access |= stat.S_IXUSR
            path.chmod(path.stat().st_mode | access)
