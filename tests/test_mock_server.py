"""The mock service in-process (no sockets): paging, $count, errors and each fault."""

import json
from urllib.parse import urlsplit

import pytest

from odata_extractor.faults import SCENARIOS
from odata_extractor.mock_server import MockODataService
from odata_extractor.schema import parse_metadata

H = {"Host": "127.0.0.1:9"}


def get(service, target, headers=None):
    reply = service.handle("GET", target, {**H, **(headers or {})})
    return reply, (json.loads(reply.body) if reply.headers.get("Content-Type", "").startswith("application/json") else None)


def walk(service, target="/odata/WorkOrders?$count=true"):
    pages = []
    while target:
        reply, body = get(service, target)
        pages.append((reply.status, body))
        link = body.get("@odata.nextLink") if body else None
        target = (urlsplit(link).path + "?" + urlsplit(link).query) if link else None
        if len(pages) > 30:
            break
    return pages


def test_service_document_and_metadata() -> None:
    service = MockODataService()
    _, doc = get(service, "/odata/")
    assert {s["name"] for s in doc["value"]} == {"Locations", "Technicians", "Equipment", "MaintenanceRequests", "WorkOrders", "ServiceEvents"}
    reply, _ = get(service, "/odata/$metadata")
    sets = parse_metadata(reply.body.decode())
    assert sets["WorkOrders"]["key"] == ["WorkOrderId"]


def test_server_driven_paging_with_count_and_prefer() -> None:
    pages = walk(MockODataService())
    assert len(pages) == 9 and all(status == 200 for status, _ in pages)
    assert sum(len(body["value"]) for _, body in pages) == 420
    assert {body["@odata.count"] for _, body in pages} == {420}
    reply, body = get(MockODataService(), "/odata/WorkOrders", {"Prefer": "odata.maxpagesize=100"})
    assert len(body["value"]) == 100 and reply.headers["Preference-Applied"] == "odata.maxpagesize=100"


def test_client_top_and_skip_window() -> None:
    _, body = get(MockODataService(), "/odata/WorkOrders?$skip=10&$top=5")
    assert [r["WorkOrderId"] for r in body["value"]] == [11, 12, 13, 14, 15]
    assert "@odata.nextLink" not in body


@pytest.mark.parametrize(("target", "code"), [("/odata/Nope", 404), ("/odata/WorkOrders?$expand=X", 501),
                                               ("/odata/WorkOrders?$filter=Nope eq 1", 400)])
def test_errors_are_odata_error_bodies(target, code) -> None:
    reply, body = get(MockODataService(), target)
    assert reply.status == code and "error" in body


def test_token_and_method_checks() -> None:
    service = MockODataService(required_token="s3cret-demo")
    assert get(service, "/odata/WorkOrders")[0].status == 401
    assert get(service, "/odata/WorkOrders", {"Authorization": "Bearer s3cret-demo"})[0].status == 200
    assert MockODataService().handle("POST", "/odata/WorkOrders", H).status == 405


def test_status_faults_are_deterministic_and_bounded() -> None:
    service = MockODataService("rate_limit")
    page2 = "/odata/WorkOrders?$count=true&$skiptoken=50"
    first, _ = get(service, page2)
    second, _ = get(service, page2)
    third, _ = get(service, page2)
    assert [first.status, second.status, third.status] == [429, 429, 200]
    assert first.headers["Retry-After"] == "2"


def test_data_faults() -> None:
    def ids(scenario):
        return [[r["WorkOrderId"] for r in body["value"]] for _, body in walk(MockODataService(scenario))]

    replay = ids("duplicate_page")
    assert replay[2] == replay[1]
    overlap = ids("overlap_duplicates")
    assert overlap[1][:3] == overlap[0][-3:]
    dropped = ids("skipped_records")
    assert len(dropped[1]) == 47
    empty = walk(MockODataService("empty_page"))
    assert len(empty) == 3 and empty[-1][1]["value"] == []
    invalid = ids("invalid_records")
    assert invalid[1][:2] == [None, "not-a-number"]


def test_link_faults() -> None:
    pages = walk(MockODataService("nextlink_cycle"))
    assert len(pages) > 9  # without cycle detection the walk loops until walk()'s own guard stops it
    _, body = get(MockODataService("foreign_nextlink"), "/odata/WorkOrders?$count=true&$skiptoken=50")
    assert urlsplit(body["@odata.nextLink"]).hostname == "evil.example"


def test_malformed_and_delay_faults() -> None:
    reply = MockODataService("malformed_json").handle("GET", "/odata/WorkOrders?$skiptoken=50", H)
    assert reply.status == 200
    with pytest.raises(json.JSONDecodeError):
        json.loads(reply.body)
    slow = MockODataService("timeout")
    assert get(slow, "/odata/WorkOrders?$skiptoken=50")[0].delay > 1
    assert get(slow, "/odata/WorkOrders?$skiptoken=50")[0].delay == 0


def test_dataset_changes_mid_extraction() -> None:
    shift = [body.get("@odata.count") for _, body in walk(MockODataService("dataset_shift"))]
    assert shift[:2] == [420, 420] and set(shift[2:]) == {416}
    growth = [body.get("@odata.count") for _, body in walk(MockODataService("dataset_growth"))]
    assert growth[0] == 420 and set(growth[1:]) == {426}


def test_reduced_capabilities() -> None:
    service = MockODataService("no_count")
    _, body = get(service, "/odata/WorkOrders?$count=true")
    assert "@odata.count" not in body
    assert get(service, "/odata/WorkOrders?$filter=WorkOrderId gt 1")[0].status == 501


def test_faults_do_not_touch_filtered_requests() -> None:
    service = MockODataService("skipped_records")
    _, body = get(service, "/odata/WorkOrders?$filter=WorkOrderId gt 50&$top=50")
    assert len(body["value"]) == 50


def test_every_scenario_declares_its_expected_status() -> None:
    assert len(SCENARIOS) >= 10
    assert {s.expected_status for s in SCENARIOS.values()} == {"COMPLETE", "INCOMPLETE", "INDETERMINATE", "FAILED"}
    assert any(not s.conformant for s in SCENARIOS.values()) and any(s.conformant for s in SCENARIOS.values())
