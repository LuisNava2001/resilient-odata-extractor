"""Integrity rules on hand-built extractions: each status, its conditions and the precedence between them."""

from odata_extractor.client import Extraction, ExtractionError, Page, Reconciliation
from odata_extractor.dataset import digest
from odata_extractor.integrity import assess


def rec(i, **extra):
    return {"Id": i, "v": f"r{i}", **extra}


def make(pages, *, counts=None, stop="end", recon=None, error=None, findings=(), key_type="integer"):
    run = Extraction(entity="Things", key="Id", ordered_by_key=True, key_type=key_type)
    for n, records in enumerate(pages, start=1):
        count = counts[n - 1] if counts else None
        run.pages.append(Page(n, f"/p{n}", records, count, None, digest(records)))
    run.stop_reason, run.error, run.reconciliation = stop, error, recon
    run.client_findings = list(findings)
    return run


def codes(report):
    return [r["code"] for r in report.reasons]


ALL = [[rec(1), rec(2)], [rec(3), rec(4)]]


def test_complete_requires_agreeing_evidence() -> None:
    report = assess(make(ALL, counts=[4, 4], recon=Reconciliation(True, [1, 2, 3, 4])))
    assert report.status == "COMPLETE" and report.reasons == []
    assert any("reconciliation" in b for b in report.basis) and any("stable at 4" in b for b in report.basis)


def test_count_only_complete_states_its_basis() -> None:
    report = assess(make(ALL, counts=[4, 4], recon=Reconciliation(False, [], "HTTP 501")))
    assert report.status == "COMPLETE"
    assert not any("reconciliation" in b for b in report.basis)
    assert "unavailable" in report.counts["reconciliation"]


def test_failed_overrides_everything() -> None:
    report = assess(make(ALL[:1], counts=[4], error=ExtractionError("TIMEOUT", "slow")))
    assert report.status == "FAILED" and codes(report) == ["TIMEOUT"]


def test_shortfall_against_stable_count_is_incomplete() -> None:
    report = assess(make([[rec(1), rec(2)], [rec(4)]], counts=[4, 4]))
    assert report.status == "INCOMPLETE" and "COUNT_SHORTFALL" in codes(report)


def test_reconciliation_names_missing_ids() -> None:
    report = assess(make([[rec(1), rec(2)], [rec(4)]], counts=[4, 4], recon=Reconciliation(True, [1, 2, 3, 4])))
    missing = next(r for r in report.reasons if r["code"] == "RECONCILIATION_MISSING")
    assert missing["sample"] == [3]


def test_premature_empty_page() -> None:
    report = assess(make([[rec(1), rec(2)], []], counts=[4, 4]))
    assert report.status == "INCOMPLETE" and "PREMATURE_END" in codes(report)


def test_cycle_is_incomplete() -> None:
    finding = {"code": "NEXTLINK_CYCLE", "page": 2, "message": "cycle"}
    report = assess(make(ALL, counts=[4, 4], stop="cycle", findings=[finding]))
    assert report.status == "INCOMPLETE" and "NEXTLINK_CYCLE" in codes(report)


def test_invalid_and_mistyped_keys_are_quarantined() -> None:
    pages = [[rec(1), {"Id": None, "v": "x"}, {"Id": "7", "v": "y"}, {"v": "z"}, {"Id": True}], [rec(2)]]
    report = assess(make(pages, counts=[2, 2]))
    assert report.status == "INCOMPLETE" and len(report.invalid_records) == 4
    assert [r["Id"] for r in report.unique_records] == [1, 2]


def test_key_type_is_taken_from_the_first_valid_key_without_metadata() -> None:
    report = assess(make([[{"Id": "a"}, {"Id": 5}, {"Id": "b"}]], counts=[2], key_type=None))
    assert len(report.invalid_records) == 1 and report.invalid_records[0]["Id"] == 5


def test_identical_duplicates_tolerated_only_when_verified() -> None:
    pages = [[rec(1), rec(2)], [rec(2), rec(3)], [rec(4)]]
    verified = assess(make(pages, counts=[4, 4, 4], recon=Reconciliation(True, [1, 2, 3, 4])))
    assert verified.status == "COMPLETE" and [w["code"] for w in verified.warnings] == ["DUPLICATES_REMOVED"]
    unverified = assess(make(pages, counts=[4, 4, 4], recon=Reconciliation(False, [], "n/a")))
    assert unverified.status == "INDETERMINATE" and "ANOMALIES_UNVERIFIED" in codes(unverified)


def test_conflicting_duplicates_are_indeterminate() -> None:
    report = assess(make([[rec(1), rec(2)], [rec(2, v="changed"), rec(3)]], counts=[3, 3], recon=Reconciliation(True, [1, 2, 3])))
    assert report.status == "INDETERMINATE" and "CONFLICTING_DUPLICATE" in codes(report)


def test_count_change_is_indeterminate_even_when_the_final_count_matches() -> None:
    report = assess(make(ALL, counts=[3, 4], recon=Reconciliation(True, [1, 2, 3, 4])))
    assert report.status == "INDETERMINATE" and codes(report) == ["COUNT_CHANGED"]
    assert "proves nothing" in report.reasons[0]["message"]


def test_matching_count_with_the_wrong_ids_is_not_complete() -> None:
    """Deleted-during-paging: same number of records, but one of them is gone and one real record was skipped."""
    report = assess(make(ALL, counts=[4, 4], recon=Reconciliation(True, [1, 2, 3, 5])))
    assert report.status == "INCOMPLETE"
    assert {"RECONCILIATION_MISSING", "RECONCILIATION_EXTRA"} <= set(codes(report))


def test_no_count_and_no_reconciliation_is_indeterminate() -> None:
    report = assess(make(ALL, recon=Reconciliation(False, [], "HTTP 501")))
    assert report.status == "INDETERMINATE" and codes(report) == ["UNVERIFIABLE"]


def test_no_count_but_reconciliation_can_confirm() -> None:
    report = assess(make(ALL, recon=Reconciliation(True, [1, 2, 3, 4])))
    assert report.status == "COMPLETE"


def test_more_records_than_count_is_indeterminate() -> None:
    report = assess(make(ALL, counts=[3, 3]))
    assert report.status == "INDETERMINATE" and "COUNT_EXCESS" in codes(report)


def test_order_violation_is_reported() -> None:
    report = assess(make([[rec(3), rec(1)], [rec(2), rec(4)]], counts=[4, 4], recon=Reconciliation(True, [1, 2, 3, 4])))
    assert "ORDER_VIOLATION" in [w["code"] for w in report.warnings]


def test_precedence_incomplete_before_indeterminate() -> None:
    report = assess(make([[rec(1)], [{"Id": None}]], counts=[3, 2]))
    assert report.status == "INCOMPLETE"
    assert {"INVALID_KEY", "COUNT_CHANGED"} <= set(codes(report))
