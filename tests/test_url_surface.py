"""No patient data may travel in a URL.

Scenario 3 asks what is going out of the doors nobody guards. Redaction before
the model is the guarded one. The ASGI access log is not: uvicorn records the
full request line — method, path and query string — for every request, and
those logs have no rotation and no scrubbing in this build.

`log_event` (D-014) and the sanitised error middleware (D-071) both stop
content we log. Neither touches this sink, because nothing here is logged by
our code at all: the server logs the URL before the application sees it, and it
logs it identically whether the request succeeded, 404'd, or was rejected by
RBAC.

That made the URL a place where PHI must never appear. It was safe by
convention until D-083, and convention had already failed once: the enrolment
route built for scenario 1 took the patient's phone number as a query
parameter, so the single feature written for "she exists as a phone number in a
WhatsApp thread" wrote that number into the access log on every use.

These tests turn the convention into a checkable invariant. They are
deliberately structural — they read the route table rather than exercising
behaviour — because the failure mode is someone adding a plausible-looking
route six months from now, not existing code misbehaving.

See DECISIONS.md D-083.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import app

# Parameter names allowed to appear in a path or query string. Every one is an
# opaque identifier, an enum, an integer or a structural pointer — nothing a
# patient said, and nothing that identifies them outside this system.
#
# Adding a name here is a deliberate act. If the value can carry free text, a
# personal name, a phone number or an identity number, it belongs in a request
# body instead, and this list is the wrong place to resolve the argument.
ALLOWED_URL_PARAMS = {
    # opaque primary keys
    "patient_id",
    "entry_id",
    "comment_id",
    "task_id",
    "highlight_id",
    "session_id",
    # structural / enumerated
    "pointer",  # "entry:<uuid>#12-40" — ids and offsets only
    "status_filter",
    "from_version",
    "to_version",
    "limit",
    "dry_run",
}

# Substrings that mean a parameter is carrying something a person said or
# something that identifies them. Matched against the parameter name.
PHI_SHAPED_NAMES = (
    "name",
    "phone",
    "mobile",
    "nric",
    "ic",
    "identifier",
    "dob",
    "birth",
    "email",
    "address",
    "content",
    "body",
    "text",
    "note",
    "query",
    "search",
    "q",
    "transcript",
)


def _api_routes():
    """Every APIRoute, walking the included-router wrappers this FastAPI uses."""

    def walk(container):
        for route in getattr(container, "routes", []):
            if isinstance(route, APIRoute):
                yield route
            else:
                inner = getattr(route, "original_router", None)
                if inner is None and hasattr(route, "routes"):
                    inner = route
                if inner is not None:
                    yield from walk(inner)

    return list(walk(app))


def test_the_route_table_is_actually_being_read():
    """Guard against the invariant passing because it inspected nothing."""
    routes = _api_routes()
    assert len(routes) > 30, f"expected the full route table, walked {len(routes)}"
    paths = {r.path for r in routes}
    assert "/patients/{patient_id}/glance" in paths


@pytest.mark.parametrize("route", _api_routes(), ids=lambda r: f"{sorted(r.methods)}{r.path}")
def test_no_route_takes_patient_data_in_the_url(route):
    """Path and query parameters must be opaque identifiers, not content."""
    declared = [f.name for f in route.dependant.path_params] + [
        f.name for f in route.dependant.query_params
    ]
    unexpected = sorted(set(declared) - ALLOWED_URL_PARAMS)
    assert not unexpected, (
        f"{sorted(route.methods)} {route.path} declares URL parameter(s) "
        f"{unexpected}. The access log records the full request line, so a URL "
        f"is a logging sink. If these carry patient data, move them into the "
        f"request body; if they are structural, add them to ALLOWED_URL_PARAMS "
        f"with a reason."
    )


def test_no_allowed_parameter_is_phi_shaped():
    """The allowlist itself has to stay honest.

    Without this, the invariant could be satisfied by widening the allowlist,
    which is how a control quietly becomes a formality.
    """
    offenders = sorted(
        param
        for param in ALLOWED_URL_PARAMS
        if any(part == token for token in PHI_SHAPED_NAMES for part in param.split("_"))
    )
    assert not offenders, (
        f"{offenders} look like they carry patient data but sit on the URL "
        f"allowlist. Move the value into a request body."
    )


def test_the_enrolment_identifier_is_not_a_query_parameter():
    """The specific regression: scenario 1's phone number in the access log.

    Kept as its own named test rather than left to the parametrised sweep, so
    that a failure here says what actually broke.
    """
    route = next(
        r
        for r in _api_routes()
        if r.path == "/patients/{patient_id}/login" and "POST" in r.methods
    )
    query_names = {f.name for f in route.dependant.query_params}
    assert "identifier" not in query_names, (
        "the patient's phone number would be written to the access log on every "
        "login issued — the feature built for scenario 1 leaking via scenario 3"
    )


def test_issuing_a_login_still_works_with_the_identifier_in_the_body(
    client, seeded, token_for
):
    """End to end: the feature is unchanged, the URL is clean.

    Moving a parameter is only a fix if the thing it powered still works. This
    asserts both halves — the login is issued against the phone number, and the
    phone number is nowhere in the request target the access log would record.
    """
    from app.core.enums import Role

    staff = token_for("u-a-staff", Role.STAFF, "clinic-a")
    phone = "0198887777"

    response = client.post(
        "/patients/patient-a1/login",
        json={"identifier": phone},
        headers=staff,
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["one_time_passcode"]) == 6
    assert phone not in str(response.request.url), (
        "the number must not reach the request target the access log records"
    )
