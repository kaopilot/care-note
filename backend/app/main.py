"""Care Note API entrypoint."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.db import Base, engine
from app.routes import (
    auth_routes,
    comment_routes,
    demo_rbac,
    entry_routes,
    glance_routes,
    highlight_routes,
    patient_routes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

app = FastAPI(
    title="Care Note API",
    version="0.1.0",
    description=(
        "Shared longitudinal patient note. Synthetic data only — this service "
        "must never be pointed at real PHI."
    ),
)

# Dev-only origins. Production would restrict this to the deployed frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(patient_routes.router)
app.include_router(entry_routes.router)
app.include_router(comment_routes.router)
app.include_router(highlight_routes.router)
app.include_router(glance_routes.router)
# Phase 0's throwaway routes. Kept for now: tests/test_rbac_pattern.py still
# proves the enforcement pattern against them independently of any feature.
# Phase 3 folds those assertions into the real-route suite and deletes these.
app.include_router(demo_rbac.router)


@app.middleware("http")
async def add_response_time_header(request: Request, call_next):
    """Report server-side handling time on every response.

    This is the instrument behind the P95 claim in the technical brief. Timing
    from inside the app measures what the app controls — query time and
    serialisation — and deliberately excludes network and browser render, which
    are reported separately by the client. Publishing a number without saying
    which segment it covers is how latency claims become fiction.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    return response


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "care-note", "phase": "2"}
