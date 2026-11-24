# Supported OData subset and simulated provider faults

The mock service implements a **documented subset of OData v4** to exercise the client. It is not a complete or
certified OData implementation.

## Supported

| Feature | Behaviour |
| --- | --- |
| Service document | `GET /odata/` lists the six entity sets |
| `$metadata` | CSDL v4 XML: entity types, single-property keys, property types and nullability, navigation properties with referential constraints, entity sets with navigation bindings |
| Collections | `GET /odata/<EntitySet>` returning `{"@odata.context", "@odata.count"?, "value", "@odata.nextLink"?}` |
| Server-driven paging | 50 records per page by default; `Prefer: odata.maxpagesize=N` honoured up to 200 and echoed in `Preference-Applied` |
| `@odata.nextLink` | Absolute URL that repeats the original options and adds `$skiptoken` (an offset, not an opaque cursor) |
| `$count=true` | Total matching records, included on every page |
| `$filter` | `<property> <eq|ne|gt|ge|lt|le> <literal>` joined by `and`; literals: integers, `'strings'` (with `''` escaping), `true`, `false`, `null` |
| `$orderby` | Comma-separated properties with `asc` or `desc`; the key is appended as a tiebreaker so the order is total |
| `$select` | Comma-separated properties |
| `$top`, `$skip` | Client-driven window; server paging continues inside it |
| Errors | OData error body `{"error": {"code", "message"}}` with 400, 401, 404, 405 or 501 |
| Auth (optional) | A static bearer token can be required (`serve --require-token-env NAME`) |

## Not supported (rejected, never ignored)

`$expand`, `$search`, `$apply`, `$compute`, `$format`, `or` and functions in `$filter`, lambda operators, batch
requests, delta links, ETags, writes. Unknown `$` options return 501 so a client never believes an option was
applied when it was not.

## Failure scenarios

`odata-extractor serve --scenario <name>` and `odata-extractor demo` use these. Faults are deterministic: they
are keyed to the page number and attempt count of the offset-paged requests (requests without `$filter`) for the
`WorkOrders` set. The key-based reconciliation uses `$filter` and is not affected, so it remains an independent
read.

| Scenario | What the provider does | OData-conformant? | Expected status |
| --- | --- | --- | --- |
| `healthy` | Clean paging with `$count` | yes | COMPLETE |
| `rate_limit` | 429 with `Retry-After: 2` twice on page 2 | yes | COMPLETE |
| `transient_503` | 503 twice on page 3 | yes | COMPLETE |
| `timeout` | First attempt at page 2 takes 1.2 s | yes (slow, not wrong) | COMPLETE |
| `nextlink_cycle` | Page 4 links back to page 2 | no, provider defect | INCOMPLETE |
| `duplicate_page` | Page 3 returns page 2's records again | no, provider defect | INCOMPLETE |
| `overlap_duplicates` | Each page repeats the last 3 records of the previous one | no, provider defect | COMPLETE (after dedupe, verified) |
| `skipped_records` | Page 2 omits 3 records, nextLink still advances | no, provider defect | INCOMPLETE |
| `empty_page` | Page 3 is an empty 200 with no nextLink | no, provider defect | INCOMPLETE |
| `invalid_records` | Two keys on page 2 are null or not integers | no, provider defect | INCOMPLETE |
| `dataset_shift` | 4 already-returned records deleted after page 2 | yes (data changed) | INCOMPLETE |
| `dataset_growth` | 6 records appended after page 1 | yes (data changed) | INDETERMINATE |
| `no_count` | `$count` ignored, `$filter` rejected | yes (optional features absent) | INDETERMINATE |
| `malformed_json` | Page 2 body is truncated JSON with status 200 | no, provider defect | FAILED |
| `foreign_nextlink` | Page 2's nextLink points to `evil.example` | no, provider defect or attack | FAILED |

The defects are deliberate: the point of the mock is to behave like a provider that is sometimes wrong.
