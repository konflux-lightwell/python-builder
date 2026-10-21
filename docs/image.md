# Task execution image

The root [Containerfile](../Containerfile) extends the digest-pinned
`quay.io/redhat-user-workloads/calunga-tenant/plumbing-builder` image with the
Python task package and publication clients. Its default `python-builder` target
supports source preparation, wheel builds, and sdist normalization. The native
toolchain, Python, Fromager, settings, patches, and plugins come from the base
image; this Containerfile does not compile or replace them.

Use the resulting image digest as `PYTHON_BUILDER_IMAGE` on the
[Tekton tasks](../tekton/tasks/README.md). Build-index generation and attachment live in the
fath-cuan repository and use its own image and Tekton task.

## Dependency audit

The task modules directly invoke these executables:

| Dependency | Used for | Supplied by |
| --- | --- | --- |
| Python 3.11.4+ | Task CLI and package/archive manipulation | Plumbing base's CPython 3.12 |
| `packaging==26.3`, `PyYAML==6.0.3` | Version/filename parsing and build settings | Isolated `/opt/lightwell` venv, installed from `uv.lock` |
| Git | Config checkout and version lineage | Plumbing base |
| Fromager 0.81.0 | Bootstrap, wheel builds, sdist normalization | Plumbing base, at `/src/venv` |
| auditwheel and patchelf | Native wheel repair and library inspection | Plumbing base |
| ORAS | Final OCI pushes and tags | Binary copied from the original task's digest-pinned Konflux ORAS image |
| cosign 3.0.2 | SPDX SBOM attachment | Copied from the upstream `cosign-bin` stage |

Native package builds also depend on GCC/G++/GFortran, Rust/Cargo, make, CMake,
pkg-config, and the source-built libraries already assembled by the native
plumbing base. These include OpenSSL, libffi, OpenBLAS, compression and image libraries,
Arrow, and FFmpeg. Package-specific requirements still come from the selected
Fromager settings and build backend. The pinned base supplies the `faiss_cpu`
plugin, with settings and patches at `/usr/local/share/fromager/overrides`.
It does not include the newer `debugpy` override. Local `overrides/`,
`build_scripts/`, and root `requirements*.txt` are retained from the original
repository but are not installed by this Containerfile. Update the base digest
to adopt newer native libraries, Fromager versions, or package overrides.

The `task-package-build` stage uses the base image's uv to run
`uv sync --locked --no-dev --no-editable` into `/opt/lightwell`. Runtime versions
and hashes come from `uv.lock`; Hatchling is supplied through the isolated build
environment defined by `pyproject.toml`. The final stage copies only the installed
environment, so the package works without its source checkout and development
tools stay out of the runtime. Both stages use the same base image and environment
path to keep the interpreter and entrypoint paths valid.

This follows uv's [multi-stage image pattern](https://docs.astral.sh/uv/guides/integration/docker/#non-editable-installs).
Fromager retains its separate environment and Python command resolution.

Trusted artifact transport remains in Tekton's separate
`quay.io/konflux-ci/build-trusted-artifacts` steps. The builder does not install or
invoke trusted-artifact transport helpers.

## Build

Run this command from the repository root. The current native builder and
wheel-repair implementation target Linux x86_64:

```console
podman build --platform linux/amd64 --target python-builder \
  -f Containerfile -t localhost/lightwell-python-builder:dev .
```

The default base digest is the one pinned by the existing orchestrator tasks:
`sha256:d8355de03d57adef033743da1f1a3bb2f0ff038192e3220b155e9dd7dba5d37c`.
Registry metadata identifies its plumbing source revision as
`77494e6323327dcf45c1ffdd46788a83debbe8c1`. That revision pins Fromager 0.81.0
and uv 0.10.11. The repository does not currently publish a `latest` tag.

To select another compatible plumbing release, supply a digest-pinned base:

```console
podman build --platform linux/amd64 --target python-builder \
  --build-arg 'BUILDER_IMAGE=quay.io/redhat-user-workloads/calunga-tenant/plumbing-builder@sha256:<digest>' \
  -f Containerfile -t localhost/lightwell-python-builder:dev .
```

The base must provide `/src/venv/bin/python`, `/src/venv/bin/uv`, Fromager, the
native tools, and the overrides. Package assembly reuses those installed tools;
there are no native compilation stages in this Containerfile.

## Smoke checks

The image runs `tests/check_runtime.py` during its build as UID 1001. It checks the
three builder entrypoints, required CLI options, writable scratch space, and a
writable home directory for build-tool caches.
It also requires `faiss_cpu`, loads all installed Fromager plugins, and probes the compiler, repair, and
publication tools. All probes are local help/version operations; they do not
publish anything or contact a registry.

Re-run them against a built image:

```console
podman run --rm --user 1001 --entrypoint /opt/lightwell/bin/python \
  localhost/lightwell-python-builder:dev \
  /usr/local/libexec/lightwell-check-runtime.py --profile builder
```

Native-toolchain validation belongs to the plumbing base build.
Run representative Python, native-wheel, and sdist ingestion tasks in staging
before replacing published execution-image digests.

## Updating Python dependencies

Declare runtime dependencies in `pyproject.toml` and commit the updated `uv.lock`.
Development tools remain in the `dev` dependency group and are excluded from the
image. Use the same uv workflow for local development and image assembly:

```console
uv lock
uv sync --locked
```

The image build checks lock consistency, installs the project as a regular package,
and runs `uv pip check` against the installed environment. There are no separate
requirements files for the task package or its image build.
