import csv
import io
import json

from odata_extractor.dataset import generate
from odata_extractor.export import validate_output, write_outputs
from odata_extractor.integrity import assess
from odata_extractor.pipeline import run
from odata_extractor.privacy import Pseudonymizer, apply, load_rules, mask_email, scrub_text

from .helpers import http_client, served

RULES = load_rules()
DEMO = Pseudonymizer.from_environment(demo=True)
TECH = generate()["Technicians"][0]


def test_rules_drop_mask_pseudonymize_and_redact() -> None:
    out = apply(TECH, "Technicians", RULES, DEMO)
    assert "Phone" not in out
    assert out["Email"].endswith("@example.org") and out["Email"].count("*") == 3 and out["Email"] != TECH["Email"]
    assert out["EmployeeNumber"].startswith("psn_") and out["EmployeeNumber"] != TECH["EmployeeNumber"]
    assert out["FullName"] == "[REDACTED]"
    assert out["TechnicianId"] == TECH["TechnicianId"] and out["Skill"] == TECH["Skill"]


def test_pseudonyms_are_stable_per_key_and_unlinkable_across_keys() -> None:
    other = Pseudonymizer(b"another-key", "test")
    assert DEMO("T.f", "EMP-1") == DEMO("T.f", "EMP-1")
    assert DEMO("T.f", "EMP-1") != other("T.f", "EMP-1")
    assert DEMO("T.f", "EMP-1") != DEMO("T.g", "EMP-1")  # scoped by field


def test_text_scrubbing_and_safety_net() -> None:
    assert scrub_text("Call +1-555-0142 or mail a.b@example.org") == "Call [PHONE] or mail [EMAIL]"
    assert scrub_text("Scheduled 2026-01-05T08:00:00Z, cost 12345") == "Scheduled 2026-01-05T08:00:00Z, cost 12345"
    sneaky = apply({"Id": 1, "Comment": "ping x.y@example.org"}, "WorkOrders", RULES, DEMO)
    assert sneaky["Comment"] == "ping [EMAIL]"  # not configured, caught by the safety net
    assert mask_email("nobody") == "***"


def test_random_key_source_is_reported_but_never_stored(monkeypatch) -> None:
    monkeypatch.delenv("ODATA_EXTRACTOR_PSEUDONYM_KEY", raising=False)
    assert "random" in Pseudonymizer.from_environment().source
    monkeypatch.setenv("ODATA_EXTRACTOR_PSEUDONYM_KEY", "k-from-env-987")
    pseudo = Pseudonymizer.from_environment()
    assert "environment" in pseudo.source and "k-from-env-987" not in pseudo.source


def test_pseudonym_key_from_environment_is_never_written(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ODATA_EXTRACTOR_PSEUDONYM_KEY", "hmac-key-must-not-leak-42")
    with served("healthy") as server:
        client = http_client(server)
        outcome = run(client.config, "Technicians", tmp_path, client=client, pseudonymizer=Pseudonymizer.from_environment())
    published = (outcome.output / "publishable/records.jsonl").read_text(encoding="utf-8")
    assert "psn_" in published
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "hmac-key-must-not-leak-42" not in path.read_text(encoding="utf-8"), path.name


def extract_to(tmp_path, entity="MaintenanceRequests", scenario="healthy"):
    with served(scenario) as server:
        client = http_client(server)
        return run(client.config, entity, tmp_path, client=client, pseudonymizer=DEMO)


def test_output_layout_separates_raw_from_publishable(tmp_path) -> None:
    outcome = extract_to(tmp_path)
    folder = outcome.output
    for relative in ("raw/records.jsonl", "publishable/records.jsonl", "publishable/records.csv", "report.json",
                     "summary.md", "manifest.json"):
        assert (folder / relative).is_file(), relative
    raw = (folder / "raw/records.jsonl").read_text(encoding="utf-8")
    published = (folder / "publishable/records.jsonl").read_text(encoding="utf-8")
    assert "@example.org" in raw and "+1-555-01" in raw
    assert "[EMAIL]" in published and "[PHONE]" in published
    for line in published.splitlines():
        record = json.loads(line)
        assert record["RequestedByName"] == "[REDACTED]" and "***@" in record["RequestedByEmail"]
    rows = list(csv.DictReader(io.StringIO((folder / "publishable/records.csv").read_text(encoding="utf-8"))))
    assert len(rows) == 320
    assert validate_output(folder) == []


def test_validate_detects_tampering_and_leaks(tmp_path) -> None:
    folder = extract_to(tmp_path).output
    published = folder / "publishable/records.jsonl"
    original = published.read_text(encoding="utf-8")
    published.write_text(original.replace("[REDACTED]", "Real Person", 1), encoding="utf-8")
    assert any("differs from manifest" in p for p in validate_output(folder))
    first = json.loads(original.splitlines()[0])
    first["RequestedByEmail"] = "leak@example.org"
    published.write_text(json.dumps(first) + "\n" + "\n".join(original.splitlines()[1:]) + "\n", encoding="utf-8")
    assert any("unmasked email" in p for p in validate_output(folder))


def test_csv_formula_injection_is_neutralised(tmp_path) -> None:
    from odata_extractor.client import Extraction, Page

    records = [{"Id": 1, "Note": "=HYPERLINK(\"http://x\")"}, {"Id": 2, "Note": "+cmd"}, {"Id": 3, "Note": "plain"}]
    extraction = Extraction("Notes", "Id", True, "integer", pages=[Page(1, "/p", records, 3, None, "x")])
    folder = write_outputs(tmp_path, extraction, assess(extraction), RULES, DEMO)
    rows = list(csv.DictReader(io.StringIO((folder / "publishable/records.csv").read_text(encoding="utf-8"))))
    assert [r["Note"] for r in rows] == ["'=HYPERLINK(\"http://x\")", "'+cmd", "plain"]


def test_failed_extraction_still_writes_an_honest_report(tmp_path) -> None:
    folder = extract_to(tmp_path, "WorkOrders", "malformed_json").output
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["integrity"]["status"] == "FAILED"
    assert "Bearer" not in (folder / "report.json").read_text(encoding="utf-8")
    assert validate_output(folder) == []
