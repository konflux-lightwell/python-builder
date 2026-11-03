"""Unit tests for lightwell_python.runtime.run_git, shared by git_checkout and versioning."""

from lightwell_python.runtime import Runner, run_git

from .conftest import FakeRunner


def test_run_git_scopes_command_to_repo(git_repo):
    repo, _ = git_repo
    runner = FakeRunner()
    run_git(runner, repo, "status")
    (args, _kwargs) = runner.calls[0]
    assert args == ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "status"]


def test_run_git_strips_trailing_newline_but_preserves_leading_status_column(git_repo):
    repo, _ = git_repo
    (repo / "source/extra.txt").write_text("untracked")
    output = run_git(Runner(), repo, "status", "--porcelain")
    # Untracked files are reported as "?? path" -- the leading two-char status
    # column and its separating space must survive, only the trailing
    # newline(s) are stripped.
    assert output == "?? source/extra.txt"


def test_run_git_capture_false_returns_empty_string_without_capturing(git_repo):
    repo, _ = git_repo
    runner = FakeRunner()
    result = run_git(runner, repo, "tag", "nomatter", capture=False)
    assert result == ""
    (_args, kwargs) = runner.calls[0]
    assert kwargs.get("capture") is False
