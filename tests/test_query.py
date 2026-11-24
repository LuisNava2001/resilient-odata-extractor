import pytest

from odata_extractor.dataset import ENTITY_SETS, generate
from odata_extractor.query import QueryError, evaluate, parse, project

WO = ENTITY_SETS["WorkOrders"]
DATA = generate()["WorkOrders"]


def run(params):
    return evaluate(DATA, parse(params, WO), WO)


def test_filter_comparisons_and_conjunction() -> None:
    rows = run({"$filter": "Status eq 'Completed' and LaborMinutes ge 100"})
    assert rows and all(r["Status"] == "Completed" and r["LaborMinutes"] >= 100 for r in rows)
    assert all(r["WorkOrderId"] > 400 for r in run({"$filter": "WorkOrderId gt 400"}))
    assert all(r["CompletedAt"] is None for r in run({"$filter": "CompletedAt eq null"}))
    assert run({"$filter": "Status eq 'O''Brien'"}) == []


def test_orderby_is_total_with_key_tiebreaker() -> None:
    rows = run({"$orderby": "Status desc"})
    statuses = [r["Status"] for r in rows]
    assert statuses == sorted(statuses, reverse=True)
    same = [r["WorkOrderId"] for r in rows if r["Status"] == rows[0]["Status"]]
    assert same == sorted(same)


def test_select_projects_fields() -> None:
    query = parse({"$select": "WorkOrderId,Status"}, WO)
    assert project(DATA[0], query.select) == {"WorkOrderId": 1, "Status": DATA[0]["Status"]}


@pytest.mark.parametrize("params", [
    {"$filter": "Unknown eq 1"}, {"$filter": "Status contains 'x'"}, {"$filter": "Status eq 'x' or Status eq 'y'"},
    {"$orderby": "Nope asc"}, {"$orderby": "Status sideways"}, {"$select": "Nope"}, {"$top": "-1"}, {"$skip": "x"},
    {"$count": "yes"}, {"$expand": "Technician"}, {"$search": "pump"},
])
def test_unsupported_or_invalid_options_are_rejected(params) -> None:
    with pytest.raises(QueryError):
        parse(params, WO)
