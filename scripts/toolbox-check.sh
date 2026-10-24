#!/usr/bin/env bash
set -euo pipefail

terraform version
terraform-docs version
tflint --version
trivy --version
checkov --version
/opt/checkov/bin/pip check
az version
# PowerShell, not bash, expands these variables.
# shellcheck disable=SC2016
pwsh -NoLogo -NoProfile -Command '
    $ErrorActionPreference = "Stop"
    $PSVersionTable.PSVersion.ToString()
    if (Get-Module -ListAvailable -Name Az, Az.*) {
        throw "Azure PowerShell modules must not be installed"
    }
'
docker --version
docker compose version
bash --version >/dev/null
git --version
jq --version
curl --version >/dev/null
wget --version >/dev/null
unzip -v >/dev/null
tar --version >/dev/null

if command -v dockerd >/dev/null || command -v gh >/dev/null || command -v ld >/dev/null; then
    echo 'Unexpected daemon, GitHub CLI, or build tools in runtime' >&2
    exit 1
fi
