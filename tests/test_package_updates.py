import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updates = load('updates', 'package-updates.py')
bundles = load('bundles', 'verify-update-bundle.py')
NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
FILES = {name: (ROOT / name).read_text() for name in ('Dockerfile', '.tflint.hcl')}


def proposal(base='a' * 40):
    current, version = updates.pins(FILES)['terraform']
    parts = list(updates.semver(version))
    parts[2] += 1
    version = '.'.join(map(str, parts))
    return {'schema': 1, 'base_sha': base, 'observed_at': NOW.isoformat(), 'candidates': [
        {'id': 'terraform', 'current': current, 'version': version, 'pin': version,
         'published': (NOW - timedelta(days=4)).isoformat(),
         'release_url': f'https://github.com/hashicorp/terraform/releases/tag/v{version}',
         'package_url': None, 'sha256': None, 'notes': '', 'issues': [], 'advisories': []}]}


def advice(p):
    return {'base_sha': p['base_sha'], 'recommendations': [
        {'id': c['id'], 'decision': 'approve', 'reason': 'Reviewed first-party evidence',
         'sources': [c['release_url']]} for c in p['candidates']]}


class PolicyTests(unittest.TestCase):
    def test_four_day_boundary(self):
        date = NOW - timedelta(days=4)
        self.assertTrue(updates.eligible('1.2.3', '1.2.4', date.isoformat(), NOW))
        for young in (date + timedelta(seconds=1), NOW, NOW + timedelta(days=1)):
            self.assertFalse(updates.eligible('1.2.3', '1.2.4', young.isoformat(), NOW))

    def test_same_major_only(self):
        for version, expected in [('1.2.4', True), ('1.3.0', True), ('2.0.0', False),
                                  ('1.2.3', False), ('1.1.9', False)]:
            self.assertEqual(updates.eligible('1.2.3', version, '2026-01-01T00:00:00Z', NOW), expected)

    def test_unstable_versions_and_naive_dates_rejected(self):
        for version in ['1.2.4-rc1', 'v1.2.4', '1.2', '01.2.4', '1.2.4\nRUN evil', None]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                updates.semver(version)
        with self.assertRaises(ValueError):
            updates.timestamp('2026-01-01')

    def test_catalog_covers_every_version_pin(self):
        self.assertEqual(len(updates.pins(FILES)), 12)
        extra = dict(FILES, Dockerfile=FILES['Dockerfile'] + '\nARG UNKNOWN_VERSION=1.2.3\n')
        with self.assertRaises(ValueError):
            updates.pins(extra)

    def test_advice_is_complete_bounded_and_cited(self):
        p = proposal()
        self.assertEqual(len(updates.validate_recommendations(p, advice(p), NOW)), 1)
        variants = []
        for field, value in [('decision', 'force'), ('reason', ''), ('sources', []),
                             ('sources', ['https://example.com']), ('version', '99.0.0'), ('id', 'unknown')]:
            a = advice(p)
            a['recommendations'][0][field] = value
            variants.append(a)
        a = advice(p); a['base_sha'] = 'b' * 40; variants.append(a)
        a = advice(p); a['recommendations'] *= 2; variants.append(a)
        a = advice(p); a['recommendations'] = []; variants.append(a)
        for a in variants:
            with self.subTest(advice=a), self.assertRaises(ValueError):
                updates.validate_recommendations(p, a, NOW)

    def test_advisory_and_age_cannot_be_overridden(self):
        for mutation in ['advisory', 'young', 'major', 'forged-pin', 'duplicate', 'stale', 'future']:
            p = proposal()
            c = p['candidates'][0]
            if mutation == 'advisory': c['advisories'] = [{'html_url': 'https://github.com/advisories/GHSA-test'}]
            if mutation == 'young': c['published'] = NOW.isoformat()
            if mutation == 'major': c['pin'] = c['version'] = '99.0.0'
            if mutation == 'forged-pin': c['pin'] += '\nRUN evil'
            if mutation == 'duplicate': p['candidates'] *= 2
            if mutation == 'stale': p['observed_at'] = (NOW - timedelta(days=2)).isoformat()
            if mutation == 'future': p['observed_at'] = (NOW + timedelta(seconds=1)).isoformat()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                updates.validate_recommendations(p, advice(p), NOW)

    def test_empty_and_deferred_recommendations(self):
        p = proposal(); a = advice(p); a['recommendations'][0]['decision'] = 'defer'
        self.assertEqual(updates.validate_recommendations(p, a, NOW), [])
        p['candidates'] = []
        self.assertEqual(updates.validate_recommendations(p, advice(p), NOW), [])

    def test_exact_render_preserves_other_bytes(self):
        p = proposal()
        result = updates.render(FILES, p['candidates'])
        self.assertEqual(result['.tflint.hcl'], FILES['.tflint.hcl'])
        c = p['candidates'][0]
        self.assertEqual(result['Dockerfile'], FILES['Dockerfile'].replace(
            f'ARG TERRAFORM_VERSION={c["current"]}\n', f'ARG TERRAFORM_VERSION={c["pin"]}\n'))
        with self.assertRaises(ValueError):
            updates.render(dict(FILES, Dockerfile=''), p['candidates'])

    def test_authoritative_hosts_and_redirect_credentials(self):
        for url in ['http://api.github.com', 'https://api.github.com.evil.test',
                    'https://user@api.github.com', 'https://api.github.com:443', 'file:///etc/passwd']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                updates.fetch(url)
        with self.assertRaises(ValueError):
            updates.NoRedirects().redirect_request(None, None, 302, '', {}, 'https://evil.test')

    def test_apt_origin_architecture_and_signed_digest(self):
        data = ('Package: azure-cli\nVersion: 2.92.0-1~noble\nArchitecture: amd64\n'
                'Filename: pool/main/a/azure-cli/tool.deb\nSHA256: ' + 'a' * 64)
        with patch.dict(os.environ, UPDATE_APT_STATE='/tmp/test-apt'), patch.object(updates, 'run', return_value=data):
            records = updates.apt_versions('azure-cli', updates.TOOLS['azure-cli'][3])
            self.assertEqual(records[0][2], 'https://packages.microsoft.com/repos/azure-cli/pool/main/a/azure-cli/tool.deb')
        for invalid in [data.replace('amd64', 'arm64'), data.replace('a' * 64, 'bad'), data.replace('pool/main', '../evil')]:
            with patch.dict(os.environ, UPDATE_APT_STATE='/tmp/test-apt'), patch.object(updates, 'run', return_value=invalid):
                self.assertEqual(updates.apt_versions('azure-cli', updates.TOOLS['azure-cli'][3]), [])

    def test_docker_requires_matching_aged_cli(self):
        spec = {'docker': updates.TOOLS['docker']}
        raw = '5:29.9.3-1~ubuntu.24.04~noble'
        engine = [(raw, '29.9.3', 'https://download.docker.com/engine.deb', 'a' * 64)]
        cli = [(raw, '29.9.3', 'https://download.docker.com/cli.deb', 'b' * 64)]
        for cli_records, cli_age, expected in [([], '2026-01-01T00:00:00Z', 0),
                                              (cli, NOW.isoformat(), 0),
                                              (cli, '2026-01-01T00:00:00Z', 1)]:
            def fetch(url, head=False):
                if not head: return {'items': []}
                return cli_age if url.endswith('cli.deb') else '2026-01-01T00:00:00Z'
            with patch.object(updates, 'TOOLS', spec), patch.object(updates, 'pins', return_value={'docker': ('5:29.9.2-1~ubuntu.24.04~noble', '29.9.2')}), \
                    patch.object(updates, 'run', return_value='a' * 40), \
                    patch.object(updates, 'releases', return_value=[('29.9.3', '2026-01-01T00:00:00Z', 'https://github.com/moby/moby/releases/tag/v29.9.3', '')]), \
                    patch.object(updates, 'apt_versions', side_effect=lambda package, base: engine if package == 'docker-ce' else cli_records), \
                    patch.object(updates, 'fetch', side_effect=fetch):
                self.assertEqual(len(updates.discover(NOW, FILES)['candidates']), expected)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.cwd = Path.cwd()
        self.environment = patch.dict(os.environ, GITHUB_REPOSITORY='kewalaka/azure-iac-toolbox')
        self.environment.start()
        os.chdir(self.work.name)
        self.directory = Path(self.work.name) / 'artifact'
        self.directory.mkdir()
        self.output = {'items': [{'type': 'create_pull_request', 'branch': 'automation/package-updates'}]}
        (self.directory / 'agent_output.json').write_text(json.dumps(self.output))
        updates.run('git', 'init', '-q', '-b', 'main')
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false'), ('core.hooksPath', '/dev/null')]:
            updates.run('git', 'config', key, value)
        for name, text in FILES.items(): Path(name).write_text(text)
        # Prove leading/trailing whitespace is not normalized by validation.
        Path('Dockerfile').write_text('\n' + FILES['Dockerfile'] + '\n')
        self.commit()
        self.base = updates.run('git', 'rev-parse', 'HEAD')
        self.p = proposal(self.base)
        self.a = advice(self.p)
        updates.run('git', 'checkout', '-q', '-b', 'automation/package-updates')
        for name, text in updates.render(updates.source_files(), self.p['candidates']).items():
            Path(name).write_text(text)
        self.commit()

    def tearDown(self):
        os.chdir(self.cwd)
        self.environment.stop()
        self.work.cleanup()

    def commit(self):
        updates.run('git', 'add', '.')
        updates.run('git', 'commit', '-qm', 'chore(deps): test update')

    def transport(self):
        bundle = self.directory / 'aw-automation-package-updates.bundle'
        patch_path = self.directory / 'aw-automation-package-updates.patch'
        updates.run('git', 'bundle', 'create', str(bundle), f'{self.base}..automation/package-updates')
        patch_path.write_bytes(subprocess.check_output(['git', 'format-patch', '--stdout', f'{self.base}..HEAD']))
        return bundle, patch_path

    def verify(self):
        bundles.verify_bundle(self.base, self.directory, self.p, self.a, NOW)

    def test_valid_incremental_bundle_and_patch(self):
        self.transport(); self.verify()

    def test_disallowed_file_change(self):
        Path('script.py').write_text('malicious')
        updates.run('git', 'add', '.')
        updates.run('git', 'commit', '--amend', '--no-edit', '-q')
        self.transport()
        with self.assertRaises(ValueError): self.verify()

    def test_wrong_pin_and_mode(self):
        Path('Dockerfile').write_text(Path('Dockerfile').read_text() + '\nRUN echo unexpected\n')
        updates.run('git', 'add', '.')
        updates.run('git', 'commit', '--amend', '--no-edit', '-q')
        self.transport()
        with self.assertRaises(ValueError): self.verify()

    def test_file_mode_cannot_change(self):
        updates.run('git', 'update-index', '--chmod=+x', 'Dockerfile')
        updates.run('git', 'commit', '--amend', '--no-edit', '-q')
        self.transport()
        with self.assertRaises(ValueError): self.verify()

    def test_unchecked_patch_fallback_rejected(self):
        _, patch_path = self.transport()
        patch_path.write_text(patch_path.read_text().replace('ARG TERRAFORM_VERSION=1.17.1', 'ARG TERRAFORM_VERSION=1.17.2'))
        with self.assertRaises(ValueError): self.verify()

    def test_multiple_transports_and_commits_rejected(self):
        bundle, _ = self.transport()
        (self.directory / 'other.bundle').write_bytes(bundle.read_bytes())
        with self.assertRaises(ValueError): self.verify()
        (self.directory / 'other.bundle').unlink()
        updates.run('git', 'commit', '--allow-empty', '-qm', 'chore: hidden commit')
        self.transport()
        with self.assertRaises(ValueError): self.verify()

    def test_no_approved_update_cannot_create_changes(self):
        self.transport()
        self.a['recommendations'][0]['decision'] = 'defer'
        with self.assertRaises(ValueError): self.verify()

    def test_request_and_filename_binding(self):
        bundle, _ = self.transport()
        for field, value in [('branch', 'other'), ('base', 'other'), ('repo', 'other/repo'), ('type', 'create_issue')]:
            output = copy.deepcopy(self.output); output['items'][0][field] = value
            (self.directory / 'agent_output.json').write_text(json.dumps(output))
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify()
        (self.directory / 'agent_output.json').write_text(json.dumps(self.output))
        bundle.rename(self.directory / 'wrong.bundle')
        with self.assertRaises(ValueError): self.verify()

    def test_clean_noop(self):
        self.a['recommendations'][0]['decision'] = 'defer'
        (self.directory / 'agent_output.json').write_text(json.dumps({'items': [{'type': 'noop'}]}))
        self.verify()

    def test_base_provenance_mismatch(self):
        self.transport()
        with self.assertRaises(ValueError):
            bundles.verify_bundle('b' * 40, self.directory, self.p, self.a, NOW)


if __name__ == '__main__':
    unittest.main()
