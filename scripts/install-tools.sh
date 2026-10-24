#!/usr/bin/env bash
set -euo pipefail

: "${TERRAFORM_VERSION:?}" "${TERRAFORM_DOCS_VERSION:?}" "${TFLINT_VERSION:?}" "${TRIVY_VERSION:?}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
mkdir -p /opt/tools/bin /opt/tools/licenses

fetch() {
    local base=$1 archive=$2 manifest=$3
    curl -fsSL --retry 3 "${base}/${archive}" -o "$archive"
    curl -fsSL --retry 3 "${base}/${manifest}" -o "$manifest"
    awk -v file="$archive" '$2 == file || $2 == "*" file {print}' "$manifest" > selected.sha256
    test -s selected.sha256
    sha256sum --check selected.sha256
}

fetch "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}" \
    "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" "terraform_${TERRAFORM_VERSION}_SHA256SUMS"
unzip -q "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" terraform
install -m 0755 terraform /opt/tools/bin/terraform
unzip -p "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" LICENSE.txt > /opt/tools/licenses/terraform.txt

fetch "https://github.com/terraform-docs/terraform-docs/releases/download/v${TERRAFORM_DOCS_VERSION}" \
    "terraform-docs-v${TERRAFORM_DOCS_VERSION}-linux-amd64.tar.gz" "terraform-docs-v${TERRAFORM_DOCS_VERSION}.sha256sum"
tar -xzf "terraform-docs-v${TERRAFORM_DOCS_VERSION}-linux-amd64.tar.gz" terraform-docs
install -m 0755 terraform-docs /opt/tools/bin/terraform-docs
tar -xOf "terraform-docs-v${TERRAFORM_DOCS_VERSION}-linux-amd64.tar.gz" LICENSE > /opt/tools/licenses/terraform-docs.txt

fetch "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}" \
    tflint_linux_amd64.zip checksums.txt
unzip -q tflint_linux_amd64.zip tflint
install -m 0755 tflint /opt/tools/bin/tflint
curl -fsSL --retry 3 "https://raw.githubusercontent.com/terraform-linters/tflint/v${TFLINT_VERSION}/LICENSE" \
    -o /opt/tools/licenses/tflint.txt

fetch "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}" \
    "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" "trivy_${TRIVY_VERSION}_checksums.txt"
tar -xzf "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" trivy
install -m 0755 trivy /opt/tools/bin/trivy
tar -xOf "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" LICENSE > /opt/tools/licenses/trivy.txt
