"""Streaming deterministic archives and exact sdist cache lookup."""

from __future__ import annotations

import gzip
import os
import shutil
import tarfile
from email.parser import Parser
from pathlib import Path

from packaging.utils import canonicalize_name, parse_sdist_filename
from packaging.version import Version

from lightwell_python.runtime import Package, TaskError


def create_archive(
    source: Path, target: Path, prefix: str = ".", *, normalize: bool = False
) -> None:
    """Stream a sorted tar.gz while preserving safe symlinks.

    This creates a package archive, not a trusted-artifact transport envelope.
    The gzip header contains neither a filename nor a creation timestamp.

    Args:
        source: File or source tree to archive.
        target: Archive destination; parent directories are created as needed.
        prefix: Root member name when archiving a directory.
        normalize: Zero ownership and timestamps, normalize access modes, and retain
            executable bits. Enable this for reproducible source distributions.

    Raises:
        TaskError: An entry is a special file or a symlink escaping the source tree.
        OSError: Source files cannot be read or the archive cannot be written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        target.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=raw) as gz,
        tarfile.open(fileobj=gz, mode="w|", format=tarfile.GNU_FORMAT) as archive,
    ):

        def add(path: Path, name: str) -> None:
            """Append one safe entry, then recurse through sorted directory children."""
            if path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(source.resolve()) or os.path.isabs(
                    os.readlink(path)
                ):
                    raise TaskError(f"Archive symlink escapes source: {path}")
            info = archive.gettarinfo(str(path), arcname=name)
            if not (info.isfile() or info.isdir() or info.issym()):
                raise TaskError(f"Unsupported archive member: {path}")
            if normalize:
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
            if info.isfile():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
            if info.isdir():
                for child in sorted(path.iterdir()):
                    add(child, f"{name}/{child.name}")

        add(source, prefix if source.is_dir() else source.name)


def create_sdist(source: Path, destination: Path, package: Package, computed: str) -> Path:
    """Create a deterministic sdist and its Fromager cache aliases.

    Args:
        source: Package source tree, with version metadata already rewritten.
        destination: Staging tree whose downloads directory receives the archives.
        package: Canonical distribution name and upstream version.
        computed: Full build version used for additional cache filenames.

    Returns:
        The canonical upstream-version archive path. Original-case and underscore
        aliases contain identical bytes and are stored alongside it.

    Raises:
        TaskError: PKG-INFO names another package or archive validation fails.
    """
    downloads = destination / "downloads"
    target = downloads / f"{package.name}-{package.version}.tar.gz"
    create_archive(source, target, f"{package.name}-{package.version}", normalize=True)
    names = {package.name, package.name.replace("-", "_")}
    metadata = source / "PKG-INFO"
    if metadata.is_file():
        raw = Parser().parsestr(metadata.read_text(encoding="utf-8"), headersonly=True).get("Name")
        if raw:
            if canonicalize_name(raw, validate=True) != package.name:
                raise TaskError("PKG-INFO Name differs from the requested package")
            names.add(raw)
    for name in sorted(names):
        for version in sorted({package.version, computed}):
            alias = downloads / f"{name}-{version}.tar.gz"
            if alias != target:
                shutil.copyfile(target, alias)
    return target


def find_sdist(root: Path, package: Package, computed: str | None = None) -> Path:
    """Find an exact distribution/version match in a restored sdist tree.

    Args:
        root: Writable copy of the restored tree; downloads is searched first.
        package: Required distribution and upstream version.
        computed: Optional computed version also accepted during cache lookup.

    Returns:
        The canonical upstream-version path under downloads, copying the matching
        archive there if necessary. Version comparisons use PEP 440 semantics.

    Raises:
        TaskError: No matching pre-placed sdist exists.
    """
    versions = {Version(package.version), Version(computed or package.version)}
    for directory in (root / "downloads", root):
        for candidate in sorted(directory.glob("*.tar.gz")):
            name, version = parse_sdist_filename(candidate.name)
            if name == package.name and version in versions:
                target = root / "downloads" / f"{package.name}-{package.version}.tar.gz"
                target.parent.mkdir(parents=True, exist_ok=True)
                if candidate != target:
                    shutil.copyfile(candidate, target)
                return target
    raise TaskError(f"No pre-placed sdist for {package.requirement}")
