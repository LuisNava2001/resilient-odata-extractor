"""End-to-end extraction: discover the key from $metadata, extract, assess integrity, write outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import ClientConfig, Extraction, ExtractionError, ODataClient
from .export import write_outputs
from .integrity import IntegrityReport, assess
from .privacy import Pseudonymizer, load_rules
from .schema import MetadataError, infer_from_samples, parse_metadata


@dataclass
class Outcome:
    run: Extraction
    integrity: IntegrityReport
    schema: dict[str, Any] | None
    output: Path | None


NO_METADATA_STATUSES = {404, 405, 501}


def fetch_metadata(client: ODataClient) -> tuple[dict[str, dict[str, Any]] | None, ExtractionError | None]:
    """(declared entity sets or None, technical error or None).

    A service without $metadata (404/405/501, or a document that is not CSDL) is not an error: the caller falls back
    to an explicit key and sample-based inference. Anything else (401, 403, timeouts, connection failures) is.
    """
    try:
        _, text = client.request(client.config.service_root + "$metadata", None, "metadata", expect_json=False)
        return parse_metadata(text), None
    except MetadataError:
        return None, None
    except ExtractionError as err:
        return (None, None) if err.status in NO_METADATA_STATUSES else (None, err)


def resolve_key(metadata: dict[str, dict[str, Any]] | None, entity: str, key: str | None) -> str:
    if key:
        return key
    declared = (metadata or {}).get(entity, {}).get("key") or []
    if len(declared) != 1:
        raise ValueError(f"cannot determine a single key for {entity} from $metadata; pass --key")
    return declared[0]


INTEGER_TYPES = {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}
STRING_TYPES = {"Edm.String", "Edm.Guid"}


def declared_key_type(metadata: dict[str, dict[str, Any]] | None, entity: str, key: str) -> str | None:
    for prop in (metadata or {}).get(entity, {}).get("properties", []):
        if prop["name"] == key:
            return "integer" if prop["type"] in INTEGER_TYPES else "string" if prop["type"] in STRING_TYPES else None
    return None


def run(config: ClientConfig, entity: str, output: Path | None = None, *, key: str | None = None, client: ODataClient | None = None,
        rules_path: Path | None = None, pseudonymizer: Pseudonymizer | None = None) -> Outcome:
    client = client or ODataClient(config)
    metadata, error = fetch_metadata(client)
    if error is not None:
        extraction = Extraction(entity=entity, key=key or "(unknown)", ordered_by_key=True, stop_reason="error", error=error)
    else:
        if metadata is not None and entity not in metadata:
            raise ValueError(f"{entity} is not an entity set declared by $metadata ({', '.join(sorted(metadata))})")
        key = resolve_key(metadata, entity, key)
        extraction = client.extract(entity, key, key_type=declared_key_type(metadata, entity, key))
    integrity = assess(extraction)
    if metadata is not None:
        schema = {**metadata[entity], "note": "declared by $metadata; not verified against the data"}
    elif integrity.unique_records:
        schema = infer_from_samples(integrity.unique_records[:100])
    else:
        schema = None
    written = None
    if output is not None:
        written = write_outputs(output, extraction, integrity, load_rules(rules_path),
                                pseudonymizer or Pseudonymizer.from_environment(), schema)
    return Outcome(extraction, integrity, schema, written)
