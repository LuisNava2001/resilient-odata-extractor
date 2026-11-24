"""Deterministic provider-failure scenarios for the mock server.

Faults are keyed to page numbers and attempt counts, never to randomness, so every run of a scenario produces the
same responses. `conformant` says whether the simulated provider behaviour is something a correct OData service
may legitimately do (rate limiting, transient errors, data changing between requests) or a provider defect
(replayed pages, cyclic or foreign nextLinks, dropped records, malformed bodies).

Faults apply to the offset-paged extraction requests (requests without $filter) for the scenario's entity set.
The key-based reconciliation pass uses $filter and is therefore not affected, which keeps it an independent read.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fault:
    kind: str
    page: int = 2
    times: int = 1  # attempts affected for status/delay/malformed faults; 0 means every attempt
    status: int = 0
    retry_after: str | None = None
    count: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    conformant: bool
    expected_status: str
    faults: tuple[Fault, ...] = field(default_factory=tuple)
    supports_count: bool = True
    supports_filter: bool = True
    entity: str = "WorkOrders"


SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("healthy", "Healthy provider", "Clean server-driven paging with $count.", True, "COMPLETE"),
    Scenario("rate_limit", "429 with Retry-After", "Page 2 is rate limited twice with Retry-After: 2.", True, "COMPLETE",
             (Fault("status", page=2, times=2, status=429, retry_after="2"),)),
    Scenario("transient_503", "Transient 503", "Page 3 returns 503 twice without Retry-After.", True, "COMPLETE",
             (Fault("status", page=3, times=2, status=503),)),
    Scenario("timeout", "Slow response", "The first attempt at page 2 takes longer than the client timeout.", True, "COMPLETE",
             (Fault("delay", page=2, times=1, seconds=1.2),)),
    Scenario("duplicate_page", "Replayed page", "The request for page 3 returns the records of page 2 again.", False, "INCOMPLETE",
             (Fault("replay_page", page=3),)),
    Scenario("nextlink_cycle", "Cyclic nextLink", "Page 4 links back to page 2.", False, "INCOMPLETE",
             (Fault("cycle", page=4),)),
    Scenario("overlap_duplicates", "Overlapping pages", "From page 2 on, each page repeats the last 3 records of the previous page.",
             False, "COMPLETE", (Fault("overlap", page=2, count=3),)),
    Scenario("skipped_records", "Dropped records", "Page 2 silently omits 3 records; the nextLink still advances.", False,
             "INCOMPLETE", (Fault("drop", page=2, count=3),)),
    Scenario("empty_page", "Unexpected empty 200", "Page 3 is a 200 with an empty value array and no nextLink.", False,
             "INCOMPLETE", (Fault("empty_end", page=3),)),
    Scenario("dataset_shift", "Deletes during paging", "After page 2 is served, 4 records already returned on page 1 are deleted, "
             "so later offsets shift and 4 existing records are never returned.", True, "INCOMPLETE",
             (Fault("mutate_delete", page=2, count=4),)),
    Scenario("dataset_growth", "Inserts during paging", "After page 1 is served, 6 new records are appended; the total changes mid-extraction.",
             True, "INDETERMINATE", (Fault("mutate_append", page=1, count=6),)),
    Scenario("malformed_json", "Malformed body", "Page 2 always returns a truncated JSON body with status 200.", False, "FAILED",
             (Fault("malformed", page=2, times=0),)),
    Scenario("invalid_records", "Records without valid keys", "Page 2 contains 2 records whose key is null or not an integer.",
             False, "INCOMPLETE", (Fault("invalid_keys", page=2, count=2),)),
    Scenario("foreign_nextlink", "nextLink to another host", "Page 2's nextLink points to an unrelated host.", False, "FAILED",
             (Fault("foreign_nextlink", page=2),)),
    Scenario("no_count", "No $count, no key filtering", "The provider ignores $count and rejects $filter, so nothing can confirm completeness.",
             True, "INDETERMINATE", supports_count=False, supports_filter=False),
)}

DEMO_ORDER = ["healthy", "rate_limit", "transient_503", "timeout", "nextlink_cycle", "duplicate_page", "overlap_duplicates",
              "skipped_records", "empty_page", "invalid_records", "dataset_shift", "dataset_growth", "no_count",
              "malformed_json", "foreign_nextlink"]
