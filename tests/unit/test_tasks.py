import json
import subprocess

import pytest

from lightwell_python.runtime import (
    BuildType,
    Package,
    Runner,
    TaskError,
    read_json,
    sha256,
    write_json,
)
from lightwell_python.tasks import sdist, source, wheels

from .conftest import FakeRegistry, FakeRunner, wheel_builder


@pytest.fixture
def config_repo(tmp_path, package_config):
    import subprocess

    path = tmp_path / "configs"
    write_json(path / "packages/demo.json", package_config)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.test"],
        ["config", "user.name", "Test"],
        ["config", "commit.gpgsign", "false"],
        ["add", "."],
        ["commit", "-qm", "config"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return path


@pytest.mark.parametrize(
    "build_type,computed,b",
    [(BuildType.BACKPORT, "1.0+rhlw.1", 1), (BuildType.VALIDATED, "1.0", 0)],
)
def test_source_end_to_end(tmp_path, git_repo, config_repo, caplog, build_type, computed, b):
    caplog.set_level("INFO")
    repo, git = git_repo
    git("commit", "--allow-empty", "-qm", "fix")
    work = tmp_path / "task"
    work.mkdir()
    results = tmp_path / "results"
    source.run(
        workdir=work,
        results=results,
        package=Package("demo", "1.0"),
        build_type=build_type,
        source_dir=repo,
        sdists_output=tmp_path / "sdists-output",
        config_url=str(config_repo),
        config_revision="HEAD",
        runner=Runner(),
    )
    assert (results / "COMPUTED_VERSION").read_text() == computed
    assert (results / "B_VALUE").read_text() == str(b)
    assert (repo / "source/PKG-INFO").read_text().endswith("Version: 1.0\n")
    assert read_json(tmp_path / "sdists-output/backend-env.json") == {}
    assert (tmp_path / "sdists-output/build-configs/.commit").read_text().strip()
    assert (tmp_path / f"sdists-output/downloads/demo-{computed}.tar.gz").is_file()
    assert not (results / "SDISTS_ARTIFACT").exists()
    assert f"Preparing source for demo==1.0; build type: {build_type.value}" in caplog.text
    assert f"Computed version: {computed} (B={b}, N=0)" in caplog.text
    assert "resolved commit:" in caplog.text
    assert "Created sdist: demo-1.0.tar.gz" in caplog.text
    assert "Completed: Finalize sdist handoff and task results" in caplog.text
    assert f"Sdist handoff ready: {tmp_path / 'sdists-output'}" in caplog.text


def test_source_git_checkout_path(tmp_path, git_repo, config_repo, caplog):
    caplog.set_level("INFO")
    repo, git = git_repo
    git("commit", "--allow-empty", "-qm", "fix")
    # Reuse config_repo's demo.json but add canonical_source: git.
    config_path = config_repo / "packages/demo.json"
    cfg = json.loads(config_path.read_text())
    cfg["canonical_source"] = {
        "type": "git",
        "upstream_url": "https://example.test/demo",
        "ref": "1.0",
    }
    config_path.write_text(json.dumps(cfg))
    subprocess.run(
        ["git", "-C", str(config_repo), "commit", "-aqm", "add canonical_source"], check=True
    )

    work = tmp_path / "task"
    work.mkdir()
    results = tmp_path / "results"
    source.run(
        workdir=work,
        results=results,
        package=Package("demo", "1.0"),
        build_type=BuildType.BACKPORT,
        source_dir=repo,
        sdists_output=tmp_path / "sdists-output",
        config_url=str(config_repo),
        config_revision="HEAD",
        runner=Runner(),
    )
    output = tmp_path / "sdists-output"
    assert not (output / "downloads").exists()
    checkout = output / "checkout/demo-1.0+rhlw.1/demo-1.0+rhlw.1"
    assert checkout.is_dir()
    assert (checkout / "PKG-INFO").read_text().endswith("Version: 1.0+rhlw.1\n")
    assert (results / "COMPUTED_VERSION").read_text() == "1.0+rhlw.1"


def test_config_clone_credentials_not_in_arguments(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "username").write_text("user")
    (secret / "password").write_text("top-secret")
    runner = FakeRunner()
    source.clone_config(
        runner, "https://example.test/configs", "main", tmp_path / "checkout", secret
    )
    assert "top-secret" not in str([args for args, _ in runner.calls])
    assert (
        runner.calls[0][1]["env"]["GIT_CONFIG_KEY_0"]
        == "http.https://example.test/configs.extraHeader"
    )
    with pytest.raises(TaskError, match="HTTPS"):
        source.clone_config(
            runner, "http://example.test/configs", "main", tmp_path / "checkout", secret
        )


def test_normalize_preserves_evidence_and_binds_digests(tmp_path, sdist_input, caplog):
    caplog.set_level("INFO")
    work = tmp_path / "task"
    work.mkdir()

    def normalize(args, kwargs):
        if args[2] == "build-sdist":
            built = kwargs["cwd"] / "sdists-repo/builds/demo-1.0.tar.gz"
            built.parent.mkdir()
            built.write_bytes(b"normalized")

    registry = FakeRegistry()
    sdist.run(
        workdir=work,
        results=tmp_path / "results",
        package=Package("Demo", "1.0"),
        sdists_dir=sdist_input,
        image="registry.test/demo:tag",
        git_url="https://git.test/demo",
        revision="abc",
        registry=registry,
        runner=FakeRunner(normalize),
    )
    artifact = work / "artifact"
    assert (artifact / "evidence.bin").read_bytes() == b"opaque evidence\x00"
    assert (artifact / "downloads/demo-1.0.tar.gz").read_bytes() == b"acquired"
    transformation = read_json(artifact / "sdist-transformation.json")
    assert transformation["input"]["sha256"] == sha256(sdist_input / "downloads/demo-1.0.tar.gz")
    assert transformation["output"]["sha256"] == sha256(artifact / "demo-1.0.tar.gz")
    assert transformation["source_origin"]["sha256"] == sha256(sdist_input / "source-origin.json")
    assert (tmp_path / "results/CHAINS-GIT_COMMIT").read_text() == "abc"
    assert "Completed: Fromager prepare-source" in caplog.text
    assert "Completed: Fromager build-sdist" in caplog.text
    assert "Normalized sdist: demo-1.0.tar.gz; SHA-256:" in caplog.text
    assert "Normalized sdist task results ready:" in caplog.text


def test_normalize_rejects_stale_output(tmp_path, sdist_input):
    (sdist_input / "builds").mkdir()
    (sdist_input / "builds/demo-1.0.tar.gz").write_bytes(b"stale")
    work = tmp_path / "task"
    work.mkdir()
    registry = FakeRegistry()
    with pytest.raises(TaskError, match="exactly one"):
        sdist.run(
            workdir=work,
            results=tmp_path / "results",
            package=Package("demo", "1.0"),
            sdists_dir=sdist_input,
            image="registry.test/demo:tag",
            git_url="url",
            revision="abc",
            registry=registry,
            runner=FakeRunner(),
        )
    assert not any(call[0] == "push" for call in registry.calls)
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "manifest-output.json").exists()


