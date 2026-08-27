"""Phase 4 end-to-end walkthrough, over real HTTP against a throwaway database.

Not a test — the graded suites are `test_self_learning_importance.py` and
`test_data_decay.py`. This is the script used while building to confirm the
learning loop and the decay lifecycle work against the API a browser actually
calls, and it doubles as executable documentation of the request sequence
behind demo Scenario C.

    python scripts/phase4_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
os.environ.setdefault("CARENOTE_DB_URL", "sqlite:///./.smoke-phase4.db")
os.environ.setdefault("CARENOTE_JWT_SECRET", "smoke-secret")

from fastapi.testclient import TestClient  # noqa: E402

import init_db  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = init_db.DEMO_PASSWORD
OK = "  ok  "
BAD = " FAIL "

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{OK if condition else BAD}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def login(client: TestClient, username: str) -> None:
    response = client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def main() -> int:
    db_path = REPO_ROOT / "backend" / ".smoke-phase4.db"
    db_path.unlink(missing_ok=True)
    init_db.seed(reset=True)

    with TestClient(app) as client:
        # ---- 1. the seeded learned state -------------------------------
        print("\n1. Learned importance, seeded from prior behaviour")
        login(client, "clinician_a")
        learning = client.get("/clinic/learning").json()
        weights = {row["feature_tag"]: row for row in learning["weights"]}

        check("clinic has learned weights", len(weights) > 0, f"{len(weights)} tags")
        check(
            "anticoagulation is promoted",
            weights.get("med:warfarin", {}).get("weight", 0) > 0,
            f"warfarin {weights.get('med:warfarin', {}).get('weight')}",
        )
        check(
            "routine BP is dampened",
            weights.get("finding:bp_elevated", {}).get("weight", 0) < 0,
            f"bp_elevated {weights.get('finding:bp_elevated', {}).get('weight')}",
        )
        allergy = weights.get("entity:allergy", {})
        check(
            "allergy dismissals recorded but not followed",
            allergy.get("weight") == 0.0 and allergy.get("negative_signals", 0) > 0,
            f"weight {allergy.get('weight')} from {allergy.get('negative_signals')} dismissals",
        )
        check("surface leaks no patient text", "Amira" not in client.get("/clinic/learning").text)

        # ---- 2. a decision visibly moves the ranking -------------------
        print("\n2. Confirming a suggestion shifts what the card surfaces")
        glance = client.get("/patients/patient-a1/glance").json()
        suggested = [h for h in glance["highlights"] if h["status"] == "suggested"]
        check("there is something to decide on", bool(suggested))

        if suggested:
            target = suggested[0]
            before = target["score"]
            decided = client.post(f"/highlights/{target['id']}/accept")
            check("accept is a single POST with no body", decided.status_code == 200)
            after = decided.json()["score"]
            check(
                "the confirmed highlight is rescored",
                after != before,
                f"{before} -> {after}",
            )
            breakdown = decided.json()["score_breakdown"]
            check(
                "the learned term is visible in the breakdown",
                "learned" in breakdown,
                f"learned={breakdown.get('learned')}",
            )

        # ---- 3. provenance survives compression ------------------------
        print("\n3. Provenance across the decay boundary")
        entries = client.get("/patients/patient-a1/entries").json()
        cold = [e for e in entries if e["decay_state"] == "cold"]
        warm = [e for e in entries if e["decay_state"] == "warm"]
        check("the seed produced a compressed entry", bool(cold),
              f"{[e['id'] for e in cold]}")
        check("and one held back as still relevant", bool(warm),
              f"{[e['id'] for e in warm]}")

        highlights = client.get("/patients/patient-a1/highlights").json()
        resolved = 0
        for highlight in highlights:
            response = client.get(
                "/provenance", params={"pointer": highlight["provenance_pointer"]}
            )
            if response.status_code == 200:
                resolved += 1
        check(
            "every highlight pointer still resolves",
            resolved == len(highlights),
            f"{resolved}/{len(highlights)}",
        )

        if cold:
            archive = client.get(f"/entries/{cold[0]['id']}/archive").json()
            check("the archive reports its own cost", archive["archived"] is True,
                  f"{archive['current_length']}ch hot, {archive['original_length']}B original")
            check(
                "archive metadata carries no content",
                "dizziness" not in client.get(f"/entries/{cold[0]['id']}/archive").text,
            )

            restored = client.post(f"/entries/{cold[0]['id']}/restore")
            check("a clinician can restore the full note", restored.status_code == 200)
            check(
                "restored content is longer than the summary",
                len(restored.json()["content"]) > archive["current_length"],
                f"{archive['current_length']} -> {len(restored.json()['content'])} chars",
            )

        # ---- 4. lifecycle access control -------------------------------
        print("\n4. Who may do what")
        check(
            "clinician cannot apply the decay policy",
            client.post("/clinic/decay/run?dry_run=false").status_code == 403,
        )
        login(client, "staff_a")
        check(
            "staff cannot restore an archived note",
            client.post("/entries/entry-a1-hist-2026/restore").status_code == 403,
        )
        login(client, "patient_a")
        check(
            "patient cannot read the clinic learning surface",
            client.get("/clinic/learning").status_code == 403,
        )
        login(client, "admin_a")
        preview = client.post("/clinic/decay/run?dry_run=true")
        check("admin may preview", preview.status_code == 200)
        check("a preview changes nothing", preview.json()["dry_run"] is True)

        # ---- 5. cross-clinic isolation ---------------------------------
        print("\n5. Learning does not cross a clinic boundary")
        login(client, "clinician_b")
        other = client.get("/clinic/learning").json()
        check(
            "clinic B has its own (empty) learned state",
            other["clinic_id"] == "clinic-b" and other["weights"] == [],
        )

    db_path.unlink(missing_ok=True)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("All Phase 4 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
