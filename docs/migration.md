# Migration contract

The Python package replaces task Bash logic for source preparation, wheel builds,
and sdist normalization. Git, Fromager, auditwheel, ORAS, and cosign remain external
tools. Build-index generation belongs to fath-cuan; clone, acquisition, and ClamAV
tasks retain their existing owners.

## Trusted artifact handoffs

Tekton owns transport through separate `quay.io/konflux-ci/build-trusted-artifacts`
steps. Python accepts filesystem paths, copies restored inputs into scratch space,
and stages outputs for `create`. It never handles trusted-artifact URIs.

| Task | Restored input | Python output | `create` result |
| --- | --- | --- | --- |
| `source-to-sdist` | `SOURCE_ARTIFACT` → `/var/workdir/source-repo-readonly` (`--source-dir`) | `/var/workdir/sdists-repo` (`--sdists-output`) | `SDISTS_ARTIFACT` |
| `python-fromager-build-wheels` | `SDISTS_ARTIFACT` → `/var/workdir/sdists-repo` (`--sdists-dir`) | `/var/workdir/build-manifest.json` (`--manifest-output`) | `MANIFEST_ARTIFACT` |
| `fromager-build-sdist` | `SDISTS_ARTIFACT` → `/var/workdir/sdists-repo` (`--sdists-dir`) | Final sdist OCI image and evidence | None |

The source handoff contains sdists, a build-config snapshot and commit, and backend
settings as JSON. Source and wheel tasks **must migrate together**: old shell-based
handoffs are incompatible. The wheel manifest records produced/cached packages
for fath-cuan; produced entries take precedence over identical cached entries.

Use fresh handoff and result paths for every run. Python writes success results
only after required processing and publication, including wheel SBOM attachment.
Run `create` only after Python succeeds. A failed attachment may leave an uploaded
image, but no successful manifest handoff. Result names remain unchanged; Python
result files have no trailing newline. Trusted-artifact URIs identify blob digests,
while `IMAGE_DIGEST` identifies the published OCI manifest.

## Build behavior

| Build type | Version | Dependency policy |
| --- | --- | --- |
| Backport | `upstream+rhlw.B`; B counts ingestion→HEAD | Source-build allowlist and optional wheel cache |
| Novel | `upstream+rhlw.B.n.N`; B counts ingestion→baseline, N counts baseline→HEAD | Same as Backport |
| Validated | Upstream version | Full dependency closure; wheel cache disabled |

The ingestion tag is `<package>/<version>` and the baseline tag is
`baseline/<version>`. Omit `.n.N` when N=0; validate ancestry before counting.
`LIGHTWELL_BUILDS_TAG` is retained for compatibility; Git history drives versioning.

- Config schema, version coverage, environment, rewrites, and allowed artifacts
  come from one selected snapshot.
- Sdist cache aliases cover canonical, underscore, and original-case package names
  with upstream and computed versions. Fromager runs bootstrap and build-sequence.
- Native x86_64 wheels undergo auditwheel repair; SPDX SBOMs gain system dependencies
  and wheel filenames. The published artifact includes the dependency closure.
- With `AI_ASSISTED=true`, the attached SBOM copy gets one document-level SPDX 2.3
  annotation that lists `AI_MODELS`. Embedded wheel SBOMs are never modified.
- Sdist normalization preserves acquired origin evidence and records input/output
  hashes in `sdist-transformation.json`.

## Deliberate corrections

- JSON replaces executable config handoffs; config repository scripts are not run.
- Archives preserve executable bits and safe symlinks with normalized timestamps
  and ownership. Their bytes may differ from the original Bash implementation.
- Metadata rewrites validate paths and matches before writing. Default TOML/INI
  edits target project/metadata sections only.
- Missing/invalid build summaries, ambiguous sdists, and mismatched primary wheel
  filename, METADATA, or SPDX versions fail the task. A primary SBOM is required.
- Wheel RECORD is regenerated as CSV with hashes. Only the root `.dist-info` is
  rewritten; vendored metadata remains intact. Duplicate members and signatures
  on the outer wheel are rejected.
- `AI_ASSISTED` accepts only `true` or `false` (case-insensitive, surrounding spaces
  ignored; empty means false). Other values fail the wheel task instead of being
  treated as false. With `AI_ASSISTED=true`, `AI_MODELS` must be empty or
  comma-separated IDs using only letters, digits, and `. _ : / @ + -`; other values
  fail the task before the build starts, so they cannot add fields to the annotation.
- Image digests come from the manifest ORAS exports at push time. Registry retries
  are bounded; build commands are not retried. Credentials remain in private
  temporary files and never enter command arguments or logs.

## Migration sources

| Repository | Revision |
| --- | --- |
| lightwell-konflux-orchestrator | `5a3e8361001e12a925e2a27e7846699ea4fc9c7e` |
| plumbing | `3a5d00d81e01b8ab6f758343cf9f56041dcfc16b` |
| calunga-python | `b94994abee416da9b462a48dc26652097ec3eec7` |

See [task configuration](../tekton/tasks/README.md) and [image builds](image.md).
