# Match the native build environment currently used by orchestrator.
ARG BUILDER_IMAGE=quay.io/redhat-user-workloads/calunga-tenant/plumbing-builder@sha256:d8355de03d57adef033743da1f1a3bb2f0ff038192e3220b155e9dd7dba5d37c

FROM quay.io/konflux-ci/oras:latest@sha256:1beeecce012c99794568f74265c065839f9703d28306a8430b667f639343a98b AS oras-bin
FROM ghcr.io/sigstore/cosign/cosign:v3.0.2@sha256:b29487e48205d875c324c79583e2806d9d269c0fa299e0861bbec023d8430c8b AS cosign-bin

# Install the package and locked runtime dependencies without development tools.
FROM ${BUILDER_IMAGE} AS task-package-build
# Root is needed only to assemble this intermediate environment.
# hadolint ignore=DL3002
USER 0
WORKDIR /opt/lightwell-source
ENV UV_PROJECT_ENVIRONMENT=/opt/lightwell \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN /src/venv/bin/uv sync --locked --no-dev --no-editable --python /src/venv/bin/python && \
    /src/venv/bin/uv pip check --python /opt/lightwell/bin/python


# Default target: native package builds, source preparation, and normalization.
FROM ${BUILDER_IMAGE} AS python-builder
USER 0
COPY --from=oras-bin /usr/bin/oras /usr/local/bin/oras
COPY --from=oras-bin /usr/local/bin/select-oci-auth /usr/local/bin/select-oci-auth
COPY --from=oras-bin /usr/local/bin/get-reference-base /usr/local/bin/get-reference-base
COPY --from=oras-bin /usr/bin/yq /usr/bin/yq
# Publication clients are installed here; the base supplies the build toolchain.
COPY --from=cosign-bin /ko-app/cosign /usr/local/bin/cosign
# Both stages share the base interpreter and the environment's absolute path.
COPY --from=task-package-build /opt/lightwell /opt/lightwell
COPY tests/check_runtime.py /usr/local/libexec/lightwell-check-runtime.py
# Fromager and existing builder scripts keep their original Python interpreter.
ENV PATH=${PATH}:/opt/lightwell/bin
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/home/builder
RUN mkdir -p /var/workdir /home/builder && \
    chown 1001:0 /var/workdir /home/builder && chmod 0775 /var/workdir /home/builder
WORKDIR /var/workdir
USER 1001
RUN /opt/lightwell/bin/python /usr/local/libexec/lightwell-check-runtime.py --profile builder
ENTRYPOINT ["manylinux-entrypoint"]
CMD ["lightwell-build-wheels", "--help"]
