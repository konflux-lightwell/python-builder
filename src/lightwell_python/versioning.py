"""Git lineage calculations and narrowly scoped package metadata rewriting."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from lightwell_python.config import expand
from lightwell_python.runtime import BuildType, Package, Runner, TaskError, contained_file


@dataclass(frozen=True)
class ComputedVersion:
    """The ADR-0020 version and its Git lineage evidence.

    Attributes:
        full: Upstream version plus the optional +rhlw.B[.n.N] suffix.
        b: Number of backport commits.
        n: Number of novel commits; zero for Backport and Validated builds.
        commit: Short HEAD commit ID, or an empty string for Validated builds.
    """

    full: str
    b: int = 0
    n: int = 0
    commit: str = ""


def compute_version(
    repo: Path, package: Package, build_type: BuildType, runner: Runner
) -> ComputedVersion:
    """Calculate the build version from authoritative Git lineage tags.

    Backport counts ingestion-to-HEAD commits. Novel splits the count at the
    baseline tag. Validated returns the upstream version without Git queries.

    Args:
        repo: Restored repository containing full history and ingestion/baseline tags.
        package: Distribution identity used to form the ingestion tag.
        build_type: Version policy to apply.
        runner: Adapter for repository-scoped Git commands.

    Returns:
        The full version, backport/novel counts, and abbreviated HEAD commit.

    Raises:
        TaskError: An upstream local suffix is already present, a required tag is
            missing, or the tagged commits do not form the expected ancestry.
    """
    if build_type == BuildType.VALIDATED:
        return ComputedVersion(package.version)
    if "+" in package.version:
        raise TaskError("Remediation requires an upstream version without a local suffix")
    base = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]

    def git(*args: str) -> str:
        """Run a read-only Git query scoped to this repository."""
        return runner.run([*base, *args], capture=True).strip()

    ingestion = git(
        "rev-parse", "--verify", f"refs/tags/{package.name}/{package.version}^{{commit}}"
    )
    head = git("rev-parse", "HEAD")
    git("merge-base", "--is-ancestor", ingestion, head)
    if build_type == BuildType.BACKPORT:
        b = int(git("rev-list", "--count", f"{ingestion}..{head}"))
        n = 0
    else:
        baseline = git("rev-parse", "--verify", f"refs/tags/baseline/{package.version}^{{commit}}")
        git("merge-base", "--is-ancestor", ingestion, baseline)
        git("merge-base", "--is-ancestor", baseline, head)
        b = int(git("rev-list", "--count", f"{ingestion}..{baseline}"))
        n = int(git("rev-list", "--count", f"{baseline}..{head}"))
    suffix = f"+rhlw.{b}" + (f".n.{n}" if n else "")
    return ComputedVersion(package.version + suffix, b, n, git("rev-parse", "--short=7", "HEAD"))


def version_counts(package: Package, computed: str, build_type: BuildType) -> tuple[int, int]:
    """Validate a computed version and extract its backport and novel counts.

    Args:
        package: Distribution with the expected upstream version.
        computed: Full build version, including any remediation suffix.
        build_type: Policy governing whether a suffix or novel count is permitted.

    Returns:
        The pair (backport_count, novel_count), both zero for Validated builds.

    Raises:
        TaskError: The computed version disagrees with the upstream version or policy.
    """
    if build_type == BuildType.VALIDATED:
        if computed != package.version:
            raise TaskError("Validated builds must use the upstream version")
        return 0, 0
    match = re.fullmatch(re.escape(package.version) + r"\+rhlw\.(\d+)(?:\.n\.?(\d+))?", computed)
    if not match:
        raise TaskError("Computed version does not match the upstream remediation version scheme")
    b, n = int(match[1]), int(match[2] or 0)
    if build_type == BuildType.BACKPORT and n:
        raise TaskError("Backport builds cannot have a novel component")
    return b, n


def rewrite_metadata(
    source: Path, package: Package, computed: str, rules: list[dict[str, str]]
) -> list[Path]:
    """Plan and apply package version metadata updates.

    Explicit rules replace the default heuristic. Without rules, updates target
    metadata headers, Python __version__ assignments, and project/metadata sections
    in TOML/INI files. All replacements are validated before any writes start;
    filesystem write errors can still leave a partially updated scratch tree.

    Args:
        source: Writable package source directory, never the restored input mount.
        package: Distribution identity and original upstream version.
        computed: Version to write. Equal to upstream means no changes are needed.
        rules: Source-relative path, regex pattern, and replacement template objects.

    Returns:
        Paths of the updated files. An empty list means no declarations changed.

    Raises:
        TaskError: The source is missing, a target escapes it, or an explicit rule
            does not match or fails to write the computed version.
        UnicodeError: A metadata file cannot be decoded as UTF-8.
    """
    if not source.is_dir():
        raise TaskError("source/ directory is missing")
    if computed == package.version:
        return []
    if rules:
        updates = _plan_configured_rewrites(source, package, computed, rules)
    else:
        updates = _plan_default_rewrites(source, package, computed)

    for path, text in updates.items():
        path.write_text(text, encoding="utf-8")
    if not updates:
        logging.getLogger(__name__).warning(
            "No version declarations changed; wheel metadata will be verified"
        )
    return list(updates)


def _plan_configured_rewrites(
    source: Path, package: Package, computed: str, rules: list[dict[str, str]]
) -> dict[Path, str]:
    """Validate configured replacements and collect changes without writing files."""
    updates: dict[Path, str] = {}
    for rule in rules:
        path = contained_file(source, rule["path"])
        original = updates.get(path, path.read_text(encoding="utf-8"))
        text, count = re.subn(
            rule["pattern"],
            expand(rule["replacement"], package, computed),
            original,
            flags=re.MULTILINE,
        )
        if not count or computed not in text:
            raise TaskError(f"Configured rewrite did not write computed version: {rule['path']}")
        updates[path] = text
    return updates


def _plan_default_rewrites(source: Path, package: Package, computed: str) -> dict[Path, str]:
    """Collect changes to authoritative metadata and Python __version__ assignments."""
    updates: dict[Path, str] = {}
    old = re.escape(package.version)
    for candidate in sorted(source.rglob("*")):
        if candidate.is_dir():
            continue
        name = candidate.name
        if (
            name not in {"PKG-INFO", "METADATA"}
            and candidate.suffix != ".py"
            and candidate not in {source / "pyproject.toml", source / "setup.cfg"}
        ):
            continue
        path = contained_file(source, str(candidate.relative_to(source)))
        original = path.read_text(encoding="utf-8")
        if name in {"PKG-INFO", "METADATA"}:
            header, separator, body = original.partition("\n\n")
            header = re.sub(
                r"^(Version:[ \t]*)" + old + r"([ \t]*)$",
                lambda m: m[1] + computed + m[2],
                header,
                flags=re.MULTILINE,
            )
            text = header + separator + body
        elif candidate.suffix == ".py":
            text = re.sub(
                r"""(^[ \t]*__version__[ \t]*=[ \t]*["'])""" + old + r"""(["'])""",
                lambda m: m[1] + computed + m[2],
                original,
                flags=re.MULTILINE,
            )
        else:
            text = _rewrite_sections(original, old, computed, name == "pyproject.toml")
        if text != original:
            updates[path] = text
    return updates


def _rewrite_sections(text: str, old: str, computed: str, toml: bool) -> str:
    """Replace version assignments only inside recognized TOML or INI sections."""
    section = ""
    lines = []
    allowed = {"project", "tool.poetry"} if toml else {"metadata"}
    if toml:
        pattern = r"""(^[ \t]*version[ \t]*=[ \t]*["'])""" + old + r"""(["'])"""
    else:
        pattern = r"(^[ \t]*version[ \t]*=[ \t]*)" + old + r"([ \t]*(?:\r?\n)?$)"

    for line in text.splitlines(keepends=True):
        match = re.match(r"\s*\[([^\]]+)\]", line)
        if match:
            section = match[1]
        if section in allowed:
            line = re.sub(pattern, lambda m: m[1] + computed + m[2], line)
        lines.append(line)
    return "".join(lines)
