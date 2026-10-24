import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "report_image_scan", Path(__file__).resolve().parents[1] / "scripts/report-image-scan.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImageReportTests(unittest.TestCase):
    def report(self, severities, target="ubuntu:24.04"):
        return {
            "SchemaVersion": 2,
            "Results": [{"Target": target, "Vulnerabilities": [{"Severity": severity} for severity in severities]}],
        }

    def test_high_and_critical_block_even_without_fixes(self):
        for severity in ("HIGH", "CRITICAL"):
            with self.subTest(severity=severity):
                summary, blocked = module.summarize(self.report([severity]), "success", "https://example.com/run")
                self.assertTrue(blocked)
                self.assertIn("Blocked", summary)

    def test_lower_severities_pass_and_are_counted(self):
        summary, blocked = module.summarize(self.report(["MEDIUM", "LOW", "LOW", "UNKNOWN"]), "success", "https://example.com/run")
        self.assertFalse(blocked)
        self.assertIn("| **Total** | 0 | 0 | 1 | 2 | 1 |", summary)

    def test_empty_scan_passes(self):
        summary, blocked = module.summarize({"SchemaVersion": 2}, "success", "https://example.com/run")
        self.assertFalse(blocked)
        self.assertIn("Passed", summary)

    def test_incomplete_scans_fail_closed(self):
        for report, outcome in ((None, "failure"), ({}, "success"), (self.report([]), "skipped")):
            with self.subTest(report=report, outcome=outcome):
                summary, blocked = module.summarize(report, outcome, "https://example.com/run")
                self.assertTrue(blocked)
                self.assertIn("Scan incomplete", summary)

    def test_command_writes_both_summaries_before_failing_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "trivy-results.json").write_text(module.json.dumps(self.report(["HIGH"])))
            environment = dict(os.environ, SCAN_OUTCOME="success", BUILD_OUTCOME="success",
                               SMOKE_OUTCOME="success", RUN_URL="https://example.com/run",
                               GITHUB_STEP_SUMMARY=str(work / "actions-summary.md"))
            result = subprocess.run([sys.executable, "-B", module.__file__], cwd=work,
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            summary = (work / "image-summary.md").read_text()
            self.assertEqual(summary, (work / "actions-summary.md").read_text())
            self.assertIn("Blocked", summary)
            self.assertIn("Build: success; smoke test: success", summary)

    def test_target_cannot_inject_markdown_rows_or_html(self):
        summary, _ = module.summarize(self.report(["LOW"], "target|\n<script>"), "success", "https://example.com/run")
        self.assertIn("target&#124; &lt;script&gt;", summary)
        self.assertNotIn("<script>", summary)

    def test_truncated_targets_still_include_all_findings_in_totals(self):
        report = self.report(["LOW"])
        report["Results"] *= 51
        summary, blocked = module.summarize(report, "success", "https://example.com/run")
        self.assertFalse(blocked)
        self.assertIn("| **Total** | 0 | 0 | 0 | 51 | 0 |", summary)
        self.assertIn("first 50 targets", summary)


if __name__ == "__main__":
    unittest.main()
