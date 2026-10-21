# Lightwell Python Builder

This repository contains the Lightwell Python builder image, its native toolchain
and Fromager overrides, and the `lightwell-python` task package. The package
provides one installed Python command per build or sdist ingestion task.
Trusted artifact transport stays in separate Tekton steps using
`quay.io/konflux-ci/build-trusted-artifacts`: `use` before Python and `create`
after Python when the task produces a trusted artifact.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Containerfile`, `build_scripts/`, `manylinux-entrypoint` | Existing builder image and native toolchain setup |
| `requirements.in`, `requirements.txt`, `requirements-build.txt` | Existing builder runtime and build dependency pins |
| `overrides/` | Fromager settings, patches, and the separate `calunga-package-plugins` package |
| `scripts/` | Existing builder commands used by the current pipelines |
| `src/lightwell_python/` | Extracted Python task implementations and shared modules |
| `tests/unit/` | Unit tests for the task package, run by pytest |
| Other files under `tests/` | Existing container/toolchain checks, driven by `tests/run_tests.sh` |
| `pyproject.toml`, `uv.lock` | Task package metadata, dependencies, and development tools |
| `docs/migration.md` | Task handoffs and the remaining image/orchestrator integration work |

The task package lives alongside the existing builder components. The current
Containerfile still installs the commands in `scripts/`; installing the new
entrypoints into the execution image is part of the next migration stage.

## Task entrypoints

| Orchestrator task | Python entrypoint |
| --- | --- |
| `source-to-sdist` | `lightwell-source-to-sdist` |
| `python-fromager-build-wheels` | `lightwell-build-wheels` |
| `python-fromager-build-sdist` | `lightwell-build-sdist` |

Build-index generation and attachment belong to the fath-cuan repository,
including its `fath-cuan-attach-build-index` CLI and `tekton/task/attach-build-index`
definition. The wheels task retains `MANIFEST_ARTIFACT` for that consumer.

Use `--help` on any command for its complete input contract. Arguments also read
uppercase underscore environment variables (`--build-type` → `BUILD_TYPE`).
Python result files retain the existing Tekton names and contain no trailing newline.
`SDISTS_ARTIFACT` and `MANIFEST_ARTIFACT` remain owned by the trusted-artifact
`create` steps. Python accepts restored filesystem paths, never trusted artifact URIs.

## Development

Requires Python 3.11.4+ (3.12 is the development default) and uv.

```console
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing
uv build
```

Commit `uv.lock` whenever task package dependencies change. The builder's
`requirements*.txt` files continue to manage its separate runtime environment.
CI checks the task package on Python 3.11–3.14, lint,
formatting, types, coverage (90% minimum), and distribution builds. Tests use temporary files,
real local Git repositories, and injected adapters; no production credentials or
registry writes are required.

Ruff is scoped to `src/` and `tests/unit/`, and pytest collects `tests/unit/`.
The existing container/toolchain checks require the built image and its native
dependencies; they are separate from this package's CI checks.

Use Google-style docstrings to explain inputs, return values, side effects, and
failure conditions where relevant. Ruff enforces the convention and checks that
documented argument lists are complete. Prefer named intermediate values and
small, focused helpers when nested expressions obscure the task flow.
Run `uv run ruff check . --fix` and `uv run ruff format .` before submitting changes.

## Running tasks

```console
uv run lightwell-source-to-sdist \
  --package ntplib --version 0.4.0 --build-type Backport \
  --source-dir /var/workdir/source-repo-readonly \
  --build-configs-repo-url https://example.org/build-configs.git \
  --build-configs-revision <commit> \
  --sdists-output /var/workdir/sdists-repo \
  --workdir /var/workdir --results-dir /tekton/results
```

Run this command after the trusted-artifact `use` step has restored the repository.
The following `create` step publishes `/var/workdir/sdists-repo` and writes
`SDISTS_ARTIFACT`. See the [handoff contract](docs/migration.md#trusted-artifact-handoffs)
for each task's paths and result ownership.

Build logic, JSON handling, hashing, package sdist/wheel manipulation, and final
OCI image publication are Python. Trusted artifact packing, integrity checks,
restoration, and publication stay with the existing trusted-artifact image. External
programs supply domain functionality: Git, Fromager, auditwheel, ORAS, and cosign. Python does not invoke the trusted-artifact image or its helpers,
or run scripts copied from the config repository.
Build backends and Fromager plugins may themselves require native toolchains.

Production execution needs a container supplying those tools, the existing
Fromager overrides/plugins and native build dependencies. Use this repository's
Containerfile and overrides as the builder baseline. This package does
not install tools at task runtime. A combined, pinned task image and orchestrator
YAML integration are the next migration stage; they are not provisioned here.

Optional CA bundles apply through SSL_CERT_FILE, REQUESTS_CA_BUNDLE and
GIT_SSL_CAINFO. Registry credentials come from `--auth-file`, AUTHFILE, or
DOCKER_CONFIG/config.json (default ~/.docker/config.json). Repository-specific
credentials take precedence over registry-wide credentials. `--git-auth` accepts
a mounted directory containing username/password for the HTTPS config clone;
`--pulp-secret` accepts either username/password or SERVICE_ACCOUNT_* files.
Temporary registry credentials and netrc files are private and cleaned on exit.

Each invocation creates an isolated subdirectory of `--workdir`, retained for
inspection. Explicit `--sdists-output` and `--manifest-output` paths live outside
that scratch directory and must not already exist. Restored input directories are
copied to writable scratch space when needed; their contents are left intact.
Use a fresh results directory for each task invocation, as Tekton
does. Failed tasks exit nonzero; successful result files are emitted only after
all required Python operations finish. Tekton must then run the trusted-artifact
`create` step successfully for the entire task to succeed. Registry pushes are not transactional: a later
attachment failure may leave an image uploaded, but does not report success.

See [migration notes](docs/migration.md) for compatibility boundaries and deliberate
behavior corrections.
