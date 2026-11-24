# Schema discovery and privacy-aware export

## Schema discovery

`odata-extractor inspect-schema` reports, with provenance on every line:

- **From `$metadata` (declared).** Entity sets, entity types, keys, property types and nullability, navigation
  properties with their target set and foreign key. These are what the provider declares; they are not verified
  against the data. Composite keys are reported; the extractor itself needs a single key.
- **From samples (inferred).** Used when there is no `$metadata`, or in addition with `--sample N`. For each field:
  the JSON types observed, how many nulls were seen, and a pattern guess (date, date-time, email address) when every
  sampled string matches.
  - Key candidates are fields that are present, non-null and unique in the sample; this is not a declared key.
  - Reference hints are exact name matches with another set's key; HomeLocationId is not guessed to mean Locations.
  - Relationships, cardinalities and constraints are never inferred.

The extractor uses the declared key and its type when `$metadata` exists. Without `$metadata`, `--key` is required,
and keys must all have the type of the first valid key.

## Privacy-aware export

Each extraction writes `<output>/<Entity>/`:

| Path | Content |
| --- | --- |
| `raw/records.jsonl` | Unique valid records as received. May contain personal data; keep local. |
| `raw/quarantine.jsonl` | Records without a valid key (only when present) |
| `publishable/records.jsonl` | Records after the privacy rules |
| `publishable/records.csv` | Same, as CSV; values starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` to defuse spreadsheet formulas |
| `report.json` | Integrity status, reasons, warnings, passed checks, metrics, timeline (paths only), schema, privacy settings (never the key) |
| `summary.md` | Human-readable summary |
| `manifest.json` | SHA-256, size and line count of every file |

A FAILED run still writes its partial records and an honest report, for diagnosis.

### Rules

Rules are JSON, per entity set (`odata_extractor/data/privacy_rules.json`, or `--privacy FILE`):

| Operation | Effect | Example |
| --- | --- | --- |
| `drop` | Field removed | Technician phone |
| `mask_email` | First character and domain kept | `a***@example.org` |
| `pseudonymize` | `psn_` plus 16 hex characters of HMAC-SHA256(key, field scope + value); stable for one key, unlinkable without it | Employee number |
| `redact` | `[REDACTED]` | Names |
| `redact_text` | Emails and `+`-prefixed phone numbers inside free text replaced | Notes |

A **safety net** then replaces any email address or `+`-prefixed phone number left in any string field, configured
or not.

**Pseudonymization key.** The key comes from `ODATA_EXTRACTOR_PSEUDONYM_KEY`. If that is not set, a random key is
generated per run (pseudonyms then change between runs). `--demo-pseudonym-key` uses a public, non-secret key so
the demo is reproducible. The report records where the key came from, never the key.

`odata-extractor validate <folder>` re-hashes every file against the manifest, checks that the line counts match
`unique_records`, that no dropped field or unmasked email address is in the publishable output, and that the
report mentions no authorization header.

### Limits

Detection is pattern-based. Phone numbers without a leading `+`, names inside free text and personal data in
fields nobody configured (other than emails and international phone numbers) are not caught. The rules shipped
here fit the fictional dataset; real data needs its own review.
