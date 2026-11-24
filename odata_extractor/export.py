"""Writes an extraction to disk and validates what was written.

Layout under <output>/<Entity>/:

    raw/records.jsonl          unique valid records as received (may contain personal data: keep local)
    raw/quarantine.jsonl       records without a valid key (only when there are any)
    publishable/records.jsonl  records after the privacy rules
    publishable/records.csv    same, as CSV, with spreadsheet-formula injection neutralised
    report.json                integrity status, reasons, metrics, timeline, schema and privacy summary
    summary.md                 human-readable summary
    manifest.json              SHA-256, size and line count of every file above

Nothing written contains request headers, tokens or pseudonymization keys.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from . import __version__
from .client import Extraction
from .integrity import IntegrityReport
from .privacy import EMAIL, Pseudonymizer, apply

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _jsonl(records: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value  # neutralise formulas when opened in a spreadsheet
    return value


def _csv(records: list[dict[str, Any]]) -> bytes:
    columns: list[str] = []
    for record in records:
        for name in record:
            if name not in columns:
                columns.append(name)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for record in records:
        writer.writerow(["" if record.get(c) is None else _csv_safe(record.get(c)) for c in columns])
    return buffer.getvalue().encode("utf-8")


def build_report(run: Extraction, integrity: IntegrityReport, schema: dict[str, Any] | None, privacy: dict[str, Any],
                 deterministic: bool = False) -> dict[str, Any]:
    report = {"tool": "resilient-odata-extractor", "version": __version__, "entity": run.entity, "key": run.key,
              "integrity": integrity.to_dict(), "metrics": run.metrics.to_dict(), "timeline": run.timeline,
              "schema": schema, "privacy": privacy,
              "reconciliation": None if run.reconciliation is None else
              {"available": run.reconciliation.available, "ids": len(run.reconciliation.ids), "reason": run.reconciliation.reason}}
    if deterministic:
        report["metrics"].pop("bytes_received", None)
    return report


def summary_markdown(report: dict[str, Any]) -> str:
    integrity, metrics = report["integrity"], report["metrics"]
    lines = [f"# Extraction summary: {report['entity']}", "", f"**Integrity status: {integrity['status']}**", "",
             f"- Requests: {metrics['requests_total']} ({metrics['requests_by_phase']}), retries: {metrics['retries']}",
             f"- Pages: {integrity['counts'].get('pages')}, records received: {integrity['counts'].get('records_received')}, "
             f"unique: {integrity['counts'].get('unique_records')}", "", "## Why", ""]
    lines += [f"- {r['code']}: {r['message']}" for r in integrity["reasons"]] or ["- no problems found"]
    if integrity["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {w['code']}: {w['message']}" for w in integrity["warnings"]]
    if integrity["basis"]:
        lines += ["", "## Checks that passed", ""] + [f"- {b}" for b in integrity["basis"]]
    lines += ["", "Raw output may contain personal data and must stay local; share only the publishable folder."]
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, run: Extraction, integrity: IntegrityReport, rules: dict[str, Any],
                  pseudonymizer: Pseudonymizer, schema: dict[str, Any] | None = None) -> Path:
    target = Path(out_dir) / run.entity
    target.mkdir(parents=True, exist_ok=True)
    published = [apply(r, run.entity, rules, pseudonymizer) for r in integrity.unique_records]
    privacy = {"rules": rules.get("entities", {}).get(run.entity, {}), "pseudonym_key_source": pseudonymizer.source,
               "safety_net": "emails and phone numbers in any string replaced"}
    report = build_report(run, integrity, schema, privacy)
    files: dict[str, bytes] = {
        "raw/records.jsonl": _jsonl(integrity.unique_records),
        "publishable/records.jsonl": _jsonl(published),
        "publishable/records.csv": _csv(published),
        "report.json": (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        "summary.md": summary_markdown(report).encode("utf-8"),
    }
    if integrity.invalid_records:
        files["raw/quarantine.jsonl"] = _jsonl(integrity.invalid_records)
    manifest = {"entity": run.entity, "status": integrity.status, "files": {}}
    for relative, data in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest["files"][relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                                       "lines": data.count(b"\n")}
    (target / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return target


def validate_output(directory: Path) -> list[str]:
    """Problems found in an output folder; empty means files, counts and privacy checks agree."""
    directory = Path(directory)
    problems: list[str] = []
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [f"cannot read manifest.json or report.json: {err}"]
    for relative, expected in manifest["files"].items():
        path = directory / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            problems.append(f"{relative}: content differs from manifest")
    unique = report["integrity"]["counts"].get("unique_records")
    for relative in ("raw/records.jsonl", "publishable/records.jsonl"):
        path = directory / relative
        if path.is_file() and path.read_text(encoding="utf-8").count("\n") != unique:
            problems.append(f"{relative}: line count does not equal unique_records ({unique})")
    csv_path = directory / "publishable/records.csv"
    if csv_path.is_file():
        rows = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
        if len(rows) - 1 != unique:
            problems.append(f"publishable/records.csv: {len(rows) - 1} data rows, expected {unique}")
    dropped = set(report.get("privacy", {}).get("rules", {}).get("drop", []))
    publishable = directory / "publishable/records.jsonl"
    if publishable.is_file():
        for number, line in enumerate(publishable.read_text(encoding="utf-8").splitlines(), start=1):
            record = json.loads(line)
            if dropped & set(record):
                problems.append(f"publishable line {number}: contains dropped field(s) {sorted(dropped & set(record))}")
                break
            if EMAIL.search(line):
                problems.append(f"publishable line {number}: contains an unmasked email address")
                break
    text = (directory / "report.json").read_text(encoding="utf-8")
    if "Bearer " in text or "Authorization" in text:
        problems.append("report.json mentions an authorization header")
    return problems
