import json

import pytest

from odata_extractor.cli import main

from .helpers import served

FAST = ["--timeout", "2", "--backoff-base", "0.01", "--max-attempts", "3", "--demo-pseudonym-key"]


@pytest.mark.parametrize(("scenario", "code"), [("healthy", 0), ("no_count", 3), ("skipped_records", 4), ("malformed_json", 5)])
def test_extract_exit_codes(tmp_path, capsys, scenario, code) -> None:
    with served(scenario) as server:
        result = main(["extract", "--base-url", server.url, "--entity", "WorkOrders", "--output", str(tmp_path), *FAST])
    assert result == code
    out = capsys.readouterr().out
    assert out.startswith("WorkOrders: ")


def test_extract_then_validate(tmp_path, capsys) -> None:
    with served("healthy") as server:
        assert main(["extract", "--base-url", server.url, "--entity", "Technicians", "--output", str(tmp_path), *FAST]) == 0
    assert main(["validate", str(tmp_path / "Technicians")]) == 0
    (tmp_path / "Technicians" / "publishable" / "records.csv").write_text("tampered\n", encoding="utf-8")
    assert main(["validate", str(tmp_path / "Technicians")]) == 4


def test_token_from_environment_is_used_and_not_echoed(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("DEMO_ODATA_TOKEN", "cli-token-456")
    with served("healthy", required_token="cli-token-456") as server:
        code = main(["extract", "--base-url", server.url, "--entity", "Locations", "--output", str(tmp_path),
                     "--token-env", "DEMO_ODATA_TOKEN", *FAST])
    assert code == 0 and "cli-token-456" not in capsys.readouterr().out


def test_unreachable_service_is_reported_as_failed(tmp_path, capsys) -> None:
    code = main(["extract", "--base-url", "http://127.0.0.1:9/odata/", "--entity", "WorkOrders", "--output", str(tmp_path),
                 "--timeout", "0.5", "--backoff-base", "0.01", "--max-attempts", "2"])
    assert code == 5  # connection refused on $metadata: a technical failure, reported as FAILED
    assert json.loads((tmp_path / "WorkOrders" / "report.json").read_text(encoding="utf-8"))["integrity"]["status"] == "FAILED"


def test_inspect_schema(capsys) -> None:
    with served("healthy") as server:
        assert main(["inspect-schema", "--base-url", server.url, "--entity", "WorkOrders"]) == 0
        assert "Declared key: WorkOrderId" in capsys.readouterr().out
        assert main(["inspect-schema", "--base-url", server.url, "--entity", "Technicians", "--no-metadata", "--sample", "10"]) == 0
        assert "inference from 10 sample record(s)" in capsys.readouterr().out


def test_serve_refuses_non_loopback_hosts(capsys) -> None:
    assert main(["serve", "--host", "0.0.0.0", "--port", "0"]) == 2
    assert "loopback" in capsys.readouterr().err


def test_seed_and_scenarios(tmp_path, capsys) -> None:
    target = tmp_path / "dataset.json"
    assert main(["seed", "--out", str(target)]) == 0
    assert len(json.loads(target.read_text(encoding="utf-8"))["WorkOrders"]) == 420
    assert main(["scenarios"]) == 0
    assert "provider defect" in capsys.readouterr().out


def test_demo_single_scenario(capsys) -> None:
    assert main(["demo", "--scenario", "healthy"]) == 0
    assert "1/1 scenarios produced their expected integrity status" in capsys.readouterr().out
