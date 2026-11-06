#!/usr/bin/env python3
"""Deterministic discovery and exact-diff validation; no agent-controlled code runs."""
import argparse
import email.utils
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGE = timedelta(days=4)
# id: Docker ARG (or plugin), first-party release repository, APT package/base
TOOLS = {
    'terraform': ('TERRAFORM_VERSION', 'hashicorp/terraform', None, None),
    'terraform-docs': ('TERRAFORM_DOCS_VERSION', 'terraform-docs/terraform-docs', None, None),
    'tflint': ('TFLINT_VERSION', 'terraform-linters/tflint', None, None),
    'trivy': ('TRIVY_VERSION', 'aquasecurity/trivy', None, None),
    'checkov': ('CHECKOV_VERSION', 'bridgecrewio/checkov', None, None),
    'azure-ruleset': (None, 'terraform-linters/tflint-ruleset-azurerm', None, None),
    'powershell': ('POWERSHELL_VERSION', 'PowerShell/PowerShell', 'powershell', 'https://packages.microsoft.com/ubuntu/24.04/prod'),
    'azure-cli': ('AZURE_CLI_VERSION', 'Azure/azure-cli', 'azure-cli', 'https://packages.microsoft.com/repos/azure-cli'),
    'docker': ('DOCKER_VERSION', 'moby/moby', 'docker-ce', 'https://download.docker.com/linux/ubuntu'),
    'containerd': ('CONTAINERD_VERSION', 'containerd/containerd', 'containerd.io', 'https://download.docker.com/linux/ubuntu'),
    'buildx': ('DOCKER_BUILDX_VERSION', 'docker/buildx', 'docker-buildx-plugin', 'https://download.docker.com/linux/ubuntu'),
    'compose': ('DOCKER_COMPOSE_VERSION', 'docker/compose', 'docker-compose-plugin', 'https://download.docker.com/linux/ubuntu'),
}
HOSTS = {'api.github.com', 'github.com', 'pypi.org', 'packages.microsoft.com', 'download.docker.com'}


