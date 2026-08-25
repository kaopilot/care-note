"""Server-side access enforcement — role AND clinic, inseparably.

The shared context requires that no route ever checks one dimension without the
other. Making that true by discipline is fragile: someone eventually writes a
route with `require_role(...)` and forgets the clinic filter, and nothing fails
loudly. So the two are fused in the type system instead.

How the fusion works
--------------------
* `require_access(*roles)` is the ONLY dependency a route can use to learn who
  the caller is. There is no `get_current_user` exported for route use.
* It yields an `AccessScope`, never a `User`. `AccessScope` is the handle to the
  database, and every query helper on it applies `clinic_id ==
  scope.clinic_id` before returning anything.
* `clinic_id` comes from the verified JWT and nowhere else. A client cannot
  supply, override, or widen it via body, query string, or header.

The result: to read data at all you must go through an object that has already
narrowed to your clinic. Forgetting the clinic check is not a mistake you can
make, because there is no unscoped path to reach for.

This is enforcement, not decoration. The frontend may also hide things; that is
a usability nicety and is assumed compromised.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import EntryType, Role
from app.security import policy
from app.security.auth import decode_access_token

T = TypeVar("T")


class AccessScope:
    """A caller, already narrowed to their role and clinic.

    Routes receive this and nothing else. All data access goes through its
    helpers so the clinic predicate is applied in exactly one place.
    """

    __slots__ = ("user_id", "role", "clinic_id", "patient_id", "db")

    def __init__(
        self,
        *,
        user_id: str,
        role: Role,
        clinic_id: str,
        db: Session,
        patient_id: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.role = role
        self.clinic_id = clinic_id
        # Only set for role=patient — the one Patient record they may read.
        self.patient_id = patient_id
        self.db = db

    # -- querying -------------------------------------------------------

    def query(self, model: type[T]):
        """A query pre-filtered to this caller's clinic.

        Every model that holds clinic-scoped data carries a `clinic_id` column
        (denormalised for exactly this reason). Models without one are refused
        rather than silently returned unfiltered — a fail-closed default.
        """
        if not hasattr(model, "clinic_id"):
            raise TypeError(
                f"{model.__name__} has no clinic_id; it cannot be queried through "
                "AccessScope. Add the column or handle it explicitly with a "
                "documented reason."
            )
        return self.db.query(model).filter(model.clinic_id == self.clinic_id)

    def get_or_404(self, model: type[T], object_id: str) -> T:
        """Fetch by id within this clinic. Cross-clinic ids 404 rather than 403,
        so the response cannot be used to probe whether an id exists elsewhere."""
        obj = self.query(model).filter(model.id == object_id).first()
        if obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{model.__name__} not found",
            )
        return obj

    # -- assertions -----------------------------------------------------

    def assert_patient_visible(self, patient_id: str) -> None:
        """A patient login may only ever read its own record."""
        if self.role is Role.PATIENT and self.patient_id != patient_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Patients may only access their own record",
            )

    def assert_can_view_type(self, entry_type: EntryType | str) -> None:
        if not policy.can_view_type(self.role, EntryType(entry_type)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{self.role}' may not view entries of type '{entry_type}'",
            )

    def assert_can_write_type(self, entry_type: EntryType | str) -> None:
        if not policy.can_write_type(self.role, EntryType(entry_type)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{self.role}' may not author or edit '{entry_type}' entries",
            )

    def viewable_types(self) -> list[str]:
        return sorted(str(t) for t in policy.viewable_types_for(self.role))

    def __repr__(self) -> str:  # no PHI, safe to log
        return f"AccessScope(user={self.user_id}, role={self.role}, clinic={self.clinic_id})"


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def require_access(*allowed_roles: Role):
    """Build the one dependency a protected route may use.

    Usage:
        @router.get("/patients/{patient_id}/entries")
        def list_entries(patient_id: str,
                         scope: AccessScope = Depends(require_access(Role.CLINICIAN,
                                                                    Role.STAFF))):
            ...

    Passing no roles means "any authenticated role", still clinic-scoped.
    """
    allowed: Sequence[Role] = allowed_roles or tuple(Role)

    def dependency(request: Request, db: Session = Depends(get_db)) -> AccessScope:
        token = _bearer_token(request)
        try:
            claims: dict[str, Any] = decode_access_token(token)
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
            ) from None
        except jwt.PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
            ) from None

        raw_role = claims.get("role")
        clinic_id = claims.get("clinic_id")
        user_id = claims.get("sub")

        # A token missing either dimension is unusable — never default one.
        if not user_id or not raw_role or not clinic_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing required role/clinic claims",
            )
        try:
            role = Role(raw_role)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown role claim"
            ) from None

        if role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' is not permitted on this route",
            )

        return AccessScope(
            user_id=user_id,
            role=role,
            clinic_id=clinic_id,
            patient_id=claims.get("patient_id"),
            db=db,
        )

    return dependency
