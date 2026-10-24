#!/usr/bin/env python3
import collections
import html
import json
import os
from pathlib import Path


SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


def summarize(report, outcome, run_url):
    lines = ["<!-- toolbox-image-scan -->", "## Container vulnerability scan", ""]
    valid = (
        isinstance(report, dict)
        and report.get("SchemaVersion") == 2
        and isinstance(report.get("Results", []), list)
    )
    if outcome != "success" or not valid:
        lines.append("**Scan incomplete: validation failed.** See the Actions logs.")
        blocked = True
    else:
        counts = collections.Counter()
        rows = []
        for result in report.get("Results", []):
            findings = collections.Counter()
            for vulnerability in result.get("Vulnerabilities") or []:
                severity = vulnerability.get("Severity", "UNKNOWN")
                findings[severity if severity in SEVERITIES else "UNKNOWN"] += 1
            counts.update(findings)
            if findings:
                target = html.escape(str(result.get("Target", "unknown")))
                target = target.replace("|", "&#124;").replace("\n", " ").replace("\r", " ")[:200]
                rows.append(f"| {target} | " + " | ".join(str(findings[s]) for s in SEVERITIES) + " |")
        blocked = bool(counts["HIGH"] or counts["CRITICAL"])
        lines.append("**Blocked: HIGH or CRITICAL vulnerabilities found.**" if blocked else "**Passed: no HIGH or CRITICAL vulnerabilities found.**")
        lines += ["", "| Target | CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        lines += rows[:50]
        lines.append("| **Total** | " + " | ".join(str(counts[s]) for s in SEVERITIES) + " |")
        if len(rows) > 50:
            lines.append("\nOnly the first 50 targets are shown; totals cover the full scan.")
    lines += ["", "Includes unfixed findings. Full JSON report is retained as the `image-scan` artifact.", "", f"[Actions run and logs]({run_url})", ""]
    return "\n".join(lines), blocked


if __name__ == "__main__":
    try:
        report = json.loads(Path("trivy-results.json").read_text())
    except (OSError, json.JSONDecodeError):
        report = None
    summary, blocked = summarize(report, os.environ["SCAN_OUTCOME"], os.environ["RUN_URL"])
    summary += f"\nBuild: {os.environ['BUILD_OUTCOME']}; smoke test: {os.environ['SMOKE_OUTCOME']}.\n"
    Path("image-summary.md").write_text(summary)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
        output.write(summary)
    raise SystemExit(1 if blocked else 0)
