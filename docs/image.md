# Task execution image

The [Containerfile.cli](../Containerfile.cli) extends a digest-pinned plumbing-builder
image for Linux x86_64. Publish the result and pass its digest as
`PYTHON_BUILDER_IMAGE` to the [Tekton tasks](../tekton/tasks/README.md).

## Dependencies

| Source | Provides |
| --- | --- |
| Plumbing base | CPython 3.12, Fromager, Git, auditwheel, patchelf, native compilers/libraries, and Fromager overrides |
| `uv.lock` | Task package dependencies, installed in `/opt/lightwell` with `uv sync --locked --no-dev --no-editable` |
| Pinned Konflux ORAS image | ORAS, `select-oci-auth`, `get-reference-base`, and `yq` |
| Pinned cosign image | SPDX SBOM attachment client |

The base supplies `/src/venv/bin/python`, `/src/venv/bin/uv`, and settings/plugins
under `/usr/local/share/fromager/overrides`. Fromager keeps its own environment.
Update the base digest to change native libraries, Fromager, or overrides; local
`build_scripts/`, `overrides/`, and `requirements*.txt` are not used by this build.
For task dependencies, edit `pyproject.toml`, run `uv lock`, and commit `uv.lock`.

## Build and check

```sh
podman build --platform linux/amd64 --target python-builder \
  -f Containerfile.cli -t localhost/lightwell-python-builder:dev .
```

To change the base, add
`--build-arg BUILDER_IMAGE=quay.io/redhat-user-workloads/calunga-tenant/plumbing-builder@sha256:<digest>`.
The exact default image pins are in Containerfile.cli.

The build checks package dependencies and runs [runtime smoke checks](../tests/check_runtime.py)
as UID 1001: CLI options, native tools, Fromager plugins (including `faiss_cpu`),
writable directories, and credential selection using synthetic credentials.
These probes do not contact a registry. To repeat them:

```sh
podman run --rm --user 1001 --entrypoint /opt/lightwell/bin/python \
  localhost/lightwell-python-builder:dev \
  /usr/local/libexec/lightwell-check-runtime.py --profile builder
```

Before rollout, exercise pure Python, native-wheel, and sdist ingestion builds
in staging. Smoke checks do not validate complete package builds.
