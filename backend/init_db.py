"""Create tables and seed the minimum synthetic fixture.

Phase 0 seeds only what the RBAC demo routes need: two clinics, so cross-clinic
isolation is provable rather than assumed. Phase 1 expands this into the full
fixture (one user per role per clinic, entries, etc.).

    python init_db.py            # create tables, seed if empty
    python init_db.py --reset    # drop everything first

ALL DATA HERE IS SYNTHETIC. Names, MRNs and dates are invented.
"""

from __future__ import annotations

import argparse
import sys

from app.core.db import Base, SessionLocal, engine
from app.core.enums import Role
from app.models import Clinic, Patient, User
from app.security.auth import hash_password

# Seed password for every demo account. Dev fixture only.
DEMO_PASSWORD = "carenote-demo"


def seed(reset: bool = False) -> None:
    if reset:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        if db.query(Clinic).count() > 0:
            print("Database already seeded; nothing to do. Use --reset to rebuild.")
            return

        clinic_a = Clinic(id="clinic-a", name="Bukit Timah Family Clinic")
        clinic_b = Clinic(id="clinic-b", name="Tampines Community Clinic")
        db.add_all([clinic_a, clinic_b])

        patient_a = Patient(
            id="patient-a1",
            clinic_id=clinic_a.id,
            name="Amira Rahman",
            dob="1968-03-11",
            mrn="MRN-A-40192",
        )
        patient_b = Patient(
            id="patient-b1",
            clinic_id=clinic_b.id,
            name="Daniel Choo",
            dob="1975-11-02",
            mrn="MRN-B-88301",
        )
        db.add_all([patient_a, patient_b])
        db.flush()

        password = hash_password(DEMO_PASSWORD)
        db.add_all(
            [
                User(
                    id="u-a-clinician",
                    clinic_id=clinic_a.id,
                    role=Role.CLINICIAN,
                    name="Dr Lim Wei Sheng",
                    username="clinician_a",
                    password_hash=password,
                ),
                User(
                    id="u-a-staff",
                    clinic_id=clinic_a.id,
                    role=Role.STAFF,
                    name="Nurse Priya Nair",
                    username="staff_a",
                    password_hash=password,
                ),
                User(
                    id="u-a-admin",
                    clinic_id=clinic_a.id,
                    role=Role.ADMIN,
                    name="Serene Koh",
                    username="admin_a",
                    password_hash=password,
                ),
                User(
                    id="u-a-patient",
                    clinic_id=clinic_a.id,
                    role=Role.PATIENT,
                    name="Amira Rahman",
                    username="patient_a",
                    password_hash=password,
                    patient_id=patient_a.id,
                ),
                User(
                    id="u-b-clinician",
                    clinic_id=clinic_b.id,
                    role=Role.CLINICIAN,
                    name="Dr Faizal Aziz",
                    username="clinician_b",
                    password_hash=password,
                ),
                User(
                    id="u-b-staff",
                    clinic_id=clinic_b.id,
                    role=Role.STAFF,
                    name="Nurse Grace Tan",
                    username="staff_b",
                    password_hash=password,
                ),
            ]
        )
        db.commit()
        print("Seeded 2 clinics, 2 patients, 6 users.")
        print(f"Login with any username above / password: {DEMO_PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise the Care Note database.")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()
    seed(reset=args.reset)
    sys.exit(0)
