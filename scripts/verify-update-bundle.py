#!/usr/bin/env python3
"""Verify both AW v0.93.2 transports offline before the safe-output writer runs."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('updates', Path(__file__).with_name('package-updates.py'))
updates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updates)


def bounded_json(path):
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ValueError('Invalid or oversized artifact')
    return json.loads(path.read_text())


def verify_bundle(base, directory, proposal, advice, now):
    if not re.fullmatch(r'[0-9a-f]{40}', base) or proposal['base_sha'] != base:
        raise ValueError('Invalid base provenance')
    approved = updates.validate_recommendations(proposal, advice, now)
    output = bounded_json(directory / 'agent_output.json')
    items = output.get('items') if isinstance(output, dict) else None
    if not isinstance(items, list) or len(items) > 2 or any(not isinstance(item, dict) for item in items):
        raise ValueError('Invalid safe-output requests')
    requests = [item for item in items if item.get('type') == 'create_pull_request']
    if any(item.get('type') not in ('create_pull_request', 'noop') for item in items):
        raise ValueError('Unexpected safe-output type')
    bundles = list(directory.rglob('*.bundle'))
    patches = list(directory.rglob('*.patch'))
    if not approved:
        if bundles or patches or requests:
            raise ValueError('Changes without approved recommendations')
        return
    repo = os.environ['GITHUB_REPOSITORY']
    if len(requests) != 1:
        raise ValueError('One consolidated PR request required')
    request = requests[0]
    if (request.get('branch') != 'automation/package-updates'
            or request.get('base') not in (None, '', 'main')
            or request.get('repo') not in (None, '', repo)):
        raise ValueError('Unexpected PR target')
    if len(bundles) != 1 or len(patches) != 1:
        raise ValueError('Exactly one bundle and patch required; no alternate transport')
    bundle, patch = bundles[0], patches[0]
    prefixes = {'aw-automation-package-updates', f'aw-{repo.lower().replace("/", "-")}-automation-package-updates'}
    if (bundle.parent != directory or patch.parent != directory
            or bundle.stem != patch.stem or bundle.stem not in prefixes):
        raise ValueError('Transport filenames do not match the PR request')
    for path in (bundle, patch):
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError('Invalid or oversized update transport')
    refs = updates.run('git', 'bundle', 'list-heads', str(bundle)).splitlines()
    if len(refs) != 1 or not re.fullmatch(r'[0-9a-f]{40} refs/heads/automation/package-updates', refs[0]):
        raise ValueError('Unexpected bundle ref')
    head = refs[0].split()[0]
    updates.run('git', 'bundle', 'verify', str(bundle))
    updates.run('git', 'bundle', 'unbundle', str(bundle))
    if updates.run('git', 'rev-list', '--count', f'{base}..{head}') != '1':
        raise ValueError('One update commit required')
    if updates.run('git', 'rev-parse', f'{head}^') != base:
        raise ValueError('Update commit must directly descend from discovery base')
    updates.verify(proposal, advice, base, now, head=head)
    # A fallback patch must produce exactly the same tree as the verified bundle.
    with tempfile.TemporaryDirectory() as work:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(work) / 'index'))
        subprocess.run(['git', 'read-tree', base], env=env, check=True)
        subprocess.run(['git', 'apply', '--cached', str(patch)], env=env, check=True)
        tree = subprocess.check_output(['git', 'write-tree'], env=env, text=True).strip()
        if tree != updates.run('git', 'rev-parse', f'{head}^{{tree}}'):
            raise ValueError('Fallback patch differs from verified bundle')


def main():
    root = Path(os.environ['RUNNER_TEMP'])
    proposal = bounded_json(root / 'trusted-discovery/package-discovery.json')
    advice = bounded_json(root / 'advice/package-advice.json')
    base = os.environ['BASE_SHA']
    # Do not apply a stale proposal to an advanced default branch.
    remote = updates.run('git', 'ls-remote', 'origin', 'refs/heads/main').split()[0]
    if remote != base:
        raise ValueError('main advanced during review; rerun discovery')
    verify_bundle(base, root / 'agent-bundle', proposal, advice, datetime.now(timezone.utc))


if __name__ == '__main__':
    main()
