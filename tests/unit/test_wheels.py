import base64
import csv
import hashlib
import io
import json
import netrc
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lightwell_python.build import (
    backend_environment,
    collect_artifacts,
    repair_wheels,
    run_fromager,
)
from lightwell_python.manifest import build_manifest
from lightwell_python.runtime import BuildType, Package, TaskError, write_json
from lightwell_python.tasks import wheels
from lightwell_python.wheel_metadata import (
    annotate_ai_attribution,
    extract_sbom,
    patch_sbom,
    patch_wheel,
    system_libraries,
    verify_primary_wheels,
)

from .conftest import FakeRegistry, FakeRunner, wheel_builder

FIXED_NOW = datetime(2026, 10, 27, 8, 5, 9, tzinfo=UTC)


def test_wheel_record_matches_every_member(tmp_path, make_wheel):
    wheel = make_wheel(tmp_path)
    patch_wheel(wheel, {"libc.so.6": ["GLIBC_2.17"]})
    verify_primary_wheels(tmp_path, Package("demo", "1.0"), "1.0+rhlw.1")
    with zipfile.ZipFile(wheel) as archive:
        assert not any(n.endswith(".cdx.json") for n in archive.namelist())
        assert not any(n.endswith("fromager.spdx.json") for n in archive.namelist())
        record = next(n for n in archive.namelist() if n.endswith("/RECORD"))
        rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
        assert {row[0] for row in rows} == set(archive.namelist())
        for name, digest, size in rows:
            if name == record:
                assert digest == size == ""
            else:
                data = archive.read(name)
                assert (
                    digest
                    == "sha256="
                    + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                )
                assert int(size) == len(data)
        sbom = json.loads(
            archive.read(next(n for n in archive.namelist() if n.endswith("redhat.spdx.json")))
        )
        assert any(p["name"] == "libc.so.6" for p in sbom["packages"])
        assert "file_name=" in sbom["packages"][0]["externalRefs"][0]["referenceLocator"]


@pytest.mark.parametrize("version", ["1.0", "1.0+rhlw.1"])
def test_vendored_metadata_is_preserved_and_hashed(tmp_path, make_wheel, version):
    wheel = make_wheel(tmp_path, version=version)
    nested = "demo/_vendor/dependency-2.0.dist-info"
    vendored = {
        f"{nested}/RECORD": b"original vendored record\n",
        f"{nested}/RECORD.jws": b"vendored signature",
        f"{nested}/METADATA": b"Name: dependency\nVersion: 2.0\n",
        f"{nested}/sboms/fromager.spdx.json": b'{"packages": []}',
        f"{nested}/sboms/redhat.spdx.json": b'{"packages": []}',
        f"{nested}/sboms/auditwheel.cdx.json": b"{}",
    }
    with zipfile.ZipFile(wheel, "a") as archive:
        for name, data in vendored.items():
            archive.writestr(name, data)

    patch_wheel(wheel)
    assert verify_primary_wheels(tmp_path, Package("demo", "1.0"), version) == [wheel]
    sbom_path = tmp_path / "primary.spdx.json"
    extract_sbom(wheel, sbom_path)
    assert json.loads(sbom_path.read_bytes())["packages"][0]["versionInfo"] == version

    with zipfile.ZipFile(wheel) as archive:
        record_name = f"demo-{version}.dist-info/RECORD"
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode())))
        assert {row[0] for row in rows} == set(archive.namelist())
        for name, data in vendored.items():
            assert archive.read(name) == data
        for name, digest, size in rows:
            if name == record_name:
                assert digest == size == ""
            else:
                data = archive.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                assert digest == f"sha256={expected.decode()}"
                assert int(size) == len(data)


@pytest.mark.parametrize("extra", [False, True])
def test_missing_or_ambiguous_root_metadata_fails_without_modifying_wheel(tmp_path, extra):
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\n")
        archive.writestr("demo/_vendor/dep-2.0.dist-info/RECORD", "")
        if extra:
            archive.writestr("demo-1.0.dist-info/RECORD", "")
            archive.writestr("other-2.0.dist-info/METADATA", "Name: other\n")
    before = wheel.read_bytes()
    message = "one top-level .dist-info" if extra else "missing its top-level RECORD"
    with pytest.raises(TaskError, match=message) as error:
        patch_wheel(wheel)
    assert wheel.name in str(error.value)
    assert wheel.read_bytes() == before


