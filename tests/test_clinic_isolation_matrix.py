"""Clinic isolation, enumerated rather than sampled.

The feedback asked: *"Name the single place clinic isolation is actually
enforced in your build. Now assume that line has a bug. How many patients
become visible to the wrong clinic, and what else would have caught it?"*

The existing RBAC tests answer the first half well and the second half by
assertion. They check isolation on the routes someone thought to check. That is
the same shape problem behind every other defect found in this build: a
hand-picked set of cases is a set someone chose, and a route added later is a
route nobody chose.

So this file does not pick. It reads the **live OpenAPI schema**, takes every
operation whose path carries a `patient_id`, and drives each one with a token
from the wrong clinic. A route added in six months is covered the day it is
added, without anyone remembering to come back here — which is the only version
of "what else would have caught it" that survives contact with a real codebase.

What counts as a pass
---------------------
404 or 403. Not 200, and not 500.

**404 is preferred over 403** and both are accepted. `403 Forbidden` on a
cross-clinic read confirms the patient exists — an attacker with a valid token
for Clinic A can enumerate Clinic B's patient ids by watching for the status
change. The build's `AccessScope.query()` returns 404 for this reason. The test
accepts 403 so that a route choosing the stricter-looking answer is not failed
outright, and reports the distinction separately.

**500 is a failure, loudly.** An unhandled exception on a cross-clinic request
means the isolation check was reached *after* something already touched the
other clinic's data. The response is a refusal either way, so a coarser test
would call it a pass.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.main import app

# One patient in each clinic, from the `seeded_p1` fixture.
FOREIGN_PATIENT = "patient-b1"  # lives in clinic-b
HOME_PATIENT = "patient-a1"  # lives in clinic-a

# Every non-patient role in clinic A. The patient role is excluded here because
# its token is bound to a specific `patient_id` and is covered by its own tests;
# what this file is about is staff-type access reaching across a clinic edge.
CLINIC_A_USERS = [
    ("staff", "u-a-staff", Role.STAFF),
    ("clinician", "u-a-clinician", Role.CLINICIAN),
    ("admin", "u-a-admin", Role.ADMIN),
]

ACCEPTABLE = {403, 404}

# A minimally valid body per write route.
#
# Sending `{}` was the first version of this file, and eleven operations
# returned 422 — FastAPI validates the body before the RBAC dependency runs, so
# an empty payload measures the request schema and never reaches the clinic
# check at all. A test that accepted 422 as "refused" would have been passing
# for the wrong reason on every write route in the build.
#
# 422-before-403 is not itself a leak: the status is identical for a real
# foreign patient and an invented one, so it reveals nothing about the patient
# (asserted below). It just is not authorisation, and cannot be counted as it.
VALID_BODIES: dict[str, dict] = {
    "/patients/{patient_id}/entries": {"type": "staff_note", "content": "probe"},
    # note: the entry type is overridden per role by `body_for` below
    "/patients/{patient_id}/tasks": {"description": "probe"},
    "/patients/{patient_id}/scribe": {"interaction_type": "doctor_patient_consult"},
    "/patients/{patient_id}/login": {},
    "/patients/{patient_id}/capture": {},
    "/patients/{patient_id}/highlights/refresh": {},
}

# `/capture` is multipart, not JSON, so it needs form fields rather than a body.
FORM_BODIES: dict[str, dict] = {
    "/patients/{patient_id}/capture": {"kind": "clinical", "transcript": "probe"},
}

# An entry type each role is actually allowed to author. Sending `staff_note`
# for every role made a clinician's cross-clinic POST return 403 for the
# *role* reason rather than the clinic one — which passes this file's assertion
# while testing something else entirely. RBAC has two dimensions and a test
# that cannot tell which one refused it is only testing one.
WRITABLE_TYPE_BY_ROLE = {
    Role.STAFF: "staff_note",
    Role.CLINICIAN: "clinician_section",
    Role.ADMIN: "clinician_section",
}


def body_for(path: str, role: Role | None = None) -> dict:
    body = dict(VALID_BODIES.get(path, {}))
    if path == "/patients/{patient_id}/entries" and role in WRITABLE_TYPE_BY_ROLE:
        body["type"] = WRITABLE_TYPE_BY_ROLE[role]
    return body


def call(client, verb: str, url: str, path: str, headers: dict, role: Role | None = None):
    """Drive one operation with whichever payload shape it actually takes."""
    if path in FORM_BODIES:
        return client.request(verb, url, headers=headers, data=FORM_BODIES[path])
    return client.request(verb, url, headers=headers, json=body_for(path, role))


# Routes whose access is restricted by ROLE as well as clinic, so a clinician
# is correctly refused even inside their own clinic. `my-care` is the
# patient-facing view (D-067) — a 403 there is the build working.
ROLE_RESTRICTED_FOR_CLINICIAN = {"/patients/{patient_id}/my-care"}


def _patient_scoped_operations() -> list[tuple[str, str]]:
    """Every operation in the live schema whose path is scoped to a patient.

    Read from `app.openapi()` rather than a hand-maintained list, so the set
    grows with the application instead of with someone's memory.
    """
    spec = app.openapi()
    found: list[tuple[str, str]] = []
    for path, operations in spec["paths"].items():
        if "{patient_id}" not in path:
            continue
        for verb in operations:
            if verb.upper() in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                found.append((verb.upper(), path))
    return sorted(found, key=lambda row: (row[1], row[0]))


OPERATIONS = _patient_scoped_operations()


def test_the_enumeration_found_the_routes_at_all():
    """A guard on the guard.

    If `_patient_scoped_operations` ever returns nothing — a schema change, a
    renamed path parameter — every parametrised test below would silently pass
    by not running. That is the failure mode of generated test cases and it is
    worth one assertion to close.
    """
    assert len(OPERATIONS) >= 8, f"only found {len(OPERATIONS)} patient-scoped operations"


@pytest.mark.parametrize("verb,path", OPERATIONS)
@pytest.mark.parametrize("role_name,user_id,role", CLINIC_A_USERS)
def test_no_clinic_a_role_can_reach_a_clinic_b_patient_on_any_route(
    client_p1, token_for, verb, path, role_name, user_id, role
):
    """The cross-product. Every role, every patient-scoped operation, one clinic
    boundary — driven through the API, so the refusal has to be server-side."""
    headers = token_for(user_id, role, "clinic-a")
    url = path.replace("{patient_id}", FOREIGN_PATIENT)
    response = call(client_p1, verb, url, path, headers, role)

    assert response.status_code != 422, (
        f"{verb} {url} returned 422 — the body was rejected before the clinic "
        "check ran, so this assertion never reached authorisation. Add a valid "
        "body for this route to VALID_BODIES."
    )

    assert response.status_code != 500, (
        f"{verb} {url} as {role_name} raised a server error. A cross-clinic "
        "request that crashes reached the other clinic's data before the "
        "isolation check did."
    )
    assert response.status_code in ACCEPTABLE, (
        f"{verb} {url} as {role_name} returned {response.status_code}; "
        f"expected one of {sorted(ACCEPTABLE)}. Clinic isolation did not hold."
    )


@pytest.mark.parametrize("verb,path", OPERATIONS)
def test_cross_clinic_refusals_do_not_confirm_the_patient_exists(
    client_p1, token_for, verb, path
):
    """Isolation that leaks existence is weaker than it looks.

    A 403 on a real foreign patient and a 404 on an invented one lets a token
    holder in Clinic A enumerate Clinic B's patient ids by watching the status
    code. The two answers must be indistinguishable.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")

    real_foreign = call(
        client_p1, verb, path.replace("{patient_id}", FOREIGN_PATIENT), path,
        headers, Role.CLINICIAN,
    ).status_code
    never_existed = call(
        client_p1, verb, path.replace("{patient_id}", "patient-does-not-exist"), path,
        headers, Role.CLINICIAN,
    ).status_code

    assert real_foreign == never_existed, (
        f"{verb} {path}: a real foreign patient returns {real_foreign} but an "
        f"invented id returns {never_existed}. The difference confirms the "
        "patient exists and makes ids enumerable across the clinic boundary."
    )


@pytest.mark.parametrize("verb,path", OPERATIONS)
def test_the_same_routes_are_reachable_for_the_home_clinic(
    client_p1, token_for, verb, path
):
    """The other half, and the reason the tests above are not vacuous.

    A build that returned 404 for everything would pass every assertion in this
    file. This asserts the same operations answer *something other than a
    clinic refusal* for a clinician's own patient — a 422 for a missing body or
    a 405 is fine here, because what is under test is the clinic edge, not the
    payload contract.
    """
    if path in ROLE_RESTRICTED_FOR_CLINICIAN:
        pytest.skip(f"{path} is role-restricted; a clinician 403 here is correct")

    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    url = path.replace("{patient_id}", HOME_PATIENT)

    response = call(client_p1, verb, url, path, headers, Role.CLINICIAN)

    assert response.status_code != 500, f"{verb} {url} raised a server error"
    assert response.status_code not in {403, 404}, (
        f"{verb} {url} refused a clinician access to a patient in their own "
        f"clinic ({response.status_code}). Either isolation is over-broad, or "
        "the cross-clinic assertions above are passing vacuously."
    )
