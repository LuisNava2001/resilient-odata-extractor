"""The OData query subset the mock server understands.

Supported: $filter (comparisons joined by `and`), $orderby, $select, $top, $skip, $count and an offset-based
$skiptoken used in server-generated nextLinks. Anything else that starts with `$` is rejected with HTTP 400, so
the server never silently ignores an option it does not implement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .dataset import EntitySet

SUPPORTED_OPTIONS = {"$filter", "$orderby", "$select", "$top", "$skip", "$count", "$skiptoken"}
_CLAUSE = re.compile(r"\s*([A-Za-z_]\w*)\s+(eq|ne|gt|ge|lt|le)\s+('(?:[^']|'')*'|-?\d+|true|false|null)\s*")
_AND = re.compile(r"and\b", re.IGNORECASE)


class QueryError(ValueError):
    def __init__(self, message: str, code: str = "BadRequest") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Clause:
    field: str
    op: str
    value: Any


@dataclass
class Query:
    filters: list[Clause] = field(default_factory=list)
    orderby: list[tuple[str, bool]] = field(default_factory=list)  # (field, descending)
    select: list[str] | None = None
    top: int | None = None
    skip: int = 0
    count: bool = False
    skiptoken: int | None = None


def _literal(token: str) -> Any:
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    if token in ("true", "false"):
        return token == "true"
    if token == "null":
        return None
    return int(token)


def parse_filter(text: str, entity: EntitySet) -> list[Clause]:
    clauses, pos = [], 0
    while True:
        match = _CLAUSE.match(text, pos)
        if not match:
            raise QueryError(f"unsupported $filter near position {pos}: only '<field> <eq|ne|gt|ge|lt|le> <literal>' joined by 'and'")
        name, op, raw = match.groups()
        if name not in entity.field_names():
            raise QueryError(f"unknown property '{name}' in $filter")
        clauses.append(Clause(name, op, _literal(raw)))
        pos = match.end()
        if pos >= len(text):
            return clauses
        conj = _AND.match(text, pos)
        if not conj:
            raise QueryError(f"unsupported $filter near position {pos}: only 'and' is supported")
        pos = conj.end()


def _non_negative(name: str, raw: str) -> int:
    if not raw.isdigit():
        raise QueryError(f"{name} must be a non-negative integer")
    return int(raw)


def parse(params: dict[str, str], entity: EntitySet) -> Query:
    unknown = sorted(k for k in params if k.startswith("$") and k not in SUPPORTED_OPTIONS)
    if unknown:
        raise QueryError(f"query option {unknown[0]} is not supported by this service", "NotImplemented")
    query = Query()
    if params.get("$filter"):
        query.filters = parse_filter(params["$filter"], entity)
    if params.get("$orderby"):
        for part in params["$orderby"].split(","):
            words = part.split()
            if not words or len(words) > 2 or (len(words) == 2 and words[1].lower() not in ("asc", "desc")):
                raise QueryError(f"invalid $orderby item '{part.strip()}'")
            if words[0] not in entity.field_names():
                raise QueryError(f"unknown property '{words[0]}' in $orderby")
            query.orderby.append((words[0], len(words) == 2 and words[1].lower() == "desc"))
    if params.get("$select"):
        query.select = [name.strip() for name in params["$select"].split(",")]
        for name in query.select:
            if name not in entity.field_names():
                raise QueryError(f"unknown property '{name}' in $select")
    if "$top" in params:
        query.top = _non_negative("$top", params["$top"])
    if "$skip" in params:
        query.skip = _non_negative("$skip", params["$skip"])
    if "$skiptoken" in params:
        query.skiptoken = _non_negative("$skiptoken", params["$skiptoken"])
    if "$count" in params:
        if params["$count"] not in ("true", "false"):
            raise QueryError("$count must be true or false")
        query.count = params["$count"] == "true"
    return query


def _matches(record: dict[str, Any], clause: Clause) -> bool:
    left, right = record.get(clause.field), clause.value
    if clause.op in ("eq", "ne"):
        return (left == right) == (clause.op == "eq")
    if left is None or right is None or type(left) is not type(right):
        return False
    return {"gt": left > right, "ge": left >= right, "lt": left < right, "le": left <= right}[clause.op]


def _sort_key(value: Any) -> tuple[int, Any]:
    return (0, "") if value is None else (1, value)  # nulls first, like many OData services


def evaluate(records: list[dict[str, Any]], query: Query, entity: EntitySet) -> list[dict[str, Any]]:
    """Filter and order (key appended as a tiebreaker so the order is total). Paging is the caller's job."""
    rows = [r for r in records if all(_matches(r, c) for c in query.filters)]
    orderby = list(query.orderby)
    if entity.key not in [name for name, _ in orderby]:
        orderby.append((entity.key, False))
    for name, descending in reversed(orderby):
        rows.sort(key=lambda r, n=name: _sort_key(r.get(n)), reverse=descending)
    return rows


def project(record: dict[str, Any], select: list[str] | None) -> dict[str, Any]:
    return {k: record.get(k) for k in select} if select else dict(record)
