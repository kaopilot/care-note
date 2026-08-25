"""Care Note API entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.db import Base, engine
from app.routes import auth_routes, demo_rbac

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
app.include_router(demo_rbac.router)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "care-note", "phase": "0"}
