"""Logging hygiene chokepoint.

Shared-context rule: never log Entry content, Comment bodies, or transcript /
session text. Logs carry IDs, action types, and timestamps only.

We enforce this structurally rather than by convention: `log_event` accepts a
fixed set of scalar fields and *drops* anything that looks like free text. There
is deliberately no `message` or `content` parameter to reach for.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("carenote.audit")

# Any metadata value longer than this is assumed to be free text (a note body, a
# transcript chunk) and is replaced with a length marker instead of the value.
MAX_METADATA_VALUE_LEN = 64

_BANNED_METADATA_KEYS = {
    "content",
    "body",
    "text",
    "transcript",
    "snapshot",
    "note",
    "summary",
    "prompt",
    "completion",
}


def _scrub(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in _BANNED_METADATA_KEYS:
            clean[key] = "<omitted:banned_key>"
            continue
        if isinstance(value, str) and len(value) > MAX_METADATA_VALUE_LEN:
            clean[key] = f"<omitted:len={len(value)}>"
            continue
        clean[key] = value
    return clean


def log_event(
    *,
    actor_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    clinic_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit one structured, content-free audit line. Returns the emitted record."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor_id": actor_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "clinic_id": clinic_id,
        "metadata": _scrub(metadata),
    }
    logger.info("%s", record)
    return record
