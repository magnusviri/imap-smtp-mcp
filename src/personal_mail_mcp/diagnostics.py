"""Structured diagnostics that never serialize private mail or local paths."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

REDACTED = "<redacted>"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "body",
    "content",
    "credential",
    "password",
    "path",
    "recipient",
    "secret",
    "token",
    "attachment",
)
_ADDRESS_KEYS = {"address", "bcc", "cc", "from", "reply_to", "sender", "to"}
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ABSOLUTE_PATH_RE = re.compile(r"(?<!\w)/(?:[^\s/]+/)*[^\s/]+")


def redact_diagnostic(value: Any, *, key: str | None = None) -> Any:
    """Return JSON-safe diagnostic data with private fields removed."""
    normalized_key = key.casefold() if key is not None else ""
    if normalized_key in _ADDRESS_KEYS or any(
        part in normalized_key for part in _SENSITIVE_KEY_PARTS
    ):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_diagnostic(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_diagnostic(item) for item in value]
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, str):
        safe = _BEARER_RE.sub("Bearer <redacted>", value)
        safe = _EMAIL_RE.sub("<redacted-email>", safe)
        return _ABSOLUTE_PATH_RE.sub("<redacted-path>", safe)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return value.__class__.__name__


class StructuredDiagnosticFormatter(logging.Formatter):
    """Serialize explicitly supplied diagnostic fields as redacted JSON."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "diagnostic_fields", {})
        payload = {
            "level": record.levelname.lower(),
            "event": redact_diagnostic(record.getMessage()),
            "fields": redact_diagnostic(fields),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_diagnostics() -> None:
    """Configure process diagnostics on stderr without protocol output."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredDiagnosticFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
