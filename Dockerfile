FROM ubuntu:24.04 AS build

ARG TARGETARCH
ARG TERRAFORM_VERSION=1.17.0
ARG TERRAFORM_DOCS_VERSION=0.24.0
ARG TFLINT_VERSION=0.65.0
ARG TRIVY_VERSION=0.76.1
ARG CHECKOV_VERSION=3.3.30

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV TFLINT_PLUGIN_DIR=/opt/tflint/plugins

# Unpinned distribution packages receive Ubuntu security fixes on rebuild.
# hadolint ignore=DL3008
RUN test "${TARGETARCH}" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg python3-venv unzip \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/install-tools.sh /tmp/install-tools.sh
COPY .tflint.hcl /root/.tflint.hcl
RUN bash /tmp/install-tools.sh \
    && /opt/tools/bin/tflint --init \
    && python3 -m venv /opt/checkov \
    && /opt/checkov/bin/pip install --no-cache-dir --only-binary=:all: "checkov==${CHECKOV_VERSION}" \
    && /opt/checkov/bin/pip check \
    && mkdir -p /opt/keyrings \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /opt/keyrings/microsoft.gpg \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /opt/keyrings/docker.gpg \
    && curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb \
        -o /opt/keyrings/packages-microsoft-prod.deb

FROM ubuntu:24.04 AS toolbox

ARG TARGETARCH
ARG POWERSHELL_VERSION=7.6.8-1.deb
ARG AZURE_CLI_VERSION=2.91.0-1~noble
ARG DOCKER_VERSION=5:29.9.2-1~ubuntu.24.04~noble
ARG CONTAINERD_VERSION=2.4.2-3~ubuntu.24.04~noble
ARG DOCKER_BUILDX_VERSION=0.38.1-1~ubuntu.24.04~noble
ARG DOCKER_COMPOSE_VERSION=5.6.1-1~ubuntu.24.04~noble

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV PATH="/opt/checkov/bin:${PATH}" \
    TFLINT_PLUGIN_DIR=/opt/tflint/plugins \
    POWERSHELL_TELEMETRY_OPTOUT=1 \
    AZURE_CORE_COLLECT_TELEMETRY=0 \
    CHECKPOINT_DISABLE=1

COPY --from=build /opt/keyrings/ /etc/apt/keyrings/
# hadolint ignore=DL3008
RUN test "${TARGETARCH}" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq libdigest-sha-perl python3 tar unzip wget zip \
    && dpkg -i /etc/apt/keyrings/packages-microsoft-prod.deb \
    && rm /etc/apt/keyrings/packages-microsoft-prod.deb \
    && echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ noble main' \
        > /etc/apt/sources.list.d/azure-cli.list \
    && echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable' \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "powershell=${POWERSHELL_VERSION}" \
        "azure-cli=${AZURE_CLI_VERSION}" \
        "docker-ce=${DOCKER_VERSION}" \
        "docker-ce-cli=${DOCKER_VERSION}" \
        "containerd.io=${CONTAINERD_VERSION}" \
        "docker-buildx-plugin=${DOCKER_BUILDX_VERSION}" \
        "docker-compose-plugin=${DOCKER_COMPOSE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/tools/bin/ /usr/local/bin/
COPY --from=build /opt/tools/licenses/ /usr/share/doc/azure-iac-toolbox/
COPY --from=build /opt/checkov/ /opt/checkov/
COPY --from=build /opt/tflint/plugins/ /opt/tflint/plugins/
COPY .tflint.hcl /root/.tflint.hcl
COPY scripts/toolbox-check.sh /usr/local/bin/toolbox-check
COPY scripts/toolbox-entrypoint.sh /usr/local/bin/toolbox-entrypoint
RUN chmod +x /usr/local/bin/toolbox-check /usr/local/bin/toolbox-entrypoint && toolbox-check

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/toolbox-entrypoint"]
CMD ["/bin/bash"]