def test_wheels_complete_task(tmp_path, wheel_input, make_wheel, caplog):
    caplog.set_level("INFO")
    work = tmp_path / "task"
    work.mkdir()
    registry = FakeRegistry()
    runner = wheel_builder(make_wheel)
    wheels.run(
        workdir=work,
        results=tmp_path / "results",
        package=Package("demo", "1.0"),
        computed="1.0+rhlw.1",
        build_type=BuildType.BACKPORT,
        sdists_dir=wheel_input,
        image="localhost:5000/org/repo:latest",
        manifest_output=tmp_path / "manifest-output.json",
        registry=registry,
        runner=runner,
        image_expires="1d",
        wheel_server="https://user:cache-secret@packages.redhat.com/simple",
    )
    assert [call[0] for call in registry.calls] == ["push", "sbom"]
    assert registry.calls[0][5] == ["demo-1.0-rhlw.1", "demo-1.0-rhlw.1-abcdef1"]
    assert (tmp_path / "results/IMAGE_REF").read_text() == registry.reference
    assert (work / "artifact/demo-1.0+rhlw.1.tar.gz").is_file()
    assert not (tmp_path / "results/MANIFEST_ARTIFACT").exists()
    assert read_json(tmp_path / "manifest-output.json") == read_json(work / "build-manifest.json")
    assert not (wheel_input / "builds").exists()
    assert read_json(work / "build-manifest.json")["produced"] == [
        {"name": "demo", "version": "1.0"}
    ]
    assert all(
        args[0] not in {"bash", "build-wheels", "collect-build-files"} for args, _ in runner.calls
    )
    assert "wheel cache: enabled" in caplog.text
    assert "cache-secret" not in caplog.text
    assert "Processing wheel 1/1: demo-1.0+rhlw.1-py3-none-any.whl" in caplog.text
    assert "Verified 1 primary wheels; manifest: 1 produced, 0 cached packages" in caplog.text
    assert "Completed: Attach primary SPDX SBOM" in caplog.text
    assert "Build manifest ready:" in caplog.text


