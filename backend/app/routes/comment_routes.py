"""Inline collaboration: threaded comments, mentions, and assigned tasks.

This is the layer that turns a record into a conversation, so the rules about
who can hear what matter more here than anywhere else in the app.

**Comments are internal by default and patients never see them.** The brief is
explicit: a patient cannot view internal staff/clinician comments. That is
enforced twice over — a patient token is refused at the route, and every comment
written by a staff/clinician/admin role is stamped `is_internal=True` at
creation so a future route that forgets the role check still cannot leak one.

**Mentions are resolved server-side against the clinic's own users.** A client
that posts `@somebody_in_another_clinic` gets that mention dropped, not
delivered. The mention list is stored as user ids rather than the typed text, so
renaming a user does not orphan the notification, and so the client renders
mentions from data rather than by pattern-matching prose it was handed.

**Comment bodies pass through `prepare_content()` like any other authored
text**, and are stored verbatim with injection markers recorded as audit
metadata. The client renders them as text children and parses mentions into
elements — never into markup (D-015).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.audit_logging import log_event
from app.core.enums import CommentStatus, InteractionAction, Role, TaskStatus
from app.core.sanitization import ContentTooLongError, prepare_content
from app.core.timeutil import UtcDateTime
from app.models import AuditLog, Comment, Entry, Patient, Task, User
from app.security import policy
from app.security.rbac import AccessScope, require_access
from app.services import features, highlights
from app.services.interactions import record_interaction

router = APIRouter(tags=["collaboration"])

# Roles that may participate in the internal thread at all. A patient's voice
# reaches the record through `patient_note` entries and AI sessions, which are
# first-class timeline content — not through a comment thread they cannot read
# the rest of.
COMMENTING_ROLES = (Role.STAFF, Role.CLINICIAN, Role.ADMIN)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Wire formats
# --------------------------------------------------------------------------


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5_000)
    parent_comment_id: str | None = None
    # User ids, from the mention picker. Validated against this clinic.
    mentions: list[str] = Field(default_factory=list)


class CommentOut(BaseModel):
    id: str
    entry_id: str
    parent_comment_id: str | None
    author_id: str
    author_name: str | None
    author_role: str
    body: str
    mentions: list[str]
    mention_names: dict[str, str]
    status: str
    is_internal: bool
    resolved_by: str | None
    resolved_by_name: str | None
    resolved_at: UtcDateTime | None
    created_at: UtcDateTime
    replies: list["CommentOut"] = Field(default_factory=list)


class TaskCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    assigned_to: str | None = None
    entry_id: str | None = None
    comment_id: str | None = None
    due_at: UtcDateTime | None = None


class TaskStatusUpdate(BaseModel):
    status: TaskStatus


class TaskOut(BaseModel):
    id: str
    patient_id: str
    entry_id: str | None
    comment_id: str | None
    description: str
    assigned_to: str | None
    assigned_to_name: str | None
    assigned_to_role: str | None
    assigned_by: str
    assigned_by_name: str | None
    status: str
    due_at: UtcDateTime | None
    created_at: UtcDateTime
    closed_at: UtcDateTime | None


class MentionableUser(BaseModel):
    id: str
    name: str
    username: str
    role: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _names(scope: AccessScope) -> dict[str, str]:
    return {
        user.id: user.name
        for user in scope.db.query(User).filter(User.clinic_id == scope.clinic_id).all()
    }


def _decode(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _comment_out(comment: Comment, names: dict[str, str]) -> CommentOut:
    mentions = _decode(comment.mentions)
    return CommentOut(
        id=comment.id,
        entry_id=comment.entry_id,
        parent_comment_id=comment.parent_comment_id,
        author_id=comment.author_id,
        author_name=names.get(comment.author_id),
        author_role=str(comment.author_role),
        body=comment.body,
        mentions=mentions,
        mention_names={uid: names.get(uid, "unknown") for uid in mentions},
        status=str(comment.status),
        is_internal=bool(comment.is_internal),
        resolved_by=comment.resolved_by,
        resolved_by_name=names.get(comment.resolved_by or ""),
        resolved_at=comment.resolved_at,
        created_at=comment.created_at,
    )


def _entry_for_comment(scope: AccessScope, entry_id: str) -> Entry:
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)
    return entry


def _refuse_patients(scope: AccessScope) -> None:
    """The patient-facing half of the brief's access rule, at the route edge.

    Belt and braces with `is_internal`: this refuses the read outright, so a
    patient token never even reaches the query that would have filtered.
    """
    if scope.role is Role.PATIENT or not policy.can_view_internal_comments(scope.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal discussion is not visible to this role",
        )


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------


@router.get("/entries/{entry_id}/comments", response_model=list[CommentOut])
def list_comments(
    entry_id: str, scope: AccessScope = Depends(require_access())
) -> list[CommentOut]:
    """Threads on one entry, roots newest first with replies nested oldest first.

    Replies read oldest-first because a thread is a conversation and reading it
    backwards makes no sense; roots read newest-first because the newest
    question is the one most likely to still be waiting on someone.
    """
    _refuse_patients(scope)
    entry = _entry_for_comment(scope, entry_id)

    rows = (
        scope.query(Comment)
        .filter(Comment.entry_id == entry.id)
        .order_by(Comment.created_at.asc())
        .all()
    )
    names = _names(scope)
    by_id = {row.id: _comment_out(row, names) for row in rows}

    roots: list[CommentOut] = []
    for row in rows:
        out = by_id[row.id]
        parent = by_id.get(row.parent_comment_id or "")
        if parent is not None:
            parent.replies.append(out)
        else:
            roots.append(out)
    roots.reverse()
    return roots


@router.post(
    "/entries/{entry_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_comment(
    entry_id: str,
    payload: CommentCreate,
    scope: AccessScope = Depends(require_access(*COMMENTING_ROLES)),
) -> CommentOut:
    entry = _entry_for_comment(scope, entry_id)

    if payload.parent_comment_id:
        parent = scope.get_or_404(Comment, payload.parent_comment_id)
        if parent.entry_id != entry.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="parent comment belongs to a different entry",
            )

    try:
        body, markers = prepare_content(payload.body)
    except ContentTooLongError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc

    # Mentions are only real if the person exists, in this clinic, and is not a
    # patient login. Anything else is silently dropped rather than stored as a
    # mention that will never resolve to a name.
    valid_mentions = [
        user.id
        for user in scope.query(User).filter(User.id.in_(payload.mentions or [""])).all()
        # `!=`, not `is not`. `User.role` is stored in a String column, so a
        # loaded row yields a plain `str` and `is not` was always true — the
        # filter dropped nothing. See DECISIONS.md D-055.
        if user.role != Role.PATIENT
    ]

    comment = Comment(
        entry_id=entry.id,
        clinic_id=scope.clinic_id,
        parent_comment_id=payload.parent_comment_id,
        author_id=scope.user_id,
        author_role=scope.role,
        body=body,
        mentions=json.dumps(valid_mentions),
        status=CommentStatus.OPEN,
        is_internal=scope.role in policy.INTERNAL_COMMENT_ROLES,
    )
    scope.db.add(comment)
    scope.db.flush()

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="comment.create",
            target_type="comment",
            target_id=comment.id,
            audit_metadata=json.dumps(
                {
                    "entry_id": entry.id,
                    "body_length": len(body),
                    "mentions": len(valid_mentions),
                    "injection_markers": markers,
                }
            ),
        )
    )
    # Commenting on a passage is one of the three signals the brief names as
    # evidence that a clinician cares about this kind of content.
    record_interaction(
        scope.db,
        user_id=scope.user_id,
        user_role=scope.role,
        clinic_id=scope.clinic_id,
        action=InteractionAction.COMMENT,
        target_type="entry",
        target_id=entry.id,
        tags=features.entry_level_tags(entry.type, entry.risk_level)
        + features.tag_span(entry.content or "")[0],
    )
    scope.db.commit()
    scope.db.refresh(comment)

    log_event(
        actor_id=scope.user_id,
        action="comment.create",
        target_type="comment",
        target_id=comment.id,
        clinic_id=scope.clinic_id,
        metadata={"entry_id": entry.id, "mentions": len(valid_mentions)},
    )
    return _comment_out(comment, _names(scope))


@router.post("/comments/{comment_id}/resolve", response_model=CommentOut)
def resolve_comment(
    comment_id: str,
    scope: AccessScope = Depends(require_access(*COMMENTING_ROLES)),
) -> CommentOut:
    return _set_comment_status(scope, comment_id, CommentStatus.RESOLVED)


@router.post("/comments/{comment_id}/unresolve", response_model=CommentOut)
def unresolve_comment(
    comment_id: str,
    scope: AccessScope = Depends(require_access(*COMMENTING_ROLES)),
) -> CommentOut:
    """Reopening matters as much as resolving: "handled" is a claim, and a
    colleague must be able to disagree with it without starting a new thread
    that loses the history of the first."""
    return _set_comment_status(scope, comment_id, CommentStatus.OPEN)


def _set_comment_status(
    scope: AccessScope, comment_id: str, new_status: CommentStatus
) -> CommentOut:
    comment = scope.get_or_404(Comment, comment_id)
    entry = _entry_for_comment(scope, comment.entry_id)

    comment.status = new_status
    if new_status is CommentStatus.RESOLVED:
        comment.resolved_by = scope.user_id
        comment.resolved_at = _now()
    else:
        comment.resolved_by = None
        comment.resolved_at = None

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action=f"comment.{new_status}",
            target_type="comment",
            target_id=comment.id,
            audit_metadata=json.dumps({"entry_id": comment.entry_id}),
        )
    )
    if new_status is CommentStatus.RESOLVED:
        record_interaction(
            scope.db,
            user_id=scope.user_id,
            user_role=scope.role,
            clinic_id=scope.clinic_id,
            action=InteractionAction.RESOLVE_COMMENT,
            target_type="comment",
            target_id=comment.id,
            tags=features.entry_level_tags(entry.type, entry.risk_level),
        )
    # An unresolved comment counts as an open action on the Glance View, so
    # closing one changes what should be surfaced.
    highlights.refresh_entry_highlights(scope.db, entry)
    scope.db.commit()
    scope.db.refresh(comment)

    log_event(
        actor_id=scope.user_id,
        action=f"comment.{new_status}",
        target_type="comment",
        target_id=comment.id,
        clinic_id=scope.clinic_id,
        metadata={"entry_id": comment.entry_id},
    )
    return _comment_out(comment, _names(scope))


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


@router.get("/patients/{patient_id}/tasks", response_model=list[TaskOut])
def list_tasks(
    patient_id: str, scope: AccessScope = Depends(require_access())
) -> list[TaskOut]:
    """Open work first, then everything else, newest first within each group."""
    _refuse_patients(scope)
    scope.assert_patient_visible(patient_id)
    scope.get_or_404(Patient, patient_id)

    rows = (
        scope.query(Task)
        .filter(Task.patient_id == patient_id)
        .order_by(Task.created_at.desc())
        .all()
    )
    names = _names(scope)
    open_states = {str(TaskStatus.OPEN), str(TaskStatus.IN_PROGRESS)}
    rows.sort(key=lambda task: str(task.status) not in open_states)
    return [_task_out(task, names) for task in rows]


@router.post(
    "/patients/{patient_id}/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    patient_id: str,
    payload: TaskCreate,
    scope: AccessScope = Depends(require_access(*COMMENTING_ROLES)),
) -> TaskOut:
    """"Assign to staff" — the open action that shows up on the Glance View."""
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)

    entry = None
    if payload.entry_id:
        entry = _entry_for_comment(scope, payload.entry_id)
    if payload.comment_id:
        scope.get_or_404(Comment, payload.comment_id)

    assignee: User | None = None
    if payload.assigned_to:
        # Clinic-scoped by `scope.query`, so a task cannot be assigned across a
        # tenancy boundary even if a client sends a valid-looking id.
        assignee = scope.query(User).filter(User.id == payload.assigned_to).first()
        # `==`, not `is` — see DECISIONS.md D-055. With the identity check this
        # guard never fired and a task could be assigned to a patient login.
        if assignee is None or assignee.role == Role.PATIENT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assignee must be a staff, clinician or admin user in this clinic",
            )

    description, markers = prepare_content(payload.description)
    task = Task(
        clinic_id=scope.clinic_id,
        patient_id=patient.id,
        entry_id=payload.entry_id,
        comment_id=payload.comment_id,
        description=description,
        assigned_to=assignee.id if assignee else None,
        assigned_to_role=assignee.role if assignee else None,
        assigned_by=scope.user_id,
        status=TaskStatus.OPEN,
        due_at=payload.due_at,
    )
    scope.db.add(task)
    scope.db.flush()

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="task.create",
            target_type="task",
            target_id=task.id,
            audit_metadata=json.dumps(
                {
                    "patient_id": patient.id,
                    "entry_id": payload.entry_id,
                    "assigned_to_role": str(assignee.role) if assignee else None,
                    "injection_markers": markers,
                }
            ),
        )
    )
    if entry is not None:
        highlights.refresh_entry_highlights(scope.db, entry)
    scope.db.commit()
    scope.db.refresh(task)

    log_event(
        actor_id=scope.user_id,
        action="task.create",
        target_type="task",
        target_id=task.id,
        clinic_id=scope.clinic_id,
        metadata={"patient_id": patient.id, "assigned": bool(assignee)},
    )
    return _task_out(task, _names(scope))


@router.post("/tasks/{task_id}/status", response_model=TaskOut)
def update_task_status(
    task_id: str,
    payload: TaskStatusUpdate,
    scope: AccessScope = Depends(require_access(*COMMENTING_ROLES)),
) -> TaskOut:
    task = scope.get_or_404(Task, task_id)
    task.status = payload.status
    task.closed_at = (
        _now() if payload.status in (TaskStatus.DONE, TaskStatus.CANCELLED) else None
    )

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="task.status",
            target_type="task",
            target_id=task.id,
            audit_metadata=json.dumps({"status": str(payload.status)}),
        )
    )
    if task.entry_id:
        entry = scope.query(Entry).filter(Entry.id == task.entry_id).first()
        if entry is not None:
            highlights.refresh_entry_highlights(scope.db, entry)
    scope.db.commit()
    scope.db.refresh(task)

    log_event(
        actor_id=scope.user_id,
        action="task.status",
        target_type="task",
        target_id=task.id,
        clinic_id=scope.clinic_id,
        metadata={"status": str(payload.status)},
    )
    return _task_out(task, _names(scope))


def _task_out(task: Task, names: dict[str, str]) -> TaskOut:
    return TaskOut(
        id=task.id,
        patient_id=task.patient_id,
        entry_id=task.entry_id,
        comment_id=task.comment_id,
        description=task.description,
        assigned_to=task.assigned_to,
        assigned_to_name=names.get(task.assigned_to or ""),
        assigned_to_role=str(task.assigned_to_role) if task.assigned_to_role else None,
        assigned_by=task.assigned_by,
        assigned_by_name=names.get(task.assigned_by),
        status=str(task.status),
        due_at=task.due_at,
        created_at=task.created_at,
        closed_at=task.closed_at,
    )


# --------------------------------------------------------------------------
# Mention directory
# --------------------------------------------------------------------------


@router.get("/clinic/users", response_model=list[MentionableUser])
def list_mentionable_users(
    scope: AccessScope = Depends(require_access(*COMMENTING_ROLES)),
) -> list[MentionableUser]:
    """Who can be mentioned or assigned. Clinic-scoped; patients excluded.

    Patients are omitted because a mention is a request for someone to act on
    an internal thread, and the patient cannot read that thread. Offering them
    in the picker would be an invitation to write into a void.
    """
    users = (
        scope.query(User)
        .filter(User.role != Role.PATIENT)
        .order_by(User.name)
        .all()
    )
    return [
        MentionableUser(
            id=user.id, name=user.name, username=user.username, role=str(user.role)
        )
        for user in users
    ]
