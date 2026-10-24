# azure-iac-toolbox

An Ubuntu 24.04 LTS container for Azure infrastructure work. Targets **Linux amd64**; other architectures are rejected at build time.

## Included tools

| Tool | Pinned version |
| --- | --- |
| PowerShell | 7.6.8 |
| Azure CLI | 2.91.0 |
| Terraform | 1.17.0 |
| terraform-docs | 0.24.0 |
| TFLint / Azure ruleset | 0.65.0 / 0.32.0 |
| Trivy | 0.76.1 |
| Checkov | 3.3.30 |
| Docker CLI / Compose | 29.9.2 / 5.6.1 |

Also includes bash, git, jq, curl, wget, unzip and tar. No Azure PowerShell modules, GitHub CLI, Docker daemon or runtime build tools.

PowerShell and Azure CLI use Microsoft's documented [Ubuntu package installation](https://learn.microsoft.com/powershell/scripting/install/install-ubuntu) and [Azure CLI APT installation](https://learn.microsoft.com/cli/azure/install-azure-cli-linux?pivots=apt). Docker clients use Docker's official Ubuntu repository. Release archives are checked against upstream SHA-256 manifests. Checkov uses its own Python 3.12 environment, separate from Azure CLI. Checkov installs from wheels only, without compilers. Environment setup and signing-key preparation stay in the build stage.

Tool pins live in `Dockerfile`; the Azure ruleset pin lives in `.tflint.hcl`. Ubuntu packages and Checkov's transitive dependencies are not locked: rebuild to receive their updates, and validate before use.

## Build and use

With a running Docker engine:

```bash
docker build --platform linux/amd64 --pull -t azure-iac-toolbox:local .
docker run --rm -it --platform linux/amd64 \
  --mount type=bind,src="$PWD",dst=/workspace azure-iac-toolbox:local
```

The default shell is bash, running as root. The build runs `toolbox-check`, which checks every tool without contacting a Docker daemon or logging into Azure. Repeat it with:

```bash
docker run --rm --platform linux/amd64 azure-iac-toolbox:local toolbox-check
```

Inside the container, use an isolated Azure configuration and specify your tenant:

```bash
export AZURE_CONFIG_DIR=$(mktemp -d)
az login --tenant <tenant-id> --use-device-code
# Run your Azure commands, then remove the temporary credentials.
rm -rf "$AZURE_CONFIG_DIR"
```

The default TFLint configuration enables the preinstalled Azure ruleset. A project's own `.tflint.hcl` takes precedence; include the same plugin version there to reuse it.

Docker commands require a separately provided engine (`DOCKER_HOST` or a mounted socket). Mounting a Docker socket grants control of that host; it is not required for Terraform or scanning files. Trivy downloads its databases on first scan. Checkov can scan local files without an API key; use `checkov -d /workspace --skip-download` to avoid platform downloads.

## Static validation

With ShellCheck, Hadolint 2.15.1 and actionlint available:

```bash
for script in scripts/*.sh; do bash -n "$script"; done
shellcheck scripts/*.sh
hadolint Dockerfile
actionlint
git diff --check
```

The validation workflow runs shell and Dockerfile static checks only. Nothing publishes an image or requires registry credentials. A successful static check does not prove the image builds or runs; perform the build and smoke check above before adopting it.
