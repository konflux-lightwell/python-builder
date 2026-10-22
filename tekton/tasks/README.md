# Python pipeline tasks

Version 0.2 runs one Python entrypoint per task, with separate trusted-artifact
`use`/`create` steps. Requires Tekton Pipelines 1.0+.

| Definition | Task resource name | Command |
| --- | --- | --- |
| [Source preparation](source-to-sdist/0.2/source-to-sdist.yaml) | `source-to-sdist` | `lightwell-source-to-sdist` |
| [Wheel build](python-fromager-build-wheels/0.2/python-fromager-build-wheels.yaml) | `python-fromager-build-wheels` | `lightwell-build-wheels` |
| [Sdist normalization](python-fromager-build-sdist/0.2/python-fromager-build-sdist.yaml) | `fromager-build-sdist` | `lightwell-build-sdist` |

The sdist resource name intentionally omits `python-`. Build-index attachment is
maintained in fath-cuan's `tekton/task/attach-build-index`.

## Configuration

- Set `PYTHON_BUILDER_IMAGE` to a digest-pinned image built from this repo's
  [Containerfile](../../Containerfile). The task YAML has no default image.
- Bind source preparation's `git-auth` workspace to `username`/`password` files
  for the build-config clone.
- Registry pushes use the existing Konflux `select-oci-auth` mechanism. No extra
  registry workspace or Secret is needed. Keep service-account credentials
  available to trusted-artifact steps. The pinned helper reads `~/.docker/config.json`;
  local CLI runs can override it with `--auth-file`.
- Wheels accept the optional `PULP_SECRET_NAME` mount and `WHEEL_SERVER_URL`.
  Leave the wheel server empty for Validated builds.
- CA configuration uses `caTrustConfigMapName`/`caTrustConfigMapKey`;
  trusted-artifact storage/expiry uses `ociStorage`/`ociArtifactExpiresAfter`.

For local CLI runs, use `--ca-bundle` for internal CA trust and `--git-auth` for
the directory containing Git credentials. `--pulp-secret` accepts `username`/
`password` or `SERVICE_ACCOUNT_USERNAME`/`SERVICE_ACCOUNT_PASSWORD` files.

## Integration

Migrate source preparation and wheel building together: version 0.2 uses a JSON
configuration handoff. All steps share `/var/workdir`; only `create` emits
`SDISTS_ARTIFACT` and `MANIFEST_ARTIFACT`. The CLI writes final image/SBOM results.
See [handoff paths and compatibility](../../docs/migration.md#trusted-artifact-handoffs).

Build and publish the [execution image](../../docs/image.md), publish task bundles,
and pin their references in consuming pipelines. Validate representative TaskRuns
in staging. Local YAML/CLI wiring checks run with:

```sh
uv run pytest tests/unit/test_tekton_tasks.py
```
