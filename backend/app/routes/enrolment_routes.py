"""Enrolment — creating a patient, and giving them a way in.

Scenario 1. The identity model was never the problem: there is no email column
anywhere in this schema, and login is username plus password, so a phone number
works as a username today with no change. The problem was that nothing could
create the row.

Every account in the build existed because a developer ran `init_db.py`. A nurse
holding a patient's WhatsApp number had no screen anywhere that turned it into a
record. The patient was not rejected — she was unreachable, which in a clinic is
the same outcome.

Scenario 5 is the same absence one level up: onboarding a second clinic meant
editing Python. Clinic creation stays out of this module deliberately (see
`admin_only` below) but patient and login creation is routine clinic work and
belongs in the product.

Three deliberate choices:

* **Identifier type is explicit.** `phone`, `nric`, `mrn` or `internal`. A
  username that happens to contain digits is not the same as knowing the clinic
  identifies this person by their phone number, and the difference matters the
  first time someone tries to reach her.
* **A login is optional.** Plenty of patients will never use a portal, and
  forcing a credential nobody wants produces dormant accounts. `reachable` on
  the Glance View (D-074) reports the consequence honestly instead.
* **The passcode is returned once and never stored.** It is handed to the staff
  member to pass on in person, and only its hash is persisted.

See DECISIONS.md D-075.
"""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.audit_logging import log_event
from app.core.enums import Role
from app.models import Patient, User
from app.security.auth import hash_password
from app.security.rbac import AccessScope, require_access

router = APIRouter(tags=["enrolment"])

# Roles that may enrol a patient. Clinicians and admins too, but a nurse at the
# front desk is the person actually holding the phone number, so staff is the
# important one on this list.
ENROLLING_ROLES = (Role.STAFF, Role.CLINICIAN, Role.ADMIN)

IDENTIFIER_TYPES = ("phone", "nric", "mrn", "internal")

# Deliberately permissive: +65, 01x-, spaces, dashes. Validating a phone number
# strictly is how you exclude the person this whole route exists for.
_PHONE = re.compile(r"^\+?[\d][\d\s\-]{5,19}$")


class EnrolPatientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dob: str | None = Field(default=None, max_length=10)
    identifier_type: str = Field(default="internal")
    identifier: str | None = Field(default=None, max_length=40)
    create_login: bool = False


class EnrolPatientOut(BaseModel):
    patient_id: str
    name: str
    identifier_type: str
    username: str | None
    # Present exactly once, in this response. Never stored, never logged, never
    # retrievable again — if it is lost, issue a new one.
    one_time_passcode: str | None
    reachable: bool


def _username_for(identifier_type: str, identifier: str | None, patient_id: str) -> str:
    """A phone number is a perfectly good username. So is a clinic MRN."""
    if identifier:
        return identifier.replace(" ", "").replace("-", "")
    return f"patient-{patient_id[:8]}"


