#!/usr/bin/env python3
"""Synthesize the single-entry build-sequence summary for a checkout build.

Fromager's ``step`` subcommands -- used for a build from a pre-staged git
checkout, rather than a resolved ``build-sequence`` -- do not write a
build-sequence summary; only ``build-sequence`` itself does, so one is
synthesized here instead. This module is the single definition of that
synthesized schema, shared by ``lightwell_python.build.run_fromager_checkout``
(imported) and ``scripts/build-wheels`` (invoked as a standalone executable,
installed into the manylinux build image as ``write-checkout-build-summary``).

That image's ``python3`` is Fromager's own venv, which does not have
``lightwell_python`` installed, so this module imports only the standard
library and must keep doing so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def checkout_build_summary(name: str, version: str) -> list[dict[str, object]]:
    """Build the single-entry build-sequence summary for a checkout build.

    Args:
        name: Package name as Fromager built it.
        version: Full version Fromager built and tagged the wheel with.

    Returns:
        A one-entry list matching Fromager's own build-sequence summary schema:
        a freshly built, non-prebuilt, non-skipped package.
    """
    return [{"name": name, "version": version, "prebuilt": False, "skipped": False}]


def write_checkout_build_summary(path: Path, name: str, version: str) -> None:
    """Write the checkout build summary as indented JSON with a trailing newline.

    Args:
        path: Destination summary file. Missing parent directories are created.
        name: Package name as Fromager built it.
        version: Full version Fromager built and tagged the wheel with.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkout_build_summary(name, version), indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the ``write-checkout-build-summary NAME VERSION OUTPUT`` CLI.

    Args:
        argv: Argument list to parse instead of ``sys.argv[1:]``; used by tests.

    Returns:
        Process exit status, always 0 on success. Invalid arguments make
        argparse exit non-zero itself before this returns.
    """
    parser = argparse.ArgumentParser(
        description="Synthesize a Fromager checkout build's build-sequence summary."
    )
    parser.add_argument("name", help="Package name as Fromager built it")
    parser.add_argument("version", help="Full version Fromager built and tagged the wheel with")
    parser.add_argument("output", type=Path, help="Destination summary JSON file")
    args = parser.parse_args(argv)
    write_checkout_build_summary(args.output, args.name, args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
