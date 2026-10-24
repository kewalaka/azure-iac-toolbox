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
    def preflight(self, case):
        script = dedent(blocks(RELEASE, '          script: |')[0], 12)
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
                              env=dict(os.environ, SOURCE_SHA=SHA, RELEASE_TAG='v0.1.1',
                                       SCRIPT=script, SCENARIO=json.dumps(case)))

    def case(self):
        return {'main': SHA, 'prs': [{'merged_at': '2026-01-10', 'base': {'ref': 'main'}, 'merge_commit_sha': SHA}]}

    def test_only_current_merged_main_can_release(self):
        self.assertEqual(self.preflight(self.case()).returncode, 0)
        for mutation in ['advanced', 'direct-push', 'other-base', 'different-merge', 'collision', 'draft']:
            case = self.case()
            if mutation == 'advanced': case['main'] = 'c' * 40
            if mutation == 'direct-push': case['prs'] = []
            if mutation == 'other-base': case['prs'][0]['base']['ref'] = 'other'
            if mutation == 'different-merge': case['prs'][0]['merge_commit_sha'] = 'c' * 40
            if mutation == 'collision': case['tag'] = 'c' * 40
            if mutation == 'draft': case['release'] = {'draft': True}
            with self.subTest(mutation=mutation):
                self.assertNotEqual(self.preflight(case).returncode, 0)

    def test_published_release_retry_is_noop(self):
        case = self.case(); case['tag'] = SHA; case['release'] = {'draft': False}
        result = self.preflight(case)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"value":"true"', result.stdout)

    def publish(self, mode, loaded_id=IMAGE_ID):
        script = dedent(blocks(RELEASE, '        run: |')[0], 10)
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


class ConfigurationTests(unittest.TestCase):
    def test_release_reuses_gated_main_image_without_checkout(self):
        self.assertIn("needs.validate.result == 'success' && github.event_name == 'push' && github.ref == 'refs/heads/main'", RELEASE)
        self.assertIn('uses: ./.github/workflows/validate.yml', RELEASE)
        self.assertIn('environment: release', RELEASE)
        self.assertNotIn('actions/checkout', RELEASE)
        validation = (ROOT / '.github/workflows/validate.yml').read_text()
        self.assertIn("success() && inputs.export-image && github.event_name == 'push' && github.ref == 'refs/heads/main'", validation)
        self.assertIn('HIGH,CRITICAL', validation)
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
        self.assertIn('agent-output-fallback', source)
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
