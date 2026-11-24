import re

from odata_extractor.dataset import ENTITY_SETS, SIZES, digest, generate


def test_generation_is_reproducible_and_seed_dependent() -> None:
    assert digest(generate()) == digest(generate())
    assert digest(generate(1)) != digest(generate(2))


def test_sizes_keys_and_multiple_pages() -> None:
    data = generate()
    for name, entity in ENTITY_SETS.items():
        rows = data[name]
        assert len(rows) == SIZES[name]
        keys = [r[entity.key] for r in rows]
        assert keys == list(range(1, len(rows) + 1))
        assert set(rows[0]) == set(entity.field_names())
    assert SIZES["WorkOrders"] > 50 * 8  # many pages at the default page size


def test_relationships_point_to_existing_records() -> None:
    data = generate()
    ids = {name: {r[ENTITY_SETS[name].key] for r in data[name]} for name in data}
    for name, entity in ENTITY_SETS.items():
        for nav in entity.navigations:
            assert {r[nav.foreign_key] for r in data[name]} <= ids[nav.target_set], (name, nav.name)


def test_personal_data_is_fictional() -> None:
    data = generate()
    emails = [t["Email"] for t in data["Technicians"]] + [r["RequestedByEmail"] for r in data["MaintenanceRequests"]]
    assert all(e.endswith("@example.org") for e in emails)
    phones = [t["Phone"] for t in data["Technicians"] if t["Phone"]]
    assert phones and all(re.fullmatch(r"\+1-555-01\d\d", p) for p in phones)
    notes = " ".join(r["Notes"] or "" for r in data["MaintenanceRequests"])
    assert "@example.org" in notes and "+1-555-01" in notes  # free text that the privacy layer must scrub
