"""Clinic B onboards next Monday.

Scenario 5. The question is specifically config-vs-schema, so these assert both
halves: that a new clinic needs no migration and no setup, and that a clinic
which *does* want different behaviour can have it without a deploy.

The third group is the one that matters most. A configuration surface is also an
attack surface and a footgun — the failure mode is a clinic turning a threshold
down until an alert stops firing. So the safety floors are asserted to be
unreachable from configuration, not merely documented as such.

See DECISIONS.md D-086.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import DecayState, EntryType, RiskLevel, Role
from app.models import Clinic, ClinicConfig, Entry, Patient
from app.services import clinic_config, decay


# --- onboarding needs neither a migration nor a setup step ------------------


def test_a_brand_new_clinic_works_with_no_config_row(db_session, seeded):
    """Zero-step onboarding. This is the actual answer to scenario 5."""
    db_session.add(Clinic(id="clinic-c", name="Clinic C"))
    db_session.commit()

    config = clinic_config.for_clinic(db_session, "clinic-c")

    assert config.is_default is True
    assert config.max_highlights == clinic_config.DEFAULTS["max_highlights"]
    assert config.warm_after_days == clinic_config.DEFAULTS["warm_after_days"]


def test_defaults_reproduce_the_previous_module_constants(db_session, seeded):
    """Adding configurability must not change behaviour for anyone."""
    from app.services import glance

    config = clinic_config.for_clinic(db_session, "clinic-a")
    assert config.max_highlights == glance.MAX_HIGHLIGHTS
    assert config.max_contradictions == glance.MAX_CONTRADICTIONS
    assert config.max_whats_new == glance.MAX_WHATS_NEW
    assert config.warm_after_days == decay.WARM_AFTER_DAYS
    assert config.cold_after_days == decay.COLD_AFTER_DAYS


def test_an_unknown_clinic_id_resolves_rather_than_raising(db_session, seeded):
    """A resolver that can fail is a new outage in the Glance View path."""
    assert clinic_config.for_clinic(db_session, "no-such-clinic").is_default is True
    assert clinic_config.for_clinic(db_session, "").is_default is True


# --- a clinic can differ, without a deploy ---------------------------------


def test_two_clinics_can_hold_different_retention_windows(db_session, seeded):
    db_session.add(ClinicConfig(clinic_id="clinic-b", warm_after_days=400, cold_after_days=800))
    db_session.commit()

    a = clinic_config.for_clinic(db_session, "clinic-a")
    b = clinic_config.for_clinic(db_session, "clinic-b")

    assert a.warm_after_days == 45
    assert b.warm_after_days == 400
    assert b.is_default is False


def test_the_retention_window_actually_reaches_the_decay_engine(db_session, seeded):
    """Config that nothing reads is documentation with a table behind it."""
    patient = db_session.query(Patient).filter(Patient.id == "patient-b1").one()
    entry = Entry(
        patient_id=patient.id,
        clinic_id=patient.clinic_id,
        author_role=Role.CLINICIAN,
        author_id="u-b-clinician",
        timestamp=datetime.now(timezone.utc) - timedelta(days=100),
        type=EntryType.CLINICIAN_SECTION,
        title="old note",
        content="routine review, nothing outstanding" * 5,
        risk_level=RiskLevel.LOW,
        provenance_pointer="entry:x",
        version_number=1,
    )
    db_session.add(entry)
    db_session.commit()

    # Default windows: 100 days is past warm (45).
    assert decay.classify(db_session, entry).target_state == str(DecayState.WARM)

    # Clinic B keeps records hot for a year.
    db_session.add(ClinicConfig(clinic_id="clinic-b", warm_after_days=365, cold_after_days=730))
    db_session.commit()

    assert decay.classify(db_session, entry).target_state == str(DecayState.HOT), (
        "the per-clinic window did not reach the decay engine"
    )


def test_one_clinics_config_does_not_move_anothers(db_session, seeded):
    db_session.add(ClinicConfig(clinic_id="clinic-b", max_highlights=20))
    db_session.commit()

    assert clinic_config.for_clinic(db_session, "clinic-a").max_highlights == 6
    assert clinic_config.for_clinic(db_session, "clinic-b").max_highlights == 20


# --- configuration cannot reach a safety floor -----------------------------


def test_values_are_bounded(db_session, seeded):
    """An unbounded setting is a new way to break the product from the database."""
    db_session.add(ClinicConfig(clinic_id="clinic-b", max_highlights=0, cold_after_days=1))
    db_session.commit()

    config = clinic_config.for_clinic(db_session, "clinic-b")
    assert config.max_highlights >= clinic_config.BOUNDS["max_highlights"][0], (
        "max_highlights=0 would empty the Glance View"
    )
    assert config.cold_after_days > config.warm_after_days, (
        "a cold threshold below warm makes the lifecycle non-monotonic"
    )


def test_a_clinic_cannot_configure_its_way_out_of_allergy_protection(db_session, seeded):
    """The floor from D-084 is not a setting, and must not become one."""
    fields = set(clinic_config.DEFAULTS)
    for forbidden in ("protected_classes", "never_dampened", "allergy_severity"):
        assert forbidden not in fields, (
            f"{forbidden} is configurable — that is a per-clinic off switch for "
            f"a safety floor"
        )


def test_redaction_is_not_configurable(db_session, seeded):
    """A clinic that could weaken PHI redaction eventually would."""
    fields = set(clinic_config.DEFAULTS)
    assert not any("redact" in key or "phi" in key for key in fields)

    row_columns = {column.name for column in ClinicConfig.__table__.columns}
    assert not any("redact" in name or "phi" in name for name in row_columns)


def test_the_configurable_set_stays_small_and_deliberate(db_session, seeded):
    """A guard against drift.

    Every addition here should be a decision someone argued for. Failing this
    test is not a problem — it is a prompt to write down why the surface grew,
    and to check the new value against the "sees, not protected from" rule.
    """
    assert set(clinic_config.DEFAULTS) == {
        "max_highlights",
        "max_contradictions",
        "max_whats_new",
        "warm_after_days",
        "cold_after_days",
    }
    assert set(clinic_config.BOUNDS) == set(clinic_config.DEFAULTS), (
        "every configurable value needs bounds"
    )
