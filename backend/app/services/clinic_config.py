"""Per-clinic configuration, and the line where configuration stops.

Scenario 5. "Clinic B onboards next Monday. What breaks? Be specific about what
is a config change versus a schema change."

The honest answer before this module existed was: **nothing in the schema, and
everything in the code.** Every table already carries `clinic_id`, cross-clinic
isolation is enforced and tested, and the seed creates two clinics — so a third
needs no migration. But every value a clinic might reasonably want to differ on
was a module constant shared by all tenants, so "Clinic B keeps records for a
year" was a code change, a review and a deploy.

This module makes the tunable set tunable. It is deliberately small: the point
is not to make everything configurable, it is to draw the line in one place and
say why it is where it is.

What is configurable, and why
-----------------------------
Display volume and retention. A clinic with long-horizon chronic patients wants
a different decay window from a walk-in practice, and a clinic seeing forty
patients a day wants a shorter card than one seeing twelve. Neither preference
can make the system less safe — the worst outcome of a bad value is a cluttered
card or a large database.

What is NOT configurable, deliberately
--------------------------------------
**Safety floors are not settings.** These stay module constants, and this
module refuses to read them from the database:

* `ai.redaction` patterns — a clinic that could weaken PHI redaction is a
  clinic that will, under deadline pressure, and the resulting leak would carry
  our name. Redaction is the same everywhere or it is not a guarantee.
* `learning.NEVER_DAMPENED` / protected highlight classes (D-084) — the whole
  value of a floor is that no local decision can lower it. A per-clinic
  "protected classes" list is a per-clinic off switch for allergy protection.
* Contradiction severities (`contradictions.ALLERGY_SEVERITY` and siblings) —
  an allergy conflict is critical as a clinical fact, not as a preference.
* The dosage reference table — 500mg is 500mg in every clinic.

The rule underneath: **a clinic may change what it sees, never what it is
protected from.** Anything that could be turned down to make an alert stop
firing belongs on the left of that line, not the right.

See DECISIONS.md D-086.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import ClinicConfig

# Defaults. These are the values the build shipped with as module constants, so
# a clinic with no config row behaves exactly as it did before this module
# existed — onboarding is opt-in, not a migration.
DEFAULTS = {
    "max_highlights": 6,
    "max_contradictions": 5,
    "max_whats_new": 8,
    "warm_after_days": 45,
    "cold_after_days": 180,
}

# Bounds. A configuration surface with no bounds is a new way to break the
# product from the database. `max_highlights: 0` would empty the Glance View;
# `cold_after_days: 1` would archive an active chart overnight.
BOUNDS: dict[str, tuple[int, int]] = {
    "max_highlights": (3, 20),
    "max_contradictions": (3, 20),
    "max_whats_new": (3, 30),
    "warm_after_days": (7, 3650),
    "cold_after_days": (30, 3650),
}


@dataclass(frozen=True)
class ResolvedConfig:
    """A clinic's settings, with defaults applied and bounds enforced."""

    clinic_id: str
    max_highlights: int
    max_contradictions: int
    max_whats_new: int
    warm_after_days: int
    cold_after_days: int
    is_default: bool

    def as_dict(self) -> dict:
        return {
            "clinic_id": self.clinic_id,
            "max_highlights": self.max_highlights,
            "max_contradictions": self.max_contradictions,
            "max_whats_new": self.max_whats_new,
            "warm_after_days": self.warm_after_days,
            "cold_after_days": self.cold_after_days,
            "is_default": self.is_default,
        }


def _clamp(key: str, value: int | None) -> int:
    if value is None:
        return DEFAULTS[key]
    low, high = BOUNDS[key]
    return max(low, min(high, int(value)))


def for_clinic(db: Session, clinic_id: str) -> ResolvedConfig:
    """This clinic's effective settings. Never raises, never returns None.

    A missing row is the normal case, not an error: a clinic that has never
    been configured gets the shipped defaults. That is what makes onboarding a
    zero-step operation rather than a checklist item someone forgets.
    """
    row = (
        db.query(ClinicConfig).filter(ClinicConfig.clinic_id == clinic_id).one_or_none()
        if clinic_id
        else None
    )

    values = {key: _clamp(key, getattr(row, key, None) if row else None) for key in DEFAULTS}

    # A cold threshold below the warm one would make the lifecycle
    # non-monotonic. Resolve it here rather than trusting the writer.
    if values["cold_after_days"] <= values["warm_after_days"]:
        values["cold_after_days"] = values["warm_after_days"] + 1

    return ResolvedConfig(clinic_id=clinic_id, is_default=row is None, **values)
