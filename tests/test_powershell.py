"""The PowerShell 7 job wrapper: orchestration only, statuses come from the Python reports."""

import csv
import json
import shutil
import subprocess
import sys

import pytest

from .helpers import REPO_ROOT, served

PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 (pwsh) is not installed")


def run_job(tmp_path, url, entities):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"baseUrl": url, "outputRoot": str(tmp_path / "runs"), "entities": entities,
                               "extraArgs": ["--timeout", "2", "--backoff-base", "0.01", "--demo-pseudonym-key"]}), encoding="utf-8")
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(REPO_ROOT / "tools" / "Invoke-ExtractionJob.ps1"),
                             "-JobFile", str(job), "-Python", sys.executable, "-RunId", "test-run"],
                            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
    summary = tmp_path / "runs" / "test-run" / "run-summary.csv"
    rows = list(csv.DictReader(summary.open(encoding="utf-8-sig"))) if summary.exists() else []
    return result, rows


def test_all_complete_job_exits_zero(tmp_path) -> None:
    with served("healthy") as server:
        result, rows = run_job(tmp_path, server.url, ["Locations", "Technicians"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert [(r["Entity"], r["Status"]) for r in rows] == [("Locations", "COMPLETE"), ("Technicians", "COMPLETE")]


def test_worst_status_becomes_the_exit_code(tmp_path) -> None:
    with served("skipped_records") as server:
        result, rows = run_job(tmp_path, server.url, ["Locations", "WorkOrders"])
    assert result.returncode == 4, result.stdout + result.stderr
    assert {r["Entity"]: r["Status"] for r in rows} == {"Locations": "COMPLETE", "WorkOrders": "INCOMPLETE"}
