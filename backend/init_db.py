"""Create tables and seed the synthetic fixture.

Phase 1 expands Phase 0's minimal fixture into the full walking-skeleton seed:
**two clinics, each with two patients and one user per role**. Two clinics are
not optional — cross-clinic isolation cannot be proved against one.

Clinic B is a full mirror of Clinic A, not a stub. Phase 0 seeded clinic B with
only a clinician, which was enough to prove a clinic-A caller cannot read
clinic B, but not enough to prove the converse or to test a clinic-B patient
login. Symmetry costs six rows and removes a whole class of "it only works one
way round" doubt.

Entries are seeded too, spanning every role's authorship and both patient-facing
and internal types, so that logging in as each role produces a *visibly
different* view of the same patient. A seed where every role sees the same three
rows proves nothing about scoping.

    python init_db.py            # create tables, seed if empty
    python init_db.py --reset    # drop everything first

ALL DATA HERE IS SYNTHETIC. Names, MRNs, dates and clinical details are
invented. Nothing in this file has ever corresponded to a real person.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from app.core.db import Base, SessionLocal, engine
from app.core.enums import EntryType, InteractionType, RiskLevel, Role
from app.core.provenance import entry_pointer, session_pointer
from app.models import AIScribedNote, Clinic, Entry, Patient, User, Version
from app.security.auth import hash_password

# Seed password for every demo account. Dev fixture only — see README.
DEMO_PASSWORD = "carenote-demo"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_entry(
    db,
    *,
    entry_id: str,
    patient_id: str,
    clinic_id: str,
    author_id: str,
    author_role: Role,
    entry_type: EntryType,
    title: str,
    content: str,
    days_ago: int,
    risk: RiskLevel = RiskLevel.NONE,
    provenance: str | None = None,
) -> Entry:
    """Seed one entry plus its version-1 snapshot.

    Every entry gets a v1 immediately so no row can exist with a
    `version_number` that has no corresponding `Version`. Phase 2's revision
    history then only ever appends.
    """
    timestamp = _now() - timedelta(days=days_ago)
    entry = Entry(
        id=entry_id,
        patient_id=patient_id,
        clinic_id=clinic_id,
        author_role=author_role,
        author_id=author_id,
        timestamp=timestamp,
        type=entry_type,
        title=title,
        content=content,
        risk_level=risk,
        version_number=1,
        # A manually authored entry is its own provenance: it was written here,
        # not derived from a transcript or an AI session. An AI-scribed entry
        # passes an explicit pointer back to the session it came from.
        provenance_pointer=provenance or entry_pointer(entry_id),
    )
    db.add(entry)
    db.flush()

    version = Version(
        entry_id=entry.id,
        version_number=1,
        content_snapshot=content,
        title_snapshot=title,
        risk_level_snapshot=str(risk),
        edited_by=author_id,
        edited_by_role=author_role,
        edited_at=timestamp,
        change_summary="seeded",
    )
    db.add(version)
    db.flush()
    entry.current_version_id = version.id
    return entry


def seed(reset: bool = False) -> None:
    if reset:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        if db.query(Clinic).count() > 0:
            print("Database already seeded; nothing to do. Use --reset to rebuild.")
            return

        # -- clinics ----------------------------------------------------
        clinic_a = Clinic(id="clinic-a", name="Bukit Timah Family Clinic")
        clinic_b = Clinic(id="clinic-b", name="Tampines Community Clinic")
        db.add_all([clinic_a, clinic_b])

        # -- patients: two per clinic -----------------------------------
        patients = [
            Patient(id="patient-a1", clinic_id="clinic-a", name="Amira Rahman",
                    dob="1968-03-11", mrn="MRN-A-40192"),
            Patient(id="patient-a2", clinic_id="clinic-a", name="Marcus Teo",
                    dob="1991-07-24", mrn="MRN-A-40233"),
            Patient(id="patient-b1", clinic_id="clinic-b", name="Daniel Choo",
                    dob="1975-11-02", mrn="MRN-B-88301"),
            Patient(id="patient-b2", clinic_id="clinic-b", name="Halimah Yusof",
                    dob="1959-01-30", mrn="MRN-B-88344"),
        ]
        db.add_all(patients)
        db.flush()

        # -- users: one per role, in BOTH clinics ------------------------
        password = hash_password(DEMO_PASSWORD)
        db.add_all(
            [
                # Clinic A
                User(id="u-a-clinician", clinic_id="clinic-a", role=Role.CLINICIAN,
                     name="Dr Lim Wei Sheng", username="clinician_a", password_hash=password),
                User(id="u-a-staff", clinic_id="clinic-a", role=Role.STAFF,
                     name="Nurse Priya Nair", username="staff_a", password_hash=password),
                User(id="u-a-admin", clinic_id="clinic-a", role=Role.ADMIN,
                     name="Serene Koh", username="admin_a", password_hash=password),
                User(id="u-a-patient", clinic_id="clinic-a", role=Role.PATIENT,
                     name="Amira Rahman", username="patient_a", password_hash=password,
                     patient_id="patient-a1"),
                # Clinic B — a full mirror, not a stub
                User(id="u-b-clinician", clinic_id="clinic-b", role=Role.CLINICIAN,
                     name="Dr Faizal Aziz", username="clinician_b", password_hash=password),
                User(id="u-b-staff", clinic_id="clinic-b", role=Role.STAFF,
                     name="Nurse Grace Tan", username="staff_b", password_hash=password),
                User(id="u-b-admin", clinic_id="clinic-b", role=Role.ADMIN,
                     name="Ravi Kumar", username="admin_b", password_hash=password),
                User(id="u-b-patient", clinic_id="clinic-b", role=Role.PATIENT,
                     name="Daniel Choo", username="patient_b", password_hash=password,
                     patient_id="patient-b1"),
            ]
        )
        db.flush()

        # -- entries on patient-a1: one per author role -----------------
        # Chosen so that each role's timeline of the SAME patient differs.
        _add_entry(
            db, entry_id="entry-a1-clin", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-clinician", author_role=Role.CLINICIAN,
            entry_type=EntryType.CLINICIAN_SECTION,
            title="Assessment & plan",
            content=(
                "T2DM with suboptimal control. HbA1c 8.4%. Considering GLP-1 agonist "
                "if metformin titration insufficient at 3/12. Query early "
                "microalbuminuria - repeat ACR. Keep BP <130/80."
            ),
            risk=RiskLevel.MEDIUM, days_ago=3,
        )
        _add_entry(
            db, entry_id="entry-a1-staff", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-staff", author_role=Role.STAFF,
            entry_type=EntryType.STAFF_NOTE,
            title="Vitals and intake",
            content=(
                "BP 138/86 seated, repeat 134/84. Weight 74.2kg, down 1.1kg. "
                "Reports missing evening metformin roughly twice a week. "
                "Foot check done, no ulceration."
            ),
            days_ago=3,
        )
        _add_entry(
            db, entry_id="entry-a1-instr", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-clinician", author_role=Role.CLINICIAN,
            entry_type=EntryType.PATIENT_INSTRUCTION,
            title="What to do before your next visit",
            content=(
                "Take metformin with your evening meal - setting a phone reminder "
                "helps. Bring your home BP readings. Fasting blood test one week "
                "before your appointment."
            ),
            days_ago=3,
        )
        _add_entry(
            db, entry_id="entry-a1-pt", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-patient", author_role=Role.PATIENT,
            entry_type=EntryType.PATIENT_NOTE,
            title="How things have been",
            content=(
                "Evening dose is the hard one, I am usually still at work. "
                "Feet have been tingling at night for about two weeks."
            ),
            days_ago=1,
        )

        # -- one AI-scribed entry ---------------------------------------
        # Seeded directly rather than generated, because the scribe pipeline is
        # Phase 2.2 and this is a Phase 1 fixture. It exists so the walking
        # skeleton can demonstrate the requirement that AI-scribed notes be
        # visually and structurally distinct from manual ones - without it the
        # frontend's AI-SCRIBED treatment never renders and the distinction is
        # asserted in tests but never seen.
        #
        # The content below is written as ALREADY-REDACTED output: no names, no
        # identifiers, no phone numbers. A seed row bypasses redact_phi() by
        # construction, so seeding text that would have failed redaction would
        # plant a misleading example for every later phase to copy.
        ai_session_id = "sess-a1-consult-0001"
        ai_entry = _add_entry(
            db, entry_id="entry-a1-ai", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="system", author_role=Role.SYSTEM,
            entry_type=EntryType.AI_DOCTOR_CONSULT_SUMMARY,
            title="Consult summary (AI-scribed)",
            content=(
                "Glycaemic control reviewed. Patient reports inconsistent evening "
                "dosing due to work schedule. New paraesthesia in both feet over "
                "roughly two weeks - neuropathy screen discussed. Agreed to repeat "
                "ACR and review titration in three months."
            ),
            days_ago=3,
            # Provenance points at the originating session, not at itself.
            provenance=session_pointer(ai_session_id),
        )
        db.add(
            AIScribedNote(
                entry_id=ai_entry.id,
                clinic_id="clinic-a",
                session_id=ai_session_id,
                interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
                model_used="stub-offline-v0",
                redaction_applied=True,
                redaction_count=2,
                confidence=0.82,
            )
        )

        # -- one entry on patient-a2 and one in clinic B ----------------
        # patient-a2 exists so "list patients in my clinic" returns more than
        # one row; clinic B has content so cross-clinic reads have something
        # real to be refused access to.
        _add_entry(
            db, entry_id="entry-a2-staff", patient_id="patient-a2", clinic_id="clinic-a",
            author_id="u-a-staff", author_role=Role.STAFF,
            entry_type=EntryType.STAFF_NOTE,
            title="Triage note",
            content="Presented with ankle sprain after football. Neurovascularly intact.",
            days_ago=6,
        )
        _add_entry(
            db, entry_id="entry-b1-clin", patient_id="patient-b1", clinic_id="clinic-b",
            author_id="u-b-clinician", author_role=Role.CLINICIAN,
            entry_type=EntryType.CLINICIAN_SECTION,
            title="Assessment & plan",
            content=(
                "Warfarin-managed AF. INR 3.4, above range - hold one dose, "
                "recheck in 3 days. Counselled on bleeding risk."
            ),
            risk=RiskLevel.HIGH, days_ago=2,
        )

        db.commit()
        print("Seeded 2 clinics, 4 patients, 8 users (one per role per clinic), "
              "7 entries (1 AI-scribed).")
        print(f"Login with any username above / password: {DEMO_PASSWORD}")
        print("Usernames: clinician_a staff_a admin_a patient_a "
              "clinician_b staff_b admin_b patient_b")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise the Care Note database.")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()
    seed(reset=args.reset)
    sys.exit(0)
