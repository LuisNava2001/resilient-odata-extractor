"""Resilient OData extraction client.

Each request has an explicit timeout and a bounded number of attempts. 429 and 502/503/504 responses, timeouts,
connection failures and unparseable bodies are retried; Retry-After is honoured (seconds or HTTP-date) up to a
ceiling, otherwise exponential backoff with full jitter is used. Other HTTP errors fail immediately. Paging follows
@odata.nextLink only to the service's own scheme, host, port and path (plus explicitly allowed hosts), detects
cycles and replayed pages, and stops at hard page and request budgets. Technical failures end the extraction with
an ExtractionError; integrity questions are left to the integrity engine.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

from .dataset import digest
from .transport import (HttpRequest, HttpResponse, SystemClock, Transport, TransportError, TransportTimeout,
                        UrllibTransport)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5  # per request, including the first
    base_delay: float = 0.5
    max_delay: float = 30.0
    max_retry_after: float = 120.0
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})


@dataclass
class ClientConfig:
    service_root: str
    timeout: float = 10.0
    page_size: int = 50
    max_pages: int = 1000
    max_requests: int = 2500
    allowed_hosts: tuple[str, ...] = ()
    token: str | None = None
    reconcile: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)


class ExtractionError(Exception):
    """A technical failure: the extraction could not reach a defined end."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Page:
    number: int
    url: str
    records: list[dict[str, Any]]
    count: int | None
    next_link: str | None
    fingerprint: str


@dataclass
class Metrics:
    requests: Counter[str] = field(default_factory=Counter)  # by phase
    statuses: Counter[int] = field(default_factory=Counter)
    retries: Counter[str] = field(default_factory=Counter)  # by reason
    wait_seconds: float = 0.0
    bytes_received: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"requests_total": sum(self.requests.values()), "requests_by_phase": dict(self.requests),
                "http_statuses": {str(k): v for k, v in sorted(self.statuses.items())},
                "retries": sum(self.retries.values()), "retries_by_reason": dict(sorted(self.retries.items())),
                "wait_seconds": round(self.wait_seconds, 3), "bytes_received": self.bytes_received}


@dataclass
class Reconciliation:
    available: bool
    ids: list[Any] = field(default_factory=list)
    reason: str = ""


@dataclass
class Extraction:
    entity: str
    key: str
    ordered_by_key: bool
    key_type: str | None = None  # "integer" or "string" when declared by $metadata
    pages: list[Page] = field(default_factory=list)
    stop_reason: str = "end"  # end | cycle | error
    error: ExtractionError | None = None
    client_findings: list[dict[str, Any]] = field(default_factory=list)
    reconciliation: Reconciliation | None = None
    metrics: Metrics = field(default_factory=Metrics)
    timeline: list[dict[str, Any]] = field(default_factory=list)


