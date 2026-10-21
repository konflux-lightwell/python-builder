import pytest

from lightwell_python import cli
from lightwell_python.runtime import TaskError


@pytest.mark.parametrize(
    "task,module,extra",
    [
        (
            "source-to-sdist",
            "source",
            [
                "--source-dir",
                "source",
                "--build-type",
                "Backport",
                "--sdists-output",
                "staged-sdists",
                "--build-configs-repo-url",
                "https://example/config",
                "--build-configs-revision",
                "main",
            ],
        ),
        (
            "build-wheels",
            "wheels",
            [
                "--sdists-dir",
                "sdists",
                "--image",
                "registry/repo",
                "--build-type",
                "Backport",
                "--computed-version",
                "1.0+rhlw.1",
                "--manifest-output",
                "staged-manifest.json",
            ],
        ),
        (
            "build-sdist",
            "sdist",
            [
                "--sdists-dir",
                "sdists",
                "--image",
                "registry/repo",
                "--git-url",
                "https://example/source",
                "--revision",
                "abc",
            ],
        ),
        (
            "attach-build-index",
            "index",
            [
                "--source-dir",
                "source",
                "--image-ref",
                "registry/repo@sha256:" + "a" * 64,
                "--build-type",
                "Validated",
                "--computed-version",
                "1.0",
            ],
        ),
    ],
)
def test_cli_dispatches_all_task_inputs(tmp_path, monkeypatch, task, module, extra):
    calls = []
    monkeypatch.setattr(getattr(cli, module), "run", lambda **kwargs: calls.append(kwargs))
    ca = tmp_path / "ca.pem"
    ca.write_text("certificate")
    args = [
        "--package",
        "Demo",
        "--version",
        "1.0",
        "--workdir",
        str(tmp_path / "work"),
        "--results-dir",
        str(tmp_path / "results"),
        "--ca-bundle",
        str(ca),
        *extra,
    ]
    assert cli.execute(task, args) == 0
    assert calls[0]["package"].name == "demo"
    assert calls[0]["runner"].environment["GIT_SSL_CAINFO"] == str(ca)
    assert calls[0]["workdir"].is_dir()
    assert calls[0]["results"] == tmp_path / "results"
    assert cli.execute(task, args) == 0
    assert calls[0]["workdir"] != calls[1]["workdir"]


def test_environment_defaults_and_failures(tmp_path, monkeypatch):
    for key, value in {
        "PACKAGE": "demo",
        "VERSION": "1.0",
        "SDISTS_DIR": "input",
        "IMAGE": "registry/repo",
        "GIT_URL": "url",
        "REVISION": "abc",
        "WORKDIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)

    def fail(**kwargs):
        raise TaskError("Missing sdist")

    monkeypatch.setattr(cli.sdist, "run", fail)
    assert cli.execute("build-sdist", []) == 1
    assert cli.execute("build-sdist", ["--command-timeout", "0"]) == 1
    with pytest.raises(SystemExit) as error:
        cli.parser("build-wheels").parse_args(["--build-type", "unsupported"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "entry,task",
    [
        ("source_to_sdist", "source-to-sdist"),
        ("build_wheels", "build-wheels"),
        ("build_sdist", "build-sdist"),
        ("attach_build_index", "attach-build-index"),
    ],
)
def test_installed_entrypoints_return_task_status(monkeypatch, entry, task):
    calls = []
    monkeypatch.setattr(cli, "execute", lambda value: calls.append(value) or 1)
    with pytest.raises(SystemExit) as error:
        getattr(cli, entry)()
    assert error.value.code == 1
    assert calls == [task]


def test_unknown_task():
    with pytest.raises(ValueError, match="Unknown task"):
        cli.parser("unknown")
