"""Editing an entry, and everything that follows from it: versions, diffs,
revert, and the clinician-precedence conflict rule.

The invariant this module protects is that **the record only ever grows**. An
edit appends a version. A revert appends a version. A clinician overriding an AI
summary appends an entry and flags the one it disagrees with. Nothing here
destroys prior state, because a longitudinal record that can silently lose what
it used to say is not evidence of anything.

Concurrency (2.7)
-----------------
Most collisions are prevented by construction: RBAC already partitions who may
write which entry types, so staff and clinicians cannot be editing the same
section at all. What remains is two users of the *same* role editing the *same*
section, and that is handled with optimistic locking on `version_number` —
the client sends the version it read, and a stale write is refused with 409
plus the current state, rather than overwriting work it never saw.

Last-write-wins was the alternative and is one line shorter. It also silently
discards a colleague's note, which is the exact failure the brief describes.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.audit_logging import log_event
from app.core.enums import (
    AI_SCRIBED_TYPES,
    EntryType,
    InteractionAction,
    RiskLevel,
    Role,
)
from app.core.provenance import entry_pointer
from app.core.sanitization import ContentTooLongError, prepare_content
from app.models import AuditLog, Entry, Patient, User, Version
from app.routes.schemas import EntryOut, entry_out
from app.security.rbac import AccessScope, require_access
from app.services import features, highlights
from app.services.interactions import record_interaction

router = APIRouter(tags=["entries"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Wire formats
# --------------------------------------------------------------------------


class EntryUpdate(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=300)
    risk_level: RiskLevel | None = None
    # The version the client believes it is editing. Required: making it
    # optional would mean the safe path is the one you have to remember.
    expected_version: int
    change_summary: str | None = Field(default=None, max_length=300)


class RevertRequest(BaseModel):
    to_version: int
    change_summary: str | None = Field(default=None, max_length=300)


class SupersedeRequest(BaseModel):
    """A clinician correcting AI or patient-contributed content."""

    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=300)
    risk_level: RiskLevel = RiskLevel.NONE
    reason: str | None = Field(default=None, max_length=300)


class VersionOut(BaseModel):
    id: str
    entry_id: str
    version_number: int
    title: str | None
    content: str
    risk_level: str | None
    edited_by: str
    edited_by_name: str | None
    edited_by_role: str
    edited_at: datetime
    change_summary: str | None
    reverted_from_version: int | None


class DiffLine(BaseModel):
    op: str  # "equal" | "insert" | "delete"
    text: str


class DiffOut(BaseModel):
    entry_id: str
    from_version: int
    to_version: int
    lines: list[DiffLine]
    added: int
    removed: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _author_name(scope: AccessScope, user_id: str) -> str | None:
    if user_id == "system":
        return "Care Note AI"
    user = scope.db.query(User).filter(User.id == user_id).first()
    return user.name if user else None


def _version_out(scope: AccessScope, version: Version) -> VersionOut:
    return VersionOut(
        id=version.id,
        entry_id=version.entry_id,
        version_number=version.version_number,
        title=version.title_snapshot,
        content=version.content_snapshot,
        risk_level=version.risk_level_snapshot,
        edited_by=version.edited_by,
        edited_by_name=_author_name(scope, version.edited_by),
        edited_by_role=str(version.edited_by_role),
        edited_at=version.edited_at,
        change_summary=version.change_summary,
        reverted_from_version=version.reverted_from_version,
    )


def _load_editable(scope: AccessScope, entry_id: str) -> Entry:
    """Fetch an entry this caller is allowed to modify, or refuse.

    Three separate refusals, all server-side:
      * wrong clinic          -> 404 (get_or_404)
      * type this role may not read -> 403
      * type this role may not write -> 403
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)

    if EntryType(entry.type) in AI_SCRIBED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "AI-scribed notes are immutable. Use /entries/{id}/supersede to "
                "record a clinician correction — the original stays visible."
            ),
        )
    scope.assert_can_write_type(entry.type)
    return entry


