"""The access policy, as data.

Kept separate from enforcement (`rbac.py`) on purpose: the rules a reviewer
wants to audit are here, in one table they can read top to bottom, and the
mechanism that applies them is somewhere else. Changing who-can-see-what should
never require touching route code.

Sources: the candidate brief's minimum access rules, plus the two judgment calls
recorded in DECISIONS.md (D-004 staff cannot view clinician_sections; D-005
staff can view AI-scribed notes).
"""

from __future__ import annotations

from app.core.enums import AI_SCRIBED_TYPES, EntryType, Role

# Which entry types each role may READ.
VIEWABLE_TYPES: dict[Role, frozenset[EntryType]] = {
    Role.PATIENT: frozenset(
        {
            EntryType.PATIENT_SUMMARY,
            EntryType.PATIENT_INSTRUCTION,
            EntryType.PATIENT_NOTE,
        }
    ),
    Role.STAFF: frozenset(
        {
            EntryType.STAFF_NOTE,
            EntryType.PATIENT_NOTE,
            EntryType.PATIENT_SUMMARY,
            EntryType.PATIENT_INSTRUCTION,
            EntryType.SYSTEM_EVENT,
            *AI_SCRIBED_TYPES,
        }
    ),
    Role.CLINICIAN: frozenset(set(EntryType)),
    Role.ADMIN: frozenset(set(EntryType)),
}

# Which entry types each role may CREATE or EDIT.
# This is what makes "clinicians cannot overwrite staff notes" true by
# construction rather than by a check someone might forget to write.
WRITABLE_TYPES: dict[Role, frozenset[EntryType]] = {
    Role.PATIENT: frozenset({EntryType.PATIENT_NOTE}),
    Role.STAFF: frozenset({EntryType.STAFF_NOTE}),
    Role.CLINICIAN: frozenset(
        {
            EntryType.CLINICIAN_SECTION,
            EntryType.PATIENT_INSTRUCTION,
            EntryType.PATIENT_SUMMARY,
        }
    ),
    # Admin is oversight, not authorship. Read everything in the clinic, write
    # no clinical content — so an admin account cannot quietly alter the record.
    Role.ADMIN: frozenset(),
}

# Roles whose comments are internal (invisible to a patient).
INTERNAL_COMMENT_ROLES: frozenset[Role] = frozenset({Role.STAFF, Role.CLINICIAN, Role.ADMIN})

# Roles that may see internal comment threads at all.
CAN_VIEW_INTERNAL_COMMENTS: frozenset[Role] = frozenset(
    {Role.STAFF, Role.CLINICIAN, Role.ADMIN}
)

# Roles that may accept/reject a suggested highlight. The brief makes this a
# clinician affordance; staff can surface but not confirm.
CAN_DECIDE_HIGHLIGHTS: frozenset[Role] = frozenset({Role.CLINICIAN})


def can_view_type(role: Role, entry_type: EntryType) -> bool:
    return entry_type in VIEWABLE_TYPES.get(role, frozenset())


def can_write_type(role: Role, entry_type: EntryType) -> bool:
    return entry_type in WRITABLE_TYPES.get(role, frozenset())


def viewable_types_for(role: Role) -> frozenset[EntryType]:
    return VIEWABLE_TYPES.get(role, frozenset())


def can_view_internal_comments(role: Role) -> bool:
    return role in CAN_VIEW_INTERNAL_COMMENTS


def can_decide_highlights(role: Role) -> bool:
    return role in CAN_DECIDE_HIGHLIGHTS
