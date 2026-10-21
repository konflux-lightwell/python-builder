# Python pipeline tasks

These tasks port the Python build and sdist ingestion logic from
`lightwell-konflux-orchestrator` revision
`5a3e8361001e12a925e2a27e7846699ea4fc9c7e` to the installed `lightwell-python` CLI.
Each TaskRun invokes one Python entrypoint for its build logic. Trusted artifact
restore and creation remain separate steps using the original digest-pinned
`quay.io/konflux-ci/build-trusted-artifacts` images.

| Definition | Task resource name | Python command |
| --- | --- | --- |
| [source-to-sdist](source-to-sdist/0.2/source-to-sdist.yaml) | `source-to-sdist` | `lightwell-source-to-sdist` |
| [python-fromager-build-wheels](python-fromager-build-wheels/0.2/python-fromager-build-wheels.yaml) | `python-fromager-build-wheels` | `lightwell-build-wheels` |
| [python-fromager-build-sdist](python-fromager-build-sdist/0.2/python-fromager-build-sdist.yaml) | `fromager-build-sdist` | `lightwell-build-sdist` |

The sdist Task resource retains its original name, `fromager-build-sdist`, even
though its catalog directory includes the `python-` prefix. Build-index generation and
attachment live in fath-cuan under `tekton/task/attach-build-index`.

Version `0.2` introduces the JSON configuration handoff, an execution-image
parameter, and explicit registry credentials. Source preparation and wheel
building must migrate together. Task result names and trusted-artifact storage,
expiration, and CA parameters retain their existing contracts. The declarations
target Tekton Pipelines 1.0 or newer.

## Execution image

Every task requires `PYTHON_BUILDER_IMAGE`. Supply an immutable
`registry/repository@sha256:...` reference to an image built with this package
installed. There is deliberately no default: the previous plumbing-builder
image pin does not contain the new commands.

| Task | Additional executables required on PATH |
| --- | --- |
| `source-to-sdist` | Git |
| `python-fromager-build-wheels` | Fromager, Git, auditwheel, ORAS, cosign |
| `fromager-build-sdist` | Fromager, Git, ORAS |

Use the Containerfile's `python-builder` target for source preparation, wheel
builds, and sdist normalization.
The image parameter can have a different value on each PipelineTask. See the
[image build instructions and dependency audit](../../docs/image.md).

Fromager tasks also need the builder's native toolchain, patches, and package
plugins. The wheels task expects its global overrides under
`/usr/local/share/fromager/overrides`. These definitions do not install packages
at task runtime. The Containerfile installs the task package and runs
runtime checks during the build. Build and publish the image before passing its
digest as `PYTHON_BUILDER_IMAGE`.

## Credentials and inputs

`source-to-sdist` retains the required `git-auth` workspace with `username` and
`password` files for the build-config clone.

The wheels and sdist tasks require a read-only `registry-auth`
workspace. Bind a `kubernetes.io/dockerconfigjson` Secret containing
`.dockerconfigjson`; its credentials must allow pushes to the final image
repository. The CLI receives that mounted file through `--auth-file`, selects
the most specific repository credential, and creates temporary private auth
files for ORAS/cosign.

For example, add this binding to the consuming TaskRun:

```yaml
workspaces:
  - name: registry-auth
    secret:
      secretName: my-image-push-secret
```

In a PipelineTask, map
`registry-auth` to a Pipeline workspace and bind that workspace to the Secret
in the PipelineRun. Also keep the required OCI credentials attached to the
TaskRun service account: the trusted-artifact images retain their own credential
discovery. A registry workspace does not replace that service-account setup.

The wheels task retains the optional Pulp Secret mount and its
`PULP_SECRET_NAME` parameter. Set `WHEEL_SERVER_URL` to an empty string for
Validated builds. `LIGHTWELL_BUILDS_TAG` remains a compatibility parameter on
source preparation; version calculation uses tags in the restored Git history.

## Artifact handoffs

All steps share `/var/workdir`. Python copies restored inputs into isolated
writable scratch space and stages outputs at the paths consumed by the next
trusted-artifact step. Only trusted-artifact `create` steps emit
`SDISTS_ARTIFACT` and `MANIFEST_ARTIFACT`. Final image, SBOM, and provenance
results come from the CLI. The wheels task retains `MANIFEST_ARTIFACT` for the
existing downstream index consumer.

See the [migration contract](../../docs/migration.md) for paths and result ownership.

## Validation and rollout

`uv run pytest tests/unit/test_tekton_tasks.py` checks YAML uniqueness, variable
references, CLI argument dispatch for each build type, and trusted-artifact
path/result ownership. These tests run in the
package CI without contacting registries or a cluster.

The original orchestrator tasks and pipelines are unchanged. Before adopting
these definitions, build and publish the execution image, pass its digest,
wire the registry workspace, and validate representative TaskRuns in staging.
Then publish the task bundles and update the orchestrator's bundle references.
