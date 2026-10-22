# Lightwell Python Builder

Python entrypoints and a container image for Lightwell's build and sdist ingestion
pipelines. Each Tekton task runs one CLI command; trusted-artifact `use` and
`create` remain separate steps in `quay.io/konflux-ci/build-trusted-artifacts`.

`Containerfile` builds the original native builder. `Containerfile.cli` builds
the Python task image on the pinned plumbing-builder base.

| Task | Command | Purpose |
| --- | --- | --- |
| Source preparation | `lightwell-source-to-sdist` | Resolve build config/version and stage a deterministic sdist |
| Wheel build | `lightwell-build-wheels` | Build and verify wheels, publish wheels/SBOMs, and stage a build manifest |
| Sdist ingestion | `lightwell-build-sdist` | Normalize an acquired sdist and publish it with origin evidence |

Build-index generation and attachment belong to **fath-cuan**.

- [Image build and runtime requirements](docs/image.md)
- [Tekton tasks and credentials](tekton/tasks/README.md)
- [Handoffs and migration compatibility](docs/migration.md)

## Development

Requires Python 3.11.4+ and uv; development defaults to Python 3.12.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing
uv build
```

CI runs these checks on Python 3.11–3.14 with a 90% coverage minimum. Use
Google-style docstrings and commit `uv.lock` when dependencies change.

Task code lives in `src/lightwell_python/`, unit tests in `tests/unit/`, and task
YAML in `tekton/tasks/`. Existing native build scripts, `requirements*.txt`, and
`overrides/` have a separate lifecycle; the task image inherits those tools from
its pinned plumbing-builder base.

## Running a task

Run inside the builder image, after restoring the source repository:

```sh
lightwell-source-to-sdist \
  --package ntplib --version 0.4.0 --build-type Backport \
  --source-dir /var/workdir/source-repo-readonly \
  --build-configs-repo-url https://example.org/build-configs.git \
  --build-configs-revision main \
  --sdists-output /var/workdir/sdists-repo \
  --workdir /var/workdir --results-dir /tekton/results
```

Use `--help` for all options. Options also accept uppercase environment variables
(`--build-type` → `BUILD_TYPE`); command-line arguments take precedence.

Each invocation retains a scratch directory under `--workdir`. Use fresh result
and handoff paths. INFO logs show stages, timings, versions, per-wheel progress,
and output paths without requiring `--verbose`.
