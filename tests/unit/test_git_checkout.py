"""Unit tests for lightwell_python.git_checkout."""

import pytest

from lightwell_python.git_checkout import retag, verify_clean
from lightwell_python.runtime import Runner, TaskError


def test_retag_replaces_existing_tag_at_head(git_repo):
    repo, git = git_repo
    # git_repo's fixture already tags HEAD as "demo/1.0" (the ingestion tag).
    retag(repo, "1.0+rhlw.1", Runner())
    assert git("tag", "--points-at", "HEAD") == "v1.0+rhlw.1"


def test_retag_works_when_head_has_no_tag(git_repo):
    repo, git = git_repo
    git("commit", "--allow-empty", "-qm", "untagged")
    retag(repo, "1.0+rhlw.2", Runner())
    assert git("tag", "--points-at", "HEAD") == "v1.0+rhlw.2"


def test_retag_untags_multiple_tags_at_head(git_repo):
    repo, git = git_repo
    git("tag", "extra-tag")  # now HEAD has both "demo/1.0" and "extra-tag"
    retag(repo, "1.0+rhlw.1", Runner())
    assert git("tag", "--points-at", "HEAD") == "v1.0+rhlw.1"


def test_retag_returns_head_sha(git_repo):
    repo, git = git_repo
    expected = git("rev-parse", "HEAD")
    assert retag(repo, "1.0+rhlw.1", Runner()) == expected


def test_retag_does_not_move_head(git_repo):
    repo, git = git_repo
    before = git("rev-parse", "HEAD")
    retag(repo, "1.0+rhlw.1", Runner())
    assert git("rev-parse", "HEAD") == before


def test_verify_clean_passes_when_only_expected_files_changed(git_repo):
    repo, _ = git_repo
    target = repo / "source/PKG-INFO"
    target.write_text("Name: demo\nVersion: 1.0+rhlw.1\n")
    verify_clean(repo, [target], Runner())  # must not raise


def test_verify_clean_fails_on_unexpected_dirty_file(git_repo):
    repo, _ = git_repo
    (repo / "source/PKG-INFO").write_text("Name: demo\nVersion: 1.0+rhlw.1\n")
    (repo / "source/extra.txt").write_text("surprise")
    with pytest.raises(TaskError, match="unexpected"):
        verify_clean(repo, [repo / "source/PKG-INFO"], Runner())

def test_verify_clean_fails_when_expected_file_not_actually_dirty(git_repo):
    repo, _ = git_repo
    with pytest.raises(TaskError):
        verify_clean(repo, [repo / "source/PKG-INFO"], Runner())


def test_verify_clean_requires_fully_clean_tree_when_nothing_expected(git_repo):
    repo, _ = git_repo
    (repo / "source/extra.txt").write_text("surprise")
    with pytest.raises(TaskError, match="unexpected"):
        verify_clean(repo, [], Runner())


def test_verify_clean_passes_on_fully_clean_tree_when_nothing_expected(git_repo):
    repo, _ = git_repo
    verify_clean(repo, [], Runner())  # must not raise
