# Migration contract

This package was extracted from these workspace revisions:

| Repository | Revision | Source |
| --- | --- | --- |
| lightwell-konflux-orchestrator | `5a3e8361001e12a925e2a27e7846699ea4fc9c7e` | Four Python task definitions under `tekton/tasks/` |
| plumbing | `3a5d00d81e01b8ab6f758343cf9f56041dcfc16b` | `builder/scripts/build-wheels`, `collect-build-files`, and wheel SBOM helpers |
| calunga-python | `b94994abee416da9b462a48dc26652097ec3eec7` | Build-config validation and version resolvers |

The orchestrator and plumbing repositories are unchanged. No task bundles,
container images, pipeline references, releases, or remote repositories have
been published by this change.

## Placement in python-builder

The package is integrated alongside the existing builder tree from
`konflux-lightwell/python-builder` at `a7fe1e3` on `main`. The upstream
Containerfile, native build scripts, dependency pins, Fromager overrides,
legacy commands, and container tests retain their existing paths.
`src/lightwell_python/` holds the task package and `tests/unit/` holds its tests.
Root-level uv, Ruff, pytest, and CI configuration applies to that package.
Python distributions include the package, unit tests, and package documentation;
the builder toolchain and its separate plugin are distributed through the image.

## Scope and runtime boundaries

Source preparation and wheel building in `python-build`, plus normalization
in `python-sdist-ingest`, each have one Python entrypoint.
Build-index generation and attachment have moved to the fath-cuan repository,
with its own `fath-cuan-attach-build-index` entrypoint and
`tekton/task/attach-build-index` definition. Shared catalog tasks
(git clone, upstream source acquisition, ClamAV) retain their existing owners.
`oci-verify-import` belongs to the external artifact import flow and is outside
this Python source/wheel pipeline migration.

Task modules implement build logic between separate trusted-artifact steps.
`quay.io/konflux-ci/build-trusted-artifacts` retains ownership of `use` and `create`,
including transport, archive packing/extraction, and integrity verification.
Python consumes restored filesystem paths and stages outputs at explicit paths. Dataclasses represent
package identity, computed versions, and resolved config. Runner and Registry are
injectable adapters, allowing unit tests to exercise complete task flows without
network services. There is no generic workflow engine or shell evaluation.

External domain tools remain required: Git, Fromager, auditwheel (native wheels),
ORAS, and cosign. Tool versions and builder plugins must be pinned in
the future Python execution image. The trusted-artifact steps keep their existing
image and digest pins. The package supports Python >=3.11.4.
Runtime testing here covers local Git and ORAS; full builds, authenticated registry
pushes, vulnerability resolution, and cluster execution need staging validation.

## Trusted artifact handoffs

| Task | Existing `use` step restores | Python input/output arguments | Existing `create` step publishes |
| --- | --- | --- | --- |
| source-to-sdist | SOURCE_ARTIFACT → `/var/workdir/source-repo-readonly` | `--source-dir /var/workdir/source-repo-readonly --sdists-output /var/workdir/sdists-repo` | `/var/workdir/sdists-repo` → SDISTS_ARTIFACT |
| python-fromager-build-wheels | SDISTS_ARTIFACT → `/var/workdir/sdists-repo` | `--sdists-dir /var/workdir/sdists-repo --manifest-output /var/workdir/build-manifest.json` | `/var/workdir/build-manifest.json` → MANIFEST_ARTIFACT |
| python-fromager-build-sdist | SDISTS_ARTIFACT → `/var/workdir/sdists-repo` | `--sdists-dir /var/workdir/sdists-repo` | None; Python publishes the final sdist OCI image |

Each `use` or `create` step runs in `quay.io/konflux-ci/build-trusted-artifacts`
with the existing pinned digest. For example, source-to-sdist keeps this sequence:

1. Trusted-artifact image, args: `use $(params.SOURCE_ARTIFACT)=/var/workdir/source-repo-readonly`.
2. Python image, command: `lightwell-source-to-sdist`, with the input/output paths above
   and package/version/config arguments. It emits COMPUTED_VERSION, B_VALUE, N_VALUE.
3. Trusted-artifact image, args: `create --store $(params.ociStorage) $(results.SDISTS_ARTIFACT.path)=/var/workdir/sdists-repo`.
   Keep IMAGE_EXPIRES_AFTER wired to `$(params.ociArtifactExpiresAfter)` on this step.

For wheels, the final `create` step similarly uses
`$(results.MANIFEST_ARTIFACT.path)=/var/workdir/build-manifest.json`.
Python stages the manifest only after image and SBOM publication succeed.
All steps share the workdir volume. The declared handoff paths are stable even
though Python's internal scratch directory is unique per invocation. Use fresh
handoff/output paths for each task run. Run `create` only after Python succeeds;
the task succeeds only when its final required step succeeds.