@pytest.mark.parametrize(
    "build_type,expected",
    [
        (BuildType.BACKPORT, "remediated"),
        (BuildType.NOVEL, "novel"),
    ],
)
def test_wheels_distribution_target_annotation(
    tmp_path, wheel_input, make_wheel, build_type, expected
):
    """Backport publishes under remediated; Novel gets its own, not-yet-allowed target.

    registry-lightwell-python-remediated-stage's lightwell_allowed_distribution_targets
    policy only allows "remediated" (or "validated") today. Novel intentionally emits
    "novel" instead of collapsing into "remediated", so it fails closed at Enterprise
    Contract release validation until the policy is updated for the novel-fix workflow.
    """
    work = tmp_path / "task"
    work.mkdir()
    registry = FakeRegistry()
    wheels.run(
        workdir=work,
        results=tmp_path / "results",
        package=Package("demo", "1.0"),
        computed="1.0+rhlw.1",
        build_type=build_type,
        sdists_dir=wheel_input,
        image="localhost:5000/org/repo:latest",
        manifest_output=tmp_path / "manifest-output.json",
        registry=registry,
        runner=wheel_builder(make_wheel),
    )
    (_, _, _, _, annotations, _) = next(call for call in registry.calls if call[0] == "push")
    assert annotations["dev.lightwell.distribution-target"] == expected


@pytest.mark.parametrize(
    "build_type,expected",
    [
        (BuildType.BACKPORT, "remediated"),
        (BuildType.NOVEL, "novel"),
        (BuildType.VALIDATED, "validated"),
    ],
)
def test_build_type_distribution_target(build_type, expected):
    """BuildType.distribution_target maps Backport->remediated, Novel->novel (separate)."""
    assert build_type.distribution_target == expected


def test_wheels_do_not_publish_unauthorized_build(tmp_path, wheel_input, make_wheel):
    work = tmp_path / "task"
    work.mkdir()
    registry = FakeRegistry()
    with pytest.raises(TaskError, match="Unauthorized"):
        wheels.run(
            workdir=work,
            results=tmp_path / "results",
            package=Package("demo", "1.0"),
            computed="1.0+rhlw.1",
            build_type=BuildType.BACKPORT,
            sdists_dir=wheel_input,
            image="registry.test/repo:latest",
            manifest_output=tmp_path / "manifest-output.json",
            registry=registry,
            runner=wheel_builder(make_wheel, unauthorized=True),
        )
    assert not any(call[0] == "push" for call in registry.calls)
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "manifest-output.json").exists()


