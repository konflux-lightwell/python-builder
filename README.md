# Lightwell Python Builder

This repository contains the Lightwell Python task image and the
`lightwell-python` task package. The image inherits its native toolchain and
Fromager environment from a digest-pinned plumbing-builder base. The package
provides one installed Python command per build or sdist ingestion task.
Trusted artifact transport stays in separate Tekton steps using
`quay.io/konflux-ci/build-trusted-artifacts`: `use` before Python and `create`
after Python when the task produces a trusted artifact.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Containerfile` | Task image based on the published plumbing-builder image |
| `build_scripts/`, `manylinux-entrypoint` | Retained native builder sources; not used by the task image build |
| `requirements.in`, `requirements.txt`, `requirements-build.txt` | Retained native builder dependency pins; the task image inherits its Fromager environment from the base |
| `overrides/` | Retained Fromager sources; the task image uses the settings, patches, and plugins already in its base |
| `scripts/` | Existing builder commands used by the current pipelines |
| `src/lightwell_python/` | Extracted Python task implementations and shared modules |
| `tests/unit/` | Unit tests for the task package, run by pytest |
| `tekton/tasks/` | Version 0.2 task definitions invoking the Python CLI with separate trusted-artifact steps |
| `tests/check_runtime.py`, `docs/image.md` | Task image runtime checks and build instructions |
| Other files under `tests/` | Existing container/toolchain checks, driven by `tests/run_tests.sh` |
| `pyproject.toml`, `uv.lock` | Task package metadata, dependencies, and development tools |
| `docs/migration.md` | Task handoffs and the remaining image/orchestrator integration work |

The task package lives alongside the existing builder components. The
Containerfile installs the new entrypoints in an isolated environment in the
default `python-builder` target. See the
[image build instructions](docs/image.md) and dependency audit.
The [Tekton tasks](tekton/tasks/README.md) require that image through
`PYTHON_BUILDER_IMAGE` and document the credential/workspace bindings.

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

Commit `uv.lock` whenever task package dependencies change. The image installs the package
and its runtime dependencies from the same lock with `uv sync --locked --no-dev
--no-editable`. Fromager versions are managed by the selected plumbing-builder base.
CI checks the task package on Python 3.11–3.14, lint,
formatting, types, coverage (90% minimum), and distribution builds. Tests use temporary files,
real local Git repositories, and injected adapters; no production credentials or
registry writes are required.

Ruff is scoped to `src/`, `tests/unit/`, and the image check at `tests/check_runtime.py`;
pytest collects `tests/unit/`.
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
Fromager overrides/plugins and native build dependencies. This repository's
Containerfile inherits them from plumbing-builder and adds the task CLI. This package does
not install tools at task runtime. Build and publish the execution image,
then supply its digest when integrating the ported tasks with orchestrator.

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
