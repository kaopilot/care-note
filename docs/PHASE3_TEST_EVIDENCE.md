# Phase 3 — Test Run Evidence

Captured 2026-08-26 16:05 UTC on Python 3.12.3, from a clean checkout.
Reproduce with `pytest tests/ -v` from the repository root. No API key or network required.

Note: do not pass `-p no:logging`. `test_llm_chokepoint.py` uses pytest's
`caplog` fixture to prove no prompt text reaches the logs, and disabling the
logging plugin errors that test rather than skipping it.

## Summary — full suite

```

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
256 passed, 1 warning in 18.84s
```

## The four required files

```
============================= test session starts ==============================
tests/test_rbac_scope.py::test_staff_cannot_author_a_clinician_section PASSED [  1%]
tests/test_rbac_scope.py::test_clinician_cannot_author_a_staff_note PASSED [  2%]
tests/test_rbac_scope.py::test_staff_cannot_edit_a_clinician_section PASSED [  3%]
tests/test_rbac_scope.py::test_clinician_cannot_edit_a_staff_note PASSED [  4%]
tests/test_rbac_scope.py::test_neither_role_can_revert_the_others_notes PASSED [  6%]
tests/test_rbac_scope.py::test_admin_can_read_everything_in_clinic_but_author_nothing PASSED [  7%]
tests/test_rbac_scope.py::test_staff_cannot_fetch_a_clinician_section_by_id PASSED [  8%]
tests/test_rbac_scope.py::test_staff_timeline_omits_clinician_sections_entirely PASSED [  9%]
tests/test_rbac_scope.py::test_staff_cannot_read_clinician_section_history_or_diffs PASSED [ 10%]
tests/test_rbac_scope.py::test_staff_cannot_reach_a_clinician_section_through_provenance PASSED [ 12%]
tests/test_rbac_scope.py::test_patient_cannot_read_internal_comments PASSED [ 13%]
tests/test_rbac_scope.py::test_patient_cannot_write_into_an_internal_thread PASSED [ 14%]
tests/test_rbac_scope.py::test_patient_cannot_fetch_a_raw_ai_scribed_note PASSED [ 15%]
tests/test_rbac_scope.py::test_patient_timeline_contains_only_patient_facing_types PASSED [ 16%]
tests/test_rbac_scope.py::test_patient_cannot_open_the_clinical_glance_view_or_task_list PASSED [ 18%]
tests/test_rbac_scope.py::test_patient_cannot_read_another_patient_in_the_same_clinic PASSED [ 19%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/patients/patient-a1-None] PASSED [ 20%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/patients/patient-a1/entries-None] PASSED [ 21%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/entries/entry-a1-clin-None] PASSED [ 22%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/entries/entry-a1-clin/versions-None] PASSED [ 24%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/entries/entry-a1-staff/comments-None] PASSED [ 25%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/patients/patient-a1/highlights-None] PASSED [ 26%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/patients/patient-a1/glance-None] PASSED [ 27%]
tests/test_rbac_scope.py::test_clinic_b_cannot_read_clinic_a_data[get-/patients/patient-a1/tasks-None] PASSED [ 28%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[post-/patients/patient-a1/entries-body0] PASSED [ 30%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[patch-/entries/entry-a1-clin-body1] PASSED [ 31%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[post-/entries/entry-a1-clin/revert-body2] PASSED [ 32%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[post-/entries/entry-a1-staff/comments-body3] PASSED [ 33%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[post-/patients/patient-a1/tasks-body4] PASSED [ 34%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[post-/entries/entry-a1-clin/highlights-body5] PASSED [ 36%]
tests/test_rbac_scope.py::test_clinic_b_cannot_write_to_clinic_a_data[post-/patients/patient-a1/scribe-body6] PASSED [ 37%]
tests/test_rbac_scope.py::test_staff_of_clinic_b_cannot_list_clinic_a_patients PASSED [ 38%]
tests/test_rbac_scope.py::test_a_valid_pointer_does_not_cross_a_clinic_boundary PASSED [ 39%]
tests/test_rbac_scope.py::test_clinic_id_is_taken_from_the_token_not_the_request PASSED [ 40%]
tests/test_rbac_scope.py::test_script_payload_in_a_note_is_returned_as_the_literal_text_written PASSED [ 42%]
tests/test_rbac_scope.py::test_script_payload_in_a_comment_is_returned_as_the_literal_text_written PASSED [ 43%]
tests/test_rbac_scope.py::test_the_payload_is_flagged_in_the_audit_trail_without_being_stored_there PASSED [ 44%]
tests/test_revision_history.py::test_a_new_entry_starts_at_version_one_with_a_version_row PASSED [ 45%]
tests/test_revision_history.py::test_each_edit_increments_the_version_number PASSED [ 46%]
tests/test_revision_history.py::test_every_version_is_retained_with_its_own_snapshot PASSED [ 48%]
tests/test_revision_history.py::test_a_version_records_who_made_it_and_in_what_role PASSED [ 49%]
tests/test_revision_history.py::test_view_changes_since_x_reports_the_actual_difference PASSED [ 50%]
tests/test_revision_history.py::test_revert_returns_content_to_the_prior_state PASSED [ 51%]
tests/test_revision_history.py::test_revert_moves_the_version_forward_never_backward PASSED [ 53%]
tests/test_revision_history.py::test_the_versions_that_were_reverted_away_from_survive PASSED [ 54%]
tests/test_revision_history.py::test_a_revert_is_itself_revertible PASSED [ 55%]
tests/test_revision_history.py::test_reverting_to_the_current_version_is_refused PASSED [ 56%]
tests/test_revision_history.py::test_reverting_to_a_version_that_does_not_exist_is_refused PASSED [ 57%]
tests/test_revision_history.py::test_version_rows_are_never_deleted_by_any_of_this PASSED [ 59%]
tests/test_revision_history.py::test_the_audit_log_records_who_changed_what_and_when PASSED [ 60%]
tests/test_revision_history.py::test_the_audit_log_records_the_version_transition_of_each_change PASSED [ 61%]
tests/test_revision_history.py::test_the_audit_log_never_contains_the_note_body PASSED [ 62%]
tests/test_revision_history.py::test_a_reverted_body_does_not_leak_into_the_log_either PASSED [ 63%]
tests/test_revision_history.py::test_history_is_readable_by_a_role_that_may_read_the_entry PASSED [ 65%]
tests/test_revision_history.py::test_ai_scribed_notes_keep_a_history_that_shows_they_were_not_edited PASSED [ 66%]
tests/test_highlight_provenance.py::test_highlights_are_generated_across_the_chart PASSED [ 67%]
tests/test_highlight_provenance.py::test_at_least_one_highlight_is_sourced_from_an_ai_scribed_note PASSED [ 68%]
tests/test_highlight_provenance.py::test_an_ai_sourced_highlight_says_so_in_its_reason PASSED [ 69%]
tests/test_highlight_provenance.py::test_every_highlight_has_a_reason_and_a_pointer PASSED [ 71%]
tests/test_highlight_provenance.py::test_every_pointer_resolves_to_a_real_entry_and_span PASSED [ 72%]
tests/test_highlight_provenance.py::test_every_pointer_carries_a_span_not_merely_an_entry PASSED [ 73%]
tests/test_highlight_provenance.py::test_the_resolved_text_is_the_text_the_card_displayed PASSED [ 74%]
tests/test_highlight_provenance.py::test_the_resolved_span_is_really_inside_the_source_entry_content PASSED [ 75%]
tests/test_highlight_provenance.py::test_glance_view_highlights_resolve_too PASSED [ 77%]
tests/test_highlight_provenance.py::test_a_manual_highlight_inside_an_ai_note_resolves_to_that_note PASSED [ 78%]
tests/test_highlight_provenance.py::test_an_ai_entry_points_back_to_the_session_that_produced_it PASSED [ 79%]
tests/test_highlight_provenance.py::test_the_transcript_segments_behind_a_session_resolve_individually PASSED [ 80%]
tests/test_highlight_provenance.py::test_a_dangling_pointer_raises_rather_than_resolving_empty PASSED [ 81%]
tests/test_highlight_provenance.py::test_a_span_beyond_the_end_of_the_entry_raises PASSED [ 83%]
tests/test_highlight_provenance.py::test_resolution_obeys_the_same_role_rules_as_reading PASSED [ 84%]
tests/test_highlight_provenance.py::test_a_pointer_does_not_resolve_across_a_clinic_boundary PASSED [ 85%]
tests/test_highlight_provenance.py::test_an_edited_entry_leaves_its_highlight_stale_but_still_resolvable PASSED [ 86%]
tests/test_concurrent_edits.py::test_two_roles_editing_different_sections_do_not_overwrite_each_other PASSED [ 87%]
tests/test_concurrent_edits.py::test_each_section_keeps_its_own_independent_version_chain PASSED [ 89%]
tests/test_concurrent_edits.py::test_rbac_prevents_most_collisions_before_locking_is_needed PASSED [ 90%]
tests/test_concurrent_edits.py::test_the_second_writer_on_the_same_section_is_refused_not_silently_applied PASSED [ 91%]
tests/test_concurrent_edits.py::test_the_refusal_tells_the_loser_what_they_are_about_to_overwrite PASSED [ 92%]
tests/test_concurrent_edits.py::test_the_loser_succeeds_after_reloading PASSED [ 93%]
tests/test_concurrent_edits.py::test_a_refused_write_leaves_no_trace_at_all PASSED [ 95%]
tests/test_concurrent_edits.py::test_parallel_edits_to_different_sections_both_succeed PASSED [ 96%]
tests/test_concurrent_edits.py::test_the_loser_of_a_real_race_gets_a_conflict_not_a_crash PASSED [ 97%]
tests/test_concurrent_edits.py::test_a_real_race_never_loses_a_write_or_duplicates_a_version PASSED [ 98%]
tests/test_concurrent_edits.py::test_parallel_reverts_never_crash_or_fork_the_history PASSED [100%]
=============================== warnings summary ===============================
======================== 83 passed, 1 warning in 8.20s =========================
```
