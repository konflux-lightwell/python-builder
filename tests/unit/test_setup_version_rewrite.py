import pytest

from lightwell_python.runtime import Package
from lightwell_python.versioning import rewrite_metadata


def rewrite(root):
    rewrite_metadata(root, Package("demo", "1.2.3"), "1.2.3+rhlw.1", [])


@pytest.mark.parametrize(
    "source",
    [
        "from setuptools import setup\nsetup(version='1.2.3')\n",
        (
            "from setuptools import setup as configure\n"
            'configure(\n    version = "1.2.3",  # release\n)\n'
        ),
        "import setuptools as st\nst.setup(name='démo', version='1.2.3')\n",
        "from distutils.core import setup\nsetup(version='1.2.3')\n",
        "import distutils.core\ndistutils.core.setup(version='1.2.3')\n",
    ],
)
def test_default_setup_literal_version(tmp_path, source):
    import ast

    target = tmp_path / "setup.py"
    target.write_text(source)
    rewrite(tmp_path)
    tree = ast.parse(target.read_text())
    values = [
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "version"
    ]
    assert values == ["1.2.3+rhlw.1"]
    if "# release" in source:
        assert "# release" in target.read_text()


@pytest.mark.parametrize(
    "source",
    [
        "from setuptools import setup\n# setup(version='1.2.3')\nother(version='1.2.3')\n",
        (
            "from setuptools import setup\n"
            'example = "setup(version=\'1.2.3\')"\nsetup(version="9.9.9")\n'
        ),
        "from setuptools import setup\nsetup(version='1.2.3' + suffix)\n",
        "not valid Python !",
    ],
)
def test_default_setup_preserves_other_content(tmp_path, source):
    target = tmp_path / "setup.py"
    target.write_text(source)
    rewrite(tmp_path)
    assert target.read_text() == source


def test_default_setup_only_at_source_root(tmp_path):
    nested = tmp_path / "vendor"
    nested.mkdir()
    target = nested / "setup.py"
    original = "from setuptools import setup\nsetup(version='1.2.3')\n"
    target.write_text(original)
    rewrite(tmp_path)
    assert target.read_text() == original


@pytest.mark.parametrize("encoding", ["latin-1", "utf-8"])
def test_setup_rewrite_preserves_declared_encoding(tmp_path, encoding):
    target = tmp_path / "setup.py"
    original = (
        f"# coding: {encoding}\nfrom setuptools import setup\nsetup(name='café', version='1.2.3')\n"
    ).encode(encoding)
    target.write_bytes(original)
    rewrite(tmp_path)
    assert target.read_bytes() == original.replace(b"1.2.3", b"1.2.3+rhlw.1")