@pytest.mark.parametrize(
    "version,sbom_version,match", [("1.0", None, "filename"), ("1.0+rhlw.1", "1.0", "SBOM")]
)
def test_primary_identity_checks(tmp_path, make_wheel, version, sbom_version, match):
    make_wheel(tmp_path, version=version, sbom_version=sbom_version, redhat=True)
    with pytest.raises(TaskError, match=match):
        verify_primary_wheels(tmp_path, Package("demo", "1.0"), "1.0+rhlw.1")


def test_no_primary_wheel(tmp_path):
    with pytest.raises(TaskError, match="No primary"):
        verify_primary_wheels(tmp_path, Package("demo", "1.0"), "1.0")


def test_duplicate_and_signed_wheels_rejected(tmp_path, make_wheel):
    wheel = make_wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("demo-1.0+rhlw.1.dist-info/RECORD.jws", "signed")
    before = wheel.read_bytes()
    with pytest.raises(TaskError, match="signed"):
        patch_wheel(wheel)
    assert wheel.read_bytes() == before


def test_sbom_is_idempotent_and_leaves_upstream_purl():
    sbom = {
        "packages": [
            {"SPDXID": "SPDXRef-wheel", "name": "demo"},
            {
                "SPDXID": "SPDXRef-upstream",
                "externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:pypi/demo@1"}],
            },
        ]
    }
    libs = {"lib++.so.6": ["ABI_1"], "lib__.so.6": ["ABI_2"]}
    first = patch_sbom(sbom, "demo.whl", libs)
    second = patch_sbom(json.loads(json.dumps(first)), "demo.whl", libs)
    assert first == second
    ids = [p["SPDXID"] for p in first["packages"]]
    assert len(ids) == len(set(ids))
    assert first["packages"][1]["externalRefs"][0]["referenceLocator"] == "pkg:pypi/demo@1"


def test_system_library_parsing():
    assert system_libraries("nothing") == {}
    assert system_libraries(
        "system-provided shared libraries: libc.so.6 "
        "with versions {'GLIBC_2.2',\n'GLIBC_2.17'} This constrains"
    ) == {"libc.so.6": ["GLIBC_2.17", "GLIBC_2.2"]}


def test_native_repair_runs_with_temporary_libraries(tmp_path, make_wheel):
    wheel = make_wheel(tmp_path)
    native = wheel.with_name(wheel.name.replace("none-any", "none-linux_x86_64"))
    wheel.rename(native)
    with zipfile.ZipFile(native, "a") as archive:
        archive.writestr("package/libdemo.so", b"native lib")

    def repair(args, kwargs):
        if args[1] == "repair":
            from pathlib import Path

            libraries = Path(kwargs["env"]["LD_LIBRARY_PATH"].split(":")[0])
            assert (libraries / "libdemo.so").read_bytes() == b"native lib"
            make_wheel(tmp_path, native=True)
        elif args[1] == "show":
            return "system-provided shared libraries: libc.so.6 with versions {'GLIBC_2.17'}"

    runner = FakeRunner(repair)
    repair_wheels(runner, tmp_path, {})
    assert [a[1] for a, _ in runner.calls] == ["repair", "show"]
    assert native.exists()


def test_manifest_produced_wins_independent_of_order(tmp_path):
    write_json(
        tmp_path / "build-sequence-summary-a.json",
        [
            {"name": "Demo", "version": "1.0", "prebuilt": True},
            {"name": "dep", "version": "2", "skipped": True},
        ],
    )
    write_json(tmp_path / "build-sequence-summary-b.json", [{"name": "demo", "version": "1.0"}])
    manifest = build_manifest(tmp_path, BuildType.BACKPORT, frozenset({"demo"}))
    assert manifest == {
        "produced": [{"name": "demo", "version": "1.0"}],
        "cached": [{"name": "dep", "version": "2"}],
    }


