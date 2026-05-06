"""PII redaction for anything that leaves the process towards LangSmith."""
from __future__ import annotations

import re
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][\w.]*\s+){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct)\b\.?",
    re.IGNORECASE)

PATTERNS = (("EMAIL", EMAIL), ("PHONE", PHONE), ("ADDRESS", ADDRESS))
PHONE_DETECTOR = PHONE.pattern          # for PIIMiddleware(detector=...)


def redact_text(s: str) -> str:
    for label, pat in PATTERNS:
        s = pat.sub(f"[{label}]", s)
    return s


def redact(obj: Any) -> Any:
    """Recursively redact strings in JSON-like structures. Used as LangSmith hide_inputs/outputs."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact(v) for v in obj)
    return obj
