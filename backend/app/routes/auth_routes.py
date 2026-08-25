"""Login. Seeded users only — no signup by design (Phase 0, step 1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.audit_logging import log_event
from app.core.db import get_db
from app.models import User
from app.security.auth import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    clinic_id: str
    user_id: str


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.query(User).filter(User.username == payload.username).first()

    # Same response for unknown user and wrong password — no account enumeration.
    if user is None or not verify_password(payload.password, user.password_hash):
        log_event(actor_id=None, action="auth.login_failed", target_type="user")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    token = create_access_token(
        user_id=user.id,
        role=user.role,
        clinic_id=user.clinic_id,
        patient_id=user.patient_id,
    )
    log_event(
        actor_id=user.id,
        action="auth.login",
        target_type="user",
        target_id=user.id,
        clinic_id=user.clinic_id,
        metadata={"role": str(user.role)},
    )
    return LoginResponse(
        access_token=token,
        role=str(user.role),
        clinic_id=user.clinic_id,
        user_id=user.id,
    )
