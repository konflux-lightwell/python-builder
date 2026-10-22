"""Check runtime probes for builder plugins and registry authentication."""

import importlib.metadata
import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def runtime_check(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "0.81.0")
    path = Path(__file__).resolve().parents[2] / "tests/check_runtime.py"
    return runpy.run_path(str(path))["check_fromager_plugins"]


@pytest.mark.parametrize("names", [("faiss_cpu",), ("faiss_cpu", "debugpy")])
def test_base_plugins_load_without_requiring_newer_overrides(runtime_check, monkeypatch, names):
    plugins = tuple(
        importlib.metadata.EntryPoint(
            name=name, value="pathlib:Path", group="fromager.project_overrides"
        )
        for name in names
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: plugins)
    runtime_check()


def test_missing_required_override_rejects_base(runtime_check, monkeypatch):
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: ())
    with pytest.raises(RuntimeError, match="missing: faiss_cpu"):
        runtime_check()


def test_broken_additional_override_rejects_base(runtime_check, monkeypatch):
    plugins = (
        importlib.metadata.EntryPoint(
            name="faiss_cpu", value="pathlib:Path", group="fromager.project_overrides"
        ),
        importlib.metadata.EntryPoint(
            name="debugpy", value="pathlib:missing_plugin", group="fromager.project_overrides"
        ),
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: plugins)
    with pytest.raises(AttributeError, match="missing_plugin"):
        runtime_check()


@pytest.mark.parametrize("failure", [None, "command", "credential"])
def test_registry_probe_isolates_credentials_and_cleans_up(monkeypatch, tmp_path, failure):
    developer_home = tmp_path / "developer"
    developer_config = developer_home / ".docker/config.json"
    developer_config.parent.mkdir(parents=True)
    developer_config.write_text("developer credentials must remain untouched")
    monkeypatch.setenv("HOME", str(developer_home))
    monkeypatch.setenv("AUTHFILE", str(developer_config))
    monkeypatch.setenv("DOCKER_CONFIG", str(developer_config.parent))
    temporary_homes = []

    def fake_run(command, *, env, **kwargs):
        assert command == ["select-oci-auth", "registry.test/team/image:tag"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 60, "check": True}
        temporary_home = Path(env["HOME"])
        temporary_homes.append(temporary_home)
        assert temporary_home != developer_home
        auth = temporary_home / ".docker/config.json"
        assert env["AUTHFILE"] == str(auth)
        assert env["DOCKER_CONFIG"] == str(auth.parent)
        assert json.loads(auth.read_text()) == {
            "auths": {"registry.test/team": {"auth": "smoke-only"}}
        }
        assert os.environ["HOME"] == str(developer_home)
        if failure == "command":
            raise subprocess.CalledProcessError(
                127, command, stderr="get-reference-base: command not found"
            )
        selected = {} if failure == "credential" else {"registry.test": {"auth": "smoke-only"}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"auths": selected}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    path = Path(__file__).resolve().parents[2] / "tests/check_runtime.py"
    probe = runpy.run_path(str(path))["check_registry_auth"]
    if failure == "command":
        with pytest.raises(RuntimeError, match=r"exit 127.*get-reference-base: command not found"):
            probe()
    elif failure == "credential":
        with pytest.raises(RuntimeError, match="did not select the repository credential"):
            probe()
    else:
        probe()

    assert len(temporary_homes) == 1
    assert not temporary_homes[0].exists()
    assert developer_config.read_text() == "developer credentials must remain untouched"
