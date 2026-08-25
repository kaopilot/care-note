"""Token issuing and verification.

Deliberately minimal: seeded users, JWT with role + clinic claims, no signup or
SSO. Phase 0's job is to make the *authorisation* boundary real; authentication
is scaffolding around it.

Password hashing is PBKDF2-HMAC-SHA256 from the standard library rather than
bcrypt/argon2 — one fewer dependency, and adequate for seeded synthetic
accounts. A production build would use argon2id.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import settings
from app.core.enums import Role

_PBKDF2_ROUNDS = 120_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_access_token(
    *, user_id: str, role: Role, clinic_id: str, patient_id: str | None = None
) -> str:
    """Issue a token. Role and clinic are both claims — RBAC reads them together
    and neither is ever taken from the request body or a query parameter."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": str(role),
        "clinic_id": clinic_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    if patient_id:
        payload["patient_id"] = patient_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate. Raises jwt exceptions on failure — callers convert
    those to 401s."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