@router.post(
    "/patients",
    response_model=EnrolPatientOut,
    status_code=status.HTTP_201_CREATED,
)
def enrol_patient(
    payload: EnrolPatientIn,
    scope: AccessScope = Depends(require_access(*ENROLLING_ROLES)),
) -> EnrolPatientOut:
    """Register a patient in the caller's clinic, optionally with a login.

    `clinic_id` is taken from the caller's scope and is not accepted from the
    body — the same rule as every read path. A staff member cannot enrol a
    patient into someone else's clinic even by asking.
    """
    if payload.identifier_type not in IDENTIFIER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"identifier_type must be one of {', '.join(IDENTIFIER_TYPES)}",
        )
    if payload.identifier_type == "phone" and payload.identifier:
        if not _PHONE.match(payload.identifier):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That does not look like a phone number.",
            )

    # A provisional MRN when the clinic has not assigned one. Refusing to
    # register a patient until another system has issued a number is how a
    # walk-in with a phone number stops being a patient.
    mrn = payload.identifier if payload.identifier_type == "mrn" else None
    if not mrn:
        mrn = f"PROV-{secrets.token_hex(4).upper()}"

    patient = Patient(
        clinic_id=scope.clinic_id,
        name=payload.name,
        dob=payload.dob,
        mrn=mrn,
    )
    scope.db.add(patient)
    scope.db.flush()

    username: str | None = None
    passcode: str | None = None
    if payload.create_login:
        username = _username_for(payload.identifier_type, payload.identifier, patient.id)
        clash = scope.db.query(User).filter(User.username == username).first()
        if clash is not None:
            scope.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That identifier is already registered.",
            )
        # Six digits: this gets read aloud or written on an appointment card.
        # A long random string would be copied down wrongly, which is its own
        # kind of access failure.
        passcode = f"{secrets.randbelow(1_000_000):06d}"
        scope.db.add(
            User(
                clinic_id=scope.clinic_id,
                role=Role.PATIENT,
                name=payload.name,
                username=username,
                password_hash=hash_password(passcode),
                patient_id=patient.id,
            )
        )

    scope.db.commit()

    # Metadata only. Not the name, not the phone number, not the passcode.
    log_event(
        actor_id=scope.user_id,
        action="patient.enrolled",
        target_type="patient",
        target_id=patient.id,
        clinic_id=scope.clinic_id,
        metadata={
            "identifier_type": payload.identifier_type,
            "login_created": payload.create_login,
            "actor_role": str(scope.role),
        },
    )

    return EnrolPatientOut(
        patient_id=patient.id,
        name=patient.name,
        identifier_type=payload.identifier_type,
        username=username,
        one_time_passcode=passcode,
        reachable=payload.create_login,
    )


class IssueLoginOut(BaseModel):
    username: str
    one_time_passcode: str


class IssueLoginIn(BaseModel):
    """The identifier travels in the body, deliberately.

    It was a query parameter until D-083. For scenario 1 the identifier *is* a
    phone number — that is the entire point of the feature — so every normal
    use of it wrote a patient's phone number into the request URL, where the
    ASGI access log records the full request line and keeps it for as long as
    the container logs live. Redaction before the model was guarded; this was
    a second door onto the same data, opened by the feature built to answer
    scenario 1.

    A request body is not logged by the access log. That is not a general
    guarantee about bodies, it is a statement about this specific sink, and it
    is the reason the parameter moved.
    """

    identifier: str | None = None


@router.post("/patients/{patient_id}/login", response_model=IssueLoginOut)
def issue_login(
    patient_id: str,
    payload: IssueLoginIn | None = None,
    scope: AccessScope = Depends(require_access(*ENROLLING_ROLES)),
) -> IssueLoginOut:
    """Give an existing patient a way in, or reset one they have lost.

    Routed through `scope.get_or_404`, so a staff member in Clinic A cannot
    issue credentials for a patient in Clinic B — the enrolment path is scoped
    by exactly the same rule as every read.
    """
    identifier = payload.identifier if payload is not None else None
    patient = scope.get_or_404(Patient, patient_id)

    existing = (
        scope.db.query(User)
        .filter(User.patient_id == patient.id, User.role == Role.PATIENT)
        .first()
    )
    passcode = f"{secrets.randbelow(1_000_000):06d}"

    if existing is not None:
        existing.password_hash = hash_password(passcode)
        username = existing.username
        action = "patient.login_reset"
    else:
        username = _username_for(
            "phone" if identifier else "internal", identifier, patient.id
        )
        if scope.db.query(User).filter(User.username == username).first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That identifier is already registered.",
            )
        scope.db.add(
            User(
                clinic_id=scope.clinic_id,
                role=Role.PATIENT,
                name=patient.name,
                username=username,
                password_hash=hash_password(passcode),
                patient_id=patient.id,
            )
        )
        action = "patient.login_issued"

    scope.db.commit()
    log_event(
        actor_id=scope.user_id,
        action=action,
        target_type="patient",
        target_id=patient.id,
        clinic_id=scope.clinic_id,
        metadata={"actor_role": str(scope.role)},
    )
    return IssueLoginOut(username=username, one_time_passcode=passcode)
