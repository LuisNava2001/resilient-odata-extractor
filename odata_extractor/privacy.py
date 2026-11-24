"""Privacy rules for publishable output.

Operations per field: drop, mask_email (keep the first character and the domain), pseudonymize (keyed HMAC-SHA256,
stable for one key, unlinkable without it), redact (fixed placeholder) and redact_text (replace emails and phone
numbers inside free text). After the rules, a safety net replaces any email address or international (+...) phone
number still present in any string field. Pseudonymization keys are never written to any output. Phone detection
is pattern-based and misses local formats without a country prefix; that is a documented limit, not a guarantee.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# International format only (leading +): a looser pattern would also match ISO dates such as 2026-01-05.
PHONE = re.compile(r"\+\d{1,3}(?:[\s.-]?\(?\d{2,4}\)?){2,4}")
KEY_ENV = "ODATA_EXTRACTOR_PSEUDONYM_KEY"
DEMO_KEY = "public-demo-key-not-a-secret"
OPERATIONS = ("drop", "mask_email", "pseudonymize", "redact", "redact_text")


def load_rules(path: Path | None = None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8") if path else resources.files(__package__).joinpath("data", "privacy_rules.json").read_text(encoding="utf-8")
    rules = json.loads(text)
    for entity, ops in rules.get("entities", {}).items():
        unknown = set(ops) - set(OPERATIONS)
        if unknown:
            raise ValueError(f"privacy rules for {entity} use unknown operations {sorted(unknown)}")
    return rules


@dataclass
class Pseudonymizer:
    key: bytes
    source: str  # where the key came from; the key itself is never reported

    @classmethod
    def from_environment(cls, demo: bool = False) -> Pseudonymizer:
        if demo:
            return cls(DEMO_KEY.encode(), "public demo key (reproducible, not secret)")
        value = os.environ.get(KEY_ENV)
        if value:
            return cls(value.encode(), f"environment variable {KEY_ENV}")
        return cls(secrets.token_bytes(32), "random per run (pseudonyms are not stable across runs)")

    def __call__(self, scope: str, value: Any) -> str:
        mac = hmac.new(self.key, f"{scope}\x00{value}".encode(), hashlib.sha256).hexdigest()
        return f"psn_{mac[:16]}"


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def scrub_text(value: str) -> str:
    return PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", value))


def apply(record: dict[str, Any], entity: str, rules: dict[str, Any], pseudonymizer: Pseudonymizer) -> dict[str, Any]:
    ops = rules.get("entities", {}).get(entity, {})
    out = {k: v for k, v in record.items() if k not in set(ops.get("drop", [])) and not k.startswith("@odata.")}
    for name in ops.get("mask_email", []):
        if isinstance(out.get(name), str):
            out[name] = mask_email(out[name])
    for name in ops.get("pseudonymize", []):
        if out.get(name) is not None:
            out[name] = pseudonymizer(f"{entity}.{name}", out[name])
    for name in ops.get("redact", []):
        if name in out and out[name] is not None:
            out[name] = "[REDACTED]"
    for name in ops.get("redact_text", []):
        if isinstance(out.get(name), str):
            out[name] = scrub_text(out[name])
    protected = set(ops.get("mask_email", []))
    for name, value in out.items():  # safety net for anything the rules missed
        if isinstance(value, str) and name not in protected:
            out[name] = scrub_text(value)
    return out
