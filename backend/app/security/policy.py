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

# --------------------------------------------------------------------------
# Patient-facing content is a higher severity class
# --------------------------------------------------------------------------
#
# Showing a clinician a hallucinated line is a bad day: internal notes carry
# provenance, get audited, and sit in front of someone trained to disbelieve
# them. Showing a PATIENT a hallucinated line is a different category of harm —
# there is no second reader, no provenance rail they can open, and the reader
# has no basis to doubt it. So the two are not governed by the same rules.
#
# The rule here is structural rather than procedural. Rather than generating
# patient-facing text and then requiring an approval step — an approval step
# being a thing under time pressure people click through — **no generated text
# can become patient-facing at all**:
#
#   * `PATIENT_FACING_TYPES` is writable only by `Role.CLINICIAN`
#     (see WRITABLE_TYPES above). Not staff, not admin, and not system.
#   * `Role.SYSTEM` appears in no WRITABLE_TYPES entry, so `can_write_type` is
#     False for it for every type, patient-facing or not.
#   * The AI scribe only ever writes `AI_SCRIBED_TYPES`, and
#     `assert_never_patient_facing()` below is called on that mapping so the
#     two sets cannot silently start overlapping.
#   * `AI_SCRIBED_TYPES` is absent from `VIEWABLE_TYPES[PATIENT]`, so even a
#     mislabelled AI note is unreadable by the patient.
#
# A clinician can of course read an AI summary and choose to write an
# instruction based on it. That is the intended path, and the human authorship
# is real rather than a rubber stamp: they type the words. See DECISIONS.md
# D-067.
PATIENT_FACING_TYPES: frozenset[EntryType] = frozenset(
    {EntryType.PATIENT_SUMMARY, EntryType.PATIENT_INSTRUCTION}
)

# The only role permitted to author them.
PATIENT_FACING_AUTHOR_ROLES: frozenset[Role] = frozenset({Role.CLINICIAN})


class PatientFacingAuthorshipError(RuntimeError):
    """Raised when generated content would become patient-visible.

    A crash, deliberately, rather than a flag or a queue. There is no
    circumstance in this build where the right answer is to write the row and
    warn about it.
    """


def is_patient_facing(entry_type: EntryType) -> bool:
    return entry_type in PATIENT_FACING_TYPES


def assert_never_patient_facing(entry_types) -> None:
    """Guard for any code path that writes entries on a machine's authority.

    Called by the AI scribe and the voice-capture pipeline against the types
    they are about to use, so a future edit that adds `patient_summary` to a
    generation path fails loudly at import/run time instead of quietly shipping
    model output to a patient.
    """
    offending = sorted(str(t) for t in entry_types if is_patient_facing(t))
    if offending:
        raise PatientFacingAuthorshipError(
            "generated content may not use patient-facing entry types: "
            + ", ".join(offending)
        )


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
