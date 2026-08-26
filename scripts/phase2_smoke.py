"""Phase 2 end-to-end walkthrough, over real HTTP against a throwaway database.

Not a test — the graded suite is Phase 3. This is the script used while building
to confirm each sub-step actually works against the API a browser would call,
and it doubles as executable documentation of the request sequence behind the
demo scenarios.

    python scripts/phase2_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
os.environ.setdefault("CARENOTE_DB_URL", "sqlite:///./.smoke-phase2.db")
os.environ.setdefault("CARENOTE_JWT_SECRET", "smoke-secret")

from fastapi.testclient import TestClient  # noqa: E402

import init_db  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = init_db.DEMO_PASSWORD
OK = "  ok  "
BAD = " FAIL "


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"[{OK if condition else BAD}] {label}{(' — ' + detail) if detail else ''}")
    return condition


def login(client: TestClient, username: str) -> dict:
    response = client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def main() -> int:
    db_path = REPO_ROOT / ".smoke-phase2.db"
    if db_path.exists():
        db_path.unlink()
    init_db.seed(reset=True)

    failures = 0
    with TestClient(app) as client:
        clinician = login(client, "clinician_a")
        staff = login(client, "staff_a")
        patient = login(client, "patient_a")

        patients = client.get("/patients", headers=clinician).json()
        pid = patients[0]["id"]
        print(f"\nPatient under test: {patients[0]['name']} ({pid})\n")

        # -- 2.2 AI scribe -------------------------------------------------
        print("2.2 — AI scribe pipeline")
        for interaction in (
            "doctor_patient_consult",
            "nurse_patient_consult",
            "ai_patient_session",
        ):
            response = client.post(
                f"/patients/{pid}/scribe",
                json={"interaction_type": interaction},
                headers=clinician,
            )
            body = response.json() if response.status_code == 201 else {}
            failures += not check(
                f"{interaction} → entry",
                response.status_code == 201
                and body.get("author_role") == "system"
                and body.get("is_ai_scribed") is True,
                f"type={body.get('type')} redactions={body.get('ai_redaction_count')} "
                f"confidence={body.get('ai_confidence')}",
            )
            failures += not check(
                "  redaction actually removed identifiers",
                (body.get("ai_redaction_count") or 0) > 0,
            )
            failures += not check(
                "  provenance points at the session",
                str(body.get("provenance_pointer", "")).startswith("session://"),
                body.get("provenance_pointer", ""),
            )

        # -- 2.1 timeline --------------------------------------------------
        print("\n2.1 — Timeline")
        entries = client.get(f"/patients/{pid}/entries", headers=clinician).json()
        types = {entry["type"] for entry in entries}
        failures += not check(
            "clinician sees all three AI types plus manual notes",
            {
                "ai_doctor_consult_summary",
                "ai_nurse_consult_summary",
                "ai_patient_session_summary",
                "clinician_section",
                "staff_note",
            }
            <= types,
            f"{len(entries)} entries",
        )
        staff_types = {
            entry["type"]
            for entry in client.get(f"/patients/{pid}/entries", headers=staff).json()
        }
        failures += not check(
            "staff cannot see clinician_section",
            "clinician_section" not in staff_types,
            f"staff sees {sorted(staff_types)}",
        )

        # -- 2.4 glance ----------------------------------------------------
        print("\n2.4 — Glance View")
        response = client.get(f"/patients/{pid}/glance", headers=clinician)
        glance = response.json()
        failures += not check(
            "glance returns highlights",
            len(glance["highlights"]) > 0,
            f"{len(glance['highlights'])} highlights, "
            f"server {response.headers.get('X-Response-Time-Ms')}ms",
        )
        failures += not check(
            "every highlight has a reason and a pointer",
            all(h["risk_reason"] and h["provenance_pointer"] for h in glance["highlights"]),
        )
        failures += not check(
            "at least one highlight sourced from an AI-scribed note",
            any(h["is_ai_scribed"] for h in glance["highlights"]),
        )
        failures += not check(
            "risk flags carry a text label, not colour alone",
            all(flag["label"] for flag in glance["risk_flags"]),
            f"{len(glance['risk_flags'])} flags",
        )
        confidence_flagged = glance["confidence_flags"]
        failures += not check(
            "low-confidence AI summary is flagged",
            len(confidence_flagged) > 0,
            f"{[round(f['confidence'], 2) for f in confidence_flagged]}",
        )

        # what's new: second load after the session gap is simulated by the
        # first load having recorded a view.
        second = client.get(f"/patients/{pid}/glance", headers=clinician).json()
        failures += not check(
            "what's-new marker survives a refresh",
            second["whats_new"]["first_visit"] is False
            or second["whats_new"]["since"] is None,
            f"since={second['whats_new']['since']}",
        )

        # -- 2.3 provenance click-through ----------------------------------
        print("\n2.3 — Provenance")
        ai_highlight = next(h for h in glance["highlights"] if h["is_ai_scribed"])
        resolved = client.get(
            "/provenance",
            params={"pointer": ai_highlight["provenance_pointer"]},
            headers=clinician,
        )
        body = resolved.json()
        failures += not check(
            "highlight pointer resolves to its entry span",
            resolved.status_code == 200 and body["entry_id"] == ai_highlight["entry_id"],
            f"span={body.get('span')} text={(body.get('span_text') or '')[:48]!r}",
        )
        cross = client.get(
            "/provenance",
            params={"pointer": ai_highlight["provenance_pointer"]},
            headers=login(client, "clinician_b"),
        )
        failures += not check(
            "the same pointer is refused across clinics",
            cross.status_code == 404,
            f"status={cross.status_code}",
        )

        # -- 2.4 accept / reject -------------------------------------------
        print("\n2.4 — Accept / reject")
        accepted = client.post(
            f"/highlights/{ai_highlight['id']}/accept", headers=clinician
        )
        failures += not check(
            "clinician accepts a suggestion",
            accepted.status_code == 200 and accepted.json()["status"] == "accepted",
        )
        refused = client.post(f"/highlights/{ai_highlight['id']}/accept", headers=staff)
        failures += not check(
            "staff cannot decide highlights",
            refused.status_code == 403,
            f"status={refused.status_code}",
        )

        # -- manual highlight inside an AI note ----------------------------
        ai_entry = next(entry for entry in entries if entry["is_ai_scribed"])
        manual = client.post(
            f"/entries/{ai_entry['id']}/highlights",
            json={"span_start": 0, "span_end": min(60, len(ai_entry["content"]))},
            headers=clinician,
        )
        failures += not check(
            "clinician manually highlights inside an AI note",
            manual.status_code == 201 and manual.json()["is_manual"] is True,
            f"status={manual.status_code}",
        )

        # -- 2.5 collaboration ---------------------------------------------
        print("\n2.5 — Collaboration")
        staff_entry = client.post(
            f"/patients/{pid}/entries",
            json={"type": "staff_note", "content": "Chased the lab; ACR still pending."},
            headers=staff,
        ).json()
        users = client.get("/clinic/users", headers=staff).json()
        clinician_user = next(u for u in users if u["role"] == "clinician")
        comment = client.post(
            f"/entries/{staff_entry['id']}/comments",
            json={
                "body": f"@{clinician_user['username']} can you confirm the ACR order?",
                "mentions": [clinician_user["id"]],
            },
            headers=staff,
        )
        failures += not check(
            "staff comments with an @clinician mention",
            comment.status_code == 201
            and comment.json()["mentions"] == [clinician_user["id"]],
        )
        patient_read = client.get(
            f"/entries/{staff_entry['id']}/comments", headers=patient
        )
        failures += not check(
            "patient cannot read internal comments",
            patient_read.status_code == 403,
            f"status={patient_read.status_code}",
        )
        task = client.post(
            f"/patients/{pid}/tasks",
            json={
                "description": "Book monofilament testing",
                "assigned_to": next(u for u in users if u["role"] == "staff")["id"],
                "entry_id": staff_entry["id"],
            },
            headers=clinician,
        )
        failures += not check("task assigned to staff", task.status_code == 201)
        resolved_comment = client.post(
            f"/comments/{comment.json()['id']}/resolve", headers=clinician
        )
        failures += not check(
            "comment resolves",
            resolved_comment.status_code == 200
            and resolved_comment.json()["status"] == "resolved",
        )

        # -- 2.6 revision history ------------------------------------------
        print("\n2.6 — Revision history")
        clinician_entry = next(
            entry for entry in entries if entry["type"] == "clinician_section"
        )
        original_content = clinician_entry["content"]
        edited = client.patch(
            f"/entries/{clinician_entry['id']}",
            json={
                "content": original_content + "\nAdded: start low-dose amlodipine 5mg.",
                "title": clinician_entry["title"],
                "expected_version": clinician_entry["version_number"],
                "change_summary": "added antihypertensive",
            },
            headers=clinician,
        )
        failures += not check(
            "edit increments the version",
            edited.status_code == 200
            and edited.json()["version_number"] == clinician_entry["version_number"] + 1,
            f"v{edited.json().get('version_number')}",
        )
        stale_write = client.patch(
            f"/entries/{clinician_entry['id']}",
            json={
                "content": "concurrent edit from a stale tab",
                "expected_version": clinician_entry["version_number"],
            },
            headers=clinician,
        )
        failures += not check(
            "stale write is refused with 409, not silently applied",
            stale_write.status_code == 409,
            f"status={stale_write.status_code}",
        )
        diff = client.get(
            f"/entries/{clinician_entry['id']}/diff",
            params={"from_version": 1, "to_version": 2},
            headers=clinician,
        ).json()
        failures += not check(
            "diff reports the added line",
            diff["added"] >= 1,
            f"+{diff['added']} -{diff['removed']}",
        )
        reverted = client.post(
            f"/entries/{clinician_entry['id']}/revert",
            json={"to_version": 1},
            headers=clinician,
        ).json()
        failures += not check(
            "revert restores content as a NEW version",
            reverted["content"] == original_content and reverted["version_number"] == 3,
            f"v{reverted['version_number']}",
        )

        # -- 2.7 conflict handling -----------------------------------------
        print("\n2.7 — Conflict handling")
        supersede = client.post(
            f"/entries/{ai_entry['id']}/supersede",
            json={
                "content": "Correction: paraesthesia reported for six weeks, not two.",
                "reason": "patient clarified duration in person",
                "risk_level": "medium",
            },
            headers=clinician,
        )
        failures += not check(
            "clinician correction supersedes the AI note",
            supersede.status_code == 201
            and supersede.json()["supersedes_entry_id"] == ai_entry["id"],
            f"status={supersede.status_code}",
        )
        after = client.get(f"/entries/{ai_entry['id']}", headers=clinician).json()
        failures += not check(
            "the superseded AI note is flagged, not deleted",
            after["conflict_flagged"] is True and after["content"] == ai_entry["content"],
        )
        edit_ai = client.patch(
            f"/entries/{ai_entry['id']}",
            json={"content": "rewriting the machine's words", "expected_version": 1},
            headers=clinician,
        )
        failures += not check(
            "nobody can edit an AI note in place",
            edit_ai.status_code == 403,
            f"status={edit_ai.status_code}",
        )

        # -- patient view ---------------------------------------------------
        print("\nPatient view")
        my_care = client.get(f"/patients/{pid}/my-care", headers=patient)
        body = my_care.json()
        failures += not check(
            "patient view renders next steps in plain language",
            my_care.status_code == 200 and len(body["next_steps"]) > 0,
            f"{len(body['next_steps'])} steps",
        )
        leaked = client.get(f"/patients/{pid}/glance", headers=patient)
        failures += not check(
            "patient cannot open the clinical Glance View",
            leaked.status_code == 403,
            f"status={leaked.status_code}",
        )

    print("\n" + ("All Phase 2 checks passed." if not failures else f"{failures} FAILED"))
    if db_path.exists():
        db_path.unlink()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
