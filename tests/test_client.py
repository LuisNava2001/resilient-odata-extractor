"""Client behaviour with a scripted transport and a virtual clock: no network, no real waiting."""

from datetime import timedelta
from email.utils import format_datetime
from urllib.parse import parse_qsl, urlsplit

import pytest

from odata_extractor.client import ExtractionError, RetryPolicy, odata_literal, parse_retry_after
from odata_extractor.transport import TransportError, TransportTimeout

from .helpers import ROOT, fake_client, page, status

R = [{"Id": i} for i in range(1, 4)]


def test_first_request_is_built_explicitly() -> None:
    client, transport, _ = fake_client([page(R, count=3)], token="tok-123", page_size=25, timeout=7.5)
    client.extract("Things", "Id")
    request = transport.requests[0]
    parts = urlsplit(request.url)
    assert parts.path == "/odata/Things"
    assert dict(parse_qsl(parts.query)) == {"$count": "true", "$orderby": "Id asc"}
    assert request.headers["Prefer"] == "odata.maxpagesize=25"
    assert request.headers["OData-MaxVersion"] == "4.0"
    assert request.headers["Authorization"] == "Bearer tok-123"
    assert request.timeout == 7.5


def test_follows_next_links_until_absent() -> None:
    client, transport, _ = fake_client([page(R[:2], 3, ROOT + "Things?$skiptoken=2"), page(R[2:], 3)])
    run = client.extract("Things", "Id")
    assert [len(p.records) for p in run.pages] == [2, 1]
    assert run.stop_reason == "end" and run.error is None
    assert transport.requests[1].url == ROOT + "Things?$skiptoken=2"


def test_relative_next_link_is_resolved() -> None:
    client, transport, _ = fake_client([page(R[:2], 3, "Things?$skiptoken=2"), page(R[2:], 3)])
    client.extract("Things", "Id")
    assert transport.requests[1].url == ROOT + "Things?$skiptoken=2"


@pytest.mark.parametrize(("link", "problem"), [
    ("http://evil.example/odata/Things?x=1", "unauthorized host"),
    ("https://svc.test/odata/Things?x=1", "changes scheme"),
    ("http://user:pw@svc.test/odata/Things", "credentials"),
    ("http://svc.test/admin/Things", "leaves the service root"),
    ("http://svc.test:8443/odata/Things", "unauthorized host"),
])
def test_unsafe_next_links_are_refused(link, problem) -> None:
    client, transport, _ = fake_client([page(R, 3, link)])
    run = client.extract("Things", "Id")
    assert run.error.code == "UNSAFE_NEXTLINK" and problem in run.error.message
    assert len(transport.requests) == 1  # the unsafe URL was never requested


def test_explicitly_allowed_host_is_followed() -> None:
    client, transport, _ = fake_client([page(R[:1], 3, "http://mirror.test/odata/Things?p=2"), page(R[1:], 3)],
                                       allowed_hosts=("mirror.test",))
    assert client.extract("Things", "Id").error is None


def test_cycle_detection_stops_paging() -> None:
    link = ROOT + "Things?$skiptoken=2"
    client, transport, _ = fake_client([page(R[:1], 3, link), page(R[1:2], 3, ROOT + "Things?$skiptoken=4"), page(R[2:], 3, link)])
    run = client.extract("Things", "Id")
    assert run.stop_reason == "cycle" and len(transport.requests) == 3
    assert run.client_findings[-1]["code"] == "NEXTLINK_CYCLE"


def test_query_parameter_order_does_not_hide_a_cycle() -> None:
    client, _, _ = fake_client([page(R[:1], 3, ROOT + "Things?b=2&a=1"), page(R[1:], 3, ROOT + "Things?a=1&b=2")])
    assert client.extract("Things", "Id").stop_reason == "cycle"


def test_replayed_page_is_flagged() -> None:
    client, _, _ = fake_client([page(R[:2], 4, ROOT + "Things?p=2"), page(R[:2], 4)])
    run = client.extract("Things", "Id")
    assert [f["code"] for f in run.client_findings] == ["DUPLICATE_PAGE"]


def test_retry_after_seconds_is_honoured() -> None:
    client, transport, clock = fake_client([status(429, {"retry-after": "7"}), page(R, 3)])
    run = client.extract("Things", "Id")
    assert run.error is None and clock.sleeps == [7.0]
    assert run.metrics.retries["http_429"] == 1


