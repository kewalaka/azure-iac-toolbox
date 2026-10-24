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
| Docker Engine / CLI | 29.9.2 |
| containerd | 2.4.2 |
| Buildx / Compose | 0.38.1 / 5.6.1 |

Also includes bash, git, jq, curl, wget, unzip and tar.

PowerShell and Azure CLI use Microsoft's documented [Ubuntu package installation](https://learn.microsoft.com/powershell/scripting/install/install-ubuntu) and [Azure CLI APT installation](https://learn.microsoft.com/cli/azure/install-azure-cli-linux?pivots=apt). Docker Engine, containerd and client plugins use Docker's official Ubuntu repository. Release archives are checked against upstream SHA-256 manifests. Checkov uses its own Python 3.12 environment, separate from Azure CLI. Checkov installs from wheels only, without compilers. Environment setup and signing-key preparation stay in the build stage.

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

Trivy downloads its databases on first scan. Checkov can scan local files without an API key; use `checkov -d /workspace --skip-download` to avoid platform downloads.

## Docker in sandbox CI

Set `TOOLBOX_DOCKER=1` in the sandbox environment and launch your CI runner command through the image entrypoint. It starts Docker as root, waits up to 30 seconds for `docker info`, and only then runs your command. It preserves the command's exit status and stops its Docker daemon on exit or termination. Docker listens only on `/var/run/docker.sock`, not an unauthenticated TCP port.

If the sandbox launcher overrides the image entrypoint, invoke it explicitly:

```bash
TOOLBOX_DOCKER=1 /usr/local/bin/toolbox-entrypoint /path/to/your-ci-runner
```

The image includes tools, not a CI runner agent. Your launcher must supply the runner command and any registration credentials. The sandbox must provide a Docker-capable kernel, networking and cgroup access. A full sandbox microVM can run Docker without a separate privileged DinD service; an ordinary container generally needs `--privileged` to run its own daemon. Use only isolated, disposable CI environments for this mode. Docker data and layer caches are local to that environment.

Daemon startup is disabled by default. For an external engine, leave `TOOLBOX_DOCKER` unset and provide `DOCKER_HOST` or mount a Docker socket. A mounted socket grants control of its host. Local startup reuses an already-ready local daemon without stopping it; it rejects a remote `DOCKER_HOST` and clears Docker context/TLS client overrides.

After startup, validate in the sandbox:

```bash
docker info
docker run --rm hello-world
docker buildx ls
docker compose version
```

### HTTPS during Docker builds

Sandbox egress may intercept TLS. Run containers can receive the sandbox CA automatically, while Docker build steps may not. Keep certificate verification enabled: copy the sandbox's public CA certificate into the job's build context and install it in the image being built before HTTPS downloads. Never copy private keys or credentials into that context.

For an Ubuntu-based job image, copy the certificate supplied by the sandbox to `sandbox-egress.crt` beside its Dockerfile, then use:

```dockerfile
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY sandbox-egress.crt /usr/local/share/ca-certificates/sandbox-egress.crt
RUN update-ca-certificates
```

Ubuntu's default HTTP package mirrors allow the initial certificate package installation. Images that need HTTPS before trust is installed need a pretrusted base image. Custom BuildKit builders may also need the CA in their own trust store. The toolbox does not embed environment-specific certificates or disable TLS checks.

## Static validation

With ShellCheck, Hadolint 2.15.1 and actionlint available:

```bash
for script in scripts/*.sh; do bash -n "$script"; done
shellcheck scripts/*.sh
python3 -B -m unittest discover -s tests -v
hadolint Dockerfile
actionlint
git diff --check
```

The validation workflow runs shell/Dockerfile static checks and startup lifecycle tests with mocked Docker commands; it never starts a real daemon. Nothing publishes an image or requires registry credentials. A successful static check does not prove the image builds or runs; perform the build and smoke check above before adopting it.
