import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lightwell_python.registry import Registry, pinned, repository, select_auth
from lightwell_python.runtime import (
    Runner,
    TaskError,
    contained_file,
    read_json,
    sha256,
    write_json,
    write_results,
)

from .conftest import FakeRunner


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("localhost:5000/org/repo:tag", "localhost:5000/org/repo"),
        ("quay.io/org/repo:tag@sha256:abc", "quay.io/org/repo"),
        ("quay.io/org/repo", "quay.io/org/repo"),
    ],
)
def test_repository_handles_ports(reference, expected):
    assert repository(reference) == expected


@pytest.mark.parametrize("value", ["image", "-bad/repo", "reg/repo with spaces"])
def test_invalid_repository(value):
    with pytest.raises(TaskError):
        repository(value)


def test_digest_pinning_and_auth_selection():
    with pytest.raises(TaskError):
        pinned("registry/repo:tag")
    ref = "localhost:5000/org/repo:tag@sha256:" + "a" * 64
    assert pinned(ref) == "localhost:5000/org/repo@sha256:" + "a" * 64
    config = {
        "auths": {"localhost:5000": {"auth": "broad"}, "localhost:5000/org": {"auth": "scoped"}}
    }
    assert select_auth(ref, config) == {"auths": {"localhost:5000": {"auth": "scoped"}}}
    assert select_auth("other/repo", config) == {"auths": {}}


def test_registry_credentials_cleanup_and_push(tmp_path):
    config = tmp_path / "config.json"
    write_json(config, {"auths": {"localhost:5000/org": {"auth": "secret"}}})
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "wheel.whl").write_bytes(b"wheel")

    def respond(args, kwargs):
        auth = Path(args[args.index("--registry-config") + 1])
        assert auth.stat().st_mode & 0o777 == 0o600
        assert read_json(auth)["auths"]["localhost:5000"]["auth"] == "secret"
        if args[1] == "push":
            Path(args[args.index("--export-manifest") + 1]).write_bytes(b"manifest")

    runner = FakeRunner(respond)
    registry = Registry(runner, config)
    reference = registry.push(
        "localhost:5000/org/repo:tag",
        artifact,
        "application/vnd.python.wheels",
        {"key": "value"},
        ["extra"],
    )
    import hashlib

    assert reference == "localhost:5000/org/repo@sha256:" + hashlib.sha256(b"manifest").hexdigest()
    assert [args[1] for args, _ in runner.calls] == ["push", "tag"]
    for args, _ in runner.calls:
        assert not Path(args[args.index("--registry-config") + 1]).exists()


def test_attach_sbom_digest(tmp_path):
    payload = tmp_path / "sbom.spdx.json"
    payload.write_text("{}")
    runner = FakeRunner()
    auth = tmp_path / "auth.json"
    write_json(auth, {"auths": {}})
    registry = Registry(runner, auth)
    ref = "registry.test/repo@sha256:" + "a" * 64
    result = registry.attach_sbom(ref, payload)
    assert result == "registry.test/repo@sha256:" + sha256(payload)
    assert not Path(runner.calls[0][1]["env"]["DOCKER_CONFIG"]).exists()


def test_runner_never_evaluates_shell_syntax(tmp_path):
    value = "$(touch should-not-exist); 'quoted'"
    output = Runner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", value], capture=True, cwd=tmp_path
    )
    assert output.strip() == value
    assert not (tmp_path / "should-not-exist").exists()


def test_runner_retry_and_no_sensitive_error(monkeypatch, caplog):
    caplog.set_level("INFO")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if len(calls) < 2 else 0, "ok")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("lightwell_python.runtime.time.sleep", lambda _: None)
    assert Runner().run(["tool", "secret"], capture=True, attempts=2) == "ok"
    assert len(calls) == 2
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 7))
    with pytest.raises(TaskError, match="status 7") as error:
        Runner().run(["tool", "top-secret"])
    assert "top-secret" not in str(error.value)
    assert "Running tool (attempt 2/2)" in caplog.text
    assert "retrying in 1s" in caplog.text
    assert "tool completed in" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("exception", [FileNotFoundError(), subprocess.TimeoutExpired("tool", 1)])
def test_runner_execution_errors(monkeypatch, exception):
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(TaskError, match="Unable to execute"):
        Runner().run(["tool"])


def test_results_and_path_validation(tmp_path):
    write_results(tmp_path / "results", {"IMAGE_REF": "value", "B_VALUE": 2})
    assert (tmp_path / "results/B_VALUE").read_bytes() == b"2"
    with pytest.raises(TaskError):
        write_results(tmp_path, {"../bad": "x"})
    with pytest.raises(TaskError):
        contained_file(tmp_path, "../outside")
    with pytest.raises(TaskError):
        contained_file(tmp_path, "missing")


