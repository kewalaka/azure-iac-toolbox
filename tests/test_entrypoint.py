import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts/toolbox-entrypoint.sh"


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.env = {
            **os.environ,
            "PATH": f"{self.work}:{os.environ['PATH']}",
            "MOCK_DIR": str(self.work),
            "MOCK_MODE": "ready",
            "TOOLBOX_DOCKER": "1",
        }
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            self.env.pop(key, None)
        self.mock("id", 'print("0")')
        self.mock("docker", '''
work = Path(os.environ["MOCK_DIR"])
mode = os.environ["MOCK_MODE"]
ready = mode == "existing" or (mode == "ready" and (work / "daemon.pid").exists())
sys.exit(0 if ready else 1)
''')
        self.mock("dockerd", '''
work = Path(os.environ["MOCK_DIR"])
(work / "daemon.args").write_text(" ".join(sys.argv[1:]))
if os.environ["MOCK_MODE"] == "crash":
    print("mock daemon failed", flush=True)
    sys.exit(1)
(work / "daemon.pid").write_text(str(os.getpid()))
def stop(*args):
    (work / "daemon.stopped").touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    signal.pause()
''')
        self.mock("sleep", "time.sleep(0.01)")

    def mock(self, name, body):
        path = self.work / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import os, signal, sys, time\nfrom pathlib import Path\n" + body
        )
        path.chmod(0o755)

    def run_entrypoint(self, code=0):
        return subprocess.run(
            ["bash", str(ENTRYPOINT), sys.executable, "-c", f"print('job ran'); exit({code})"],
            env=self.env, capture_output=True, text=True, timeout=10,
        )

    def assert_stopped(self):
        self.assertTrue((self.work / "daemon.stopped").exists())
        pid = int((self.work / "daemon.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_disabled_preserves_command_status_without_starting_docker(self):
        self.env["TOOLBOX_DOCKER"] = "0"
        result = self.run_entrypoint(7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("job ran", result.stdout)
        self.assertFalse((self.work / "daemon.args").exists())

    def test_default_mode_allows_external_engine_without_starting_docker(self):
        self.env.pop("TOOLBOX_DOCKER")
        self.env["DOCKER_HOST"] = "tcp://remote:2376"
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "daemon.args").exists())

    def test_local_command_keeps_standard_input(self):
        result = subprocess.run(
            ["bash", str(ENTRYPOINT), sys.executable, "-c", "print(input())"],
            env=self.env, input="job input\n", capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "job input\n")
        self.assert_stopped()

    def test_ready_runs_command_and_stops_owned_daemon(self):
        result = self.run_entrypoint(7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("job ran", result.stdout)
        self.assertEqual(
            (self.work / "daemon.args").read_text(),
            "--host=unix:///var/run/docker.sock --exec-opt=native.cgroupdriver=cgroupfs",
        )
        self.assert_stopped()

    def test_existing_daemon_is_not_started_or_stopped(self):
        self.env["MOCK_MODE"] = "existing"
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "daemon.args").exists())

    def test_crashed_daemon_blocks_command_and_reports_log(self):
        self.env["MOCK_MODE"] = "crash"
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("job ran", result.stdout)
        self.assertIn("mock daemon failed", result.stderr)

    def test_readiness_timeout_blocks_command_and_stops_daemon(self):
        self.env["MOCK_MODE"] = "timeout"
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("job ran", result.stdout)
        self.assertIn("within 30 seconds", result.stderr)
        self.assert_stopped()

    def test_invalid_configuration_is_rejected(self):
        for key, value in (("TOOLBOX_DOCKER", "yes"), ("DOCKER_HOST", "tcp://remote:2376")):
            with self.subTest(key=key):
                original = self.env.copy()
                self.env[key] = value
                self.assertEqual(self.run_entrypoint().returncode, 1)
                self.assertFalse((self.work / "daemon.args").exists())
                self.env = original

    def test_non_root_cannot_start_local_daemon(self):
        self.mock("id", 'print("1000")')
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires root", result.stderr)
        self.assertFalse((self.work / "daemon.args").exists())

    def test_termination_stops_command_and_daemon(self):
        command = "import os, pathlib, time; pathlib.Path(os.environ['MOCK_DIR'], 'job.pid').write_text(str(os.getpid())); time.sleep(30)"
        with subprocess.Popen(
            ["bash", str(ENTRYPOINT), sys.executable, "-c", command],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as process:
            deadline = time.monotonic() + 5
            while not (self.work / "job.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            process.terminate()
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, stderr)
        self.assert_stopped()
        pid = int((self.work / "job.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