@pytest.mark.parametrize(
    "entries",
    [
        [],
        {},
        ["bad"],
        [{"version": "1.0"}],
        [{"name": "demo", "version": "1.0", "prebuilt": "false"}],
    ],
)
def test_manifest_malformed_fails_closed(tmp_path, entries):
    write_json(tmp_path / "build-sequence-summary-demo.json", entries)
    with pytest.raises(TaskError):
        build_manifest(tmp_path, BuildType.BACKPORT, frozenset({"demo"}))


def test_validated_accepts_dependency_closure(tmp_path):
    write_json(
        tmp_path / "build-sequence-summary-demo.json",
        [{"name": "dep", "version": "2.0"}, {"name": "demo", "version": "1.0", "skipped": True}],
    )
    assert build_manifest(tmp_path, BuildType.VALIDATED, frozenset())["produced"] == [
        {"name": "dep", "version": "2.0"}
    ]


def test_netrc_is_private_handles_quotes_and_is_removed(tmp_path):
    (tmp_path / "username").write_text("user name")
    (tmp_path / "password").write_text('quoted "password"')
    from pathlib import Path

    with backend_environment({}, tmp_path) as env:
        path = Path(env["NETRC"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert netrc.netrc(path).authenticators("packages.redhat.com") == (
            "user name",
            "",
            'quoted "password"',
        )
    assert not path.exists()
    with backend_environment({}, None) as env:
        assert "NETRC" not in env


def test_collect_rejects_stale_destination(tmp_path):
    with pytest.raises(FileExistsError):
        collect_artifacts(tmp_path, tmp_path)


def test_missing_build_summary_fails(tmp_path):
    with pytest.raises(TaskError, match="summary"):
        run_fromager(
            FakeRunner(),
            tmp_path / "output",
            tmp_path / "sdists",
            tmp_path / "settings",
            Package("demo", "1.0"),
            {},
        )


def attribution(models: str, date: str = "2026-10-27T08:05:09Z") -> dict:
    """Build the attribution annotation expected for a model list.

    Args:
        models: Comma-separated model IDs recorded in the comment.
        date: Expected annotationDate string.

    Returns:
        The SPDX 2.3 annotation dictionary.
    """
    return {
        "annotationType": "OTHER",
        "annotator": "Organization: Red Hat Lightwell",
        "annotationDate": date,
        "comment": (
            f"lightwell:aiAssisted=true; lightwell:aiModels={models}; "
            "lightwell:aiSource=Red Hat Lightwell"
        ),
    }


@pytest.fixture
def sbom_file(tmp_path):
    """Provide a minimal SPDX 2.3 document on disk that has no annotations key.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        Path to the written SPDX JSON file.
    """
    path = tmp_path / "sbom.spdx.json"
    path.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}))
    return path


def test_annotation_is_created_when_missing(sbom_file):
    """A document without annotations gets a new list and keeps the rest of its content.

    The rewrite uses two-space indentation and a trailing newline.
    """
    annotate_ai_attribution(sbom_file, "model-a,model-b", now=FIXED_NOW)
    expected = {
        "spdxVersion": "SPDX-2.3",
        "packages": [],
        "annotations": [attribution("model-a,model-b")],
    }
    assert sbom_file.read_text() == json.dumps(expected, indent=2) + "\n"


def test_annotation_is_appended_after_existing_annotations(tmp_path):
    """Existing annotations keep their position and the attribution follows them."""
    existing = {
        "annotationType": "REVIEW",
        "annotator": "Person: Jane Reviewer",
        "annotationDate": "2026-01-01T00:00:00Z",
        "comment": "reviewed",
    }
    path = tmp_path / "sbom.spdx.json"
    path.write_text(json.dumps({"annotations": [existing]}))
    annotate_ai_attribution(path, "model-a", now=FIXED_NOW)
    assert json.loads(path.read_text())["annotations"] == [existing, attribution("model-a")]


def test_fixed_now_sets_the_exact_utc_date(sbom_file):
    """An injected timestamp is formatted as the exact UTC string with a Z suffix."""
    annotate_ai_attribution(sbom_file, "model-a", now=FIXED_NOW)
    (annotation,) = json.loads(sbom_file.read_text())["annotations"]
    assert annotation["annotationDate"] == "2026-10-27T08:05:09Z"


