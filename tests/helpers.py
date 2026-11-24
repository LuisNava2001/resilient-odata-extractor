from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from odata_extractor.client import ClientConfig, ODataClient, RetryPolicy
from odata_extractor.mock_server import MockServer
from odata_extractor.transport import HttpRequest, HttpResponse, VirtualClock

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = "http://svc.test/odata/"


def page(records: list[dict[str, Any]], count: int | None = None, next_link: str | None = None, status: int = 200,
         headers: dict[str, str] | None = None) -> HttpResponse:
    payload: dict[str, Any] = {"value": records}
    if count is not None:
        payload["@odata.count"] = count
    if next_link:
        payload["@odata.nextLink"] = next_link
    return HttpResponse(status, headers or {}, json.dumps(payload).encode())


def status(code: int, headers: dict[str, str] | None = None, body: bytes = b"{}") -> HttpResponse:
    return HttpResponse(code, headers or {}, body)


class FakeTransport:
    """Replays a script of responses (or exceptions, or callables) and records every request."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.script:
            raise AssertionError(f"unexpected request {request.url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item


def fake_client(script: list[Any], **config: Any) -> tuple[ODataClient, FakeTransport, VirtualClock]:
    transport, clock = FakeTransport(script), VirtualClock()
    retry = config.pop("retry", RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=8.0))
    cfg = ClientConfig(service_root=config.pop("service_root", ROOT), reconcile=config.pop("reconcile", False), retry=retry, **config)
    return ODataClient(cfg, transport=transport, clock=clock, rng=random.Random(1)), transport, clock


@contextmanager
def served(scenario: str = "healthy", **kwargs: Any) -> Iterator[MockServer]:
    with MockServer(scenario, **kwargs) as server:
        yield server


def http_client(server: MockServer, **config: Any) -> ODataClient:
    retry = config.pop("retry", RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=8.0))
    cfg = ClientConfig(service_root=server.url, timeout=config.pop("timeout", 0.4), retry=retry, **config)
    return ODataClient(cfg, clock=VirtualClock(), rng=random.Random(7))


ClientFactory = Callable[..., ODataClient]