def test_retry_after_http_date_is_honoured() -> None:
    client, _, clock = fake_client([])
    moment = format_datetime(clock.now() + timedelta(seconds=12), usegmt=True)
    client.transport.script = [status(503, {"retry-after": moment}), page(R, 3)]
    client.extract("Things", "Id")
    assert clock.sleeps == [12.0]


def test_excessive_retry_after_fails_instead_of_waiting() -> None:
    client, _, clock = fake_client([status(429, {"retry-after": "3600"})])
    run = client.extract("Things", "Id")
    assert run.error.code == "RETRY_AFTER_TOO_LONG" and clock.sleeps == []


def test_backoff_is_exponential_jittered_capped_and_bounded() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay=1.0, max_delay=4.0)
    client, transport, clock = fake_client([status(503)] * 6, retry=policy)
    run = client.extract("Things", "Id")
    assert run.error.code == "RETRIES_EXHAUSTED" and len(transport.requests) == 6
    ceilings = [1.0, 2.0, 4.0, 4.0, 4.0]
    assert len(clock.sleeps) == 5
    assert all(0 <= wait <= ceiling for wait, ceiling in zip(clock.sleeps, ceilings))
    assert len(set(clock.sleeps)) > 1  # jitter


def test_timeouts_and_connection_errors_are_retried() -> None:
    client, _, _ = fake_client([TransportTimeout("slow"), TransportError("reset"), page(R, 3)])
    run = client.extract("Things", "Id")
    assert run.error is None
    assert dict(run.metrics.retries) == {"timeout": 1, "connection_error": 1}


def test_persistent_timeout_fails_with_timeout_code() -> None:
    client, _, _ = fake_client([TransportTimeout("slow")] * 4)
    assert client.extract("Things", "Id").error.code == "TIMEOUT"


@pytest.mark.parametrize("code", [400, 401, 403, 404, 500, 301])
def test_non_retryable_statuses_fail_immediately(code) -> None:
    client, transport, clock = fake_client([status(code, {}, b'{"error":{"code":"X","message":"nope"}}')])
    run = client.extract("Things", "Id")
    assert run.error.code == "HTTP_ERROR" and run.error.status == code
    assert len(transport.requests) == 1 and clock.sleeps == []


def test_malformed_bodies_are_retried_then_fail() -> None:
    bad = [status(200, {}, b'{"value": [1, 2'), status(200, {}, b'{"items": []}'), status(200, {}, b'{"value": "x"}'),
           status(200, {}, b'{"value": [], "@odata.count": "lots"}')]
    client, _, _ = fake_client(bad)
    run = client.extract("Things", "Id")
    assert run.error.code == "MALFORMED_RESPONSE"


def test_request_and_page_budgets_prevent_endless_loops() -> None:
    endless = [lambda r, n=i: page([{"Id": n}], None, ROOT + f"Things?p={n + 1}") for i in range(10)]
    client, _, _ = fake_client(endless, max_pages=5)
    assert client.extract("Things", "Id").error.code == "BUDGET_EXCEEDED"
    client, _, _ = fake_client([status(503)] * 10, max_requests=3)
    assert client.extract("Things", "Id").error.code == "BUDGET_EXCEEDED"


def test_reconciliation_uses_keyset_filters() -> None:
    script = [page(R, 3), page([{"Id": 1}, {"Id": 2}]), page([{"Id": 3}]), page([])]
    client, transport, _ = fake_client(script, reconcile=True, page_size=2)
    run = client.extract("Things", "Id")
    assert run.reconciliation.available and run.reconciliation.ids == [1, 2, 3]
    filters = [dict(parse_qsl(urlsplit(r.url).query)).get("$filter") for r in transport.requests[1:]]
    assert filters == ["Id ne null", "Id gt 2", "Id gt 3"]


def test_reconciliation_reports_unavailability() -> None:
    client, _, _ = fake_client([page(R, 3), status(501)], reconcile=True)
    recon = client.extract("Things", "Id").reconciliation
    assert not recon.available and "HTTP 501" in recon.reason


def test_token_never_appears_in_the_timeline_or_errors() -> None:
    client, _, _ = fake_client([status(401)], token="super-secret-token")
    run = client.extract("Things", "Id")
    assert "super-secret-token" not in repr(run.timeline) + repr(run.error) + repr(run.metrics.to_dict())


def test_helpers() -> None:
    assert odata_literal(5) == "5" and odata_literal("O'Neil") == "'O''Neil'"
    with pytest.raises(ValueError):
        odata_literal(True)
    assert parse_retry_after("garbage", None) is None and parse_retry_after(None, None) is None
    with pytest.raises(ValueError):
        fake_client([], service_root="ftp://svc.test/odata/")