def _append_version(
    scope: AccessScope,
    entry: Entry,
    *,
    content: str,
    title: str | None,
    risk_level: RiskLevel,
    change_summary: str | None,
    reverted_from: int | None = None,
) -> Version:
    entry.version_number += 1
    version = Version(
        entry_id=entry.id,
        version_number=entry.version_number,
        content_snapshot=content,
        title_snapshot=title,
        risk_level_snapshot=str(risk_level),
        edited_by=scope.user_id,
        edited_by_role=scope.role,
        edited_at=_now(),
        change_summary=change_summary,
        reverted_from_version=reverted_from,
    )
    scope.db.add(version)
    scope.db.flush()

    entry.content = content
    entry.title = title
    entry.risk_level = risk_level
    entry.current_version_id = version.id
    entry.updated_at = _now()
    return version


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.patch("/entries/{entry_id}", response_model=EntryOut)
def update_entry(
    entry_id: str,
    payload: EntryUpdate,
    scope: AccessScope = Depends(require_access()),
) -> EntryOut:
    """Edit an entry. Appends a version; never mutates history."""
    entry = _load_editable(scope, entry_id)

    if payload.expected_version != entry.version_number:
        # 409 with the current state attached, so the client can show the user
        # what they are about to lose rather than just saying "try again".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "version_conflict",
                "message": (
                    "This note changed while you were editing it. Reload to see "
                    "the current version before saving."
                ),
                "expected_version": payload.expected_version,
                "current_version": entry.version_number,
                "current_content": entry.content,
                "last_edited_by": _author_name(scope, entry.author_id),
            },
        )

    try:
        content, markers = prepare_content(payload.content)
        title, title_markers = (
            prepare_content(payload.title) if payload.title else (None, [])
        )
    except ContentTooLongError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc

    risk_level = payload.risk_level or RiskLevel(str(entry.risk_level))
    version = _append_version(
        scope,
        entry,
        content=content,
        title=title,
        risk_level=risk_level,
        change_summary=payload.change_summary or "edited",
    )

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="entry.update",
            target_type="entry",
            target_id=entry.id,
            audit_metadata=json.dumps(
                {
                    "version": version.version_number,
                    "previous_version": version.version_number - 1,
                    "content_length": len(content),
                    "risk_level": str(risk_level),
                    "injection_markers": markers + title_markers,
                }
            ),
        )
    )

    # Existing highlights on this entry are now anchored to an older version.
    # They are marked stale by comparison rather than re-anchored (D-030), and
    # new suggestions are generated against the new text.
    highlights.refresh_entry_highlights(scope.db, entry)
    record_interaction(
        scope.db,
        user_id=scope.user_id,
        user_role=scope.role,
        clinic_id=scope.clinic_id,
        action=InteractionAction.EDIT,
        target_type="entry",
        target_id=entry.id,
        tags=features.entry_level_tags(entry.type, risk_level)
        + features.tag_span(content)[0],
    )
    scope.db.commit()
    scope.db.refresh(entry)

    log_event(
        actor_id=scope.user_id,
        action="entry.update",
        target_type="entry",
        target_id=entry.id,
        clinic_id=scope.clinic_id,
        metadata={"version": entry.version_number, "markers": len(markers)},
    )
    return entry_out(entry, author_name=_author_name(scope, entry.author_id))


@router.get("/entries/{entry_id}/versions", response_model=list[VersionOut])
def list_versions(
    entry_id: str, scope: AccessScope = Depends(require_access())
) -> list[VersionOut]:
    """Full history, newest first. Readable by anyone who may read the entry —
    including for AI-scribed notes, where the history is the proof that the
    machine's words have not been quietly edited."""
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)

    versions = (
        scope.db.query(Version)
        .filter(Version.entry_id == entry.id)
        .order_by(Version.version_number.desc())
        .all()
    )
    return [_version_out(scope, version) for version in versions]


