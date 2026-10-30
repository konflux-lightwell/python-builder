"""Git checkout retag-and-verify operations."""

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
    # Get current tags at HEAD
    tags_output = runner.run(["git", "-C", str(repo), "tag", "--points-at", "HEAD"], capture=True)
    current_tags = [tag.strip() for tag in tags_output.split("\n") if tag.strip()]

    # Untag all current tags
    for tag in current_tags:
        runner.run(["git", "-C", str(repo), "tag", "-d", tag])

    # Apply new tag
    new_tag = f"v{computed}"
    runner.run(["git", "-C", str(repo), "tag", new_tag])

    # Verify the tag is exactly what we applied
    verify_output = runner.run(
        ["git", "-C", str(repo), "describe", "--tags", "--exact-match", "HEAD"], capture=True
    )
    verified_tag = verify_output.strip()
    if verified_tag != new_tag:
        raise TaskError(
            f"Tag verification failed: expected {new_tag}, got {verified_tag}"
        )

    # Return HEAD SHA
    head_sha = runner.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture=True)
    return head_sha.strip()


def verify_clean(repo: Path, rewritten: list[Path], runner: Runner) -> None:
    """Verify git status shows modifications only for paths in rewritten.

    Args:
        repo: Git repository root.
        rewritten: List of absolute paths that are expected to be dirty.
        runner: Command executor.

    Raises:
        TaskError: Actual dirty files don't match expected set exactly.
    """
    # Get git status output
    status_output = runner.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture=True
    )

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
        raise TaskError(
            f"Git status unexpected: expected {expected_dirty}, got {dirty_files}"
        )
