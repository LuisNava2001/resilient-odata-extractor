import pytest

from odata_extractor.dataset import generate
from odata_extractor.mock_server import metadata_xml
from odata_extractor.schema import MetadataError, infer_from_samples, parse_metadata, render_report


def test_metadata_declares_keys_types_and_navigation() -> None:
    sets = parse_metadata(metadata_xml())
    work_orders = sets["WorkOrders"]
    assert work_orders["key"] == ["WorkOrderId"]
    types = {p["name"]: (p["type"], p["nullable"]) for p in work_orders["properties"]}
    assert types["CompletedAt"] == ("Edm.DateTimeOffset", True) and types["WorkOrderId"] == ("Edm.Int32", False)
    navs = {n["name"]: n for n in work_orders["navigations"]}
    assert navs["Technician"]["target_set"] == "Technicians" and navs["Technician"]["foreign_key"] == "TechnicianId"


@pytest.mark.parametrize("text", ["<not-xml", "<root/>"])
def test_bad_metadata_is_rejected(text) -> None:
    with pytest.raises(MetadataError):
        parse_metadata(text)


def test_inference_describes_only_the_sample() -> None:
    sample = generate()["Technicians"][:20]
    result = infer_from_samples(sample, {"Locations": "LocationId", "Technicians": "TechnicianId"})
    fields = {f["name"]: f for f in result["fields"]}
    assert fields["TechnicianId"]["observed_types"] == ["integer"]
    assert fields["Email"]["pattern"] == "email address"
    assert fields["Phone"]["nulls_seen"] >= 0
    assert result["key_candidates"][0] == "TechnicianId"
    # Hints are exact name matches only: HomeLocationId is not guessed to mean Locations.
    assert result["reference_hints"] == []
    orders = infer_from_samples(generate()["WorkOrders"][:20], {"Technicians": "TechnicianId", "WorkOrders": "WorkOrderId"})
    assert orders["reference_hints"] == [{"field": "TechnicianId", "possible_target": "Technicians"}]


def test_inference_patterns_and_mixed_types() -> None:
    sample = [{"Id": 1, "When": "2026-01-05T08:00:00Z", "Day": "2026-01-05", "Mixed": 1},
              {"Id": 2, "When": "2026-01-06T08:00:00Z", "Day": "2026-01-06", "Mixed": "one", "Late": None}]
    fields = {f["name"]: f for f in infer_from_samples(sample)["fields"]}
    assert fields["When"]["pattern"] == "date-time" and fields["Day"]["pattern"] == "date"
    assert fields["Mixed"]["observed_types"] == ["integer", "string"]
    assert fields["Late"]["observed_types"] == ["null"] and fields["Late"]["present_in"] == 1


def test_report_labels_provenance_and_limits() -> None:
    sets = parse_metadata(metadata_xml())
    declared = render_report("WorkOrders", sets["WorkOrders"], None)
    assert "not verified against the data" in declared and "| WorkOrderId | Edm.Int32 | no |" in declared
    inferred = render_report("WorkOrders", None, infer_from_samples(generate()["WorkOrders"][:10]))
    assert "describe only this sample" in inferred and "no relationship, cardinality or constraint is inferred" in inferred
    assert "not a declared key" in inferred
