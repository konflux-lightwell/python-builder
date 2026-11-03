import json
import subprocess
import sys
from pathlib import Path

from lightwell_python.build_summary import (
    checkout_build_summary,
    main,
    write_checkout_build_summary,
)


def test_checkout_build_summary_schema():
    assert checkout_build_summary("demo", "1.0+rhlw.1") == [
        {"name": "demo", "version": "1.0+rhlw.1", "prebuilt": False, "skipped": False}
    ]


def test_write_checkout_build_summary_creates_parent_and_matches_runtime_format(tmp_path):
    path = tmp_path / "nested" / "summary.json"
    write_checkout_build_summary(path, "demo", "1.0+rhlw.1")
    expected = checkout_build_summary("demo", "1.0+rhlw.1")
    assert path.read_text() == json.dumps(expected, indent=2) + "\n"


def test_main_cli_writes_summary(tmp_path):
    out = tmp_path / "summary.json"
    exit_code = main(["demo", "1.0+rhlw.1", str(out)])
    assert exit_code == 0
    assert json.loads(out.read_text()) == checkout_build_summary("demo", "1.0+rhlw.1")


def test_module_runs_as_standalone_script_with_no_lightwell_python_dependency(tmp_path):
    """Run the module as a plain script, isolated (``-I``) and outside the repo.

    This is exactly what the 0.1 manylinux image needs: its python3 is Fromager's
    venv, which does not have lightwell_python installed, and scripts/build-wheels
    invokes this file directly as an executable, not as an import. ``-I`` plus a
    non-repo cwd proves the module needs nothing beyond the standard library.
    """
    module_path = Path(__file__).resolve().parents[2] / "src/lightwell_python/build_summary.py"
    out = tmp_path / "summary.json"
    subprocess.run(
        [sys.executable, "-I", str(module_path), "demo", "1.0+rhlw.1", str(out)],
        cwd=tmp_path,
        check=True,
    )
    assert json.loads(out.read_text()) == checkout_build_summary("demo", "1.0+rhlw.1")
