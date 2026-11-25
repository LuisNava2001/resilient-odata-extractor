# Testing

```bash
python -m pip install -e ".[dev]"
python -m pytest -v                                        # 138 tests
python -m odata_extractor demo --out demo/data.js --check  # 15 scenarios + demo-data drift check
```

No test waits on a real clock for retries: waits go through a virtual clock and jitter uses a seeded generator.
The only real waits are the short network timeouts in the timeout scenario.

## Inventory

| File | Tests | Covers |
| --- | --- | --- |
| `test_dataset.py` | 4 | Seeded reproducibility, sizes and keys, referential integrity of the synthetic data, fictional emails and phones |
| `test_query.py` | 14 | `$filter`, `$orderby` with key tiebreak, `$select`, rejection of invalid or unsupported options |
| `test_mock_server.py` | 15 | Service document, `$metadata`, paging and `$count`, `Prefer`, `$top`/`$skip`, error bodies, token, every fault type, dataset changes, reduced capabilities, faults not touching filtered requests |
| `test_client.py` | 30 | Request construction, nextLink following, relative links, unsafe links (host, scheme, credentials, path, port), allow-list, cycles, replayed pages, Retry-After (seconds, HTTP-date, ceiling), exponential jittered capped backoff, timeouts, connection errors, non-retryable statuses and redirects, malformed bodies, budgets, keyset reconciliation, token never recorded |
| `test_integrity.py` | 19 | Every status, its conditions and their precedence, including "matching count with the wrong IDs" and "matching count without a key reconciliation" (INDETERMINATE) |
| `test_e2e.py` | 24 | Real loopback HTTP: all 15 scenarios reach their expected status, healthy output equals the dataset, other entity sets, Retry-After waits, real timeout, token sent but never written, a 302 to a foreign host is not followed by the real transport, outputs match data |
| `test_schema.py` | 6 | CSDL parsing, bad metadata, sample inference, patterns, provenance and limits in the report |
| `test_privacy_export.py` | 9 | Rules, pseudonym stability and scoping, text scrubbing and safety net, key source, environment key never written to any output, output layout, `validate` catching tampering and leaks, CSV formula neutralisation, honest FAILED output |
| `test_cli.py` | 12 | Exit codes 0/3/4/5, `--no-reconcile` cannot reach COMPLETE, validate, token from environment, unreachable service, inspect-schema, mock server refuses non-loopback hosts, seed, scenarios, demo |
| `test_powershell.py` | 2 | PowerShell 7 wrapper: run summary CSV and worst-status exit code (skipped only if `pwsh` is absent) |
| `test_demo.py` | 3 | Determinism, committed demo data equals a fresh run, required stories, static and offline page |

## Local results (2026-11-25, Windows, Python 3.11.9, PowerShell 7.6)

- `python -m pytest -v`: 138 passed, 0 skipped, 0 failed.
- `python -m odata_extractor demo`: 15/15 scenarios produced their expected integrity status.

## Continuous integration

`.github/workflows/ci.yml` runs the suite, the scenario and drift check, and a CLI smoke test on Ubuntu and Windows
with Python 3.11, 3.12 and 3.13. Results are in the repository's Actions tab once the workflow has run.

## Limits

- The client is tested against this mock and scripted transports only, not against a real third-party OData
  service.
- TLS is not exercised (the mock is plain HTTP on loopback).
- Load, very large result sets and long-running extractions are out of scope.
