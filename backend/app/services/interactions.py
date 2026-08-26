"""Behavioural signal capture.

Every time a clinician or staff member does something deliberate to a piece of
content — highlights it by hand, edits it, comments on it, accepts or rejects a
suggestion — a row lands in `InteractionLog` carrying the *feature tags* of what
they touched, never the content itself.

Phase 4 turns those rows into learned weights. Phase 2 only has to make sure the
signal is being recorded correctly from the moment the features exist, because
a learning loop that starts collecting data on the day it is switched on has
nothing to learn from.

Two invariants:

* `content_features` holds tags (`["med:warfarin", "type:staff_note"]`) and
  nothing else. Storing the span text here would rebuild, in a behavioural
  analytics table, exactly the note content the logging-hygiene rule keeps out
  of the logs.
* Writing a signal must never fail the user's actual request. Recording that
  someone highlighted a sentence is bookkeeping; refusing their highlight
  because the bookkeeping failed would be the wrong trade.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.enums import InteractionAction, Role
from app.models import InteractionLog


def record_interaction(
    db: Session,
    *,
    user_id: str,
    user_role: Role | str,
    clinic_id: str,
    action: InteractionAction | str,
    target_type: str,
    target_id: str,
    tags: list[str] | None = None,
) -> InteractionLog | None:
    """Append one behavioural signal. Never raises into the caller's path."""
    try:
        row = InteractionLog(
            user_id=user_id,
            user_role=Role(str(user_role)),
            clinic_id=clinic_id,
            action=str(action),
            target_type=target_type,
            target_id=target_id,
            content_features=json.dumps(sorted(set(tags or []))),
        )
        db.add(row)
        db.flush()
        return row
    except Exception:  # noqa: BLE001 — bookkeeping must not break the request
        return None


def decode_features(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
