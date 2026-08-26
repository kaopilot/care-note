"""Login. Seeded users only — no signup by design (Phase 0, step 1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.audit_logging import log_event
from app.core.config import settings
from app.core.db import get_db
from app.models import Clinic, User
from app.security.auth import create_access_token, verify_password
from app.security.rbac import AccessScope, require_access

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
    expires_in_minutes: int


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> LoginResponse:
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

    # The browser's copy: httpOnly so no injected script can read it, SameSite
    # so it does not ride along on cross-site requests. See DECISIONS.md D-016.
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.jwt_ttl_minutes * 60,
        path="/",
    )

    log_event(
        actor_id=user.id,
        action="auth.login",
        target_type="user",
        target_id=user.id,
        clinic_id=user.clinic_id,
        metadata={"role": str(user.role)},
    )
    # `access_token` in the body is for non-browser clients (tests, curl, API
    # consumers). The browser client must use the cookie and must not persist
    # this value — see DECISIONS.md D-016 for why that sharp edge is accepted.
    return LoginResponse(
        access_token=token,
        role=str(user.role),
        clinic_id=user.clinic_id,
        user_id=user.id,
        expires_in_minutes=settings.jwt_ttl_minutes,
    )


class MeResponse(BaseModel):
    user_id: str
    role: str
    clinic_id: str
    clinic_name: str
    name: str
    patient_id: str | None = None
    viewable_types: list[str]


@router.get("/me", response_model=MeResponse)
def me(scope: AccessScope = Depends(require_access())) -> MeResponse:
    """Who is this session? Read from the token, never from the request body.

    This exists so the browser can restore its session after a page refresh
    *without persisting anything client-side*. The token lives only in the
    httpOnly cookie (D-016); if the frontend had to remember the role itself it
    would need somewhere to put it, and the obvious somewhere is localStorage —
    which is exactly what D-016 rules out. One cheap round-trip removes the
    temptation. See DECISIONS.md D-020.
    """
    user = scope.get_or_404(User, scope.user_id)

    # Clinic is the tenant row itself: it has `id`, not `clinic_id`, so
    # AccessScope.query() refuses it (fail-closed by design). This is the
    # documented explicit handling that refusal asks for — and it is safe
    # because the id being looked up IS scope.clinic_id, which came from the
    # verified token. No caller-supplied value reaches this query.
    clinic = scope.db.query(Clinic).filter(Clinic.id == scope.clinic_id).first()
    if clinic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clinic not found")

    return MeResponse(
        user_id=user.id,
        role=str(user.role),
        clinic_id=user.clinic_id,
        clinic_name=clinic.name,
        name=user.name,
        patient_id=user.patient_id,
        viewable_types=scope.viewable_types(),
    )


@router.post("/logout")
def logout(response: Response) -> dict:
    """Clear the session cookie.

    Tokens are stateless and not revocable server-side (no denylist — see
    DECISIONS.md D-016), so this ends the browser session but a token already
    copied elsewhere stays valid until it expires. Stated rather than implied.
    """
    response.delete_cookie(key=settings.cookie_name, path="/")
    log_event(actor_id=None, action="auth.logout", target_type="session")
    return {"ok": True}
