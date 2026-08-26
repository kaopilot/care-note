#!/usr/bin/env python3
"""Phase 1 walking-skeleton walkthrough, against a running server.

The pytest suite is the authoritative proof; this script exists for the two
things pytest is bad at. It runs over a real TCP socket against a real uvicorn
process rather than an in-process TestClient, so it catches anything that only
works because the test harness shares a session. And it prints a table a human
can read, which is what the Phase 6 demo needs.

    # terminal 1
    cd backend && python init_db.py --reset && uvicorn app.main:app --port 8000

    # terminal 2
    python scripts/phase1_smoke.py

Exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import sys

import httpx

PASSWORD = "carenote-demo"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

results: list[tuple[bool, str, str]] = []


def check(passed: bool, label: str, detail: str = "") -> None:
    results.append((passed, label, detail))
    mark = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


def login(base: str, username: str) -> dict[str, str]:
    """Log in and return a bearer header.

    Uses the header transport rather than the cookie because this is a
    non-browser client — exactly the split DECISIONS.md D-016 describes.
    """
    response = httpx.post(
        f"{base}/auth/login", json={"username": username, "password": PASSWORD}, timeout=10
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def main(base: str) -> int:
    print(f"{BOLD}Care Note — Phase 1 walking skeleton{RESET}")
    print(f"{DIM}Target: {base} · all data synthetic{RESET}")

    try:
        health = httpx.get(f"{base}/health", timeout=5).json()
    except httpx.HTTPError as exc:
        print(f"\n{RED}Cannot reach {base}: {exc}{RESET}")
        print("Start it with: cd backend && uvicorn app.main:app --port 8000")
        return 2
    print(f"{DIM}Server phase: {health.get('phase')}{RESET}")

    # -- 1. every seeded role can log in ---------------------------------
    section("1. Login — four roles, two clinics")
    tokens: dict[str, dict[str, str]] = {}
    for username in (
        "clinician_a", "staff_a", "admin_a", "patient_a",
        "clinician_b", "staff_b", "admin_b", "patient_b",
    ):
        try:
            tokens[username] = login(base, username)
            me = httpx.get(f"{base}/auth/me", headers=tokens[username], timeout=10).json()
            check(True, f"{username:12}", f"role={me['role']} clinic={me['clinic_id']}")
        except httpx.HTTPError as exc:
            check(False, f"{username:12}", str(exc))

    # -- 2. same patient, four scoped views ------------------------------
    section("2. One patient (patient-a1), four roles, four views")
    views: dict[str, set[str]] = {}
    for username in ("clinician_a", "staff_a", "admin_a", "patient_a"):
        response = httpx.get(
            f"{base}/patients/patient-a1/entries", headers=tokens[username], timeout=10
        )
        types = {e["type"] for e in response.json()}
        views[username] = types
        print(f"  {DIM}{username:12} n={len(types)}  {sorted(types)}{RESET}")

    check(
        "clinician_section" in views["clinician_a"],
        "clinician sees clinician_section",
    )
    check(
        "clinician_section" not in views["staff_a"],
        "staff does NOT see clinician_section",
        "documented assumption D-004",
    )
    check(
        views["patient_a"] <= {"patient_note", "patient_instruction", "patient_summary"},
        "patient sees only patient-facing types",
    )
    check(
        views["patient_a"] < views["staff_a"] < views["clinician_a"],
        "views are strictly nested, not identical",
    )

    # -- 3. a real entry, written through the API ------------------------
    section("3. Write one real Entry through the API")
    created = httpx.post(
        f"{base}/patients/patient-a1/entries",
        headers=tokens["staff_a"],
        json={
            "type": "staff_note",
            "title": "Smoke-test contact",
            # Angle brackets on purpose: D-015 says these survive verbatim.
            "content": "Reviewed home readings. Target BP <130/80, dose <5mg tolerated.",
        },
        timeout=10,
    )
    check(created.status_code == 201, "staff writes a staff_note", f"HTTP {created.status_code}")
    if created.status_code == 201:
        entry = created.json()
        check(entry["author_id"] == "u-a-staff", "author taken from token")
        check(
            entry["provenance_pointer"] == f"entry://{entry['id']}",
            "provenance pointer present",
        )
        check("<130/80" in entry["content"], "clinical angle brackets stored verbatim")

    # -- 4. cross-role, direct at the API --------------------------------
    section("4. Cross-role reads — refused server-side (task 5)")
    r = httpx.get(f"{base}/entries/entry-a1-clin", headers=tokens["patient_a"], timeout=10)
    check(r.status_code == 403, "patient fetches clinician entry by id", f"HTTP {r.status_code}")
    check("HbA1c" not in r.text, "refusal does not leak the content")

    r = httpx.get(f"{base}/entries/entry-a1-clin", headers=tokens["staff_a"], timeout=10)
    check(r.status_code == 403, "staff fetches clinician entry by id", f"HTTP {r.status_code}")

    r = httpx.post(
        f"{base}/patients/patient-a1/entries",
        headers=tokens["staff_a"],
        json={"type": "clinician_section", "content": "writing outside my role"},
        timeout=10,
    )
    check(r.status_code == 403, "staff authors a clinician_section", f"HTTP {r.status_code}")

    r = httpx.post(
        f"{base}/patients/patient-a1/entries",
        headers=tokens["clinician_a"],
        json={"type": "ai_doctor_consult_summary", "content": "fabricated machine output"},
        timeout=10,
    )
    check(r.status_code == 403, "human forges an AI-scribed entry", f"HTTP {r.status_code}")

    # -- 5. cross-clinic, direct at the API ------------------------------
    section("5. Cross-clinic reads — refused server-side (task 6)")
    r = httpx.get(f"{base}/patients/patient-b1", headers=tokens["clinician_a"], timeout=10)
    check(r.status_code == 404, "clinic A clinician reads clinic B patient", f"HTTP {r.status_code}")
    check("Daniel Choo" not in r.text, "refusal does not leak patient identity")

    r = httpx.get(f"{base}/patients/patient-a1", headers=tokens["clinician_b"], timeout=10)
    check(r.status_code == 404, "clinic B clinician reads clinic A patient (converse)",
          f"HTTP {r.status_code}")

    r = httpx.get(f"{base}/entries/entry-b1-clin", headers=tokens["clinician_a"], timeout=10)
    check(r.status_code == 404, "clinic A clinician reads clinic B entry by id",
          f"HTTP {r.status_code}")

    r = httpx.post(
        f"{base}/patients/patient-b1/entries",
        headers=tokens["staff_a"],
        json={"type": "staff_note", "content": "should never land in clinic B"},
        timeout=10,
    )
    check(r.status_code == 404, "clinic A staff writes into clinic B", f"HTTP {r.status_code}")

    r = httpx.get(f"{base}/patients?clinic_id=clinic-b", headers=tokens["clinician_a"], timeout=10)
    ids = {p["id"] for p in r.json()}
    check(
        not any(i.startswith("patient-b") for i in ids),
        "clinic cannot be widened by query parameter",
        f"returned {sorted(ids)}",
    )

    # -- 6. unauthenticated ----------------------------------------------
    section("6. No token at all")
    check(
        httpx.get(f"{base}/patients", timeout=10).status_code == 401,
        "unauthenticated list is refused",
    )
    check(
        httpx.get(
            f"{base}/patients", headers={"Authorization": "Bearer not-a-jwt"}, timeout=10
        ).status_code == 401,
        "garbage token is refused",
    )

    # -- summary ----------------------------------------------------------
    failed = [label for ok, label, _ in results if not ok]
    print(f"\n{BOLD}{'─' * 60}{RESET}")
    if failed:
        print(f"{RED}{len(failed)} of {len(results)} checks FAILED{RESET}")
        for label in failed:
            print(f"  {RED}·{RESET} {label}")
        return 1
    print(f"{GREEN}All {len(results)} checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API base URL")
    sys.exit(main(parser.parse_args().base))
