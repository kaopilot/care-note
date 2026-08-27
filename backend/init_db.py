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
from app.core.enums import (
    CommentStatus,
    EntryType,
    InteractionAction,
    InteractionType,
    RiskLevel,
    Role,
    TaskStatus,
)
from app.core.provenance import entry_pointer, session_pointer
from app.models import (
    AIScribedNote,
    Clinic,
    Comment,
    Entry,
    FeatureWeight,
    InteractionLog,
    Patient,
    Task,
    User,
    Version,
)
from app.services import decay, learning
from app.services import highlights as highlight_service
from app.services.interactions import record_interaction
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


def _seed_interaction_history(db) -> None:
    """Six months of prior clinical attention in clinic A, as real log rows.

    This is what a clinic that has been using the product for a while looks
    like. The pattern is deliberate rather than random, and it is the story the
    demo tells:

    * **Anticoagulation gets attention.** Repeated hand-highlighting and
      confirmation of warfarin and bleeding-risk content, spread over months.
      This clinic has an anticoagulation clinic and it shows in the ranking.
    * **Routine BP readings get dismissed.** Suggestions about elevated blood
      pressure were rejected several times — not because BP does not matter,
      but because in this clinic it is already handled by a nurse-led pathway
      and does not need to be on a doctor's top card.
    * **Allergy content was dismissed too, twice.** Included on purpose: it is
      the case where the system must refuse to learn. `NEVER_DAMPENED` floors
      that weight at zero, and `GET /clinic/learning` shows the two negative
      signals alongside a weight of 0.0 — the evidence stays visible, the
      behaviour does not follow it.

    Dates are spread so the 90-day evidence half-life does visible work: the
    oldest signals contribute measurably less than the recent ones.
    """
    history: list[tuple[int, str, InteractionAction, list[str]]] = [
        # (days ago, user, action, tags)
        (150, "u-a-clinician", InteractionAction.MANUAL_HIGHLIGHT,
         ["med:warfarin", "medclass:anticoagulant"]),
        (120, "u-a-clinician", InteractionAction.ACCEPT_HIGHLIGHT,
         ["med:warfarin", "medclass:anticoagulant", "symptom:bleeding"]),
        (95, "u-a-staff", InteractionAction.COMMENT,
         ["med:warfarin", "finding:inr_out_of_range"]),
        (60, "u-a-clinician", InteractionAction.MANUAL_HIGHLIGHT,
         ["symptom:bleeding", "medclass:anticoagulant"]),
        (40, "u-a-clinician", InteractionAction.ACCEPT_HIGHLIGHT,
         ["med:warfarin", "finding:inr_out_of_range"]),
        (21, "u-a-clinician", InteractionAction.MANUAL_HIGHLIGHT,
         ["symptom:numbness", "symptom:tingling"]),
        (14, "u-a-staff", InteractionAction.COMMENT,
         ["symptom:tingling", "entity:open_action"]),
        (9, "u-a-clinician", InteractionAction.ACCEPT_HIGHLIGHT,
         ["symptom:tingling", "entity:open_action"]),
        # Dismissals: routine BP is handled elsewhere in this clinic.
        (75, "u-a-clinician", InteractionAction.REJECT_HIGHLIGHT, ["finding:bp_elevated"]),
        (50, "u-a-clinician", InteractionAction.REJECT_HIGHLIGHT, ["finding:bp_elevated"]),
        (30, "u-a-clinician", InteractionAction.REJECT_HIGHLIGHT,
         ["finding:bp_elevated", "med:amlodipine"]),
        # Dismissals the system must decline to learn from.
        (45, "u-a-clinician", InteractionAction.REJECT_HIGHLIGHT, ["entity:allergy"]),
        (20, "u-a-clinician", InteractionAction.REJECT_HIGHLIGHT, ["entity:allergy"]),
    ]

    for days_ago, user_id, action, tags in history:
        row = record_interaction(
            db,
            user_id=user_id,
            user_role=Role.CLINICIAN if user_id.endswith("clinician") else Role.STAFF,
            clinic_id="clinic-a",
            action=action,
            target_type="entry",
            target_id="entry-a1-clin",
            tags=tags,
            # One rebuild after the loop is cheaper than a recompute per row,
            # and produces the identical result — that equality is asserted in
            # test_self_learning_importance.py.
            learn=False,
        )
        if row is not None:
            row.timestamp = _now() - timedelta(days=days_ago)
    db.flush()


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
        # A code-switched patient note, added in Phase 6 alongside the Malay
        # clinical vocabulary (D-058). It exists so the capability is visible in
        # the demo rather than only true in a test: before D-058 this entry
        # produced no feature tags at all and never reached the Glance View,
        # despite describing exactly the oedema the consult is about. The
        # English half of the sentence is what a Singapore/Malaysian patient
        # actually writes — the two languages are interleaved, not separated.
        _add_entry(
            db, entry_id="entry-a1-pt-ms", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-patient", author_role=Role.PATIENT,
            entry_type=EntryType.PATIENT_NOTE,
            title="Kaki saya",
            content=(
                "Kaki bengkak again this week, worse at night. Kebas sikit "
                "waktu pagi. Tiada demam, no pain when I walk."
            ),
            days_ago=2,
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

        # -- longitudinal depth -----------------------------------------
        # Phase 1 seeded only the last week, which cannot demonstrate the one
        # thing this product exists for: context that survives across visits.
        # These two entries sit roughly sixteen and seven months back so the
        # timeline has real distance in it, and so the Glance View's recency
        # decay has something to actually decay against.
        _add_entry(
            db, entry_id="entry-a1-hist-2025", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-clinician", author_role=Role.CLINICIAN,
            entry_type=EntryType.CLINICIAN_SECTION,
            title="Annual review",
            content=(
                "T2DM stable on metformin 1g BD. HbA1c 7.1%. No neuropathy on "
                "monofilament testing. Penicillin allergy confirmed - rash in "
                "childhood, no anaphylaxis. Continue current management, review "
                "in 12 months."
            ),
            risk=RiskLevel.LOW, days_ago=498,
        )
        _add_entry(
            db, entry_id="entry-a1-hist-2026", patient_id="patient-a1", clinic_id="clinic-a",
            author_id="u-a-staff", author_role=Role.STAFF,
            entry_type=EntryType.STAFF_NOTE,
            title="Phone follow-up",
            content=(
                "Called regarding the missed review appointment on the 4th. "
                "Patient answered and apologised, citing pressure at work and "
                "difficulty getting time off during clinic hours. Rebooked for "
                "the following month and offered an early morning slot. "
                "Mentioned occasional dizziness on standing, mostly first thing "
                "in the morning, not associated with palpitations. Advised to "
                "raise this at the next visit. Confirmed the current metformin "
                "supply is sufficient until then."
            ),
            days_ago=201,
        )

        # -- collaboration state ----------------------------------------
        # A chart with no outstanding work looks finished, and a Glance View
        # with an empty "open actions" column cannot show what it is for.
        staff_entry = db.get(Entry, "entry-a1-staff")
        comment = Comment(
            id="comment-a1-1",
            entry_id=staff_entry.id,
            clinic_id="clinic-a",
            author_id="u-a-staff",
            author_role=Role.STAFF,
            body=(
                "@clinician_a she is missing the evening dose fairly consistently. "
                "Worth discussing a once-daily option?"
            ),
            mentions='["u-a-clinician"]',
            status=CommentStatus.OPEN,
            is_internal=True,
            created_at=_now() - timedelta(days=2),
        )
        db.add(comment)
        db.add(
            Task(
                id="task-a1-1",
                clinic_id="clinic-a",
                patient_id="patient-a1",
                entry_id=staff_entry.id,
                description="Arrange repeat urine ACR before next review",
                assigned_to="u-a-staff",
                assigned_to_role=Role.STAFF,
                assigned_by="u-a-clinician",
                status=TaskStatus.OPEN,
                created_at=_now() - timedelta(days=3),
            )
        )
        db.flush()

        # -- highlight generation ----------------------------------------
        # Scores are computed on write, so a seeded chart needs the same pass a
        # written one gets. Without this a reviewer's first login shows an empty
        # Glance View and the product looks like it does nothing.
        for entry in db.query(Entry).all():
            highlight_service.refresh_entry_highlights(db, entry)
        db.flush()

        # -- prior clinician behaviour (Phase 4) -------------------------
        # A learning system with an empty history demos as a system that does
        # nothing. What is seeded here is BEHAVIOUR, not weights: real
        # InteractionLog rows, dated, which `rebuild_clinic` then aggregates
        # through exactly the same code path a live click goes through.
        #
        # Seeding FeatureWeight directly would have been three lines shorter and
        # would have been a lie — the demo would show numbers no interaction
        # produced, in a build whose whole argument is that surfaced claims are
        # traceable to their source.
        _seed_interaction_history(db)
        learning.rebuild_clinic(db, "clinic-a")
        for entry in db.query(Entry).all():
            highlight_service.refresh_entry_highlights(db, entry)

        db.commit()

        # -- data decay pass (Phase 4) -----------------------------------
        # Run once at seed time so a reviewer's first login already shows the
        # policy having acted: one old entry compressed, one held back because
        # it documents an allergy. Both states visible side by side is the whole
        # point — a demo where nothing is protected proves only half the design.
        report = decay.run(db, clinic_id="clinic-a", dry_run=False)
        compressed = [
            change["entry_id"] for change in report["changes"]
            if change["target_state"] == "cold"
        ]
        held = [
            change["entry_id"] for change in report["changes"] if change["protected"]
        ]

        print("Seeded 2 clinics, 4 patients, 8 users (one per role per clinic), "
              "10 entries (1 AI-scribed, 1 code-switched), 1 open comment thread, 1 open task, "
              "and generated highlights.")
        print(f"Phase 4: seeded {db.query(InteractionLog).count()} prior interactions; "
              f"learned {db.query(FeatureWeight).count()} feature weights for clinic-a.")
        print(f"Phase 4: decay compressed {len(compressed)} entr(y/ies) {compressed}; "
              f"held {len(held)} back as still-relevant {held}.")
        print(f"Phase 4: timeline read path for compressed entries "
              f"{report['hot_bytes_before']}B -> {report['hot_bytes_after']}B "
              f"(+{report['archive_bytes']}B archived, recoverable).")
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
