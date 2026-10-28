import os
import tarfile

import pytest

from lightwell_python.archives import create_sdist, find_sdist
from lightwell_python.config import BuildConfig, expand
from lightwell_python.runtime import BuildType, Package, Runner, TaskError, read_json, write_json
from lightwell_python.versioning import compute_version, rewrite_metadata, version_counts


@pytest.mark.parametrize(
    "kind,b,n", [(BuildType.BACKPORT, 2, 0), (BuildType.NOVEL, 1, 1), (BuildType.VALIDATED, 0, 0)]
)
def test_version_lineage(git_repo, kind, b, n):
    repo, git = git_repo
    git("commit", "--allow-empty", "-qm", "fix")
    git("tag", "-a", "baseline/1.0", "-m", "baseline")
    git("commit", "--allow-empty", "-qm", "novel")
    version = compute_version(repo, Package("demo", "1.0"), kind, Runner())
    assert (version.b, version.n) == (b, n)
    assert version_counts(Package("demo", "1.0"), version.full, kind) == (b, n)


def test_missing_baseline_fails(git_repo):
    repo, _ = git_repo
    with pytest.raises(TaskError):
        compute_version(repo, Package("demo", "1.0"), BuildType.NOVEL, Runner())


def test_unrelated_lineage_rejected(git_repo):
    repo, git = git_repo
    git("checkout", "--orphan", "unrelated")
    git("commit", "-qm", "other root")
    with pytest.raises(TaskError):
        compute_version(repo, Package("demo", "1.0"), BuildType.BACKPORT, Runner())


@pytest.mark.parametrize(
    "kind,computed",
    [
        (BuildType.BACKPORT, "1.0+rhlw.1.n.2"),
        (BuildType.NOVEL, "1.0+rhlw.1garbage"),
        (BuildType.VALIDATED, "1.0+rhlw.0"),
        (BuildType.SCRATCH, "1.0+rhlw.1.n.2"),
    ],
)
def test_invalid_computed_versions(kind, computed):
    with pytest.raises(TaskError):
        version_counts(Package("demo", "1.0"), computed, kind)


def test_scratch_version_skips_lineage(tmp_path):
    # Scratch never queries Git, so a fixed +rhlw.0 version is produced even without
    # the ingestion/baseline lineage tags that Backport and Novel require.
    version = compute_version(tmp_path, Package("demo", "1.0"), BuildType.SCRATCH, Runner())
    assert (version.full, version.b, version.n, version.commit) == ("1.0+rhlw.0", 0, 0, "")
    assert version_counts(Package("demo", "1.0"), version.full, BuildType.SCRATCH) == (0, 0)


def test_rewrite_authoritative_sections(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.0"\n[tool.unrelated]\nversion = "1.0"\n'
    )
    (tmp_path / "setup.cfg").write_text("[metadata]\nversion = 1.0\n[other]\nversion = 1.0\n")
    (tmp_path / "a.py").write_text('__version__ = "1.0"\ndependency_version = "1.0"\n')
    (tmp_path / "PKG-INFO").write_text("Name: demo\nVersion: 1.0\n\nVersion: 1.0\n")
    changes = rewrite_metadata(tmp_path, Package("demo", "1.0"), "1.0+rhlw.1", [])
    assert len(changes) == 4
    assert '[tool.unrelated]\nversion = "1.0"' in (tmp_path / "pyproject.toml").read_text()
    assert "[other]\nversion = 1.0" in (tmp_path / "setup.cfg").read_text()
    assert 'dependency_version = "1.0"' in (tmp_path / "a.py").read_text()
    assert (tmp_path / "PKG-INFO").read_text().endswith("\n\nVersion: 1.0\n")


def test_explicit_rewrite_is_validated_before_writes(tmp_path):
    target = tmp_path / "version.txt"
    target.write_text("1.0")
    rules = [
        {"path": "version.txt", "pattern": "1.0", "replacement": "{COMPUTED_VERSION}"},
        {"path": "missing.txt", "pattern": "1.0", "replacement": "{COMPUTED_VERSION}"},
    ]
    with pytest.raises(TaskError):
        rewrite_metadata(tmp_path, Package("demo", "1.0"), "1.0+rhlw.1", rules)
    assert target.read_text() == "1.0"
    assert rewrite_metadata(tmp_path, Package("demo", "1.0"), "1.0+rhlw.1", rules[:1]) == [target]


def test_rewrite_symlink_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "outside.py").write_text('__version__ = "1.0"')
    (source / "link.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(TaskError, match="escapes"):
        rewrite_metadata(source, Package("demo", "1.0"), "1.0+rhlw.1", [])


def test_archive_reproducible_and_preserves_executable_and_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    script = source / "build"
    script.write_text("executable content")
    script.chmod(0o755)
    (source / "PKG-INFO").write_text("Name: Demo\nVersion: 1.0\n")
    (source / "link").symlink_to("build")
    package = Package("demo", "1.0")
    first = create_sdist(source, tmp_path / "one", package, "1.0+rhlw.1")
    os.utime(script, (500, 500))
    second = create_sdist(source, tmp_path / "two", package, "1.0+rhlw.1")
    assert first.read_bytes() == second.read_bytes()
    assert (first.parent / "Demo-1.0+rhlw.1.tar.gz").read_bytes() == first.read_bytes()
    with tarfile.open(first) as archive:
        assert archive.getmember("demo-1.0/build").mode == 0o755
        assert archive.getmember("demo-1.0/link").issym()
        assert all(member.mtime == member.uid == member.gid == 0 for member in archive)


def test_archive_rejects_external_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "outside").symlink_to("/etc/passwd")
    with pytest.raises(TaskError, match="escapes"):
        create_sdist(source, tmp_path / "out", Package("demo", "1.0"), "1.0")


def test_sdist_lookup_requires_exact_version(tmp_path):
    (tmp_path / "demo-1.00.tar.gz").write_bytes(b"equivalent")
    assert find_sdist(tmp_path, Package("demo", "1.0")).read_bytes() == b"equivalent"
    with pytest.raises(TaskError):
        find_sdist(tmp_path, Package("demo", "1.1"))


def test_config_resolution_and_literal_environment(tmp_path, package_config):
    package_config["version_configs"] = {
        ">=1.0": {
            "env": {"VERSION_OVERRIDE": "{COMPUTED_VERSION}", "LITERAL": "$(touch /tmp/nope)"},
            "allowed_artifacts": ["demo", "companion"],
        }
    }
    path = tmp_path / "demo.json"
    write_json(path, package_config)
    config = BuildConfig.load(path, Package("Demo", "1.0"))
    env = config.environment(Package("demo", "1.0"), "1.0+rhlw.1")
    assert env == {"VERSION_OVERRIDE": "1.0+rhlw.1", "LITERAL": "$(touch /tmp/nope)"}
    assert config.allowed == {"demo", "companion"}
    config.render_settings(tmp_path / "settings/demo.yaml", Package("demo", "1.0"), "1.0+rhlw.1")
    assert config.settings == {}
    assert "pkg:pypi/demo@1.0" in (tmp_path / "settings/demo.yaml").read_text()
    with pytest.raises(TaskError, match="one version rule"):
        BuildConfig.load(path, Package("demo", "0.9"))


@pytest.mark.parametrize("value", ["{OTHER}", "{VERSION", "{}", "{VERSION.__class__}"])
def test_invalid_tokens(value):
    with pytest.raises(TaskError):
        expand(value, Package("demo", "1.0"), "1.0+rhlw.1")


def test_duplicate_json_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"env": {}, "env": {}}')
    with pytest.raises(TaskError, match="Duplicate"):
        read_json(path)
