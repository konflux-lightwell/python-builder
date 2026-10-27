from pathlib import Path

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
    ],
)
def test_cli_dispatches_all_task_inputs(tmp_path, monkeypatch, task, module, extra, caplog):
    caplog.set_level("INFO")
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
    assert f"Starting: Task {task} for demo==1.0" in caplog.text
    assert f"Completed: Task {task} for demo==1.0 in" in caplog.text


def test_environment_defaults_and_failures(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
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
    assert "Failed: Task build-sdist for demo==1.0" in caplog.text
    assert "Completed: Task build-sdist" not in caplog.text
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


@pytest.mark.parametrize(
    "task,module,environment,override,expected",
    [
        (
            "source-to-sdist",
            "source",
            {
                "BUILD_TYPE": "Validated",
                "SOURCE_DIR": "source",
                "SDISTS_OUTPUT": "handoff",
                "BUILD_CONFIGS_REPO_URL": "https://example/configs",
                "BUILD_CONFIGS_REVISION": "main",
            },
            "--build-configs-revision=pinned-commit",
            {"config_url": "https://example/configs", "config_revision": "pinned-commit"},
        ),
        (
            "build-wheels",
            "wheels",
            {
                "BUILD_TYPE": "Backport",
                "SDISTS_DIR": "sdists",
                "IMAGE": "registry/repo",
                "COMPUTED_VERSION": "1.0+rhlw.1",
                "MANIFEST_OUTPUT": "manifest.json",
                "WHEEL_SERVER_URL": "https://example/wheels",
                "JOBS": "4",
            },
            "--wheel-server-url=",
            {"computed": "1.0+rhlw.1", "wheel_server": "", "jobs": 4},
        ),
        (
            "build-sdist",
            "sdist",
            {
                "SDISTS_DIR": "sdists",
                "IMAGE": "registry/repo",
                "IMAGE_EXPIRES_AFTER": "1d",
                "GIT_URL": "https://example/source",
                "REVISION": "abc",
            },
            "--image-expires-after=2d",
            {"image_expires": "2d", "git_url": "https://example/source", "revision": "abc"},
        ),
    ],
)
def test_environment_names_survive_argument_destinations(
    tmp_path, monkeypatch, task, module, environment, override, expected
):
    for key, value in {
        "PACKAGE": "demo",
        "VERSION": "1.0",
        "WORKDIR": str(tmp_path / "work"),
        "RESULTS_DIR": str(tmp_path / "results"),
        "COMMAND_TIMEOUT": "45",
        **environment,
    }.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr(getattr(cli, module), "run", lambda **kwargs: calls.append(kwargs))

    assert cli.execute(task, [override]) == 0
    assert len(calls) == 1
    assert calls[0].items() >= expected.items()
    assert calls[0]["runner"].timeout == 45
    assert calls[0]["results"] == tmp_path / "results"


def test_execute_invokes_the_argparse_target(tmp_path, monkeypatch):
    selected = cli.sdist_parser()
    calls = []
    selected.set_defaults(func=lambda args, context: calls.append((args, context)))
    monkeypatch.setattr(cli, "parser", lambda _: selected)

    assert (
        cli.execute(
            "build-sdist",
            [
                "--package=demo",
                "--version=1.0",
                "--sdists-dir=input",
                "--image=registry/repo",
                "--git-url=https://example/source",
                "--revision=abc",
                f"--workdir={tmp_path}",
            ],
        )
        == 0
    )
    assert len(calls) == 1
    args, context = calls[0]
    assert args.sdists_dir == Path("input")
    assert context["package"].name == "demo"
    assert context["workdir"].parent == tmp_path


WHEEL_REQUIRED_ARGS = [
    "--package",
    "demo",
    "--version",
    "1.0",
    "--sdists-dir",
    "sdists",
    "--image",
    "registry/repo",
    "--build-type",
    "Backport",
    "--computed-version",
    "1.0+rhlw.1",
    "--manifest-output",
    "manifest.json",
]


@pytest.fixture
def wheel_calls(tmp_path, monkeypatch):
    """Replace the wheel task with a recorder and clear AI inputs from the environment.

    Args:
        tmp_path: Per-test temporary directory used for workdir and results.
        monkeypatch: Pytest fixture that replaces the task and controls the environment.

    Returns:
        List that receives the keyword arguments of each wheels.run call.
    """
    calls = []
    monkeypatch.setattr(cli.wheels, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setenv("WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.delenv("AI_ASSISTED", raising=False)
    monkeypatch.delenv("AI_MODELS", raising=False)
    return calls


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("TRUE", True), (" true ", True), ("false", False), ("", False)],
)
def test_ai_assisted_accepts_strict_boolean_spellings(wheel_calls, value, expected):
    """Accepted spellings are trimmed and case-folded before becoming a boolean."""
    assert cli.execute("build-wheels", [*WHEEL_REQUIRED_ARGS, f"--ai-assisted={value}"]) == 0
    assert wheel_calls[0]["ai_assisted"] is expected


def test_ai_attribution_defaults_are_off(wheel_calls):
    """Without AI inputs, the build runs unannotated with an empty model list."""
    assert cli.execute("build-wheels", WHEEL_REQUIRED_ARGS) == 0
    assert wheel_calls[0]["ai_assisted"] is False
    assert wheel_calls[0]["ai_models"] == ""


def test_ai_attribution_inputs_fall_back_to_environment(wheel_calls, monkeypatch):
    """AI_ASSISTED and AI_MODELS environment values feed the matching task inputs."""
    monkeypatch.setenv("AI_ASSISTED", "True")
    monkeypatch.setenv("AI_MODELS", "model-a,model-b")
    assert cli.execute("build-wheels", WHEEL_REQUIRED_ARGS) == 0
    assert wheel_calls[0]["ai_assisted"] is True
    assert wheel_calls[0]["ai_models"] == "model-a,model-b"


def test_explicit_ai_arguments_override_environment(wheel_calls, monkeypatch):
    """Command-line AI options take precedence over the environment fallback."""
    monkeypatch.setenv("AI_ASSISTED", "true")
    monkeypatch.setenv("AI_MODELS", "environment-model")
    args = [*WHEEL_REQUIRED_ARGS, "--ai-assisted=false", "--ai-models=cli-model"]
    assert cli.execute("build-wheels", args) == 0
    assert wheel_calls[0]["ai_assisted"] is False
    assert wheel_calls[0]["ai_models"] == "cli-model"


@pytest.mark.parametrize("value", ["yes", "1", "truee"])
def test_invalid_ai_assisted_is_rejected_before_publication(wheel_calls, caplog, value):
    """Unknown AI_ASSISTED values fail the task instead of silently disabling attribution."""
    caplog.set_level("INFO")
    assert cli.execute("build-wheels", [*WHEEL_REQUIRED_ARGS, f"--ai-assisted={value}"]) == 1
    assert wheel_calls == []
    assert f"ai-assisted must be true or false, got '{value}'" in caplog.text
