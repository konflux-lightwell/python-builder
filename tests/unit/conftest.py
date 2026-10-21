import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from lightwell_python.runtime import Runner


@pytest.fixture
def package_config():
    return {
        "package": "demo",
        "canonical_name": "demo",
        "upstream_registry": "pypi.org",
        "lightwell_builds_repo": "pypi.org-demo",
        "versions": ["1.0"],
    }


@pytest.fixture
def git_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (
        ("user.email", "test@example.test"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    source = root / "source"
    source.mkdir()
    (source / "PKG-INFO").write_text("Name: demo\nVersion: 1.0\n")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("add", ".")
    git("commit", "-qm", "ingest")
    git("tag", "demo/1.0")
    return root, git


@pytest.fixture
def make_wheel():
    def make(
        directory: Path,
        version="1.0+rhlw.1",
        *,
        sbom_version=None,
        name="demo",
        redhat=False,
        native=False,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        wheel = (
            directory
            / f"{name}-{version}-py3-none-{'manylinux_2_28_x86_64' if native else 'any'}.whl"
        )
        dist = f"{name}-{version}.dist-info"
        sbom = {
            "packages": [
                {
                    "SPDXID": "SPDXRef-wheel",
                    "name": name,
                    "versionInfo": sbom_version or version,
                    "externalRefs": [
                        {"referenceType": "purl", "referenceLocator": f"pkg:pypi/{name}@{version}"}
                    ],
                }
            ],
            "documentDescribes": ["SPDXRef-wheel"],
        }
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"{dist}/METADATA", f"Name: {name}\nVersion: {version}\n")
            archive.writestr(
                f"{dist}/sboms/{'redhat' if redhat else 'fromager'}.spdx.json", json.dumps(sbom)
            )
            archive.writestr(f"{dist}/sboms/auditwheel.cdx.json", "{}")
            archive.writestr("demo,a.py", "__version__ = '" + version + "'\n")
            archive.writestr(f"{dist}/RECORD", "")
        return wheel

    return make


class FakeRunner(Runner):
    def __init__(self, callback=None):
        super().__init__(environment={})
        self.calls = []
        self.callback = callback

    def run(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append((args, kwargs))
        if self.callback:
            return self.callback(args, kwargs) or ""
        return ""


class FakeRegistry:
    def __init__(self):
        self.calls = []
        self.reference = "localhost:5000/org/repo@sha256:" + "a" * 64

    def push(self, image, directory, artifact_type, annotations, tags=None):
        self.calls.append(("push", image, directory, artifact_type, annotations, tags))
        return self.reference

    def attach_sbom(self, reference, sbom):
        self.calls.append(("sbom", reference, sbom))
        return reference
