"""Git checkout retag-and-verify operations."""

from __future__ import annotations

from pathlib import Path

from lightwell_python.runtime import Runner, TaskError


def retag(repo: Path, computed: str, runner: Runner) -> str:
    """Untag every tag pointing at HEAD, apply v{computed}, and return HEAD SHA.

    Args:
        repo: Git repository root.
        computed: Version string (e.g., "1.0+rhlw.1").
        runner: Command executor.

    Returns:
        Full HEAD commit SHA.

    Raises:
        TaskError: Git operations fail or verification fails.
    """
    base = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]

    def git(*args: str) -> str:
        """Run a read-only git query scoped to this repository."""
        return runner.run([*base, *args], capture=True).strip()

    def git_run(*args: str) -> None:
        """Run a git mutation scoped to this repository."""
        runner.run([*base, *args])

    # Get current tags at HEAD
    tags_output = git("tag", "--points-at", "HEAD")
    current_tags = [tag.strip() for tag in tags_output.split("\n") if tag.strip()]

    # Untag all current tags
    for tag in current_tags:
        git_run("tag", "-d", tag)

    # Apply new tag
    new_tag = f"v{computed}"
    git_run("tag", new_tag)

    # Verify the tag is exactly what we applied
    verified_tag = git("describe", "--tags", "--exact-match", "HEAD")
    if verified_tag != new_tag:
        raise TaskError(f"Tag verification failed: expected {new_tag}, got {verified_tag}")

    # Return HEAD SHA
    return git("rev-parse", "HEAD")


def verify_clean(repo: Path, rewritten: list[Path], runner: Runner) -> None:
    """Verify git status shows modifications only for paths in rewritten.

    Args:
        repo: Git repository root.
        rewritten: List of absolute paths that are expected to be dirty.
        runner: Command executor.

    Raises:
        TaskError: Actual dirty files don't match expected set exactly.
    """
    base = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]

    def git(*args: str) -> str:
        """Run a read-only git query scoped to this repository."""
        return runner.run([*base, *args], capture=True).rstrip("\n")

    # Get git status output
    status_output = git("status", "--porcelain")

    # Parse status output into a set of repo-relative paths
    dirty_files = set()
    for line in status_output.split("\n"):
        if line.strip():
            # Skip the 3-char status prefix (2 chars + space) to get the path
            path_str = line[3:]
            dirty_files.add(path_str)

    # Convert expected rewritten paths to repo-relative strings
    expected_dirty = {str(p.relative_to(repo)) for p in rewritten}

    # Check if they match exactly
    if dirty_files != expected_dirty:
        raise TaskError(f"Git status unexpected: expected {expected_dirty}, got {dirty_files}")