def test_source_failure_does_not_expose_handoff(tmp_path, git_repo, caplog):
    caplog.set_level("INFO")
    repo, _ = git_repo
    work = tmp_path / "scratch"
    work.mkdir()

    def fail(args, kwargs):
        raise TaskError("Config clone failed")

    with pytest.raises(TaskError, match="Config clone"):
        source.run(
            workdir=work,
            results=tmp_path / "results",
            package=Package("demo", "1.0"),
            build_type=BuildType.VALIDATED,
            source_dir=repo,
            sdists_output=tmp_path / "handoff",
            config_url="https://git.test/config",
            config_revision="main",
            runner=FakeRunner(fail),
        )
    assert not (tmp_path / "handoff").exists()
    assert not (tmp_path / "results").exists()
    assert "Failed: Clone and validate build configuration" in caplog.text
    assert "Completed: Clone and validate build configuration" not in caplog.text
    assert "Sdist handoff ready:" not in caplog.text


def test_failed_sbom_attachment_does_not_stage_manifest(tmp_path, wheel_input, make_wheel, caplog):
    caplog.set_level("INFO")

    class FailingRegistry(FakeRegistry):
        def attach_sbom(self, reference, sbom):
            raise TaskError("SBOM upload failed")

    work = tmp_path / "scratch"
    work.mkdir()
    with pytest.raises(TaskError, match="SBOM upload"):
        wheels.run(
            workdir=work,
            results=tmp_path / "results",
            package=Package("demo", "1.0"),
            computed="1.0+rhlw.1",
            build_type=BuildType.BACKPORT,
            sdists_dir=wheel_input,
            manifest_output=tmp_path / "manifest.json",
            image="registry.test/demo:latest",
            registry=FailingRegistry(),
            runner=wheel_builder(make_wheel),
        )
    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "results").exists()
    assert "Completed: Publish wheels OCI artifact" in caplog.text
    assert "Failed: Attach primary SPDX SBOM" in caplog.text
    assert "Completed: Attach primary SPDX SBOM" not in caplog.text
    assert "Build manifest ready:" not in caplog.text


def test_canonical_source_defaults_to_pypi_sdist(tmp_path, package_config):
    from lightwell_python.config import BuildConfig
    path = tmp_path / "demo.json"
    write_json(path, package_config)
    config = BuildConfig.load(path, Package("demo", "1.0"))
    assert config.canonical_source == {"type": "pypi-sdist"}


def test_canonical_source_package_level_default(tmp_path, package_config):
    from lightwell_python.config import BuildConfig
    package_config["canonical_source"] = {
        "type": "git",
        "upstream_url": "https://example.test/demo",
        "ref": "1.0",
    }
    path = tmp_path / "demo.json"
    write_json(path, package_config)
    config = BuildConfig.load(path, Package("demo", "1.0"))
    assert config.canonical_source == {
        "type": "git",
        "upstream_url": "https://example.test/demo",
        "ref": "1.0",
    }


def test_canonical_source_version_override_wins(tmp_path, package_config):
    from lightwell_python.config import BuildConfig
    package_config["canonical_source"] = {"type": "pypi-sdist"}
    package_config["version_configs"] = {
        "1.0": {
            "canonical_source": {
                "type": "git",
                "upstream_url": "https://example.test/demo",
                "ref": "v1.0",
            }
        }
    }
    path = tmp_path / "demo.json"
    write_json(path, package_config)
    config = BuildConfig.load(path, Package("demo", "1.0"))
    assert config.canonical_source["type"] == "git"
    assert config.canonical_source["ref"] == "v1.0"


def test_canonical_source_version_match_without_override_falls_back_to_package_default(
    tmp_path, package_config
):
    from lightwell_python.config import BuildConfig
    package_config["canonical_source"] = {"type": "pypi-sdist"}
    package_config["version_configs"] = {"1.0": {"fromager_settings": {}}}
    path = tmp_path / "demo.json"
    write_json(path, package_config)
    config = BuildConfig.load(path, Package("demo", "1.0"))
    assert config.canonical_source == {"type": "pypi-sdist"}
