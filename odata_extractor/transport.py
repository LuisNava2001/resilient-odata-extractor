"""HTTP transport and clocks, both injectable so tests never wait in real time.

The real transport uses urllib with an explicit timeout, never follows redirects (a 3xx is returned to the client
as an ordinary response) and ignores proxy environment variables, so traffic only goes where the caller pointed it.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol


@dataclass
class HttpRequest:
    url: str
    headers: dict[str, str]
    timeout: float


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes


class TransportTimeout(Exception):
    """No complete response within the timeout."""


class TransportError(Exception):
    """Connection-level failure (refused, reset, truncated)."""


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib signature
        return None  # surface the 3xx instead of following it


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def send(self, request: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(request.url, headers=request.headers, method="GET")
        try:
            with self._opener.open(req, timeout=request.timeout) as response:
                body = response.read()
                return HttpResponse(response.status, {k.lower(): v for k, v in response.headers.items()}, body)
        except urllib.error.HTTPError as err:
            body = err.read() if err.fp else b""
            return HttpResponse(err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, body)
        except TimeoutError as err:
            raise TransportTimeout(str(err)) from None
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError):
                raise TransportTimeout(str(err.reason)) from None
            raise TransportError(str(err.reason)) from None
        except (ConnectionError, http.client.HTTPException) as err:
            raise TransportError(type(err).__name__) from None


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class VirtualClock:
    """Time that only moves when someone sleeps. Waits are recorded, not slept."""

    start: datetime = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    elapsed: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed)
