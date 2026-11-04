"""Git checkout retag-and-verify operations."""

from __future__ import annotations

from pathlib import Path

from lightwell_python.runtime import Runner, TaskError, run_git


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
    tags_output = run_git(runner, repo, "tag", "--points-at", "HEAD").strip()
    current_tags = [tag.strip() for tag in tags_output.split("\n") if tag.strip()]

    # Untag all current tags
    for tag in current_tags:
        run_git(runner, repo, "tag", "-d", tag, capture=False)

    # Apply new tag
    new_tag = f"v{computed}"
    run_git(runner, repo, "tag", new_tag, capture=False)

    # Verify the tag is exactly what we applied
    verified_tag = run_git(runner, repo, "describe", "--tags", "--exact-match", "HEAD").strip()
    if verified_tag != new_tag:
        raise TaskError(f"Tag verification failed: expected {new_tag}, got {verified_tag}")

    # Return HEAD SHA
    return run_git(runner, repo, "rev-parse", "HEAD").strip()


def verify_clean(repo: Path, rewritten: list[Path], runner: Runner) -> None:
    """Verify git status shows modifications only for paths in rewritten.

    Untracked files also count as dirty here, not just modifications to
    tracked ones: this is intentional, fail-closed behavior for a checkout
    that must otherwise be pristine, not an oversight to relax later.

    Args:
        repo: Git repository root. Resolved internally, so a symlinked working
            tree does not need to match ``rewritten``'s own resolution.
        rewritten: List of absolute paths that are expected to be dirty.
        runner: Command executor.

    Raises:
        TaskError: Actual dirty files don't match expected set exactly.
    """
    resolved_repo = repo.resolve()

    # Use -z (NUL-separated, unquoted paths) so renames and quoted/non-ASCII
    # paths parse correctly. A normal entry is one "XY PATH" field; a
    # rename/copy entry (X or Y is 'R'/'C') is followed by a SEPARATE field
    # holding the original path with no status prefix, which must be consumed
    # rather than parsed as a new record.
    status_output = run_git(runner, repo, "status", "--porcelain=v1", "-z")
    fields = status_output.split("\0")
    dirty_files = set()
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        code, path = record[:2], record[3:]
        dirty_files.add(path)
        if "R" in code or "C" in code:
            index += 1  # consume the rename/copy entry's original-path field

    # Convert expected rewritten paths to repo-relative strings
    expected_dirty = {str(p.resolve().relative_to(resolved_repo)) for p in rewritten}

    # Check if they match exactly
    if dirty_files != expected_dirty:
        raise TaskError(f"Git status unexpected: expected {expected_dirty}, got {dirty_files}")
