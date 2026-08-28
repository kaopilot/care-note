"""One place that decides what a timestamp means on the wire.

Why this module exists
----------------------
Every datetime in this system is UTC. SQLite's `DATETIME` column has no
timezone, so SQLAlchemy hands rows back as **naive** datetimes, and Pydantic
serialises a naive datetime with no offset at all:

    "2026-08-28T00:52:42.767309"

A browser parsing that string applies ISO 8601's rule for a date-time with no
designator and reads it as **local time**. In Singapore that shifts every
timestamp in the UI by eight hours: a note written seconds ago rendered as
"8h ago", and west of UTC the arithmetic went negative so everything within
the offset read "just now". Worse, it was inconsistent — the Glance View's
`since` marker was built from an already-aware value and so converted
correctly, sitting directly beside entry ages that did not.

The fix is to say what we mean. `as_utc` labels a naive value as UTC (it is
never anything else here), and `UtcDateTime` applies that on the way into every
response model, so the serialised form always carries `+00:00`. Nothing about
storage changes; only the contract at the boundary does.

See DECISIONS.md D-061.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BeforeValidator


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime. Aware values pass through untouched."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """ISO 8601 with an explicit offset, for hand-built dict payloads.

    Used by `services/glance.py`, which returns plain dicts rather than
    response models and so cannot rely on `UtcDateTime`.
    """
    aware = as_utc(value)
    return aware.isoformat() if aware else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Response-model annotation. Swap `datetime` for this on any field read from the
# database and the value is normalised before Pydantic ever serialises it.
UtcDateTime = Annotated[datetime, BeforeValidator(as_utc)]
