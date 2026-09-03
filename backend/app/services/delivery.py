"""Whether patient-facing content ever actually reached the patient.

Scenarios 11 and 12. This build has no sender — no email, no SMS, no WhatsApp,
no push — and adding one is not what this module is for. The failure it fixes is
narrower and worse:

    A clinician writes "come back in two weeks for a BP check", marks it done,
    and moves on. The patient never opens the portal. The instruction is
    correct, versioned, traceable and unread, and the system reports success.

A build that cannot send is a limitation. A build that cannot tell you it did not
send is a false assurance, and that is what was shipped.

So this models the state and derives what it can from data already being
recorded. `PatientView` has held the patient's own read timestamps since D-033;
nothing had ever asked it this question.

Two states are honest today and one is deliberately absent:

* ``unread``    — written, and the patient has not opened the record since.
* ``read``      — the patient opened the record after this version was written.
* ``corrected`` — the patient read an *earlier* version, and it has since
  changed. This is the scenario-12 case: she took the wrong dose on Tuesday, the
  clinician fixed it on Wednesday, and nothing anywhere knows she is still acting
  on the old number.
* ``dispatched`` is **not** modelled, because nothing dispatches. Inventing the
  state would be the same false assurance in a new place.

**Scope: content the clinic wrote FOR the patient.** That is
`policy.PATIENT_FACING_TYPES` — `patient_summary` and `patient_instruction` —
and specifically *not* `patient_note`, which the patient writes herself. This
module asks "did what we sent her land?", and her own words are not something
that can fail to reach her. Importing the wrong set made the clinician's card
report a note she had just typed as "not yet opened by the patient", and made
her own view lead with "this was updated after you last read it" when she
edited it. See DECISIONS.md D-100.

See DECISIONS.md D-074.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.enums import Role
from app.security.policy import PATIENT_FACING_TYPES
from app.models import Entry, Patient, PatientView, User, Version

UNREAD = "unread"
READ = "read"
CORRECTED = "corrected"


@dataclass(frozen=True)
class DeliveryStatus:
    entry_id: str
    title: str
    type: str
    state: str
    written_at: datetime
    version_number: int
    patient_last_read_at: datetime | None

    @property
    def needs_attention(self) -> bool:
        """A correction the patient has not seen outranks merely unread.

        Unread is normal — she may simply not have logged in yet. Corrected is
        not normal: it means she is acting on information the clinic already
        knows to be wrong.
        """
        return self.state in {CORRECTED, UNREAD}


def _patient_user(db: Session, patient: Patient) -> User | None:
    return (
        db.query(User)
        .filter(
            User.clinic_id == patient.clinic_id,
            User.role == Role.PATIENT,
            User.patient_id == patient.id,
        )
        .first()
    )


def _last_read_at(db: Session, patient: Patient) -> datetime | None:
    """When the patient themself last opened their own record.

    None means one of two very different things — she has never opened it, or
    she has no login at all — and the caller is told which, because "she has not
    read it" and "there is no way for her to read it" call for different actions
    from the clinic.
    """
    user = _patient_user(db, patient)
    if user is None:
        return None
    view = (
        db.query(PatientView)
        .filter(PatientView.user_id == user.id, PatientView.patient_id == patient.id)
        .one_or_none()
    )
    return view.last_viewed_at if view else None


def _first_version_at(db: Session, entry: Entry) -> datetime:
    """When the patient could first have read *any* version of this entry."""
    first = (
        db.query(Version)
        .filter(Version.entry_id == entry.id)
        .order_by(Version.version_number.asc())
        .first()
    )
    return first.edited_at if first else entry.timestamp


def _current_version_at(db: Session, entry: Entry) -> datetime:
    latest = (
        db.query(Version)
        .filter(Version.entry_id == entry.id)
        .order_by(Version.version_number.desc())
        .first()
    )
    return latest.edited_at if latest else entry.timestamp


def statuses(db: Session, patient: Patient) -> list[DeliveryStatus]:
    """Delivery state for every patient-facing entry on this patient's record."""
    entries = (
        db.query(Entry)
        .filter(
            Entry.patient_id == patient.id,
            Entry.clinic_id == patient.clinic_id,
            Entry.type.in_([str(t) for t in PATIENT_FACING_TYPES]),
        )
        .order_by(Entry.timestamp.desc())
        .all()
    )
    read_at = _last_read_at(db, patient)

    out: list[DeliveryStatus] = []
    for entry in entries:
        current_at = _current_version_at(db, entry)
        if read_at is None or read_at < _first_version_at(db, entry):
            state = UNREAD
        elif read_at < current_at:
            # She read an earlier version and this one has changed since.
            state = CORRECTED
        else:
            state = READ
        out.append(
            DeliveryStatus(
                entry_id=entry.id,
                title=entry.title or "",
                type=str(entry.type),
                state=state,
                written_at=current_at,
                version_number=entry.version_number,
                patient_last_read_at=read_at,
            )
        )
    return out


def clinician_summary(db: Session, patient: Patient) -> dict:
    """What the clinician needs to know about content aimed at this patient."""
    all_statuses = statuses(db, patient)
    corrected = [s for s in all_statuses if s.state == CORRECTED]
    unread = [s for s in all_statuses if s.state == UNREAD]
    has_login = _patient_user(db, patient) is not None

    return {
        "patient_has_login": has_login,
        # The distinction that matters: "she has not read it" and "there is no
        # way for her to read it" are different problems for the clinic.
        "reachable": has_login,
        "unread_count": len(unread),
        "corrected_unread_count": len(corrected),
        "items": [
            {
                "entry_id": s.entry_id,
                "title": s.title,
                "type": s.type,
                "state": s.state,
                "version": s.version_number,
                "label": _label(s, has_login),
            }
            for s in all_statuses
            if s.needs_attention
        ],
    }


def _label(status: DeliveryStatus, has_login: bool) -> str:
    if not has_login:
        return "No patient login exists — this cannot be read by the patient"
    if status.state == CORRECTED:
        return "Corrected since the patient last read it — they may be acting on the old version"
    return "Not yet opened by the patient"


def corrections_for_patient(db: Session, patient: Patient) -> list[dict]:
    """Corrections the patient has not seen, phrased for the patient.

    The clinician-side view answers "did this land?". This answers the question
    the patient does not know to ask: the text in front of her is not the text
    she read last time, and the difference might be a dose.
    """
    return [
        {
            "entry_id": s.entry_id,
            "title": s.title,
            "message": (
                "This was updated after you last read it. Please check it again — "
                "if you were following the earlier version, stop and read this one."
            ),
        }
        for s in statuses(db, patient)
        if s.state == CORRECTED
    ]
