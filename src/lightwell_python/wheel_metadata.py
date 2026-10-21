"""Wheel SBOM transformations with correctly regenerated CSV RECORD hashes."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import quote

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from lightwell_python.runtime import Package, TaskError


def system_libraries(output: str) -> dict[str, list[str]]:
    """Parse system library names and version symbols from auditwheel output.

    Args:
        output: Text returned by auditwheel show, possibly containing wrapped lines.

    Returns:
        Library sonames mapped to sorted symbol lists, or an empty mapping when no
        system-library section is present.
    """
    text = " ".join(output.split())
    match = re.search(r"system-provided shared libraries:\s*(.+?)(?:This constrains|$)", text)
    if not match:
        return {}

    libraries = {}
    library_pattern = r"(\S+\.so\S*)\s+with\s+versions\s+\{([^}]+)\}"
    for library_match in re.finditer(library_pattern, match[1]):
        name = library_match[1]
        symbols = library_match[2].split(",")
        libraries[name] = sorted(symbol.strip().strip("'\"") for symbol in symbols)
    return libraries


def patch_sbom(sbom: dict, filename: str, libraries: dict[str, list[str]]) -> dict:
    """Enrich an SPDX document with system dependencies and wheel filenames.

    Args:
        sbom: Decoded SPDX document, modified in place.
        filename: Final wheel basename used in file_name PURL qualifiers.
        libraries: External library sonames and their referenced version symbols.

    Returns:
        The same document with missing dependency entries and filename qualifiers
        added. Existing entries are preserved, and the upstream package PURL is
        left unchanged. Reapplying the same enrichment does not duplicate entries.
    """
    packages = sbom.setdefault("packages", [])
    relationships = sbom.setdefault("relationships", [])
    root = _root_package_id(sbom)
    existing = {p.get("name") for p in packages}
    ids = {p.get("SPDXID") for p in packages}
    if root:
        for name, symbols in sorted(libraries.items()):
            if name in existing:
                continue
            identifier = "SPDXRef-SystemLib-" + re.sub(r"[^a-zA-Z0-9.-]", "-", name)
            if identifier in ids:
                identifier += "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
            ids.add(identifier)
            entry = {
                "SPDXID": identifier,
                "name": name,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "primaryPackagePurpose": "LIBRARY",
                "supplier": "NOASSERTION",
            }
            if symbols:
                entry["comment"] = (
                    "System-provided shared library detected by auditwheel. "
                    "Referenced versioned symbols: " + ", ".join(symbols)
                )
            packages.append(entry)
            relationships.append(
                {
                    "spdxElementId": root,
                    "relationshipType": "DEPENDS_ON",
                    "relatedSpdxElement": identifier,
                }
            )
    for package in packages:
        if package.get("SPDXID") == "SPDXRef-upstream":
            continue
        for ref in package.get("externalRefs", []):
            if ref.get("referenceType") == "purl" and "file_name=" not in ref["referenceLocator"]:
                purl = ref["referenceLocator"]
                ref["referenceLocator"] = (
                    purl + ("&" if "?" in purl else "?") + "file_name=" + quote(filename, safe="")
                )
    return sbom


def _root_package_id(sbom: dict) -> str | None:
    """Find the described SPDX package, falling back to the first package entry."""
    for relationship in sbom.get("relationships", []):
        if (
            relationship.get("relationshipType") == "DESCRIBES"
            and relationship.get("spdxElementId") == "SPDXRef-DOCUMENT"
        ):
            return relationship["relatedSpdxElement"]

    described_packages = sbom.get("documentDescribes", [])
    if described_packages:
        return described_packages[0]
    packages = sbom.get("packages", [])
    if packages:
        return packages[0].get("SPDXID")
    return None


def patch_wheel(path: Path, libraries: dict[str, list[str]] | None = None) -> None:
    """Update embedded SBOMs and regenerate the wheel's CSV RECORD.

    Fromager SBOMs become redhat.spdx.json; auditwheel CDX documents are removed.
    Members are processed one at a time. A temporary sibling wheel replaces the
    original only after the rewrite completes successfully.

    Args:
        path: Wheel file to modify in place.
        libraries: Optional system dependencies to add to the Fromager SBOM.

    Raises:
        TaskError: The wheel is signed, has duplicate members, has no unique RECORD,
            or an SBOM rename collides with an existing member.
        zipfile.BadZipFile: The input is not a readable wheel archive.
    """
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".wheel-") as directory:
        target = Path(directory) / path.name
        with (
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output,
        ):
            names = source.namelist()
            if len(set(names)) != len(names):
                raise TaskError("Wheel contains duplicate members")
            records = [n for n in names if n.endswith(".dist-info/RECORD")]
            if len(records) != 1:
                raise TaskError("Wheel must contain exactly one RECORD")
            if any(n.endswith((".dist-info/RECORD.jws", ".dist-info/RECORD.p7s")) for n in names):
                raise TaskError("Cannot modify an already signed wheel")
            rows = []
            written: set[str] = set()
            for info in source.infolist():
                name = info.filename
                if name == records[0] or (
                    ".dist-info/sboms/" in name and name.endswith(".cdx.json")
                ):
                    continue
                data = source.read(name)
                if name.endswith(".dist-info/sboms/fromager.spdx.json"):
                    data = json.dumps(
                        patch_sbom(json.loads(data), path.name, libraries or {}), indent=2
                    ).encode()
                    name = name.removesuffix("fromager.spdx.json") + "redhat.spdx.json"
                if name in written:
                    raise TaskError("SBOM rename collides with an existing wheel member")
                written.add(name)
                info.filename = name
                output.writestr(info, data)
                digest = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                )
                rows.append((name, f"sha256={digest}", str(len(data))))
            rows.append((records[0], "", ""))
            record = io.StringIO(newline="")
            csv.writer(record, lineterminator="\n").writerows(rows)
            output.writestr(records[0], record.getvalue())
        target.replace(path)


def verify_primary_wheels(artifact: Path, package: Package, computed: str) -> list[Path]:
    """Require the primary wheel's filename and embedded versions to agree.

    Args:
        artifact: Staging directory containing wheels to inspect.
        package: Primary distribution; unrelated dependency wheels are ignored.
        computed: Expected version in the filename, METADATA, and primary SPDX entry.

    Returns:
        Sorted paths of all successfully verified primary wheels.

    Raises:
        TaskError: No primary wheel exists, required metadata is missing/ambiguous,
            or the embedded identity/version differs from the requested build.
    """
    found = []
    for path in sorted(artifact.glob("*.whl")):
        name, version, _, _ = parse_wheel_filename(path.name)
        if name != package.name:
            continue
        if version != Version(computed):
            raise TaskError(f"Primary wheel filename version mismatch: {path.name}")
        with zipfile.ZipFile(path) as wheel:
            metadata = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
            sboms = [n for n in wheel.namelist() if n.endswith(".dist-info/sboms/redhat.spdx.json")]
            if len(metadata) != 1 or len(sboms) != 1:
                raise TaskError("Primary wheel must contain one METADATA and one redhat.spdx.json")
            headers = BytesParser().parsebytes(wheel.read(metadata[0]))
            if (
                headers.get("Version") != computed
                or canonicalize_name(headers.get("Name", "")) != package.name
            ):
                raise TaskError("Primary wheel METADATA identity/version mismatch")
            sbom = json.loads(wheel.read(sboms[0]))
            primary = [p for p in sbom.get("packages", []) if p.get("SPDXID") == "SPDXRef-wheel"]
            if len(primary) != 1 or primary[0].get("versionInfo") != computed:
                raise TaskError("Primary wheel SBOM version mismatch")
        found.append(path)
    if not found:
        raise TaskError(f"No primary wheel produced for {package.name}")
    return found


def extract_sbom(wheel: Path, target: Path) -> None:
    """Copy the unique primary SPDX document out of a verified wheel.

    Args:
        wheel: Wheel containing a redhat.spdx.json under its dist-info directory.
        target: Destination file; its parent directory must already exist.

    Raises:
        TaskError: The wheel does not contain exactly one primary SPDX document.
    """
    with zipfile.ZipFile(wheel) as archive:
        members = [n for n in archive.namelist() if n.endswith(".dist-info/sboms/redhat.spdx.json")]
        if len(members) != 1:
            raise TaskError("Expected exactly one primary SBOM")
        target.write_bytes(archive.read(members[0]))
