import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = (ROOT / '.github/workflows/release.yml').read_text()
SHA = 'a' * 40
IMAGE_ID = 'sha256:' + 'b' * 64


def blocks(text, header):
    width = len(header) - len(header.lstrip()) + 2
    return re.findall(r'^' + re.escape(header) + rf'\n((?:^ {{{width}}}[^\n]*\n|^\n)+)', text, re.M)


def dedent(text, width):
    return '\n'.join(line[width:] for line in text.splitlines())


class ReleaseTests(unittest.TestCase):
    def preflight(self, case, publishing=False):
        script = dedent(blocks(RELEASE, '          script: |')[int(publishing)], 12)
        harness = '''
const scenario = JSON.parse(process.env.SCENARIO);
const missing = () => { throw Object.assign(new Error('missing'), {status:404}); };
const github = { paginate: async () => scenario.prs,
  rest: { repos: {
    getBranch: async () => ({data:{commit:{sha:scenario.main}}}),
    listPullRequestsAssociatedWithCommit: () => {},
    getReleaseByTag: async () => scenario.release ? {data:scenario.release} : missing(),
  }, git: {getRef: async () => scenario.tag ? {data:{object:{sha:scenario.tag}}} : missing()} }
};
const core = {setOutput:(name,value) => console.log(JSON.stringify({name,value}))};
const context = {repo:{owner:'kewalaka',repo:'azure-iac-toolbox'}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context',process.env.SCRIPT)(github,core,context)
  .catch(error => { console.error(error.message); process.exitCode=1; });
'''
        return subprocess.run(['node', '-e', harness], text=True, capture_output=True,
                              env=dict(os.environ, SOURCE_SHA=SHA, RELEASE_TAG=case.get('version', 'v0.1.1'),
                                       RELEASE_SHA=case.get('release_sha', SHA),
                                       SCRIPT=script, SCENARIO=json.dumps(case)))

    def case(self):
        return {'main': SHA, 'prs': [{'merged_at': '2026-01-10', 'base': {'ref': 'main'}, 'merge_commit_sha': SHA}]}

    def test_only_current_merged_main_can_release(self):
        self.assertEqual(self.preflight(self.case()).returncode, 0)
        for mutation in ['advanced', 'direct-push', 'other-base', 'different-merge']:
            case = self.case()
            if mutation == 'advanced': case['main'] = 'c' * 40
            if mutation == 'direct-push': case['prs'] = []
            if mutation == 'other-base': case['prs'][0]['base']['ref'] = 'other'
            if mutation == 'different-merge': case['prs'][0]['merge_commit_sha'] = 'c' * 40
            with self.subTest(mutation=mutation):
                self.assertNotEqual(self.preflight(case).returncode, 0)

    def test_publishing_requires_stable_release_of_validated_sha(self):
        case = self.case(); case['tag'] = SHA; case['release'] = {'draft': False, 'prerelease': False}
        result = self.preflight(case, publishing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for mutation in ['advanced', 'collision', 'draft', 'prerelease', 'missing-tag',
                         'missing-release', 'different-release-sha', 'invalid-version', 'prerelease-version']:
            changed = dict(case)
            if mutation == 'advanced': changed['main'] = 'c' * 40
            if mutation == 'collision': changed['tag'] = 'c' * 40
            if mutation == 'draft': changed['release'] = {'draft': True}
            if mutation == 'prerelease': changed['release'] = {'prerelease': True}
            if mutation == 'missing-tag': changed.pop('tag')
            if mutation == 'missing-release': changed.pop('release')
            if mutation == 'different-release-sha': changed['release_sha'] = 'c' * 40
            if mutation == 'invalid-version': changed['version'] = 'latest'
            if mutation == 'prerelease-version': changed['version'] = 'v1.0.0-beta.1'
            with self.subTest(mutation=mutation):
                self.assertNotEqual(self.preflight(changed, publishing=True).returncode, 0)

    def publish(self, mode, loaded_id=IMAGE_ID):
        script = dedent(blocks(RELEASE.split('\n  publish:\n')[1], '        run: |')[0], 10)
        with tempfile.TemporaryDirectory() as work:
            path = Path(work)
            (path / 'validated-image.tar.gz').write_bytes(gzip.compress(b'tested image'))
            docker = path / 'docker'
            docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open('commands', 'a') as log: log.write(' '.join(args)+'\\n')
if args[0] == 'load': sys.stdin.buffer.read()
elif args[0] == 'image': print(os.environ['LOADED_ID'])
elif args[0] == 'login': sys.stdin.read()
elif args[0] == 'manifest':
    mode=os.environ['MODE']
    if mode in ('same', 'different'): print(json.dumps({'config':{'digest':os.environ['EXPECTED_IMAGE_ID'] if mode=='same' else 'sha256:'+'c'*64}}))
    else:
        print('manifest unknown' if mode=='missing' else 'unauthorized', file=sys.stderr)
        sys.exit(1)
''')
            docker.chmod(0o755)
            jq = path / 'jq'
            jq.write_text('''#!/usr/bin/env python3
import json, sys
print(json.load(open(sys.argv[-1]))['config']['digest'])
''')
            jq.chmod(0o755)
            env = dict(os.environ, PATH=str(path) + os.pathsep + os.environ['PATH'], MODE=mode,
                       LOADED_ID=loaded_id, EXPECTED_IMAGE_ID=IMAGE_ID, SOURCE_SHA=SHA,
                       GH_TOKEN='test', GITHUB_ACTOR='test', GH_REPO='Kewalaka/azure-iac-toolbox', RELEASE_TAG='v0.1.1')
            result = subprocess.run(['bash', '-c', script], cwd=work, env=env, text=True, capture_output=True)
            return result, (path / 'commands').read_text()

    def test_exact_validated_bytes_pushed_when_missing(self):
        result, commands = self.publish('missing')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.count('push ghcr.io/'), 2)
        self.assertIn('push ghcr.io/kewalaka/azure-iac-toolbox:sha-' + SHA, commands)
        self.assertNotIn('build', commands)

    def test_existing_identical_bytes_are_not_overwritten(self):
        result, commands = self.publish('same')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('push ', commands)

    def test_different_bytes_and_registry_errors_fail_closed(self):
        for mode in ['different', 'unauthorized']:
            result, commands = self.publish(mode)
            with self.subTest(mode=mode):
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('push ', commands)
        result, commands = self.publish('missing', 'sha256:' + 'c' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('login', commands)
        self.assertNotIn('push ', commands)


class PathDecisionTests(unittest.TestCase):
    def decision(self, files, event='pull_request', total=None):
        validation = (ROOT / '.github/workflows/validate.yml').read_text()
        script = dedent(blocks(validation, '          script: |')[0], 12)
        harness = '''
const scenario = JSON.parse(process.env.SCENARIO);
const github = {paginate: async () => scenario.files, rest:{pulls:{listFiles:()=>{}}}};
const core = {setOutput:(name,value)=>console.log(value)};
const context = {eventName:scenario.event, repo:{owner:'test',repo:'test'},
  issue:{number:3}, payload:{pull_request:{changed_files:scenario.total}}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context',process.env.SCRIPT)(github,core,context)
  .catch(error=>{console.error(error.message);process.exitCode=1;});
'''
        return subprocess.run(['node', '-e', harness], capture_output=True, text=True,
                              env=dict(os.environ, SCRIPT=script, SCENARIO=json.dumps({
                                  'files': files, 'event': event,
                                  'total': len(files) if total is None else total})))

    def test_only_prs_without_image_inputs_skip(self):
        for path in ['Dockerfile', '.dockerignore', '.tflint.hcl', 'scripts/install-tools.sh',
                     'scripts/toolbox-check.sh', 'scripts/toolbox-entrypoint.sh']:
            for file in [{'filename': path}, {'filename': 'archived', 'previous_filename': path}]:
                with self.subTest(file=file):
                    result = self.decision([file])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'true')
        files = [{'filename': path} for path in [
            'README.md', 'CHANGELOG.md', 'version.txt', 'tests/test_update_workflows.py',
            '.github/workflows/release.yml', '.release-please-manifest.json']]
        result = self.decision(files)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'false')
        for event in ['push', 'workflow_dispatch', 'workflow_call']:
            result = self.decision(files, event)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'true')

    def test_incomplete_file_discovery_cannot_skip(self):
        for files, total in [([], 1), ([{'filename': 'README.md'}] * 3000, 3000)]:
            result = self.decision(files, total=total)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Incomplete PR file list', result.stderr)

    def test_image_check_remains_stable_and_static_always_runs(self):
        validation = (ROOT / '.github/workflows/validate.yml').read_text()
        static = validation.split('\n  static:\n')[1].split('\n  image:\n')[0]
        image = validation.split('\n  image:\n')[1].split('\n  pr-report:\n')[0]
        self.assertNotIn('\n    if:', static)
        self.assertNotIn('\n    if:', image)
        self.assertIn('needs: static', image)
        self.assertIn('Image validation skipped', image)
        self.assertEqual(image.count("if: ${{ needs.static.outputs.image-required != 'false' }}"), 4)
        self.assertIn("!cancelled() && needs.static.outputs.image-required != 'false'", image)
        self.assertIn("steps.build.outcome == 'success'", image)
        self.assertIn("success() && inputs.export-image && github.event_name == 'push' && github.ref == 'refs/heads/main'", image)


class ConfigurationTests(unittest.TestCase):
    def test_main_push_has_only_reusable_validation(self):
        validation = (ROOT / '.github/workflows/validate.yml').read_text()
        triggers = validation.split('\non:\n')[1].split('\npermissions:')[0]
        self.assertIn('  push:\n    branches-ignore: [main]\n', triggers)
        for event in ('pull_request', 'workflow_dispatch', 'workflow_call'):
            self.assertIn(f'\n  {event}:\n', triggers)
        release_triggers = RELEASE.split('\non:\n')[1].split('\npermissions:')[0]
        self.assertEqual(release_triggers.strip(), 'push:\n    branches: [main]')
        # All main commits, including version/changelog-only release PRs, must validate.
        self.assertNotIn('paths:', release_triggers)
        self.assertNotIn('paths-ignore:', release_triggers)
        self.assertEqual(RELEASE.count('uses: ./.github/workflows/validate.yml'), 1)

    def test_manifest_tracks_the_simple_package_version(self):
        config = json.loads((ROOT / 'release-please-config.json').read_text())
        manifest = json.loads((ROOT / '.release-please-manifest.json').read_text())
        package = config['packages']['.']
        self.assertEqual(package['release-type'], 'simple')
        self.assertEqual(package['package-name'], 'azure-iac-toolbox')
        self.assertFalse(package['include-component-in-tag'])
        self.assertTrue(package['include-v-in-tag'])
        self.assertEqual(package['changelog-path'], 'CHANGELOG.md')
        self.assertTrue((ROOT / package['changelog-path']).is_file())
        self.assertEqual(manifest['.'], (ROOT / 'version.txt').read_text().strip())
        self.assertRegex(manifest['.'], r'^\d+\.\d+\.\d+$')
        self.assertRegex(config['bootstrap-sha'], r'^[0-9a-f]{40}$')
        self.assertNotIn('release-as', config)
        self.assertNotIn('release-as', package)

    def test_release_please_owns_versions_tags_and_releases(self):
        release_job = RELEASE.split('\n  release-please:\n')[1].split('\n  publish:\n')[0]
        publish_job = RELEASE.split('\n  publish:\n')[1]
        self.assertIn('needs: validate', release_job)
        self.assertIn("needs.validate.result == 'success'", release_job)
        self.assertIn('googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7', release_job)
        self.assertIn('config-file: release-please-config.json', release_job)
        self.assertIn('manifest-file: .release-please-manifest.json', release_job)
        self.assertIn('token: ${{ secrets.RELEASE_PLEASE_TOKEN }}', release_job)
        self.assertIn('if [ -z "$RELEASE_PLEASE_TOKEN" ]', release_job)
        self.assertIn("needs.release-please.outputs.release-created == 'true'", publish_job)
        self.assertIn('RELEASE_TAG: ${{ needs.release-please.outputs.tag }}', publish_job)
        self.assertIn('RELEASE_SHA: ${{ needs.release-please.outputs.sha }}', publish_job)
        self.assertIn('environment: release', release_job)
        self.assertIn('environment: release', publish_job)
        self.assertNotIn('packages: write', release_job)
        self.assertIn('contents: read\n      packages: write', publish_job)
        self.assertNotIn('contents: write', publish_job)
        self.assertNotIn('github.run_number', RELEASE)
        self.assertNotIn('createRef', RELEASE)
        self.assertNotIn('createRelease', RELEASE)
        self.assertNotIn('published !=', publish_job)

    def test_release_reuses_scanned_main_image_without_checkout(self):
        self.assertIn("needs.validate.result == 'success' && github.event_name == 'push' && github.ref == 'refs/heads/main'", RELEASE)
        self.assertIn('uses: ./.github/workflows/validate.yml', RELEASE)
        self.assertIn('environment: release', RELEASE)
        self.assertNotIn('actions/checkout', RELEASE)
        validation = (ROOT / '.github/workflows/validate.yml').read_text()
        self.assertIn("success() && inputs.export-image && github.event_name == 'push' && github.ref == 'refs/heads/main'", validation)
        self.assertIn('HIGH,CRITICAL', validation)
        self.assertIn('Vulnerability findings are informational', RELEASE)
        self.assertNotIn('--ignore-unfixed', validation)
        self.assertIn('name: validated-image', validation)
        self.assertIn('permissions:\n  contents: read', validation)

    def test_agent_credentials_and_diff_gate(self):
        source = (ROOT / '.github/workflows/package-updates.md').read_text()
        lock = (ROOT / '.github/workflows/package-updates.lock.yml').read_text()
        agent = lock.split('\n  agent:\n')[1].split('\n  conclusion:\n')[0]
        discovery = source.split('  discovery:\n')[1].split('  safe_outputs:\n')[0]
        self.assertNotIn('environment:', agent)
        self.assertNotIn(': write', agent.split('steps:')[0])
        self.assertNotIn('environment:', discovery)
        self.assertIn('test -z "$MISSCOPED_PR_TOKEN$TOKEN_OVERRIDE"', discovery)
        self.assertIn("needs.verify-proposal.result == 'success'", lock)
        writer = lock.split('\n  safe_outputs:\n')[1].split('\n  verify-proposal:\n')[0]
        self.assertIn('needs.detection.result', writer)
        self.assertIn('environment: package-updates', writer)
        self.assertIn('name: agent-output-fallback', source)
        self.assertIn('verify_fallback(', (ROOT / 'scripts/verify-update-bundle.py').read_text())
        self.assertIn('allowed-files: [Dockerfile, .tflint.hcl]', source)
        self.assertIn('min-integrity: approved', source)
        self.assertIn('cli-proxy: false', source)

    def test_all_external_actions_are_sha_pinned(self):
        for workflow in (ROOT / '.github/workflows').glob('*.yml'):
            for action in re.findall(r'^\s+(?:- )?uses: ([^\s#]+)', workflow.read_text(), re.M):
                if action.startswith('./'): continue
                with self.subTest(workflow=workflow.name, action=action):
                    self.assertRegex(action, r'@[0-9a-f]{40}$')


if __name__ == '__main__':
    unittest.main()