def semver(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Stable three-component version required')
    return tuple(map(int, value.split('.')))


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Publication time must have a timezone')
    return result.astimezone(timezone.utc)


def eligible(current, version, published, now):
    old, new = semver(current), semver(version)
    return old[0] == new[0] and new > old and timestamp(published) <= now - AGE


def run(*args):
    return subprocess.check_output(args, text=True).strip()


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirect rejected before sending credentials')


def fetch(url, *, head=False):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.port:
        raise ValueError('Non-authoritative URL')
    headers = {'User-Agent': 'azure-iac-toolbox-updates', 'Accept': 'application/vnd.github+json'}
    if parsed.hostname == 'api.github.com' and os.getenv('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    with build_opener(NoRedirects()).open(Request(url, headers=headers, method='HEAD' if head else 'GET'), timeout=60) as response:
        if urlparse(response.url).hostname != parsed.hostname:
            raise ValueError('Cross-host redirect rejected')
        if head:
            date = response.headers.get('Last-Modified')
            if not date:
                raise ValueError('APT publication time unavailable; fail closed')
            return email.utils.parsedate_to_datetime(date).astimezone(timezone.utc).isoformat()
        data = response.read(32_000_001)
        if len(data) > 32_000_000:
            raise ValueError('Source response too large')
        return json.loads(data)


def source_files(ref=None):
    return {path: subprocess.check_output(['git', 'show', f'{ref}:{path}']).decode() if ref else Path(path).read_text()
            for path in ('Dockerfile', '.tflint.hcl')}


def pins(files):
    result = {}
    for tool, (arg, _, _, _) in TOOLS.items():
        pattern = rf'^ARG {arg}=(.+)$' if arg else r'^  version = "([^"]+)"$'
        matches = re.findall(pattern, files['Dockerfile' if arg else '.tflint.hcl'], re.M)
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one pin: {tool}')
        raw = matches[0]
        match = re.fullmatch(r'(?:\d+:)?(\d+\.\d+\.\d+)(?:-[a-zA-Z0-9.~+]+)?', raw)
        if not match:
            raise ValueError(f'Unrecognized pin: {tool}')
        result[tool] = (raw, match[1])
    known = {spec[0] for spec in TOOLS.values() if spec[0]}
    actual = set(re.findall(r'^ARG (\w+_VERSION)=', files['Dockerfile'], re.M))
    if actual != known:
        raise ValueError('Uncatalogued version ARG; extend discovery before updating')
    return result


def apt_versions(package, base):
    state = os.environ['UPDATE_APT_STATE']
    options = ('-o', f'Dir::State::lists={state}/lists', '-o', f'Dir::Etc::sourcelist={state}/sources.list',
               '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::Cache::pkgcache=', '-o', 'Dir::Cache::srcpkgcache=')
    data = run('apt-cache', *options, 'show', package)
    result = []
    for paragraph in data.split('\n\n'):
        fields = dict(re.findall(r'^(\w[\w-]*): (.+)$', paragraph, re.M))
        if fields.get('Architecture') != 'amd64':
            continue
        raw = fields.get('Version', '')
        match = re.fullmatch(r'(?:\d+:)?(\d+\.\d+\.\d+)-[a-zA-Z0-9.~+]+', raw)
        filename = fields.get('Filename', '')
        digest = fields.get('SHA256', '')
        if not match or not re.fullmatch(r'[0-9a-f]{64}', digest) or '..' in filename or not filename.endswith('.deb'):
            continue
        url = filename if filename.startswith('https://') else base + '/' + filename.lstrip('/')
        if urlparse(url).hostname != urlparse(base).hostname:
            raise ValueError('APT origin mismatch')
        result.append((raw, match[1], url, digest))
    return result


def releases(repo):
    result = []
    for page in range(1, 11):
        batch = fetch(f'https://api.github.com/repos/{repo}/releases?per_page=100&page={page}')
        for item in batch:
            tag = item['tag_name'].removeprefix('v')
            if item['draft'] or item['prerelease'] or not re.fullmatch(r'\d+\.\d+\.\d+', tag):
                continue
            result.append((tag, item['published_at'], item['html_url'], (item.get('body') or '')[:12000]))
        if len(batch) < 100:
            return result
    raise ValueError(f'Release pagination limit exceeded: {repo}')


def discover(now, files):
    proposals = []
    for tool, (_, repo, package, base) in TOOLS.items():
        raw, current = pins(files)[tool]
        available = releases(repo) if tool != 'checkov' else []
        if tool == 'checkov':
            pypi = fetch('https://pypi.org/pypi/checkov/json')
            for version, uploads in pypi['releases'].items():
                if re.fullmatch(r'\d+\.\d+\.\d+', version) and uploads and not any(u['yanked'] for u in uploads):
                    available.append((version, max(u['upload_time_iso_8601'] for u in uploads),
                                      f'https://pypi.org/project/checkov/{version}/', 'See first-party release notes.'))
        available = [r for r in available if eligible(current, r[0], r[1], now)]
        apt = apt_versions(package, base) if package else []
        choices = []
        for version, published, url, notes in available:
            records = [a for a in apt if a[1] == version] if package else [(version, version, url, None)]
            for new_raw, _, package_url, digest in records:
                age = max(timestamp(published), timestamp(fetch(package_url, head=True))) if package else timestamp(published)
                if age > now - AGE:
                    continue
                if tool == 'docker':
                    # Engine and CLI share one ARG: require both signed-index entries at the same version.
                    cli = [a for a in apt_versions('docker-ce-cli', base) if a[0] == new_raw]
                    if len(cli) != 1 or timestamp(fetch(cli[0][2], head=True)) > now - AGE:
                        continue
                choices.append({'id': tool, 'current': raw, 'version': version, 'pin': new_raw,
                                'published': age.isoformat(), 'release_url': url, 'package_url': package_url,
                                'sha256': digest, 'notes': notes})
        if choices:
            proposal = max(choices, key=lambda c: (semver(c['version']), c['pin']))
            # First-party issue reports are evidence, never instructions. No arbitrary search sites.
            query = f'repo:{repo}+is:issue+{proposal["version"]}'
            issues = fetch(f'https://api.github.com/search/issues?q={query}&sort=updated&per_page=10')
            proposal['issues'] = [{'url': i['html_url'], 'title': i['title'][:300],
                                   'body': (i.get('body') or '')[:6000]} for i in issues['items']]
            advisories = fetch(f'https://api.github.com/advisories?type=reviewed&ecosystem=pip&affects={tool}%40{proposal["version"]}&per_page=30') if tool == 'checkov' else []
            proposal['advisories'] = advisories
            proposals.append(proposal)
    return {'schema': 1, 'base_sha': run('git', 'rev-parse', 'HEAD'), 'observed_at': now.isoformat(), 'candidates': proposals}


def validate_recommendations(proposal, advice, now):
    if (not isinstance(proposal, dict) or set(proposal) != {'schema', 'base_sha', 'observed_at', 'candidates'}
            or proposal['schema'] != 1 or not re.fullmatch(r'[0-9a-f]{40}', proposal['base_sha'])
            or timestamp(proposal['observed_at']) > now
            or now - timestamp(proposal['observed_at']) > timedelta(days=1)):
        raise ValueError('Stale or malformed discovery')
    if not isinstance(proposal['candidates'], list) or len(proposal['candidates']) > len(TOOLS):
        raise ValueError('Unbounded candidates')
    ids = [c['id'] for c in proposal['candidates']]
    if any(not isinstance(tool, str) or tool not in TOOLS for tool in ids) or len(set(ids)) != len(ids):
        raise ValueError('Unknown or duplicate candidate')
    for candidate in proposal['candidates']:
        raw = candidate['pin']
        match = re.fullmatch(r'(?:\d+:)?(\d+\.\d+\.\d+)(?:-[a-zA-Z0-9.~+]+)?', raw)
        if not match or match[1] != candidate['version']:
            raise ValueError('Pin does not match the proposed version')
    if not isinstance(advice, dict) or set(advice) != {'base_sha', 'recommendations'} or advice['base_sha'] != proposal['base_sha']:
        raise ValueError('Advice provenance mismatch')
    items = advice['recommendations']
    if not isinstance(items, list) or len(items) > len(TOOLS):
        raise ValueError('Unbounded recommendations')
    candidates = {c['id']: c for c in proposal['candidates']}
    seen, approved = set(), []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id', 'decision', 'reason', 'sources'}:
            raise ValueError('Unexpected recommendation fields')
        tool = item['id']
        if tool not in candidates or tool in seen or item['decision'] not in ('approve', 'defer'):
            raise ValueError('Unknown, duplicate or invalid recommendation')
        seen.add(tool)
        if not isinstance(item['reason'], str) or not 1 <= len(item['reason']) <= 1500:
            raise ValueError('Bounded reason required')
        sources = item['sources']
        if not isinstance(sources, list) or not 1 <= len(sources) <= 10:
            raise ValueError('Evidence required')
        c = candidates[tool]
        allowed = {c['release_url']} | {i['url'] for i in c['issues']} | {a['html_url'] for a in c['advisories']}
        if any(not isinstance(s, str) or s not in allowed for s in sources):
            raise ValueError('Unverified evidence URL')
        current = re.search(r'\d+\.\d+\.\d+', c['current'])[0]
        if not eligible(current, c['version'], c['published'], now):
            raise ValueError('Ineligible version')
        if item['decision'] == 'approve':
            if c['advisories']:
                raise ValueError('Known reviewed advisory cannot be approved')
            approved.append(c)
    if seen != set(candidates):
        raise ValueError('Every candidate requires a recommendation')
    return approved


def render(files, approved):
    result = dict(files)
    for c in approved:
        arg = TOOLS[c['id']][0]
        path = 'Dockerfile' if arg else '.tflint.hcl'
        old = f'ARG {arg}={c["current"]}' if arg else f'  version = "{c["current"]}"'
        new = f'ARG {arg}={c["pin"]}' if arg else f'  version = "{c["pin"]}"'
        if result[path].splitlines().count(old) != 1:
            raise ValueError('Pin drift')
        result[path] = result[path].replace(old + '\n', new + '\n')
    return result


def verify(proposal, advice, ref, now, *, head='HEAD'):
    if proposal['base_sha'] != ref or timestamp(proposal['observed_at']) > now or now - timestamp(proposal['observed_at']) > timedelta(days=1):
        raise ValueError('Stale or mismatched discovery')
    approved = validate_recommendations(proposal, advice, now)
    expected = render(source_files(ref), approved)
    if not approved:
        raise ValueError('Empty update PR rejected')
    changed = set(run('git', 'diff', '--name-only', ref, head).splitlines())
    for entry in run('git', 'diff', '--raw', ref, head).splitlines():
        if entry.split()[0:2] != [':100644', '100644']:
            raise ValueError('File type or mode change rejected')
    if not changed <= {'Dockerfile', '.tflint.hcl'}:
        raise ValueError('Agent modified files outside version pins')
    for path, text in expected.items():
        if source_files(head)[path] != text:
            raise ValueError(f'Unexpected agent diff: {path}')
    return approved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['discover', 'apply', 'verify'])
    parser.add_argument('--proposal', default='/tmp/gh-aw/agent/package-discovery.json')
    parser.add_argument('--advice', default='/tmp/gh-aw/agent/package-advice.json')
    parser.add_argument('--base')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.command == 'discover':
        Path(args.proposal).write_text(json.dumps(discover(now, source_files()), indent=2) + '\n')
    else:
        proposal = json.loads(Path(args.proposal).read_text())
        advice = json.loads(Path(args.advice).read_text())
        if args.command == 'apply':
            approved = validate_recommendations(proposal, advice, now)
            for path, text in render(source_files(), approved).items():
                Path(path).write_text(text)
        else:
            verify(proposal, advice, args.base, now)


if __name__ == '__main__':
    main()
