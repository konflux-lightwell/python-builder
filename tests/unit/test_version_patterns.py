import pytest

from lightwell_python.runtime import Package
from lightwell_python.versioning import rewrite_metadata


def rewrite(root, rules=None):
    return rewrite_metadata(root, Package("demo", "1.2.3"), "1.2.3+rhlw.1", rules or [])


@pytest.mark.parametrize("relative", ["VERSION", "demo/VERSION"])
@pytest.mark.parametrize("padding", [("", ""), (" ", "\n"), ("\t", "\r\n")])
def test_plain_version_file(tmp_path, relative, padding):
    target = tmp_path / relative
    target.parent.mkdir(exist_ok=True)
    before = (padding[0] + "1.2.3" + padding[1]).encode()
    target.write_bytes(before)
    assert rewrite(tmp_path) == [target]
    assert target.read_bytes() == before.replace(b"1.2.3", b"1.2.3+rhlw.1")


@pytest.mark.parametrize("content", ["9.9.9\n", "release 1.2.3", "1.2.3\n1.2.3", "# 1.2.3", ""])
def test_other_version_file_contents_unchanged(tmp_path, content):
    target = tmp_path / "VERSION"
    target.write_text(content)
    assert rewrite(tmp_path) == []
    assert target.read_text() == content


@pytest.mark.parametrize("assignment", ["RELEASE = '1.2.3'", 'RELEASE: str = "1.2.3"'])
def test_setup_variable(tmp_path, assignment):
    target = tmp_path / "setup.py"
    source = f"from setuptools import setup\n{assignment}  # release\nsetup(version=RELEASE)\n"
    target.write_text(source)
    assert rewrite(tmp_path) == [target]
    assert target.read_text() == source.replace("1.2.3", "1.2.3+rhlw.1")


def test_repeated_setup_calls_rewrite_variable_once(tmp_path):
    target = tmp_path / "setup.py"
    source = "from setuptools import setup\nV = '1.2.3'\nsetup(version=V)\nsetup(version=V)\n"
    target.write_text(source)
    rewrite(tmp_path)
    assert target.read_text() == source.replace("1.2.3", "1.2.3+rhlw.1")


@pytest.mark.parametrize(
    "source",
    [
        "V = '1.2.3'\nV = '1.2.3'\nsetup(version=V)",
        "V = '1.2.3'\nV += suffix\nsetup(version=V)",
        "V = get_version()\nsetup(version=V)",
        "if condition:\n    V = '1.2.3'\nsetup(version=V)",
        "setup(version=V)\nV = '1.2.3'",
        "V = OTHER = '1.2.3'\nsetup(version=V)",
        "V = '9.9.9'\nsetup(version=V)",
        "V = '1.2.3'\nfrom other import V\nsetup(version=V)",
        "V = '1.2.3'\ndef configure(V):\n    setup(version=V)",
        "V = '1.2.3'\ndel V\nsetup(version=V)",
    ],
)
def test_ambiguous_or_dynamic_variable_unchanged(tmp_path, source):
    target = tmp_path / "setup.py"
    original = "from setuptools import setup\n" + source + "\n"
    target.write_text(original)
    assert rewrite(tmp_path) == []
    assert target.read_text() == original


def test_explicit_rules_replace_new_defaults(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.3")
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nV = '1.2.3'\nsetup(version=V)\n"
    )
    (tmp_path / "custom").write_text("1.2.3")
    changes = rewrite(
        tmp_path, [{"path": "custom", "pattern": "1.2.3", "replacement": "{COMPUTED_VERSION}"}]
    )
    assert changes == [tmp_path / "custom"]
    assert (tmp_path / "VERSION").read_text() == "1.2.3"
    assert "rhlw" not in (tmp_path / "setup.py").read_text()
