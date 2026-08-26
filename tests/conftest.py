"""Shared test fixtures. Every test runs against a throwaway in-memory DB."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

# Must be set before app.core.config is imported anywhere.
os.environ.setdefault("CARENOTE_DB_URL", "sqlite:///:memory:")
os.environ.setdefault("CARENOTE_JWT_SECRET", "test-secret")
os.environ.setdefault("CARENOTE_LLM_PROVIDER", "stub")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.db import Base, get_db  # noqa: E402
from app.core.enums import Role  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Clinic, Patient, User  # noqa: E402
from app.security.auth import create_access_token, hash_password  # noqa: E402


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded(db_session):
    """Two clinics, one patient each, one user per role in clinic A."""
    clinic_a = Clinic(id="clinic-a", name="Clinic A")
    clinic_b = Clinic(id="clinic-b", name="Clinic B")
    db_session.add_all([clinic_a, clinic_b])

    patient_a = Patient(
        id="patient-a1", clinic_id="clinic-a", name="Amira Rahman",
        dob="1968-03-11", mrn="MRN-A-40192",
    )
    patient_b = Patient(
        id="patient-b1", clinic_id="clinic-b", name="Daniel Choo",
        dob="1975-11-02", mrn="MRN-B-88301",
    )
    db_session.add_all([patient_a, patient_b])

    pw = hash_password("pw")
    users = [
        User(id="u-a-clinician", clinic_id="clinic-a", role=Role.CLINICIAN,
             name="Dr Lim", username="clinician_a", password_hash=pw),
        User(id="u-a-staff", clinic_id="clinic-a", role=Role.STAFF,
             name="Nurse Priya", username="staff_a", password_hash=pw),
        User(id="u-a-admin", clinic_id="clinic-a", role=Role.ADMIN,
             name="Serene", username="admin_a", password_hash=pw),
        User(id="u-a-patient", clinic_id="clinic-a", role=Role.PATIENT,
             name="Amira Rahman", username="patient_a", password_hash=pw,
             patient_id="patient-a1"),
        User(id="u-b-clinician", clinic_id="clinic-b", role=Role.CLINICIAN,
             name="Dr Faizal", username="clinician_b", password_hash=pw),
    ]
    db_session.add_all(users)
    db_session.commit()
    return {"db": db_session, "clinic_a": clinic_a, "clinic_b": clinic_b}


@pytest.fixture()
def client(seeded):
    """TestClient wired to the seeded session."""

    def override_get_db():
        yield seeded["db"]

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def token_for():
    """token_for('u-a-staff', Role.STAFF, 'clinic-a') -> Authorization header."""

    def _make(user_id: str, role: Role, clinic_id: str, patient_id: str | None = None):
        token = create_access_token(
            user_id=user_id, role=role, clinic_id=clinic_id, patient_id=patient_id
        )
        return {"Authorization": f"Bearer {token}"}

    return _make


# --------------------------------------------------------------------------
# Phase 1 fixtures
#
# Deliberately separate from `seeded` above rather than an extension of it.
# Phase 0's tests assert exact patient lists (`== ["patient-a1"]`), so widening
# the shared fixture would break passing tests for no reason. A second fixture
# costs a few lines and keeps Phase 0's proofs untouched.
# --------------------------------------------------------------------------


@pytest.fixture()
def seeded_p1(db_session):
    """Two clinics, each with two patients, one user per role, and entries.

    Mirrors backend/init_db.py. Entries deliberately span every author role and
    both internal and patient-facing types, so a role-scoped read of the SAME
    patient returns visibly different rows.
    """
    from datetime import datetime, timedelta, timezone

    from app.core.enums import EntryType, RiskLevel
    from app.models import Entry

    now = datetime.now(timezone.utc)

    db_session.add_all(
        [Clinic(id="clinic-a", name="Clinic A"), Clinic(id="clinic-b", name="Clinic B")]
    )
    db_session.add_all(
        [
            Patient(id="patient-a1", clinic_id="clinic-a", name="Amira Rahman",
                    dob="1968-03-11", mrn="MRN-A-40192"),
            Patient(id="patient-a2", clinic_id="clinic-a", name="Marcus Teo",
                    dob="1991-07-24", mrn="MRN-A-40233"),
            Patient(id="patient-b1", clinic_id="clinic-b", name="Daniel Choo",
                    dob="1975-11-02", mrn="MRN-B-88301"),
            Patient(id="patient-b2", clinic_id="clinic-b", name="Halimah Yusof",
                    dob="1959-01-30", mrn="MRN-B-88344"),
        ]
    )

    pw = hash_password("pw")
    db_session.add_all(
        [
            User(id="u-a-clinician", clinic_id="clinic-a", role=Role.CLINICIAN,
                 name="Dr Lim", username="clinician_a", password_hash=pw),
            User(id="u-a-staff", clinic_id="clinic-a", role=Role.STAFF,
                 name="Nurse Priya", username="staff_a", password_hash=pw),
            User(id="u-a-admin", clinic_id="clinic-a", role=Role.ADMIN,
                 name="Serene", username="admin_a", password_hash=pw),
            User(id="u-a-patient", clinic_id="clinic-a", role=Role.PATIENT,
                 name="Amira Rahman", username="patient_a", password_hash=pw,
                 patient_id="patient-a1"),
            User(id="u-b-clinician", clinic_id="clinic-b", role=Role.CLINICIAN,
                 name="Dr Faizal", username="clinician_b", password_hash=pw),
            User(id="u-b-staff", clinic_id="clinic-b", role=Role.STAFF,
                 name="Nurse Grace", username="staff_b", password_hash=pw),
            User(id="u-b-admin", clinic_id="clinic-b", role=Role.ADMIN,
                 name="Ravi", username="admin_b", password_hash=pw),
            User(id="u-b-patient", clinic_id="clinic-b", role=Role.PATIENT,
                 name="Daniel Choo", username="patient_b", password_hash=pw,
                 patient_id="patient-b1"),
        ]
    )

    def entry(eid, patient, clinic, author, role, etype, content, days, risk=RiskLevel.NONE):
        return Entry(
            id=eid, patient_id=patient, clinic_id=clinic, author_id=author,
            author_role=role, type=etype, content=content, title=None,
            risk_level=risk, version_number=1,
            timestamp=now - timedelta(days=days),
            provenance_pointer=f"entry://{eid}",
        )

    db_session.add_all(
        [
            entry("entry-a1-clin", "patient-a1", "clinic-a", "u-a-clinician",
                  Role.CLINICIAN, EntryType.CLINICIAN_SECTION,
                  "T2DM, HbA1c 8.4%. Query microalbuminuria. Keep BP <130/80.",
                  3, RiskLevel.MEDIUM),
            entry("entry-a1-staff", "patient-a1", "clinic-a", "u-a-staff",
                  Role.STAFF, EntryType.STAFF_NOTE,
                  "BP 138/86 seated. Weight 74.2kg. Foot check done.", 3),
            entry("entry-a1-instr", "patient-a1", "clinic-a", "u-a-clinician",
                  Role.CLINICIAN, EntryType.PATIENT_INSTRUCTION,
                  "Take metformin with your evening meal. Bring home BP readings.", 3),
            entry("entry-a1-pt", "patient-a1", "clinic-a", "u-a-patient",
                  Role.PATIENT, EntryType.PATIENT_NOTE,
                  "Evening dose is the hard one. Feet tingling at night.", 1),
            entry("entry-a1-ai", "patient-a1", "clinic-a", "system",
                  Role.SYSTEM, EntryType.AI_DOCTOR_CONSULT_SUMMARY,
                  "Consult summary: glycaemic control reviewed; titration discussed.", 3),
            entry("entry-a2-staff", "patient-a2", "clinic-a", "u-a-staff",
                  Role.STAFF, EntryType.STAFF_NOTE,
                  "Ankle sprain, neurovascularly intact.", 6),
            entry("entry-b1-clin", "patient-b1", "clinic-b", "u-b-clinician",
                  Role.CLINICIAN, EntryType.CLINICIAN_SECTION,
                  "Warfarin-managed AF. INR 3.4, hold one dose.", 2, RiskLevel.HIGH),
        ]
    )
    db_session.commit()
    return {"db": db_session}


@pytest.fixture()
def client_p1(seeded_p1):
    """TestClient wired to the Phase 1 seed."""

    def override_get_db():
        yield seeded_p1["db"]

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
