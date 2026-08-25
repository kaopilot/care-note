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
