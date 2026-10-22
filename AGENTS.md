# AGENTS.md — lightwell-python task package

## Scope

- `src/lightwell_python/` implements the Python pipeline tasks. Read
  `docs/migration.md` for task contracts and deliberate behavior changes.
- `build_scripts/`, `scripts/`, `overrides/`, and native dependency pins are
  retained builder-image tooling with a separate lifecycle. Keep task-package
  changes separate from changes to that tooling. Preserve the original native
  builder definition in `Containerfile`.
- `Containerfile.cli` builds the current task image using the pinned plumbing-builder
  base; it belongs to task-image integration. Local native sources and overrides
  do not participate in this image build. See `docs/image.md` and
  `tests/check_runtime.py` for its runtime contract.
- Trusted-artifact transport (`use`/`create`) belongs to Tekton steps. Python tasks
  consume restored filesystem paths and stage outputs at explicit paths; task
  code must never accept or emit trusted-artifact URIs.

## Invariants

1. Execute subprocesses with argument vectors. Never evaluate shell strings or
   execute scripts supplied by build-config repositories.
2. Pass credential values only through subprocess environments or private 0600
   temporary files cleaned on exit. Never put secrets in argv, logs, error
   messages, or global Git/Docker configuration. Mounted credential inputs stay
   read-only; paths to credential files may be passed to tools.
3. Fail closed on missing, malformed, empty, or ambiguous required data. Raise
   `TaskError` for task-contract violations; appropriate I/O and parsing errors
   may propagate to the CLI's error handler. Do not warn and continue after a
   contract violation. Preserve documented optional inputs and defaults.
4. Write Python success results and final handoff outputs only after all required
   Python processing and publication steps succeed, including SBOM publication
   where required. Handoff paths must not already exist. Tekton `create` consumes
   the staged handoff only after Python succeeds and owns trusted-artifact
   results; the overall task succeeds only after its final required step succeeds.
5. Preserve ADR-0005/0020 versioning as implemented in `versioning.py` and
   documented in `docs/migration.md`: `+rhlw.{B}[.n.{N}]`. Backport counts
   ingestion→HEAD for B and uses N=0. Novel counts ingestion→baseline for B and
   baseline→HEAD for N, using `baseline/{version}`. Omit `.n.N` when N=0.
   Validated uses the upstream version unchanged. Validate ancestry before
   counting commits.
6. Registry results hash the manifest ORAS exports at push time. Never resolve a
   mutable tag to determine the digest of the just-published artifact.
7. Treat restored inputs as read-only. Use `copy_input_tree` to copy into scratch
   before mutation. Plan and validate all explicit rewrites before writing.
8. Build-index generation, attachment, and vulnerability requirements belong to
   `fath-cuan`. Preserve the wheel build-manifest handoff consumed by that task;
   do not reintroduce index generation or its `--require-vuln` option here.

## Conventions

- A new task needs a module in `src/lightwell_python/tasks/`, a dedicated parser
  and handler in `cli.py`, a `PARSER_FACTORIES` entry, a console-entrypoint wrapper
  and `[project.scripts]` entry, a Tekton task definition, unit coverage, and a
  handoff table row in `docs/migration.md`.
- Register task input options through `_option`: the environment fallback uses
  the public option name in `UPPERCASE_UNDERSCORE`, even when `dest` differs.
  Explicit CLI arguments take precedence. `--help` and `--verbose` are CLI-only
  controls.
- Keep compatibility with Python >=3.11.4. Annotate `@contextmanager` generators
  with `Generator[T, None, None]` from `collections.abc`.
- Follow the Ruff configuration in `pyproject.toml` (E, F, I, UP, B, D, SIM and
  D417, with Google-style docstrings) and keep mypy checks passing.
- Task unit tests live in `tests/unit/`. Use injected Runner/Registry fakes and
  temporary filesystem fixtures; local Git is allowed. Unit tests must not need
  network services or real credentials. Preserve the 90% coverage floor. Runtime
  image checks and retained builder tests have their existing separate locations.
- Record behavior changes relative to the orchestrator under “Deliberate
  corrections” in `docs/migration.md`.

## Validation

For task-package code changes, use the existing development checks:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing
uv build
```

For image/runtime changes, also follow `docs/image.md`. Documentation-only
changes need a content and diff review rather than rerunning the code suite.