def relative(url: str) -> str:
    """Path and query only: reports never carry the scheme, host or credentials of the target."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def normalize(url: str) -> tuple[str, str, int | None, str, tuple[tuple[str, str], ...]]:
    parts = urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port, parts.path.rstrip("/"),
            tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))))


def _host_port(url: str) -> str:
    parts = urlsplit(url)
    default = 443 if parts.scheme == "https" else 80
    return f"{(parts.hostname or '').lower()}:{parts.port or default}"


def parse_retry_after(value: str | None, now: Any) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (moment - now).total_seconds())


def odata_literal(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("keys must be integers or strings")
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class ODataClient:
    def __init__(self, config: ClientConfig, transport: Transport | None = None, clock: Any = None,
                 rng: random.Random | None = None) -> None:
        if not config.service_root.endswith("/"):
            config.service_root += "/"
        root = urlsplit(config.service_root)
        if root.scheme not in ("http", "https") or not root.hostname or root.username or root.password:
            raise ValueError("service root must be an http(s) URL without credentials")
        self.config = config
        self.transport = transport or UrllibTransport()
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self._root = root
        self._allowed = {_host_port(config.service_root)} | {h.lower() if ":" in h else f"{h.lower()}:{root.port or (443 if root.scheme == 'https' else 80)}"
                                                              for h in config.allowed_hosts}

    # -- single request with retries -------------------------------------------------

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "OData-MaxVersion": "4.0", "Prefer": f"odata.maxpagesize={self.config.page_size}"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"  # never recorded anywhere
        return headers

    def _event(self, run: Extraction | None, **fields: Any) -> None:
        if run is not None:
            run.timeline.append({"seq": len(run.timeline) + 1, **fields})

    def request(self, url: str, run: Extraction | None, phase: str, page: int | None = None,
                expect_json: bool = True) -> tuple[HttpResponse, Any]:
        policy = self.config.retry
        metrics = run.metrics if run else Metrics()
        attempt = 0
        while True:
            attempt += 1
            if sum(metrics.requests.values()) >= self.config.max_requests:
                raise ExtractionError("BUDGET_EXCEEDED", f"request budget of {self.config.max_requests} reached")
            metrics.requests[phase] += 1
            self._event(run, phase=phase, kind="request", page=page, attempt=attempt, url=relative(url))
            retry_after: float | None = None
            try:
                response = self.transport.send(HttpRequest(url, self._headers("application/json" if expect_json else "application/xml"),
                                                           self.config.timeout))
            except TransportTimeout:
                reason, status = "timeout", None
            except TransportError as err:
                reason, status = "connection_error", None
                self._event(run, phase=phase, kind="error", page=page, detail=str(err)[:120])
            else:
                status = response.status
                metrics.statuses[status] += 1
                metrics.bytes_received += len(response.body)
                if 200 <= status < 300:
                    if not expect_json:
                        self._event(run, phase=phase, kind="response", page=page, status=status)
                        return response, response.body.decode("utf-8", errors="replace")
                    payload = self._parse(response.body)
                    if payload is not None:
                        self._event(run, phase=phase, kind="response", page=page, status=status,
                                    records=len(payload["value"]))
                        return response, payload
                    reason = "malformed_response"
                elif status in policy.retry_statuses:
                    reason = f"http_{status}"
                    retry_after = parse_retry_after(response.headers.get("retry-after"), self.clock.now())
                else:
                    self._event(run, phase=phase, kind="response", page=page, status=status)
                    raise ExtractionError("HTTP_ERROR", self._describe(response), status)
            self._event(run, phase=phase, kind="response", page=page, status=status, detail=reason)
            if attempt >= policy.max_attempts:
                code = {"timeout": "TIMEOUT", "malformed_response": "MALFORMED_RESPONSE",
                        "connection_error": "CONNECTION_ERROR"}.get(reason, "RETRIES_EXHAUSTED")
                raise ExtractionError(code, f"{reason} on {relative(url)} after {attempt} attempts", status)
            if retry_after is not None:
                if retry_after > policy.max_retry_after:
                    raise ExtractionError("RETRY_AFTER_TOO_LONG",
                                          f"server asked to wait {retry_after:.0f}s, above the {policy.max_retry_after:.0f}s ceiling", status)
                wait, basis = retry_after, "retry-after"
            else:
                ceiling = min(policy.max_delay, policy.base_delay * 2 ** (attempt - 1))
                wait, basis = self.rng.uniform(0, ceiling), "backoff+jitter"
            metrics.retries[reason] += 1
            metrics.wait_seconds += wait
            self._event(run, phase=phase, kind="retry", page=page, attempt=attempt, reason=reason,
                        wait=round(wait, 3), basis=basis)
            self.clock.sleep(wait)

    @staticmethod
    def _parse(body: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            return None
        if "@odata.count" in payload and (not isinstance(payload["@odata.count"], int) or payload["@odata.count"] < 0):
            return None
        if "@odata.nextLink" in payload and not isinstance(payload["@odata.nextLink"], str):
            return None
        if not all(isinstance(item, dict) for item in payload["value"]):
            return None
        return payload

    @staticmethod
    def _describe(response: HttpResponse) -> str:
        detail = ""
        try:
            error = json.loads(response.body.decode("utf-8")).get("error", {})
            detail = f" {error.get('code', '')}: {str(error.get('message', ''))[:160]}"
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        if 300 <= response.status < 400:
            detail = " redirects are not followed"
        return f"HTTP {response.status}{detail}".rstrip(":")

    # -- URL safety -----------------------------------------------------------------

    def entity_url(self, entity: str, params: dict[str, str]) -> str:
        return urljoin(self.config.service_root, quote(entity)) + "?" + urlencode(params, quote_via=quote, safe="$,'")

    def check_link(self, link: str, current: str) -> str:
        resolved = urljoin(current, link)
        parts = urlsplit(resolved)
        problem = None
        if len(resolved) > 8192:
            problem = "nextLink is unreasonably long"
        elif parts.scheme != self._root.scheme:
            problem = f"nextLink changes scheme to {parts.scheme or 'none'}"
        elif parts.username or parts.password:
            problem = "nextLink carries credentials"
        elif _host_port(resolved) not in self._allowed:
            problem = f"nextLink points to unauthorized host {parts.hostname}"
        elif not parts.path.startswith(self._root.path):
            problem = "nextLink leaves the service root path"
        if problem:
            raise ExtractionError("UNSAFE_NEXTLINK", problem)
        return parts._replace(fragment="").geturl()

    # -- extraction -----------------------------------------------------------------

    def extract(self, entity: str, key: str, *, key_type: str | None = None, filter_: str | None = None,
                select: list[str] | None = None) -> Extraction:
        run = Extraction(entity=entity, key=key, ordered_by_key=True, key_type=key_type)
        params = {"$count": "true", "$orderby": f"{key} asc"}
        if filter_:
            params["$filter"] = filter_
        if select:
            params["$select"] = ",".join(dict.fromkeys([key, *select]))
        url = self.entity_url(entity, params)
        seen_links = {normalize(url)}
        seen_pages: dict[str, int] = {}
        try:
            while True:
                if len(run.pages) >= self.config.max_pages:
                    raise ExtractionError("BUDGET_EXCEEDED", f"page budget of {self.config.max_pages} reached")
                number = len(run.pages) + 1
                _, payload = self.request(url, run, "extract", number)
                records = payload["value"]
                fingerprint = digest(records)
                page = Page(number, relative(url), records, payload.get("@odata.count"), payload.get("@odata.nextLink"), fingerprint)
                if records and fingerprint in seen_pages:
                    finding = {"code": "DUPLICATE_PAGE", "page": number,
                               "message": f"page {number} repeats page {seen_pages[fingerprint]} exactly"}
                    run.client_findings.append(finding)
                    self._event(run, phase="extract", kind="finding", page=number, detail=finding["message"])
                seen_pages.setdefault(fingerprint, number)
                run.pages.append(page)
                if not page.next_link:
                    self._event(run, phase="extract", kind="stop", page=number, detail="no nextLink: paging ended")
                    break
                next_url = self.check_link(page.next_link, url)
                if normalize(next_url) in seen_links:
                    finding = {"code": "NEXTLINK_CYCLE", "page": number,
                               "message": f"page {number} links to an already requested URL; stopped following"}
                    run.client_findings.append(finding)
                    run.stop_reason = "cycle"
                    self._event(run, phase="extract", kind="stop", page=number, detail=finding["message"])
                    break
                seen_links.add(normalize(next_url))
                url = next_url
        except ExtractionError as err:
            run.error, run.stop_reason = err, "error"
            self._event(run, phase="extract", kind="stop", detail=f"{err.code}: {err.message}")
            return run
        if self.config.reconcile and not filter_:
            run.reconciliation = self.reconcile(entity, key, run)
        return run

    def reconcile(self, entity: str, key: str, run: Extraction) -> Reconciliation:
        """Independent keyset read of the key only ($filter key gt last), used to confirm or refute completeness."""
        ids: list[Any] = []
        last: Any = None
        for _ in range(self.config.max_pages):
            condition = f"{key} ne null" if last is None else f"{key} gt {odata_literal(last)}"
            params = {"$select": key, "$orderby": f"{key} asc", "$filter": condition, "$top": str(self.config.page_size)}
            try:
                _, payload = self.request(self.entity_url(entity, params), run, "reconcile")
            except ExtractionError as err:
                reason = f"{err.code}: {err.message}"
                self._event(run, phase="reconcile", kind="stop", detail=f"reconciliation unavailable ({reason})")
                return Reconciliation(False, [], reason)
            values = [row.get(key) for row in payload["value"]]
            if not values:
                return Reconciliation(True, ids)
            if any(v is None or isinstance(v, bool) for v in values) or (last is not None and values[-1] <= last):
                return Reconciliation(False, [], "keyset read did not make progress; the provider's key ordering is unreliable")
            ids.extend(values)
            last = values[-1]
        return Reconciliation(False, [], f"keyset read exceeded {self.config.max_pages} pages")
