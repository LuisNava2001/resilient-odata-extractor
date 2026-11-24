"""The integrity engine: what can and cannot be concluded about an extraction.

Statuses, in order of precedence:

FAILED        The extraction stopped on a technical error (retries exhausted, malformed body, unsafe nextLink,
              non-retryable HTTP status, budget). No completeness claim is made.
INCOMPLETE    There is positive evidence that records are missing or unusable: records without a valid key,
              paging stopped by a nextLink cycle, a stable $count above the number of unique records, an empty
              page that ended paging early, or the key reconciliation found IDs on the server that were not
              extracted.
INDETERMINATE No evidence of loss, but completeness cannot be demonstrated: $count changed during the
              extraction (no consistent snapshot), duplicates disagree on content, more unique records than
              $count, records extracted that the server no longer lists, or no count and no reconciliation.
COMPLETE      Paging ended normally, every key is valid and unique after removing identical duplicates, and the
              available evidence agrees: a stable $count equal to the unique count, and (when the provider
              supports it) an independent key reconciliation with exactly the same ID set. Anomalies such as
              identical duplicates are reported as warnings and are only tolerated when the reconciliation
              confirms the set.

COMPLETE means complete as far as the protocol and this provider let us check. It says nothing about whether the
field values themselves are correct, and a matching count alone is never treated as proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import Extraction
from .dataset import digest

COMPLETE, INCOMPLETE, INDETERMINATE, FAILED = "COMPLETE", "INCOMPLETE", "INDETERMINATE", "FAILED"
SAMPLE = 20


@dataclass
class IntegrityReport:
    status: str
    entity: str
    key: str
    reasons: list[dict[str, Any]] = field(default_factory=list)  # why the status is what it is
    warnings: list[dict[str, Any]] = field(default_factory=list)
    basis: list[str] = field(default_factory=list)  # checks that passed
    counts: dict[str, Any] = field(default_factory=dict)
    unique_records: list[dict[str, Any]] = field(default_factory=list)
    invalid_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "entity": self.entity, "key": self.key, "reasons": self.reasons,
                "warnings": self.warnings, "basis": self.basis, "counts": self.counts}


def _key_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return "integer"
    if isinstance(value, str) and value.strip():
        return "string"
    return None


def _expected_key_kind(run: Extraction) -> str | None:
    """The declared key type, or else the type of the first usable key: keys must not mix types."""
    if run.key_type:
        return run.key_type
    for page in run.pages:
        for record in page.records:
            kind = _key_kind(record.get(run.key))
            if kind:
                return kind
    return None


def _finding(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _ordered(values: set[Any]) -> list[Any]:
    """Integers numerically, then anything else as text."""
    return sorted(values, key=lambda v: (0, v, "") if isinstance(v, int) and not isinstance(v, bool) else (1, 0, str(v)))


def assess(run: Extraction) -> IntegrityReport:
    report = IntegrityReport(status=COMPLETE, entity=run.entity, key=run.key)
    seen: dict[Any, str] = {}
    conflicting: set[Any] = set()
    identical_duplicates = 0
    order_violations = 0
    highest: Any = None
    received = 0
    expected_kind = _expected_key_kind(run)
    for page in run.pages:
        for record in page.records:
            received += 1
            key = record.get(run.key)
            if _key_kind(key) is None or _key_kind(key) != expected_kind:
                report.invalid_records.append(record)
                continue
            fingerprint = digest(record)
            if key in seen:
                if seen[key] == fingerprint:
                    identical_duplicates += 1
                else:
                    conflicting.add(key)
                continue
            seen[key] = fingerprint
            report.unique_records.append(record)
            if run.ordered_by_key and highest is not None and type(key) is type(highest) and key < highest:
                order_violations += 1
            if highest is None or (type(key) is type(highest) and key > highest):
                highest = key

    counts_reported = [page.count for page in run.pages if page.count is not None]
    unique = len(report.unique_records)
    extracted_ids = set(seen)
    report.counts = {
        "pages": len(run.pages), "records_received": received, "unique_records": unique,
        "identical_duplicates": identical_duplicates, "conflicting_duplicates": len(conflicting),
        "invalid_records": len(report.invalid_records), "count_first_page": counts_reported[0] if counts_reported else None,
        "count_last_page": counts_reported[-1] if counts_reported else None, "count_values_seen": sorted(set(counts_reported)),
    }

    def reason(code: str, message: str, **extra: Any) -> None:
        report.reasons.append(_finding(code, message, **extra))

    def warn(code: str, message: str, **extra: Any) -> None:
        report.warnings.append(_finding(code, message, **extra))

    if run.error is not None:
        report.status = FAILED
        reason(run.error.code, run.error.message)
        report.counts["stopped_after_pages"] = len(run.pages)
        return report

    for finding in run.client_findings:
        (reason if finding["code"] == "NEXTLINK_CYCLE" else warn)(finding["code"], finding["message"], page=finding.get("page"))

    incomplete, indeterminate = [], []
    if report.invalid_records:
        incomplete.append("INVALID_KEY")
        reason("INVALID_KEY", f"{len(report.invalid_records)} record(s) have a missing, empty or non-{expected_kind} {run.key}; "
               "they cannot be identified or deduplicated and were quarantined")
    if run.stop_reason == "cycle":
        incomplete.append("NEXTLINK_CYCLE")

    count_stable = bool(counts_reported) and len(set(counts_reported)) == 1
    expected = counts_reported[0] if count_stable else None
    if counts_reported and not count_stable:
        indeterminate.append("COUNT_CHANGED")
        coincidence = (f"; the final count happens to equal the {unique} unique records, which by itself proves nothing"
                       if counts_reported[-1] == unique else "")
        reason("COUNT_CHANGED", f"@odata.count changed during the extraction ({counts_reported[0]} -> {counts_reported[-1]}): "
               f"offset paging over a changing dataset is not a consistent snapshot{coincidence}")
    if count_stable and unique < expected:
        incomplete.append("COUNT_SHORTFALL")
        reason("COUNT_SHORTFALL", f"{expected - unique} fewer unique records than @odata.count ({unique} of {expected})")
        if run.pages and not run.pages[-1].records and len(run.pages) > 1:
            incomplete.append("PREMATURE_END")
            reason("PREMATURE_END", f"page {len(run.pages)} was an empty 200 response without nextLink while records were still expected")
    elif count_stable and unique > expected:
        indeterminate.append("COUNT_EXCESS")
        reason("COUNT_EXCESS", f"{unique - expected} more unique records than @odata.count ({unique} vs {expected})")
    if conflicting:
        indeterminate.append("CONFLICTING_DUPLICATE")
        reason("CONFLICTING_DUPLICATE", f"{len(conflicting)} key(s) arrived more than once with different content",
               sample=_ordered(conflicting)[:SAMPLE])
    if identical_duplicates:
        warn("DUPLICATES_REMOVED", f"{identical_duplicates} identical duplicate record(s) removed")
    if order_violations:
        warn("ORDER_VIOLATION", f"{order_violations} new key(s) arrived out of the requested {run.key} order")

    recon = run.reconciliation
    if recon is not None and recon.available:
        server_ids = set(recon.ids)
        missing = _ordered(server_ids - extracted_ids)
        extra = _ordered(extracted_ids - server_ids)
        report.counts.update(reconciled_ids=len(server_ids), missing_ids=len(missing), extra_ids=len(extra))
        if missing:
            incomplete.append("RECONCILIATION_MISSING")
            reason("RECONCILIATION_MISSING", f"key reconciliation found {len(missing)} ID(s) on the server that were not extracted",
                   sample=missing[:SAMPLE])
        if extra:
            indeterminate.append("RECONCILIATION_EXTRA")
            reason("RECONCILIATION_EXTRA", f"{len(extra)} extracted ID(s) are no longer listed by the server", sample=extra[:SAMPLE])
        if not missing and not extra:
            report.basis.append(f"independent key reconciliation returned exactly the same {len(server_ids)} IDs")
    else:
        why = recon.reason if recon is not None else "reconciliation disabled"
        report.counts["reconciliation"] = f"unavailable: {why}"
        if not counts_reported:
            indeterminate.append("UNVERIFIABLE")
            reason("UNVERIFIABLE", "the provider returned no @odata.count and the key reconciliation was unavailable "
                   f"({why}); nothing can confirm that paging returned every record")
        elif identical_duplicates or order_violations or any(f["code"] == "DUPLICATE_PAGE" for f in run.client_findings):
            indeterminate.append("ANOMALIES_UNVERIFIED")
            reason("ANOMALIES_UNVERIFIED", "paging anomalies occurred and no independent reconciliation was available to rule out loss")

    if run.stop_reason == "end":
        report.basis.insert(0, "paging ended normally (last page had no nextLink)")
    if count_stable and unique == expected:
        report.basis.append(f"@odata.count was stable at {expected} and equals the unique record count")
    if not report.invalid_records:
        report.basis.append(f"every record has a valid, unique {run.key} after removing identical duplicates")

    if incomplete:
        report.status = INCOMPLETE
    elif indeterminate:
        report.status = INDETERMINATE
    else:
        report.status = COMPLETE
    return report
