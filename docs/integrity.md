# Data integrity

The extractor never reports success just because paging stopped. It reports one of four statuses, with the reasons
and the checks that passed.

## Statuses

Evaluated in this order; the first that applies wins.

| Status | Conditions |
| --- | --- |
| **FAILED** | A technical error ended the run: retries exhausted, persistent timeout, malformed body, unsafe nextLink, non-retryable HTTP status, request or page budget exceeded, or `$metadata` unreachable for a reason other than "not provided". No completeness claim is made. |
| **INCOMPLETE** | Positive evidence of missing or unusable records: a record with a missing or mistyped key (quarantined); paging stopped by a nextLink cycle; a stable `@odata.count` greater than the unique records; an empty page ending paging while records were still expected; the key reconciliation lists IDs that were not extracted. |
| **INDETERMINATE** | No evidence of loss, but completeness cannot be shown: `@odata.count` changed during the run; duplicates of a key with different content; more unique records than the count; extracted IDs the server no longer lists; no count and no reconciliation; paging anomalies with no reconciliation to rule out loss; a stable count that matches the unique records but no key reconciliation to confirm which records they are (`COUNT_ONLY`: the provider cannot filter on the key, or `--no-reconcile`). |
| **COMPLETE** | Paging ended normally, every key is valid and unique after removing identical duplicates, the count (when provided) was stable and equals the unique records, and the key reconciliation returned exactly the same IDs. Without a reconciliation there is no COMPLETE. Identical duplicates are tolerated only when the reconciliation confirms the set. |

The report always lists the checks that passed (`basis`). A consistent count is recorded there as a passed check,
but on its own it only leads to INDETERMINATE: the same number of records is not proof of the same records.

## What can be verified

- Every record has a key of the declared type, and no key appears twice with different content.
- The number of unique records equals a count that did not change during the run.
- Paging ended because the provider said so (no nextLink), not because of a cycle or a budget.
- A second, independent read of the key returns exactly the same ID set (when the provider can filter on the key).
- No page was an exact replay of another.

## What cannot be guaranteed

- **Field values are correct.** COMPLETE is about which records arrived, not whether their contents are true.
- **A consistent snapshot.** `$skip`/`$skiptoken` plus `$orderby` do not freeze the dataset. Inserts, deletes or
  updates between page requests shift offsets or change records already read. The count check only catches
  changes in the total. An update that leaves the total unchanged is invisible without change tracking (delta
  links, ETags or a modification timestamp), which this MVP does not use.
- **That the reconciliation is right.** It is a second read of the same provider. Agreement makes a silent loss
  unlikely; it is not proof against a provider that is consistently wrong in both read paths. In the mock, faults
  only affect requests without `$filter`, so the reconciliation always sees the true data; a real provider gives no
  such guarantee.
- **Completeness without a key filter.** Then the honest answer is INDETERMINATE, even when the count matches.

## A matching count is not proof

Scenario `dataset_shift` deletes 4 records that page 1 already returned. Later offsets shift, so 4 records that
still exist are never returned. At the end, the 416 unique records match the final `@odata.count` of 416, but the
set is wrong. The integrity engine reports:

- `COUNT_CHANGED` (420 then 416);
- `RECONCILIATION_MISSING` with IDs 101 to 104 (exist, not extracted);
- `RECONCILIATION_EXTRA` with IDs 6, 16, 26 and 36 (extracted, now deleted).

The status is INCOMPLETE, even though the counts "match".
