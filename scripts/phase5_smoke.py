"""Phase 5 end-to-end walkthrough, over real HTTP against a throwaway database.

Not a test — the graded suite is `tests/test_voice_capture.py`. This is the
script used while building to confirm ambient capture works against the API a
browser actually calls, and it doubles as executable documentation of the
request sequence behind the voice-capture portion of the demo.

    python scripts/phase5_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
os.environ.setdefault("CARENOTE_DB_URL", "sqlite:///./.smoke-phase5.db")
os.environ.setdefault("CARENOTE_JWT_SECRET", "smoke-secret")

from fastapi.testclient import TestClient  # noqa: E402

import init_db  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = init_db.DEMO_PASSWORD
OK = "  ok  "
BAD = " FAIL "

# Not real audio. The stub recogniser is deterministic on the digest and never
# decodes the container.
FAKE_AUDIO = b"\x1aE\xdf\xa3" + b"synthetic-consult-audio" * 300

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
    db_path = REPO_ROOT / "backend" / ".smoke-phase5.db"
    db_path.unlink(missing_ok=True)
    init_db.seed(reset=True)

    with TestClient(app) as client:
        # ---- 1. clinician records a consult ----------------------------
        print("\n1. Clinician captures a consult (live recording)")
        login(client, "clinician_a")
        response = client.post(
            "/patients/patient-a1/capture",
            data={
                "kind": "clinical",
                "source": "live_recording",
                "duration_ms": "62000",
                "device_label": "Pixel 7 / Chrome",
            },
            files={"audio": ("consult.webm", FAKE_AUDIO, "audio/webm")},
        )
        check("capture accepted", response.status_code == 201, response.text[:120])
        if response.status_code != 201:
            return 1
        body = response.json()
        capture, entry = body["capture"], body["entry"]
        session_id = capture["session_id"]

        check(
            "entry is AI-scribed, authored by the system",
            entry["type"] == "ai_doctor_consult_summary"
            and entry["author_role"] == "system",
            entry["type"],
        )
        check(
            "recogniser admits it is simulated",
            capture["transcription_simulated"] is True,
            capture["asr_model"],
        )
        check(
            "audio was not retained",
            capture["audio_retained"] is False,
            f"{capture['audio_bytes_received']} bytes received, none kept",
        )
        check(
            "identifiers were stripped before the model",
            capture["redaction_count"] >= 3,
            f"{capture['redaction_count']} redactions",
        )
        check(
            "code-switching preserved",
            "en-ms" in capture["languages"],
            ", ".join(capture["languages"]),
        )
        check(
            "overlapping speech detected from timings",
            capture["overlap_segments"] >= 1,
            f"{capture['overlap_segments']} overlaps",
        )
        check(
            "low-confidence segments flagged",
            capture["low_confidence_segments"] >= 1,
            f"{capture['low_confidence_segments']} below 0.6",
        )

        # ---- 2. no identifier survives anywhere ------------------------
        print("\n2. Nothing identifying survives at rest")
        detail = client.get(f"/captures/{session_id}").json()
        stored = "\n".join(segment["text"] for segment in detail["segments"])
        for needle in ("S8412345D", "6123 4567", "Amira", "Rahman"):
            check(f"'{needle}' absent from stored transcript", needle not in stored)
        check(
            "placeholders present instead",
            "[ID_" in stored and "[PHONE_" in stored and "[NAME_" in stored,
        )
        check("summary inherits redacted text", "Amira" not in entry["content"])

        # ---- 3. provenance back to spoken segments ---------------------
        print("\n3. Every summary line traces to the words behind it")
        links = client.get(f"/entries/{entry['id']}/attribution").json()
        check("attribution rows exist", len(links) > 0, f"{len(links)} lines linked")
        check(
            "every pointer resolves",
            all(link["resolves"] for link in links),
        )
        check(
            "every pointer addresses a transcript segment",
            all(
                link["provenance_pointer"].startswith("transcript://")
                and "#segment:" in link["provenance_pointer"]
                for link in links
            ),
        )
        verbatim = [link for link in links if link["match_type"] == "verbatim"]
        check(
            "verbatim links are provable, not asserted",
            all(
                " ".join(link["segment_text"].lower().split())
                in " ".join(link["span_text"].lower().split())
                for link in verbatim
            ),
            f"{len(verbatim)} of {len(links)} verbatim",
        )
        coverage = body["attribution_coverage"]
        check(
            "coverage is reported honestly",
            coverage["linked_lines"] <= coverage["attributable_lines"],
            f"{coverage['linked_lines']}/{coverage['attributable_lines']} lines "
            f"({int(coverage['coverage'] * 100)}%)",
        )
        for link in links[:3]:
            print(
                f"        {link['match_type']:<8} {link['speaker_label']:<9} "
                f"@{link['start_ms']:>6}ms  {link['span_text'][:56]}"
            )

        # ---- 4. transcript upload needs no recogniser ------------------
        print("\n4. Nurse adds a consult from a pasted transcript")
        login(client, "staff_a")
        response = client.post(
            "/patients/patient-a1/capture",
            data={
                "kind": "clinical",
                "transcript": (
                    "[00:00] staff: Good afternoon, I'm the nurse today.\n"
                    "[00:06] patient: My ankle is still swollen, worse at night.\n"
                    "[00:14] staff: I'll arrange a blood pressure check next week.\n"
                ),
            },
        )
        check("transcript accepted", response.status_code == 201, response.text[:120])
        nurse = response.json()
        check(
            "timestamped transcript is not mistaken for JSON",
            nurse["capture"]["segment_count"] == 3,
            f"{nurse['capture']['segment_count']} segments",
        )
        check(
            "no recogniser is credited for text that arrived as text",
            nurse["capture"]["asr_provider"] == "none"
            and nurse["capture"]["transcription_simulated"] is False,
        )
        check(
            "entry type follows the role, not the request",
            nurse["entry"]["type"] == "ai_nurse_consult_summary",
            nurse["entry"]["type"],
        )

        # ---- 5. the capture view boundary ------------------------------
        print("\n5. Capture kind is bound to the view, server-side")
        check(
            "clinician cannot submit a patient capture",
            client.post(
                "/patients/patient-a1/capture",
                data={"kind": "patient", "transcript": "patient: hello"},
            ).status_code
            == 403,
        )
        login(client, "patient_a")
        check(
            "patient cannot submit a clinical capture",
            client.post(
                "/patients/patient-a1/capture",
                data={"kind": "clinical", "transcript": "patient: hello"},
            ).status_code
            == 403,
        )

        response = client.post(
            "/patients/patient-a1/capture",
            data={"kind": "patient", "source": "live_recording", "duration_ms": "43000"},
            files={"audio": ("memo.webm", FAKE_AUDIO, "audio/webm")},
        )
        check("patient may record their own", response.status_code == 201)
        patient_capture = response.json()
        check(
            "patient gets a receipt, not the clinical note",
            patient_capture["entry"] is None,
            patient_capture["message"][:60],
        )
        check(
            "patient cannot read the raw transcript, even their own",
            client.get(
                f"/captures/{patient_capture['capture']['session_id']}"
            ).status_code
            == 403,
        )

        login(client, "admin_a")
        check(
            "admin is oversight, not authorship",
            client.post(
                "/patients/patient-a1/capture",
                data={"kind": "clinical", "transcript": "staff: hello"},
            ).status_code
            == 403,
        )

        # ---- 6. cross-clinic isolation ---------------------------------
        print("\n6. Captures do not cross a clinic boundary")
        login(client, "clinician_b")
        check(
            "cannot capture into another clinic's patient",
            client.post(
                "/patients/patient-a1/capture",
                data={"kind": "clinical", "transcript": "clinician: hello"},
            ).status_code
            == 404,
        )
        check(
            "cannot read another clinic's transcript",
            client.get(f"/captures/{session_id}").status_code == 404,
        )

    db_path.unlink(missing_ok=True)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("All Phase 5 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
