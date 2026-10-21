"""Check compatibility with plugins supplied by the plumbing base image."""

import importlib.metadata
import runpy
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
