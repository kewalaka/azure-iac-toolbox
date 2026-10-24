# azure-iac-toolbox

An Ubuntu 24.04 LTS container for Azure infrastructure work. Targets **Linux amd64**; other architectures are rejected at build time.

## Included tools

PowerShell, Azure CLI, Terraform, terraform-docs, TFLint with its Azure ruleset,
Trivy, Checkov, Docker Engine/CLI, containerd, Buildx and Compose. See `Dockerfile`
and `.tflint.hcl` for their exact versions.

Also includes bash, git, jq, curl, wget, zip, unzip, tar and `shasum` (from `libdigest-sha-perl`).

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

When running the toolbox inside an ordinary Docker container, give `/var/lib/docker` its own volume to avoid nested overlay-filesystem failures:

```bash
docker run --rm --privileged --volume /var/lib/docker \
  -e TOOLBOX_DOCKER=1 azure-iac-toolbox:local \
  bash -c 'docker run --rm hello-world'
```

The anonymous volume is removed with the container. A microVM with its own writable filesystem does not need this outer-container mount.

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
actionlint .github/workflows/validate.yml .github/workflows/release.yml
gh aw compile --strict
git diff --check
```

These local checks do not start a Docker daemon or build the image.

## GitHub Actions validation

The workflow runs static checks, builds and loads the Linux amd64 image, checks its tools, and tests Docker startup and a nested container/build on an isolated GitHub runner. Trivy scans the built image's OS and application dependencies with updated vulnerability databases. HIGH or CRITICAL findings fail validation, including findings without a fix. Lower-severity findings remain visible in the report.

Each image job writes an Actions summary and retains the full JSON scan report for 14 days as the `image-scan` artifact, including when the vulnerability gate fails. Same-repository PRs receive an updated scan comment from a separate reporting job. Fork PRs and Dependabot runs still receive summaries and artifacts, but no comment because their tokens are restricted.

A passing CI run is not a substitute for checking your sandbox's kernel, networking and certificate trust.

## Scheduled package updates

`package-updates.md` is a [GitHub Agentic Workflow](https://github.github.com/gh-aw/).
It runs Mondays and Thursdays at 05:17 UTC, or manually on `main`. Deterministic
Python discovery covers all twelve image tool pins; Dependabot does not cover
these Docker ARG/APT/plugin versions. It permits only stable, strictly newer
minor/patch versions in the current major, published at least four days ago.
CI Actions, the AW compiler and validation tools remain separately reviewed;
the agent cannot edit executable policy or workflows.

Evidence comes from upstream GitHub releases/issues, PyPI and GitHub's reviewed
Checkov advisories. APT versions/digests come from authenticated Microsoft/Docker
indexes. Their age uses the later of the upstream release date and the package
origin's HTTP `Last-Modified`; that timestamp is origin-provided, **not signed**.
Missing timestamps or source failures block discovery. Known reviewed Checkov
advisories cannot be approved. Binary/transitive vulnerabilities are also checked
by the full image scan; empty upstream issue searches are not proof of safety.

The network-restricted, read-only agent can defer candidates, not invent versions
or bypass age checks. A separate trusted job checks advice, provenance, the exact
pin-only Git tree, both Git bundle/patch transports and the PR request before AW's
safe-output writer can create **one** `chore(deps):` PR. No automatic merge occurs.
Only `Dockerfile` and `.tflint.hcl` can change. An outstanding update PR pauses new
proposals; enable automatic head-branch deletion and delete abandoned closed
`automation/package-updates` branches before retrying.

### Repository setup

- Enable Actions and creation of pull requests. Protect `main`: require PR review
  and the `static` and `image` validation checks; do not allow bypassing the scan.
- Add repository secret `COPILOT_GITHUB_TOKEN`: a fine-grained token with
  account-level **Copilot Requests** permission and no repository write access;
  follow the [Copilot authentication guidance](https://github.github.com/gh-aw/reference/engines/).
- Create environment **`package-updates`**, restricted to `main`. Add
  **only there** secret `PACKAGE_UPDATES_PR_TOKEN`: a fine-grained token limited
  to this repository, with Contents and Pull requests write. This separate token
  lets update PRs trigger normal CI. Never put it in repository/organization
  secrets: compiler references also exist in the agent job, which must resolve
  empty. Discovery explicitly rejects mis-scoping. Do not configure generic
  `GH_AW_GITHUB_TOKEN` or `GH_AW_GITHUB_MCP_SERVER_TOKEN` overrides.
- Create environment **`release`**, restricted to `main`; add required reviewers
  if publication needs approval. GitHub's job token handles GHCR and releases;
  no Docker Hub credential is needed. Set the GHCR package's visibility to public
  after its first publication if anonymous pulls are required.

Install the pinned compiler with `gh extension install github/gh-aw --pin v0.93.2`.
Edit the Markdown source, run `gh aw compile --strict`, and commit the generated
lock, `.github/aw/actions-lock.json` and `.gitattributes` with it. CI repeats strict
compilation and checks for lock drift. The generated lock uses platform features
not understood by actionlint 1.7.12, which also flags undeclared compiler-generated
admission outputs. Strict compilation succeeds; hosted AW execution still needs
verification after setup. Lint handwritten workflows separately; do not hand-edit
the generated lock.

## Releases

A merged PR changing image inputs triggers reusable validation on `main`. Only
its successfully tested and scanned image is exported to a same-run artifact.
A separate `release` job never checks out PR code or rebuilds: it loads those
bytes, verifies the image config digest, and publishes
`ghcr.io/kewalaka/azure-iac-toolbox:sha-<commit>` and `:v0.1.<run-number>` before
creating the matching GitHub tag/release. There is no mutable `latest` tag.
Direct pushes, failed checks, advanced `main`, tag collisions, registry errors
or different existing image bytes block publication. Reruns reuse immutable
names; remove a conflicting draft release before retrying.

Existing HIGH/CRITICAL image findings, including unfixed vendor dependencies,
currently block publication. Do not suppress them to enable a release.
