# Resilience and client safety

## Per-request policy

| Situation | Behaviour |
| --- | --- |
| Every request | Explicit timeout (default 10 s), `Accept`, `OData-MaxVersion: 4.0`, `Prefer: odata.maxpagesize=N` |
| 429, 502, 503, 504 | Retried |
| Timeout, connection reset or refused | Retried |
| 200 with a body that is not a JSON object with a `value` array (or with a non-integer count) | Retried, then `MALFORMED_RESPONSE` |
| Any other status (400, 401, 403, 404, 500, any 3xx) | Fails immediately with `HTTP_ERROR`; redirects are never followed |
| `Retry-After` (seconds or HTTP-date) | Waited exactly; above 120 s the run fails with `RETRY_AFTER_TOO_LONG` instead of hanging |
| No `Retry-After` | Exponential backoff with full jitter: `wait = uniform(0, min(max_delay, base * 2^(attempt-1)))` |
| Attempts | At most `max_attempts` per request (default 5, including the first) |

Defaults: base 0.5 s, cap 30 s. All of them are CLI options. Waits go through an injectable clock, so tests and
the demo record the exact waits without sleeping.

## Run-level limits

- `max_pages` (default 1000) and `max_requests` (default 2500) bound every extraction; exceeding either ends the
  run with `BUDGET_EXCEEDED`.
- A nextLink already requested (compared after normalising the scheme, host, port and path, and sorting the query
  parameters) is a cycle: paging stops and the run is assessed, not repeated.
- A page whose records hash exactly like an earlier page is flagged as replayed.

## nextLink safety

A nextLink is followed only if, after resolving it against the current URL, it:

- keeps the service root's scheme (no downgrade from https to http);
- carries no user name or password;
- targets the service root's host and port, or a host passed explicitly with `--allow-host`;
- stays under the service root path;
- is shorter than 8 KB.

Anything else ends the run with `UNSAFE_NEXTLINK` before the URL is requested.

## Credentials

A bearer token is read from an environment variable named on the command line (`--token-env NAME`). It is sent in
the `Authorization` header only. No report, log line, timeline entry or error message contains headers or the
token, and tests assert that the token string does not appear in any output file. The timeline stores URLs as
path and query only.

## Limits of these protections

- Proxies are not supported (environment proxy settings are ignored on purpose).
- TLS verification uses Python's defaults; there is no certificate pinning.
- The allow-list protects the token from being sent to a foreign host by a malicious nextLink. It does not protect
  against a compromised service root itself.
- A malformed body is retried because truncation can be transient. A provider that keeps returning bad data will
  still fail, and the retries cost requests against the budget.
