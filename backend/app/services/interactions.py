"""Behavioural signal capture.

Every time a clinician or staff member does something deliberate to a piece of
content — highlights it by hand, edits it, comments on it, accepts or rejects a
suggestion — a row lands in `InteractionLog` carrying the *feature tags* of what
they touched, never the content itself.

Phase 4 turns those rows into learned weights. Phase 2 only had to make sure the
signal was being recorded correctly from the moment the features existed,
because a learning loop that starts collecting data on the day it is switched on
has nothing to learn from.

Three invariants:

* `content_features` holds tags (`["med:warfarin", "type:staff_note"]`) and
  nothing else. Storing the span text here would rebuild, in a behavioural
  analytics table, exactly the note content the logging-hygiene rule keeps out
  of the logs.
* Writing a signal must never fail the user's actual request. Recording that
  someone highlighted a sentence is bookkeeping; refusing their highlight
  because the bookkeeping failed would be the wrong trade. Phase 4 extends that
  to the learning update: a weight that fails to move is a slightly worse
  ranking, and it must not become a failed clinical action.
* **Recording a signal and learning from it are one operation.** Phase 4 calls
  `learning.apply_signal()` here rather than from each route, so no future
  caller can log an interaction that the learning table never sees. Same
  reasoning as the redaction chokepoint.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.enums import InteractionAction, Role
from app.models import InteractionLog
from app.services import learning


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
    learn: bool = True,
) -> InteractionLog | None:
    """Append one behavioural signal and fold it into the learned weights.

    Never raises into the caller's path. `learn=False` exists for seeding and
    backfill, where thousands of rows are written and one rebuild at the end is
    cheaper than a recompute per row — it does not change what is learned, only
    when.
    """
    normalised = sorted(set(tags or []))
    try:
        row = InteractionLog(
            user_id=user_id,
            user_role=Role(str(user_role)),
            clinic_id=clinic_id,
            action=str(action),
            target_type=target_type,
            target_id=target_id,
            content_features=json.dumps(normalised),
        )
        db.add(row)
        db.flush()
    except Exception:  # noqa: BLE001 — bookkeeping must not break the request
        return None

    if learn:
        try:
            learning.apply_signal(
                db, clinic_id=clinic_id, user_role=user_role, tags=normalised
            )
        except Exception:  # noqa: BLE001 — a stale weight is not a failed request
            pass
    return row


def decode_features(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
