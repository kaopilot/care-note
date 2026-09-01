"""Sanitised failure handling.

`log_event` (app/core/audit_logging.py) makes it hard to log patient content on
purpose. This module covers the other case: content logged on our behalf, by
code we did not write, when something crashes.

The specific defect this fixes: SQLAlchemy embeds bound parameters in its
exception messages, so an unhandled `IntegrityError` on a version insert put a
patient name, an NRIC and note content into a single stderr line —

    [SQL: INSERT INTO versions ...]
    [parameters: ('e1', 2, 'Amira Rahman, NRIC S8412345D, allergic to penicillin')]

Redaction before the model was guarded. This door was not. It also had no
clinician-visible symptom, which is why it survived the whole build.

Why a middleware rather than an `@app.exception_handler(Exception)`: Starlette's
ServerErrorMiddleware calls the registered handler and then *re-raises* so the
ASGI server can log the traceback. A handler alone therefore sanitises the
response and leaves the log leak exactly as it was. Catching inside the
middleware stack means ServerErrorMiddleware never sees the exception and
uvicorn never prints it.

See DECISIONS.md D-071.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.ai.llm_client import LLMUnavailableError, PHILeakError

logger = logging.getLogger("carenote.errors")

# Exception types whose *message* is known not to carry row data, and which
# mean something specific enough to be worth telling the client. Everything
# else is reported by type name only.
_SAFE_TO_NAME: tuple[type[BaseException], ...] = (PHILeakError, LLMUnavailableError)


def _reference() -> str:
    """Short opaque id, logged and returned, so a report can be traced.

    This is the whole substitute for a traceback: a clinician says "reference
    a1b2c3d4", an engineer greps the log, and the log holds a type name and a
    route — not a patient.
    """
    return uuid.uuid4().hex[:8]


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def sanitised_errors(request: Request, call_next):
        try:
            return await call_next(request)
        except PHILeakError:
            # Fail-closed safety stop, not a crash. The categories that
            # triggered it are already in the audit log; the message itself is
            # built from category names and carries no values, but there is no
            # reason to hand it to a client either.
            ref = _reference()
            logger.error(
                "phi_leak_blocked ref=%s method=%s path=%s",
                ref,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": (
                        "Blocked: text could not be safely de-identified. "
                        "Nothing was sent to the model."
                    ),
                    "reference": ref,
                },
                headers={"X-Error-Reference": ref},
            )
        except LLMUnavailableError as exc:
            # Reached only if a caller chose not to degrade. The scribe does
            # degrade, so in practice this is a route that decided a summary
            # was mandatory — tell the client plainly that the model is down
            # rather than returning a generic failure.
            ref = _reference()
            logger.warning(
                "llm_unavailable ref=%s provider=%s path=%s",
                ref,
                exc.provider,
                request.url.path,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "The summarisation model is unavailable. Please retry.",
                    "reference": ref,
                },
                headers={"X-Error-Reference": ref, "Retry-After": "30"},
            )
        except Exception as exc:  # noqa: BLE001 — deliberately broad; see module docstring
            ref = _reference()
            # Type name and route only. Never str(exc): that is precisely where
            # SQLAlchemy puts the bound parameters.
            detail = type(exc).__name__
            if isinstance(exc, _SAFE_TO_NAME):
                detail = f"{detail}: {exc}"
            logger.error(
                "unhandled_exception ref=%s type=%s method=%s path=%s",
                ref,
                type(exc).__name__,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal error. Nothing was saved for this request.",
                    "reference": ref,
                },
                headers={"X-Error-Reference": ref},
            )
