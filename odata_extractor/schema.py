"""Limited schema discovery: read OData CSDL $metadata, or infer from sample records when there is none.

Every statement in the report carries its provenance. Metadata facts are what the service declares, not what its
data was verified to contain. Inferred facts describe only the sample that was read: types and patterns can be
wrong for unseen records, and no relationship or cardinality is ever inferred, only flagged as a name-based hint.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

EDMX = "{http://docs.oasis-open.org/odata/ns/edmx}"
EDM = "{http://docs.oasis-open.org/odata/ns/edm}"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MetadataError(ValueError):
    pass


def parse_metadata(xml_text: str) -> dict[str, dict[str, Any]]:
    """Entity sets declared in a CSDL v4 document: key, properties (type, nullability) and navigation properties."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        raise MetadataError(f"$metadata is not well-formed XML: {err}") from None
    if root.tag != f"{EDMX}Edmx":
        raise MetadataError("not an OData v4 CSDL document (expected edmx:Edmx)")
    types: dict[str, dict[str, Any]] = {}
    sets: dict[str, dict[str, Any]] = {}
    for schema in root.iter(f"{EDM}Schema"):
        namespace = schema.get("Namespace", "")
        for entity_type in schema.findall(f"{EDM}EntityType"):
            keys = [ref.get("Name") for ref in entity_type.findall(f"{EDM}Key/{EDM}PropertyRef")]
            props = [{"name": p.get("Name"), "type": p.get("Type"), "nullable": p.get("Nullable", "true") != "false"}
                     for p in entity_type.findall(f"{EDM}Property")]
            navs = []
            for nav in entity_type.findall(f"{EDM}NavigationProperty"):
                constraint = nav.find(f"{EDM}ReferentialConstraint")
                navs.append({"name": nav.get("Name"), "type": nav.get("Type"),
                             "collection": (nav.get("Type") or "").startswith("Collection("),
                             "foreign_key": constraint.get("Property") if constraint is not None else None})
            types[f"{namespace}.{entity_type.get('Name')}"] = {"keys": keys, "properties": props, "navigations": navs}
        for container in schema.findall(f"{EDM}EntityContainer"):
            for entity_set in container.findall(f"{EDM}EntitySet"):
                bindings = {b.get("Path"): b.get("Target") for b in entity_set.findall(f"{EDM}NavigationPropertyBinding")}
                sets[entity_set.get("Name")] = {"entity_type": entity_set.get("EntityType"), "bindings": bindings}
    result = {}
    for name, info in sets.items():
        entity_type = types.get(info["entity_type"])
        if entity_type is None:
            continue
        navs = [{**nav, "target_set": info["bindings"].get(nav["name"])} for nav in entity_type["navigations"]]
        result[name] = {"source": "metadata", "entity_type": info["entity_type"], "key": entity_type["keys"],
                        "properties": entity_type["properties"], "navigations": navs}
    return result


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _pattern(values: list[str]) -> str | None:
    for name, regex in (("date", _DATE), ("date-time", _DATETIME), ("email address", _EMAIL)):
        if values and all(regex.match(v) for v in values):
            return name
    return None


def infer_from_samples(records: list[dict[str, Any]], known_keys: dict[str, str] | None = None) -> dict[str, Any]:
    """Describe fields seen in a sample. `known_keys` maps other entity sets to their key names, for reference hints."""
    names: list[str] = []
    for record in records:
        for name in record:
            if name not in names and not name.startswith("@odata."):
                names.append(name)
    fields, candidates = [], []
    for name in names:
        values = [r.get(name) for r in records if name in r]
        kinds = sorted({_kind(v) for v in values} - {"null"})
        strings = [v for v in values if isinstance(v, str)]
        entry = {"name": name, "observed_types": kinds or ["null"], "present_in": len(values),
                 "nulls_seen": sum(1 for v in values if v is None),
                 "pattern": _pattern(strings) if kinds == ["string"] else None}
        fields.append(entry)
        non_null = [v for v in values if v is not None]
        if len(values) == len(records) and len(non_null) == len(records) and len({str(v) for v in non_null}) == len(records):
            candidates.append(name)
    candidates.sort(key=lambda n: (not n.lower().endswith("id"), names.index(n)))
    hints = []
    for name in names:
        for other_set, other_key in (known_keys or {}).items():
            if name == other_key and (not candidates or name != candidates[0]):
                hints.append({"field": name, "possible_target": other_set})
    return {"source": "inferred", "sample_size": len(records), "fields": fields, "key_candidates": candidates,
            "reference_hints": hints}


def render_report(entity: str, metadata: dict[str, Any] | None, inferred: dict[str, Any] | None) -> str:
    lines = [f"# Schema report: {entity}", ""]
    if metadata:
        lines += ["Source: the service's $metadata document. These are declarations by the provider; they were not "
                  "verified against the data.", "", f"- Entity type: `{metadata['entity_type']}`",
                  f"- Declared key: {', '.join(metadata['key']) or '(none)'}", "", "| Property | Declared type | Nullable |",
                  "| --- | --- | --- |"]
        lines += [f"| {p['name']} | {p['type']} | {'yes' if p['nullable'] else 'no'} |" for p in metadata["properties"]]
        if metadata["navigations"]:
            lines += ["", "Declared navigation properties (relationships stated by $metadata, not verified):", ""]
            for nav in metadata["navigations"]:
                many = "many" if nav["collection"] else "single"
                lines.append(f"- {nav['name']} -> {nav.get('target_set') or nav['type']} ({many}; foreign key "
                             f"{nav['foreign_key'] or 'not declared'})")
    if inferred:
        lines += ["", f"Source: inference from {inferred['sample_size']} sample record(s). Limits: types and patterns describe "
                  "only this sample; unseen records may differ; nullability means 'null was or was not seen'; no "
                  "relationship, cardinality or constraint is inferred.", "",
                  "| Field | Observed types | Pattern (sample-based guess) | Nulls seen |", "| --- | --- | --- | --- |"]
        lines += [f"| {f['name']} | {', '.join(f['observed_types'])} | {f['pattern'] or '-'} | {f['nulls_seen']} |"
                  for f in inferred["fields"]]
        lines += ["", "Key candidates (unique and non-null in this sample only, not a declared key): "
                  + (", ".join(inferred["key_candidates"]) or "none")]
        if inferred["reference_hints"]:
            lines += ["", "Possible references (name-based guesses, not verified):"]
            lines += [f"- {h['field']} might refer to {h['possible_target']}" for h in inferred["reference_hints"]]
    return "\n".join(lines) + "\n"
