"""Check the Tekton-to-CLI contract without a cluster or registry writes."""

import re
from pathlib import Path

import pytest
import yaml

from lightwell_python import cli

TASKS_ROOT = Path(__file__).resolve().parents[2] / "tekton/tasks"
TASK_COMMANDS = {
    "source-to-sdist": "source-to-sdist",
    "python-fromager-build-wheels": "build-wheels",
    "python-fromager-build-sdist": "build-sdist",
}
COMMAND_BY_TASK_NAME = {**TASK_COMMANDS, "fromager-build-sdist": "build-sdist"}
VARIABLE = re.compile(r"\$\(([^)]+)\)")


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        assert len(keys) == len(set(keys)), f"Duplicate YAML keys: {keys}"
        return super().construct_mapping(node, deep=deep)


@pytest.fixture(params=TASK_COMMANDS)
def task(request):
    path = TASKS_ROOT / request.param / "0.2" / f"{request.param}.yaml"
    return yaml.load(path.read_text(), Loader=UniqueKeyLoader)


def bindings(task, build_type="Backport"):
    spec = task["spec"]
    values = {
        f"params.{param['name']}": param["default"]
        for param in spec["params"]
        if "default" in param
    }
    samples = {
        "PACKAGE": "demo",
        "VERSION": "1.0",
        "COMPUTED_VERSION": "1.0" if build_type == "Validated" else "1.0+rhlw.1",
        "BUILD_TYPE": build_type,
        "LIGHTWELL_BUILDS_TAG": "demo/1.0",
        "SOURCE_ARTIFACT": "registry.test/source@sha256:" + "a" * 64,
        "SDISTS_ARTIFACT": "registry.test/sdists@sha256:" + "b" * 64,
        "IMAGE": "registry.test/demo:1.0",
        "IMAGE_REF": "registry.test/demo@sha256:" + "c" * 64,
        "PYTHON_BUILDER_IMAGE": "registry.test/python-builder@sha256:" + "d" * 64,
        "BUILD_CONFIGS_REPO_URL": "https://git.example/configs.git",
        "BUILD_CONFIGS_REVISION": "a" * 40,
        "ociStorage": "registry.test/trusted-artifacts",
        "git-url": "https://git.example/orchestrator.git",
        "revision": "b" * 40,
    }
    if build_type == "Validated":
        samples["WHEEL_SERVER_URL"] = ""
    for param in spec["params"]:
        if param["name"] in samples:
            values[f"params.{param['name']}"] = samples[param["name"]]
    for workspace in spec.get("workspaces", []):
        values[f"workspaces.{workspace['name']}.path"] = workspace.get(
            "mountPath", f"/workspace/{workspace['name']}"
        )
    for result in spec["results"]:
        values[f"results.{result['name']}.path"] = f"/tekton/results/{result['name']}"
    return values


def render(value, values):
    return VARIABLE.sub(lambda match: values[match[1]], value)


def test_task_catalog_is_complete():
    paths = list(TASKS_ROOT.glob("*/*/*.yaml"))
    assert {path.stem for path in paths} == set(TASK_COMMANDS)
    assert len(paths) == len(TASK_COMMANDS)


def test_task_structure_and_variable_references(task):
    assert task["apiVersion"] == "tekton.dev/v1"
    assert task["kind"] == "Task"
    spec = task["spec"]
    for collection in ("params", "results", "steps", "volumes", "workspaces"):
        names = [entry["name"] for entry in spec.get(collection, [])]
        assert len(names) == len(set(names)), f"Duplicate {collection}: {names}"

    values = bindings(task)
    for reference in VARIABLE.findall(yaml.safe_dump(task)):
        assert reference in values, f"Undeclared variable: {reference}"

    volumes = {volume["name"] for volume in spec["volumes"]}
    for step in spec["steps"]:
        assert "script" not in step
        mounts = [*spec["stepTemplate"]["volumeMounts"], *step.get("volumeMounts", [])]
        assert all(mount["name"] in volumes for mount in mounts)
        assert any(mount["mountPath"] == "/var/workdir" for mount in mounts)
        assert any(mount["name"] == "trusted-ca" and mount["readOnly"] for mount in mounts)
        assert step.get("onError", "stopAndFail") == "stopAndFail"


@pytest.mark.parametrize("build_type", ["Backport", "Novel", "Validated"])
def test_task_arguments_dispatch_to_the_python_entrypoint(task, build_type, tmp_path, monkeypatch):
    values = bindings(task, build_type)
    steps = task["spec"]["steps"]
    cli_steps = [step for step in steps if "command" in step]
    assert len(cli_steps) == 1
    step = cli_steps[0]
    cli_name = COMMAND_BY_TASK_NAME[task["metadata"]["name"]]
    assert step["command"] == [f"lightwell-{cli_name}"]
    assert step["image"] == "$(params.PYTHON_BUILDER_IMAGE)"

    args = [
        render(arg, values)
        .replace("/var/workdir", str(tmp_path / "workdir"))
        .replace("/tekton/results", str(tmp_path / "results"))
        for arg in step["args"]
    ]
    calls = []
    module = {
        "source-to-sdist": cli.source,
        "build-wheels": cli.wheels,
        "build-sdist": cli.sdist,
    }[cli_name]
    monkeypatch.setattr(module, "run", lambda **kwargs: calls.append(kwargs))
    assert cli.execute(cli_name, args) == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["package"].name == "demo"
    assert call["package"].version == "1.0"
    assert call["results"] == tmp_path / "results"
    assert call["workdir"].parent == tmp_path / "workdir"
    assert not any("ARTIFACT" in arg or "registry.test/trusted-artifacts" in arg for arg in args)
    if cli_name != "source-to-sdist":
        assert call["registry"].auth_file == Path("/mnt/registry-auth/.dockerconfigjson")


def test_trusted_artifact_paths_and_ownership(task):
    values = bindings(task)
    steps = task["spec"]["steps"]
    cli_position = next(i for i, step in enumerate(steps) if "command" in step)
    cli_step = steps[cli_position]
    cli_name = COMMAND_BY_TASK_NAME[task["metadata"]["name"]]
    args = cli.parser(cli_name).parse_args([render(arg, values) for arg in cli_step["args"]])
    restored = set()
    published = {}
    for position, step in enumerate(steps):
        if step is cli_step:
            continue
        assert re.fullmatch(
            r"quay.io/konflux-ci/build-trusted-artifacts:latest@sha256:[0-9a-f]{64}", step["image"]
        )
        operation, *arguments = [render(arg, values) for arg in step["args"]]
        if operation == "use":
            assert position < cli_position
            restored.add(Path(arguments[0].split("=", 1)[1]))
        else:
            assert operation == "create"
            assert position > cli_position
            assert arguments[:2] == ["--store", values["params.ociStorage"]]
            result, path = arguments[-1].split("=", 1)
            published[Path(result).name] = Path(path)
            assert step["env"] == [
                {"name": "IMAGE_EXPIRES_AFTER", "value": "$(params.ociArtifactExpiresAfter)"}
            ]
    assert getattr(args, "source_dir", getattr(args, "sdists_dir", None)) in restored
    if cli_name == "source-to-sdist":
        assert published == {"SDISTS_ARTIFACT": args.sdists_output}
    elif cli_name == "build-wheels":
        assert published == {"MANIFEST_ARTIFACT": args.manifest_output}
    else:
        assert published == {}
