"""End to end over real HTTP: the mock server on a loopback port, the real urllib transport, a virtual clock."""

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from odata_extractor.client import ClientConfig, ODataClient
from odata_extractor.dataset import generate
from odata_extractor.faults import SCENARIOS
from odata_extractor.pipeline import run
from odata_extractor.privacy import Pseudonymizer
from odata_extractor.transport import VirtualClock

from .helpers import http_client, served

DATA = generate()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_reaches_its_expected_status(name) -> None:
    scenario = SCENARIOS[name]
    with served(name) as server:
        client = http_client(server)
        outcome = run(client.config, scenario.entity, client=client)
    assert outcome.integrity.status == scenario.expected_status, outcome.integrity.reasons


def test_healthy_extraction_returns_exactly_the_dataset() -> None:
    with served("healthy") as server:
        client = http_client(server)
        outcome = run(client.config, "WorkOrders", client=client)
    assert outcome.integrity.unique_records == DATA["WorkOrders"]
    assert outcome.run.metrics.retries == {} or sum(outcome.run.metrics.retries.values()) == 0


@pytest.mark.parametrize("entity", ["Locations", "Technicians", "ServiceEvents"])
def test_other_entity_sets_extract_completely(entity) -> None:
    with served("healthy") as server:
        client = http_client(server, page_size=200)
        outcome = run(client.config, entity, client=client)
    assert outcome.integrity.status == "COMPLETE"
    assert len(outcome.integrity.unique_records) == len(DATA[entity])


def test_rate_limit_waits_follow_retry_after() -> None:
    with served("rate_limit") as server:
        client = http_client(server)
        run(client.config, "WorkOrders", client=client)
    assert client.clock.sleeps == [2.0, 2.0]


def test_real_timeout_is_retried() -> None:
    with served("timeout") as server:
        client = http_client(server, timeout=0.3)
        outcome = run(client.config, "WorkOrders", client=client)
    assert outcome.integrity.status == "COMPLETE"
    assert dict(outcome.run.metrics.retries) == {"timeout": 1}


def test_token_is_sent_but_never_written(tmp_path) -> None:
    with served("healthy", required_token="tok-e2e-123") as server:
        denied = http_client(server)
        assert run(denied.config, "Locations", client=denied).integrity.status == "FAILED"
        client = http_client(server, token="tok-e2e-123")
        outcome = run(client.config, "Locations", tmp_path, client=client, pseudonymizer=Pseudonymizer.from_environment(demo=True))
    assert outcome.integrity.status == "COMPLETE"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "tok-e2e-123" not in path.read_text(encoding="utf-8")


def test_real_transport_does_not_follow_redirects() -> None:
    """A 302 to a foreign host is reported, not followed (following it would try to reach evil.example)."""

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://evil.example/odata/WorkOrders")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        config = ClientConfig(service_root=f"http://127.0.0.1:{httpd.server_address[1]}/odata/", timeout=2, reconcile=False)
        run_ = ODataClient(config, clock=VirtualClock(), rng=random.Random(1)).extract("WorkOrders", "WorkOrderId")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert run_.error.code == "HTTP_ERROR" and run_.error.status == 302
    assert "redirects are not followed" in run_.error.message


def test_outputs_match_the_data(tmp_path) -> None:
    with served("healthy") as server:
        client = http_client(server)
        outcome = run(client.config, "WorkOrders", tmp_path, client=client, pseudonymizer=Pseudonymizer.from_environment(demo=True))
    raw = [json.loads(line) for line in (outcome.output / "raw/records.jsonl").read_text(encoding="utf-8").splitlines()]
    assert raw == DATA["WorkOrders"]
    report = json.loads((outcome.output / "report.json").read_text(encoding="utf-8"))
    assert report["integrity"]["counts"]["unique_records"] == len(raw) == 420
    assert report["integrity"]["status"] == "COMPLETE"
