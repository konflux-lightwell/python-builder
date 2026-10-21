"""Publish final OCI images, SBOMs, and indexes; trusted artifacts are owned by Tekton."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lightwell_python.runtime import Runner, TaskError, read_json, sha256, write_json


def repository(reference: str) -> str:
    """Remove a tag and digest while retaining the registry hostname and port.

    Args:
        reference: Qualified OCI reference, for example localhost:5000/org/repo:tag.

    Returns:
        The repository portion, such as localhost:5000/org/repo.

    Raises:
        TaskError: The reference lacks a repository path or contains unsafe syntax.
    """
    value = reference.split("@", 1)[0]
    last = value.rsplit("/", 1)[-1]
    if ":" in last:
        value = value.rsplit(":", 1)[0]
    if not value or value.startswith("-") or "/" not in value or any(c.isspace() for c in value):
        raise TaskError("Expected a qualified OCI repository reference")
    return value


def pinned(reference: str) -> str:
    """Validate a SHA-256 image reference and remove any redundant tag.

    Args:
        reference: Qualified repository reference ending in @sha256:<64 hex digits>.

    Returns:
        The normalized repository@sha256:digest reference.

    Raises:
        TaskError: The reference is not qualified or lacks a valid SHA-256 digest.
    """
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", reference):
        raise TaskError("Image reference must be pinned to a sha256 digest")
    return f"{repository(reference)}@{reference.rsplit('@', 1)[1]}"


def select_auth(reference: str, config: dict) -> dict:
    """Select the most specific credential matching the target repository.

    Args:
        reference: OCI image reference for the operation.
        config: Docker authentication configuration, left unchanged.

    Returns:
        A registry-scoped auth mapping. When no inline credential matches, configured
        credential helpers are preserved so developer environments can still resolve
        credentials externally.
    """
    repo = repository(reference)
    candidate = repo
    auths = config.get("auths", {})
    while candidate:
        if candidate in auths:
            return {"auths": {repo.split("/", 1)[0]: auths[candidate]}}
        if "/" not in candidate:
            break
        candidate = candidate.rsplit("/", 1)[0]
    # Preserve credential helper support for developer environments.
    return {key: value for key, value in config.items() if key in {"credsStore", "credHelpers"}} | {
        "auths": {}
    }


@dataclass
class Registry:
    """Publish final build images and attachments through external OCI tools.

    Trusted artifact transport belongs to separate Tekton steps. This adapter only
    handles final images, SBOMs, and build indexes.

    Attributes:
        runner: Process adapter used for ORAS and cosign execution.
        auth_file: Optional explicit Docker auth file, overriding environment defaults.
    """

    runner: Runner
    auth_file: Path | None = None

    @contextmanager
    def auth(self, reference: str) -> Iterator[Path]:
        """Temporarily materialize credentials scoped to an image repository.

        Args:
            reference: Target image or repository reference used to select credentials.

        Yields:
            A mode-0600 config.json in a private temporary directory. The directory is
            removed on context exit, even if publication fails. Credentials are read from
            auth_file, AUTHFILE, DOCKER_CONFIG/config.json, or ~/.docker/config.json,
            in that order of precedence.
        """
        env = self.runner.environment
        docker_directory = Path(env.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        default_auth_file = docker_directory / "config.json"
        configured_auth_file = Path(env.get("AUTHFILE", str(default_auth_file)))
        original = self.auth_file or configured_auth_file
        config = read_json(original) if original.is_file() else {}
        with tempfile.TemporaryDirectory(prefix="lightwell-auth-") as directory:
            path = Path(directory) / "config.json"
            path.touch(mode=0o600)
            write_json(path, select_auth(reference, config))
            yield path

    def push(
        self,
        image: str,
        directory: Path,
        artifact_type: str,
        annotations: Mapping[str, str],
        tags: list[str] | None = None,
    ) -> str:
        """Publish staged files and identify the exact manifest that was pushed.

        Args:
            image: Destination OCI reference, normally including a tag.
            directory: Staged artifact directory; non-hidden entries are uploaded.
            artifact_type: OCI artifact media type for the build output.
            annotations: Manifest annotations, including any expiration policy.
            tags: Optional additional tags to apply to the pushed digest.

        Returns:
            A digest-pinned image reference derived from ORAS's exported manifest.
            No mutable-tag lookup is used to calculate the result.

        Raises:
            TaskError: The staging directory is empty or publication/tagging fails.
        """
        files = [p.name for p in sorted(directory.iterdir()) if not p.name.startswith(".")]
        if not files:
            raise TaskError("Artifact directory is empty")
        args = ["oras", "push", image, "--artifact-type", artifact_type]
        for key, value in annotations.items():
            args += ["--annotation", f"{key}={value}"]
        with self.auth(image) as auth:
            manifest = auth.parent / "manifest.json"
            self.runner.run(
                [*args, "--registry-config", auth, "--export-manifest", manifest, *files],
                cwd=directory,
                attempts=3,
            )
            # Hash the exact manifest just pushed. Resolving a mutable tag here
            # could return another concurrent build's digest.
            ref = pinned(f"{repository(image)}@sha256:{sha256(manifest)}")
            if tags:
                self.runner.run(["oras", "tag", "--registry-config", auth, ref, *tags], attempts=3)
        return ref

    def attach(self, reference: str, path: Path, media_type: str) -> None:
        """Attach a file to a digest-pinned image as an OCI referrer.

        Args:
            reference: Immutable image reference receiving the attachment.
            path: Local file to attach, retaining its basename for downstream consumers.
            media_type: Artifact and layer media type required by the consumer.

        Raises:
            TaskError: The subject is not digest-pinned or attachment fails.
        """
        reference = pinned(reference)
        with self.auth(reference) as auth:
            self.runner.run(
                [
                    "oras",
                    "attach",
                    reference,
                    "--registry-config",
                    auth,
                    "--artifact-type",
                    media_type,
                    f"{path.name}:{media_type}",
                ],
                cwd=path.parent,
                attempts=3,
            )

    def attach_sbom(self, reference: str, sbom: Path) -> str:
        """Attach an SPDX SBOM with cosign and return its content address.

        Args:
            reference: Immutable image reference receiving the SBOM.
            sbom: Local SPDX JSON file to upload.

        Returns:
            The repository-qualified SHA-256 address of the SBOM bytes.

        Raises:
            TaskError: The subject is not digest-pinned or cosign attachment fails.
        """
        reference = pinned(reference)
        with self.auth(reference) as auth:
            self.runner.run(
                ["cosign", "attach", "sbom", "--sbom", sbom, "--type", "spdx", reference],
                env={"DOCKER_CONFIG": str(auth.parent)},
                attempts=3,
            )
        return f"{repository(reference)}@sha256:{sha256(sbom)}"