## Preserved behavior

- Backport: B counts ingestion→HEAD, N=0. Novel: B counts ingestion→baseline,
  N counts baseline→HEAD. N=0 omits the `.n.N` suffix. Validated uses the original
  upstream version without rewriting.
- Package settings, override environment, explicit rewrite rules, and allowed
  artifacts resolve from one selected build-config snapshot. Validation retains
  the complete original config schema, including declared-version coverage.
- Offline sdist aliases include canonical, underscore, and PKG-INFO original-case
  names with both upstream and computed versions. Archives live in downloads/;
  Fromager creates builds/ itself.
- Fromager bootstrap and build-sequence both run, using the existing global
  overrides and optional cache server. Native x86_64 wheels get auditwheel repair;
  system dependencies and final wheel filenames enrich SPDX SBOMs.
- The full dependency closure remains in the wheels artifact. Source-build
  allowlists constrain remediated builds. Validated builds allow the closure.
  Produced entries take precedence over identical cached entries.
- Ingestion carries acquired origin/provenance evidence unchanged, publishing a
  normalized root sdist and the same byte-binding transformation schema.
- Fath-cuan owns index generation, attachment, and companion-PURL handling.
  The wheels task continues to emit the produced/cached build manifest for it.
- Existing task result names remain unchanged. Trusted artifact URIs identify
  archive **blob** digests, whereas IMAGE_DIGEST identifies an OCI **manifest**.

## Deliberate corrections

- JSON replaces `version-env.sh`/`backend-env.sh`; config scripts are no longer
  copied into trusted artifacts or executed. Source and wheel tasks must be
  switched together. Previously produced shell-based handoffs are not accepted.
- Source archives stream to disk and preserve executable bits and safe symlinks.
  Ownership/timestamps remain normalized. Output bytes can differ from the old
  archive implementation, which flattened file modes and dereferenced symlinks.
- Default TOML/INI rewriting only updates project/metadata sections, avoiding
  unrelated tool settings. Explicit rewrites validate every change before writes.
  Malformed text, escaping paths, and unmatched explicit patterns fail.
- Git lineage must actually connect ingestion, baseline, and HEAD. Missing tags
  or unrelated histories cannot generate a plausible but incorrect version.
- Sdist lookup uses parsed exact package/version identities instead of prefix
  comparisons. Normalization requires exactly one matching newly generated sdist.
- Missing, malformed, or empty build summaries fail instead of logging warnings.
  Primary wheel filename, METADATA identity/version, and SPDX version must agree;
  the primary SPDX document is mandatory before any publication.
- Wheel RECORD is regenerated as CSV with hashes for every member, including
  renamed/enriched SBOMs. Signed wheels and duplicate members are rejected.
- Registry parsing preserves ports. Results hash the manifest ORAS exported at
  push time, avoiding mutable-tag races. Result files are written after required
  publication/attachment steps succeed.
- Network retries are bounded; build processes are not blindly retried. Each
  subprocess has a configurable timeout and executes an argument vector.
- Trusted artifact transport and verification stay with the existing
  `build-trusted-artifacts` image. Python copies restored inputs into writable
  scratch space when mutation is needed. Final-image credentials use private
  temporary files, with no global Git config or HOME changes.

## Next integration stage

Extend this repository's Containerfile to install this wheel and supply the
remaining domain tools in a pinned Python execution image. Retain each task's existing trusted-artifact
`use` and `create` steps, their image pins, credentials, CA mounts, and params.
Replace only the intervening build-logic steps with the corresponding Python
entrypoint. `ociStorage`, `ociArtifactExpiresAfter`, SOURCE_ARTIFACT,
SDISTS_ARTIFACT, and MANIFEST_ARTIFACT remain Tekton transport parameters/results;
they are not Python CLI arguments.

The original LIGHTWELL_BUILDS_TAG was only diagnostic; version calculation
continues to use canonical ingestion and baseline tags from restored Git history.

Exercise a pure Python package, a native package, an explicit version-override
package, Backport, Novel, Validated, and ingestion in staging. Compare result
names, artifact contents, metadata versions, build index, and release-consumer
behavior before updating published bundle digests in the orchestrator.

## References

- [uv CI integration](https://docs.astral.sh/uv/guides/integration/github/)
- [Ruff settings](https://docs.astral.sh/ruff/settings/)
- [ORAS push and exported manifests](https://oras.land/docs/commands/oras_push/)
- [Konflux trusted artifact creation](https://github.com/konflux-ci/build-trusted-artifacts/blob/main/create-oci.sh)
- [Konflux trusted artifact restoration](https://github.com/konflux-ci/build-trusted-artifacts/blob/main/use-oci.sh)
