"""A local mock OData v4 service over the synthetic dataset, with deterministic fault injection.

It implements a documented subset of OData (service document, $metadata, collections, server-driven paging with
@odata.nextLink, $count, $filter, $orderby, $select, $top, $skip). It is a test double for the client, not an
OData implementation: see docs/odata-subset.md for what is and is not supported.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from xml.sax.saxutils import escape

from .dataset import ENTITY_SETS, generate
from .faults import SCENARIOS, Fault, Scenario
from .query import QueryError, evaluate, parse, project

NAMESPACE = "Halcyon.Maintenance"
ROOT = "/odata"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


def _json(status: int, payload: Any, extra: dict[str, str] | None = None) -> Reply:
    headers = {"Content-Type": "application/json; odata.metadata=minimal", "OData-Version": "4.0", **(extra or {})}
    return Reply(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"), headers)


def _error(status: int, code: str, message: str, extra: dict[str, str] | None = None) -> Reply:
    return _json(status, {"error": {"code": code, "message": message}}, extra)


def metadata_xml() -> str:
    types, sets = [], []
    for entity in ENTITY_SETS.values():
        props = "".join(f'<Property Name="{f.name}" Type="{f.edm_type}" Nullable="{str(f.nullable).lower()}"/>' for f in entity.fields)
        navs, bindings = "", ""
        for nav in entity.navigations:
            target = ENTITY_SETS[nav.target_set]
            navs += (f'<NavigationProperty Name="{nav.name}" Type="{NAMESPACE}.{target.type_name}" Nullable="false">'
                     f'<ReferentialConstraint Property="{nav.foreign_key}" ReferencedProperty="{target.key}"/></NavigationProperty>')
            bindings += f'<NavigationPropertyBinding Path="{nav.name}" Target="{nav.target_set}"/>'
        types.append(f'<EntityType Name="{entity.type_name}"><Key><PropertyRef Name="{entity.key}"/></Key>{props}{navs}</EntityType>')
        sets.append(f'<EntitySet Name="{entity.name}" EntityType="{NAMESPACE}.{entity.type_name}">{bindings}</EntitySet>')
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>'
            f'<Schema Namespace="{escape(NAMESPACE)}" xmlns="http://docs.oasis-open.org/odata/ns/edm">'
            + "".join(types) + '<EntityContainer Name="Container">' + "".join(sets) + "</EntityContainer>"
            "</Schema></edmx:DataServices></edmx:Edmx>")


class MockODataService:
    """Request handling without sockets, so it can be unit-tested directly."""

    def __init__(self, scenario: str = "healthy", page_size: int = DEFAULT_PAGE_SIZE, required_token: str | None = None,
                 data: dict[str, list[dict[str, Any]]] | None = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {sorted(SCENARIOS)}")
        self.scenario: Scenario = SCENARIOS[scenario]
        self.page_size = page_size
        self.required_token = required_token
        self.data = copy.deepcopy(data) if data is not None else generate()
        self.attempts: Counter[tuple[str, int]] = Counter()
        self.mutated = False
        self.requests_served = 0
        self._lock = threading.Lock()

    def handle(self, method: str, target: str, headers: dict[str, str]) -> Reply:
        with self._lock:
            self.requests_served += 1
            return self._handle(method, target, {k.lower(): v for k, v in headers.items()})

    def _handle(self, method: str, target: str, headers: dict[str, str]) -> Reply:
        if self.required_token and headers.get("authorization") != f"Bearer {self.required_token}":
            return _error(401, "Unauthorized", "a valid bearer token is required", {"WWW-Authenticate": "Bearer"})
        if method != "GET":
            return _error(405, "MethodNotAllowed", "this service is read-only")
        parts = urlsplit(target)
        path = parts.path.rstrip("/")
        host = headers.get("host", "127.0.0.1")
        base = f"http://{host}{ROOT}"
        if path == ROOT:
            return _json(200, {"@odata.context": f"{base}/$metadata",
                               "value": [{"name": n, "kind": "EntitySet", "url": n} for n in ENTITY_SETS]})
        if path == f"{ROOT}/$metadata":
            return Reply(200, metadata_xml().encode("utf-8"), {"Content-Type": "application/xml", "OData-Version": "4.0"})
        name = path[len(ROOT) + 1:] if path.startswith(ROOT + "/") else ""
        if name not in ENTITY_SETS:
            return _error(404, "NotFound", f"no entity set at {path}")
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        return self._collection(name, params, headers, base)

    def _collection(self, name: str, params: dict[str, str], headers: dict[str, str], base: str) -> Reply:
        entity = ENTITY_SETS[name]
        scenario = self.scenario
        if "$filter" in params and not scenario.supports_filter:
            return _error(501, "NotImplemented", "$filter is not supported by this provider")
        try:
            query = parse(params, entity)
        except QueryError as err:
            return _error(501 if err.code == "NotImplemented" else 400, err.code, str(err))
        page_size = self.page_size
        prefer = headers.get("prefer", "")
        applied = {}
        if "odata.maxpagesize=" in prefer:
            try:
                page_size = max(1, min(int(prefer.split("odata.maxpagesize=")[1].split(",")[0]), MAX_PAGE_SIZE))
                applied = {"Preference-Applied": f"odata.maxpagesize={page_size}"}
            except ValueError:
                pass
        rows = evaluate(self.data[name], query, entity)
        total = len(rows)
        token = query.skiptoken or 0
        start = query.skip + token
        end_limit = min(total, query.skip + query.top) if query.top is not None else total
        page_number = token // page_size + 1
        targeted = name == scenario.entity and "$filter" not in params
        faults = [f for f in scenario.faults if targeted]
        key = (name, page_number)
        if targeted:
            self.attempts[key] += 1
        attempt = self.attempts[key]

        delay = 0.0
        for fault in faults:
            if fault.page != page_number or fault.kind not in ("status", "delay", "malformed"):
                continue
            if fault.times and attempt > fault.times:
                continue
            if fault.kind == "status":
                extra = {"Retry-After": fault.retry_after} if fault.retry_after else {}
                return _error(fault.status, "Throttled" if fault.status == 429 else "Unavailable",
                              f"simulated {fault.status} for page {page_number}", extra)
            if fault.kind == "delay":
                delay = fault.seconds

        page_rows = rows[start:min(start + page_size, end_limit)]
        next_offset = start + page_size
        link_offset: int | None = next_offset if next_offset < end_limit else None
        link_host = base
        for fault in faults:
            if fault.kind == "replay_page" and page_number == fault.page:
                previous = start - page_size
                page_rows = rows[previous:previous + page_size]
            elif fault.kind == "overlap" and page_number >= fault.page:
                page_rows = rows[max(0, start - fault.count):min(start + page_size, end_limit)]
            elif fault.kind == "drop" and page_number == fault.page:
                doomed = {page_rows[i][entity.key] for i in range(10, 10 + 10 * fault.count, 10) if i < len(page_rows)}
                page_rows = [r for r in page_rows if r[entity.key] not in doomed]
            elif fault.kind == "empty_end" and page_number == fault.page:
                page_rows, link_offset = [], None
            elif fault.kind == "cycle" and page_number == fault.page:
                link_offset = page_size  # back to page 2
            elif fault.kind == "foreign_nextlink" and page_number == fault.page:
                link_host = f"http://evil.example{ROOT}"
        value = [project(r, query.select) for r in page_rows]
        for fault in faults:
            if fault.kind == "invalid_keys" and page_number == fault.page:
                for index, bad in zip(range(fault.count), (None, "not-a-number")):
                    if index < len(value):
                        value[index][entity.key] = bad

        payload: dict[str, Any] = {"@odata.context": f"{base}/$metadata#{name}"}
        if query.count and scenario.supports_count:
            payload["@odata.count"] = total
        payload["value"] = value
        if link_offset is not None:
            link_params = {k: v for k, v in params.items() if k != "$skiptoken"}
            link_params["$skiptoken"] = str(link_offset - query.skip)
            payload["@odata.nextLink"] = f"{link_host}/{name}?" + urlencode(link_params, quote_via=quote, safe="$,'")

        for fault in faults:
            if fault.kind == "malformed" and page_number == fault.page and (not fault.times or attempt <= fault.times):
                body = json.dumps(payload).encode("utf-8")
                return Reply(200, body[: len(body) // 2], {"Content-Type": "application/json", "OData-Version": "4.0"})
            if fault.kind in ("mutate_delete", "mutate_append") and page_number == fault.page and not self.mutated:
                self._mutate(name, fault)
        reply = _json(200, payload, applied)
        reply.delay = delay
        return reply

    def _mutate(self, name: str, fault: Fault) -> None:
        entity = ENTITY_SETS[name]
        records = self.data[name]
        if fault.kind == "mutate_delete":
            doomed = {records[i][entity.key] for i in range(5, 5 + 10 * fault.count, 10)}
            self.data[name] = [r for r in records if r[entity.key] not in doomed]
        else:
            top = max(r[entity.key] for r in records)
            template = records[-1]
            for offset in range(1, fault.count + 1):
                records.append({**template, entity.key: top + offset})
        self.mutated = True


class _Handler(BaseHTTPRequestHandler):
    service: MockODataService  # set on the subclass created per server
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._respond(self.service.handle("GET", self.path, dict(self.headers)))

    def do_POST(self) -> None:  # noqa: N802
        self._respond(self.service.handle("POST", self.path, dict(self.headers)))

    def _respond(self, reply: Reply) -> None:
        if reply.delay:
            time.sleep(reply.delay)
        try:
            self.send_response(reply.status)
            for key, value in reply.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client gave up (simulated timeout); nothing to do

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - keep test output quiet
        return


class MockServer:
    """`with MockServer("rate_limit") as server:` runs the service on a free loopback port in a background thread."""

    def __init__(self, scenario: str = "healthy", port: int = 0, page_size: int = DEFAULT_PAGE_SIZE,
                 required_token: str | None = None, host: str = "127.0.0.1") -> None:
        self.service = MockODataService(scenario, page_size, required_token)
        handler = type("Handler", (_Handler,), {"service": self.service})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}{ROOT}/"

    def __enter__(self) -> MockServer:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="mock-odata", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def serve_forever(self) -> None:
        self.httpd.serve_forever()