@pytest.mark.skipif(
    shutil.which("oras") is None, reason="ORAS is optional in unit-test environments"
)
def test_real_oras_final_image_manifest(tmp_path):
    class LayoutRunner(Runner):
        def run(self, args, **kwargs):
            return super().run([*args, "--oci-layout"], **kwargs)

    auth = tmp_path / "auth.json"
    write_json(auth, {"auths": {}})
    registry = Registry(LayoutRunner(), auth)
    source = tmp_path / "wheels"
    source.mkdir()
    (source / "demo.whl").write_bytes(b"wheel payload")
    layout = tmp_path / "oci-layout"
    reference = registry.push(str(layout) + ":wheels", source, "application/vnd.python.wheels", {})
    digest = read_json(layout / "index.json")["manifests"][0]["digest"]
    assert reference == f"{layout}@{digest}"


def test_registry_discovers_credentials_with_existing_helper(tmp_path):
    executable = tmp_path / "select-oci-auth"
    record = tmp_path / "helper-call.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['AUTH_RECORD']).write_text(json.dumps(sys.argv[1:]))\n"
        "print(Path(os.environ['AUTHFILE']).read_text())\n"
    )
    executable.chmod(0o755)
    original = tmp_path / "existing-auth.json"
    config = {"auths": {"registry.test/team": {"auth": "test-credential"}}}
    write_json(original, config)
    runner = Runner(
        environment=dict(os.environ)
        | {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "AUTHFILE": str(original),
            "AUTH_RECORD": str(record),
        }
    )
    registry = Registry(runner)
    with registry.auth("registry.test/team/image:tag") as auth:
        assert auth.stat().st_mode & 0o777 == 0o600
        assert auth.parent.stat().st_mode & 0o777 == 0o700
        assert read_json(auth) == {"auths": {"registry.test": {"auth": "test-credential"}}}
    assert read_json(record) == ["registry.test/team/image:tag"]
    assert read_json(original) == config
    assert not auth.exists()


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing", "malformed", "invalid-shape"])
def test_registry_discovery_failure_is_private_and_cleans_up(
    tmp_path, monkeypatch, capsys, failure
):
    paths = []

    def helper(command, **kwargs):
        assert command == ["select-oci-auth", "registry.test/team/image:tag"]
        assert kwargs["stderr"] == subprocess.PIPE
        assert kwargs["timeout"] == 17
        output = kwargs["stdout"]
        paths.append(Path(output.name))
        output.write("[]" if failure == "invalid-shape" else "private-token")
        if failure == "exit":
            raise subprocess.CalledProcessError(1, command, stderr=b"private-token")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 17, stderr=b"private-token")
        if failure == "missing":
            raise FileNotFoundError("private-token")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", helper)
    with (
        pytest.raises(TaskError) as error,
        Registry(Runner(timeout=17)).auth("registry.test/team/image:tag"),
    ):
        pytest.fail("Invalid discovery must not reach publication")
    assert "private-token" not in str(error.value)
    assert "private-token" not in repr(capsys.readouterr())
    assert paths and all(not path.parent.exists() for path in paths)


def test_missing_explicit_auth_file_does_not_fall_back_to_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Unexpected discovery"))
    with (
        pytest.raises(TaskError, match="authentication configuration"),
        Registry(Runner(), tmp_path / "missing").auth("registry.test/team/image"),
    ):
        pytest.fail("Missing explicit credentials must fail")


def test_restored_input_copy_is_writable_and_preserves_source(tmp_path):
    from lightwell_python.runtime import copy_input_tree

    source = tmp_path / "restored"
    source.mkdir()
    file = source / "file"
    file.write_text("original")
    file.chmod(0o444)
    (source / "link").symlink_to("file")
    source.chmod(0o555)
    destination = tmp_path / "scratch"
    copy_input_tree(source, destination)
    assert (destination / "file").stat().st_mode & 0o200
    assert (destination / "link").is_symlink()
    (destination / "file").write_text("changed")
    assert file.read_text() == "original"
    assert file.stat().st_mode & 0o777 == 0o444
    assert source.stat().st_mode & 0o777 == 0o555
    with pytest.raises(TaskError, match="outside"):
        copy_input_tree(source, source / "scratch")
    with pytest.raises(TaskError, match="missing"):
        copy_input_tree(tmp_path / "missing", tmp_path / "other")