@router.get("/entries/{entry_id}/diff", response_model=DiffOut)
def diff_versions(
    entry_id: str,
    from_version: int,
    to_version: int,
    scope: AccessScope = Depends(require_access()),
) -> DiffOut:
    """"View changes since X" — a line-level diff between two versions.

    Returned as structured operations rather than a rendered string. The client
    turns each line into an element; nothing here emits markup, so a note whose
    text happens to contain angle brackets diffs like any other line (D-015).
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)

    versions = {
        version.version_number: version
        for version in scope.db.query(Version).filter(Version.entry_id == entry.id).all()
    }
    for number in (from_version, to_version):
        if number not in versions:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"version {number} does not exist for this entry",
            )

    before = (versions[from_version].content_snapshot or "").splitlines()
    after = (versions[to_version].content_snapshot or "").splitlines()

    lines: list[DiffLine] = []
    added = removed = 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            lines.extend(DiffLine(op="equal", text=text) for text in before[i1:i2])
        elif op == "delete":
            lines.extend(DiffLine(op="delete", text=text) for text in before[i1:i2])
            removed += i2 - i1
        elif op == "insert":
            lines.extend(DiffLine(op="insert", text=text) for text in after[j1:j2])
            added += j2 - j1
        else:  # replace — shown as a delete block followed by an insert block
            lines.extend(DiffLine(op="delete", text=text) for text in before[i1:i2])
            lines.extend(DiffLine(op="insert", text=text) for text in after[j1:j2])
            removed += i2 - i1
            added += j2 - j1

    return DiffOut(
        entry_id=entry.id,
        from_version=from_version,
        to_version=to_version,
        lines=lines,
        added=added,
        removed=removed,
    )


@router.post("/entries/{entry_id}/revert", response_model=EntryOut)
def revert_entry(
    entry_id: str,
    payload: RevertRequest,
    scope: AccessScope = Depends(require_access()),
) -> EntryOut:
    """Restore an earlier version — as a new version.

    Reverting by rolling `version_number` backwards would erase the record of
    the edit being undone, which is the one thing an audit trail exists to
    prevent. So a revert to v2 from v5 produces v6 whose content equals v2's,
    with `reverted_from_version` recording where it came from.
    """
    entry = _load_editable(scope, entry_id)

    target = (
        scope.db.query(Version)
        .filter(Version.entry_id == entry.id, Version.version_number == payload.to_version)
        .first()
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"version {payload.to_version} does not exist for this entry",
        )
    if payload.to_version == entry.version_number:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that version is already current",
        )

    version = _append_version(
        scope,
        entry,
        content=target.content_snapshot,
        title=target.title_snapshot,
        risk_level=RiskLevel(str(target.risk_level_snapshot or RiskLevel.NONE)),
        change_summary=payload.change_summary
        or f"reverted to v{payload.to_version}",
        reverted_from=payload.to_version,
    )

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="entry.revert",
            target_type="entry",
            target_id=entry.id,
            audit_metadata=json.dumps(
                {
                    "version": version.version_number,
                    "reverted_from_version": payload.to_version,
                    "content_length": len(target.content_snapshot or ""),
                }
            ),
        )
    )
    highlights.refresh_entry_highlights(scope.db, entry)
    scope.db.commit()
    scope.db.refresh(entry)

    log_event(
        actor_id=scope.user_id,
        action="entry.revert",
        target_type="entry",
        target_id=entry.id,
        clinic_id=scope.clinic_id,
        metadata={"to_version": payload.to_version, "new_version": entry.version_number},
    )
    return entry_out(entry, author_name=_author_name(scope, entry.author_id))


@router.post(
    "/entries/{entry_id}/supersede",
    response_model=EntryOut,
    status_code=status.HTTP_201_CREATED,
)
def supersede_entry(
    entry_id: str,
    payload: SupersedeRequest,
    scope: AccessScope = Depends(require_access(Role.CLINICIAN)),
) -> EntryOut:
    """Clinician correction of AI or patient-contributed content (D-007).

    The brief allows either "clinician wins" or "flag for review". This does
    both, and the reason is the product thesis rather than indecision: the
    clinician's version takes effect immediately, so care is never blocked on a
    resolution workflow, while the original stays in the timeline flagged as
    disputed, because the fact that the machine said something different is
    itself clinically interesting. Quietly deleting the disagreement is how a
    system trains its users to stop trusting it.
    """
    original = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(original.patient_id)
    scope.assert_can_view_type(original.type)

    if EntryType(original.type) not in AI_SCRIBED_TYPES and str(
        original.author_role
    ) != str(Role.PATIENT):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Supersede records a clinician correction of AI or "
                "patient-contributed content. Edit clinician sections directly."
            ),
        )

    patient = scope.get_or_404(Patient, original.patient_id)
    content, markers = prepare_content(payload.content)
    title, _ = prepare_content(payload.title) if payload.title else (None, [])

    correction = Entry(
        patient_id=patient.id,
        clinic_id=scope.clinic_id,
        author_role=scope.role,
        author_id=scope.user_id,
        type=EntryType.CLINICIAN_SECTION,
        title=title or "Clinician correction",
        content=content,
        risk_level=payload.risk_level,
        version_number=1,
        supersedes_entry_id=original.id,
    )
    scope.db.add(correction)
    scope.db.flush()
    correction.provenance_pointer = entry_pointer(correction.id)

    version = Version(
        entry_id=correction.id,
        version_number=1,
        content_snapshot=content,
        title_snapshot=correction.title,
        risk_level_snapshot=str(payload.risk_level),
        edited_by=scope.user_id,
        edited_by_role=scope.role,
        change_summary=payload.reason or f"supersedes {original.id}",
    )
    scope.db.add(version)
    scope.db.flush()
    correction.current_version_id = version.id

    # The disputed entry keeps its content and gains a flag. Nothing is deleted.
    original.conflict_flagged = True

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="entry.supersede",
            target_type="entry",
            target_id=correction.id,
            audit_metadata=json.dumps(
                {
                    "supersedes_entry_id": original.id,
                    "superseded_type": str(original.type),
                    "content_length": len(content),
                    "injection_markers": markers,
                }
            ),
        )
    )
    highlights.refresh_entry_highlights(scope.db, correction)
    record_interaction(
        scope.db,
        user_id=scope.user_id,
        user_role=scope.role,
        clinic_id=scope.clinic_id,
        action=InteractionAction.EDIT,
        target_type="entry",
        target_id=original.id,
        tags=features.entry_level_tags(original.type, original.risk_level)
        + features.tag_span(content)[0]
        + ["signal:clinician_correction"],
    )
    scope.db.commit()
    scope.db.refresh(correction)

    log_event(
        actor_id=scope.user_id,
        action="entry.supersede",
        target_type="entry",
        target_id=correction.id,
        clinic_id=scope.clinic_id,
        metadata={"supersedes": original.id, "type": str(original.type)},
    )
    return entry_out(correction, author_name=_author_name(scope, scope.user_id))