def test_default_timestamp_is_current_utc(sbom_file):
    """Without an injected time, the annotation date falls inside the call's UTC window."""
    before = datetime.now(UTC).replace(microsecond=0)
    annotate_ai_attribution(sbom_file, "model-a")
    after = datetime.now(UTC)
    (annotation,) = json.loads(sbom_file.read_text())["annotations"]
    stamp = datetime.strptime(annotation["annotationDate"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    assert before <= stamp <= after


@pytest.mark.parametrize("annotations", [{}, "text", None, 7])
def test_non_list_annotations_fail_without_rewriting(tmp_path, annotations):
    """A malformed annotations value fails closed and leaves the SBOM bytes unchanged."""
    path = tmp_path / "sbom.spdx.json"
    path.write_text(json.dumps({"annotations": annotations}))
    before = path.read_bytes()
    with pytest.raises(TaskError, match="annotations must be a list"):
        annotate_ai_attribution(path, "model-a", now=FIXED_NOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize("document", [[], "text", None])
def test_non_object_document_fails_without_rewriting(tmp_path, document):
    """A top-level value that is not a JSON object fails closed and stays unchanged."""
    path = tmp_path / "sbom.spdx.json"
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(TaskError, match="must be a JSON object"):
        annotate_ai_attribution(path, "model-a", now=FIXED_NOW)
    assert path.read_bytes() == before


@pytest.fixture
def run_wheels(tmp_path, wheel_input, make_wheel):
    """Provide a runner for the wheel task that uses the fake builder and registry.

    Args:
        tmp_path: Per-test temporary directory.
        wheel_input: Restored sdist and build-config tree.
        make_wheel: Fixture that writes a primary wheel.

    Returns:
        A callable that accepts extra wheels.run keyword arguments, executes the task
        once, and returns the recording registry and the task scratch directory.
    """

    def run(**options):
        """Run the wheel task once with the given options.

        Args:
            **options: Extra keyword arguments forwarded to wheels.run.

        Returns:
            Tuple of the FakeRegistry and the task scratch directory.
        """
        work = tmp_path / "task"
        work.mkdir()
        registry = FakeRegistry()
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
            runner=wheel_builder(make_wheel),
            **options,
        )
        return registry, work

    return run


def attached_sbom(registry: FakeRegistry) -> bytes:
    """Return the bytes of the SBOM file the registry was asked to attach.

    Args:
        registry: Fake registry that recorded the attach call.

    Returns:
        Raw bytes of the attached SBOM file.
    """
    (sbom,) = [call[2] for call in registry.calls if call[0] == "sbom"]
    return Path(sbom).read_bytes()


def embedded_sbom(work: Path) -> bytes:
    """Return the redhat.spdx.json bytes stored inside the built primary wheel.

    Args:
        work: Task scratch directory containing the collected artifact.

    Returns:
        Raw bytes of the embedded SPDX document.
    """
    (wheel,) = (work / "artifact").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        return archive.read("demo-1.0+rhlw.1.dist-info/sboms/redhat.spdx.json")


@pytest.mark.parametrize("models", ["model-a,model-b", ""])
def test_ai_assisted_build_attaches_annotated_sbom(run_wheels, models):
    """Assisted builds attach the SBOM with one AI annotation and no other changes.

    The embedded SBOM has no annotations, so removing the new one from the attached
    document must reproduce the embedded content. Empty models still record the flag.
    """
    registry, work = run_wheels(ai_assisted=True, ai_models=models)

    attached = json.loads(attached_sbom(registry))
    embedded = json.loads(embedded_sbom(work))
    (annotation,) = attached.pop("annotations")
    assert annotation["annotationType"] == "OTHER"
    assert annotation["annotator"] == "Organization: Red Hat Lightwell"
    assert annotation["comment"] == (
        f"lightwell:aiAssisted=true; lightwell:aiModels={models}; "
        "lightwell:aiSource=Red Hat Lightwell"
    )
    datetime.strptime(annotation["annotationDate"], "%Y-%m-%dT%H:%M:%SZ")
    assert "annotations" not in embedded
    assert attached == embedded


def test_unassisted_build_attaches_embedded_sbom_bytes(run_wheels):
    """Builds without AI assistance attach the embedded SBOM byte for byte.

    Models supplied without the assistance flag must not add an annotation.
    """
    registry, work = run_wheels(ai_models="model-a")
    assert attached_sbom(registry) == embedded_sbom(work)
