"""Measure Glance View latency. Produces the number quoted in the brief.

The requirement is P95 ≤ 300ms on a warm path, and the brief asks how it was
measured rather than what it is. So this states its method plainly, including
what it excludes.

**What is measured:** server handling time for `GET /patients/{id}/glance` — the
request arriving, every query running, the payload serialising, the response
leaving. This is the segment the application controls.

**What is excluded:** network transit and browser rendering. Those depend on
where the thing is deployed and what it is opened on, and folding them into a
figure produced on a developer machine would be inventing precision.

**Warm path:** the first `WARMUP` iterations are discarded. They pay for
connection setup, SQLAlchemy's first compile of each query, and an empty page
cache — none of which a clinician opening their fifth chart of the morning
experiences.

**Honest limits:** SQLite on local disk with a seeded chart of ~10 entries. A
real deployment would have hundreds of entries per patient and Postgres over a
network. What this measurement does establish is that the *application* work is
a small fraction of the budget and that no accidental N+1 is hiding in the hot
path — the design decision that matters (precomputing highlight scores on write
rather than on read) is what the number is really evidence for.

    python scripts/bench_glance.py
    python scripts/bench_glance.py --iterations 500
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
os.environ.setdefault("CARENOTE_DB_URL", "sqlite:///./.bench-glance.db")
os.environ.setdefault("CARENOTE_JWT_SECRET", "bench-secret")

import logging  # noqa: E402

logging.disable(logging.INFO)  # audit lines would dominate the timing loop

from fastapi.testclient import TestClient  # noqa: E402

import init_db  # noqa: E402
from app.core.enums import InteractionType  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Patient  # noqa: E402
from app.services import scribe  # noqa: E402

WARMUP = 20


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()

    db_path = REPO_ROOT / ".bench-glance.db"
    if db_path.exists():
        db_path.unlink()
    init_db.seed(reset=True)

    # Give the chart realistic depth: the seed plus all three AI-scribed
    # summaries, so the measured path is scoring and serialising real content
    # rather than an almost-empty table.
    session = SessionLocal()
    patient = session.query(Patient).filter(Patient.id == "patient-a1").first()
    for interaction in InteractionType:
        scribe.run_scribe(
            session, patient=patient, interaction_type=interaction, actor_id="u-a-clinician"
        )
    session.close()

    with TestClient(app) as client:
        token = client.post(
            "/auth/login",
            json={"username": "clinician_a", "password": init_db.DEMO_PASSWORD},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        url = "/patients/patient-a1/glance"

        entries = len(client.get("/patients/patient-a1/entries", headers=headers).json())
        highlights = len(client.get(url, headers=headers).json()["highlights"])

        server_samples: list[float] = []
        wall_samples: list[float] = []
        for index in range(args.iterations + WARMUP):
            started = time.perf_counter()
            response = client.get(url, headers=headers)
            wall_ms = (time.perf_counter() - started) * 1000.0
            response.raise_for_status()
            if index < WARMUP:
                continue
            server_samples.append(float(response.headers["X-Response-Time-Ms"]))
            wall_samples.append(wall_ms)

    print("Glance View latency — GET /patients/{id}/glance")
    print(f"  chart depth        : {entries} entries, {highlights} highlights on the card")
    print(f"  iterations         : {len(server_samples)} (after {WARMUP} warm-up)")
    print(f"  store              : SQLite, local disk")
    print()
    for label, samples in (("server handling", server_samples), ("in-process wall", wall_samples)):
        print(
            f"  {label:<16} p50 {statistics.median(samples):6.2f}ms   "
            f"p95 {percentile(samples, 0.95):6.2f}ms   "
            f"p99 {percentile(samples, 0.99):6.2f}ms   "
            f"max {max(samples):6.2f}ms"
        )
    print()
    p95 = percentile(server_samples, 0.95)
    verdict = "within" if p95 <= 300 else "OVER"
    print(f"  P95 server handling {p95:.2f}ms — {verdict} the 300ms budget.")
    print("  Excludes network transit and browser render; see this file's docstring.")

    if db_path.exists():
        db_path.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
