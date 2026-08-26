# Phase-doc amendments — security

The phase prompts in the project folder are read-only from inside the build, so
these are the exact lines to paste in. Each is written to drop straight into the
existing document without rewording.

The underlying work is already **done** in Phase 0 — this is about making sure
the later phases, which may be handed to a fresh LLM session, inherit the
constraints instead of rediscovering or contradicting them.

---

## 1. `00_shared_context.md` — add under "Non-negotiable rules"

Paste after the **PHI redaction** block:

> **Content safety (stored XSS):**
> - Note and comment bodies are untrusted, multi-author content. They are stored
>   as **plain text** and are **never rendered as HTML**. React's default text
>   escaping is the defence.
> - Never use `dangerouslySetInnerHTML`, `innerHTML =`, or `outerHTML =` on
>   any user-supplied content. A test scans the frontend and fails the build if
>   these appear.
> - Do **not** HTML-escape or tag-strip content on write. Clinical prose
>   legitimately contains `BP <120/80` and `dose <5mg`; escaping on write
>   double-escapes on render, and tag-stripping can silently delete a dose
>   limit. Escaping belongs at the render boundary — use
>   `app.core.sanitization.escape_html()` only for surfaces that genuinely emit
>   HTML (PDF export, emailed summaries).
> - All write paths call `app.core.sanitization.prepare_content()`, which
>   normalises the text and returns injection markers to record as **metadata**
>   (never the content).
> - If a Markdown renderer is introduced, it MUST be configured with raw HTML
>   disabled (`html: false`). See DECISIONS.md D-015.
>
> **Session handling:**
> - The browser carries its token in an **httpOnly cookie**
>   (`SameSite=lax`, 60-minute TTL). Never write the token to `localStorage` or
>   `sessionStorage` — one stored-XSS bug would become durable account takeover.
> - `Authorization: Bearer` remains available for tests and non-browser clients.
> - There is no refresh flow. Do not add one without recording the decision.
>   See DECISIONS.md D-016.

---

## 2. `03_phase2_core_features.md` § 2.5 — Inline collaboration

Append to the task list:

> - Comment bodies pass through `prepare_content()` before storage; injection
>   markers are recorded as audit metadata, never the body text. Comments render
>   as plain text — no HTML, no Markdown-with-HTML. @mention parsing must
>   operate on the stored plain text and produce React elements, not an HTML
>   string.

---

## 3. `03_phase2_core_features.md` § 2.6 — Revision history + revert

Append to the task list:

> - Entry content passes through `prepare_content()` on every create and every
>   edit, so a payload cannot enter the record via a later version having
>   bypassed the check on the first. Diff rendering ("view changes since X") is
>   plain-text diffing — do not render diffs as HTML.

---

## 4. `03_phase2_core_features.md` — Exit criteria

Add two boxes:

> - [ ] `test_frontend_never_renders_raw_html` still passes after all Phase 2 UI
>       work (this is the real XSS defence, and Phase 2 is where it is most
>       likely to be broken)
> - [ ] Every content write path calls `prepare_content()`

---

## 5. `04_phase3_tests.md` — add to `test_rbac_scope.py`

> - Assert a stored payload containing `<script>` in a note or comment body is
>   returned by the API as the literal text that was written — neither executed
>   nor silently altered. This pins both halves of D-015 at once.

---

## 6. `07_phase6_docs_demo.md` § 1 — Technical brief

Append:

> - Include the security-posture table from `ARCHITECTURE.md`, with its three
>   statuses (implemented / documented decision / known gap) intact. The
>   disclosed gaps are part of the argument, not an embarrassment to trim — a
>   control whose edges nobody knows is worse than a weaker one everybody
>   understands, and the rubric explicitly scores explicit discussion of
>   trade-offs.

---

## 7. `07_phase6_docs_demo.md` — Exit criteria

Add:

> - [ ] README states plainly that the build is not safe for real PHI as-is
> - [ ] Known gaps (no token refresh/revocation, no rate limiting, no TLS or
>       encryption at rest locally, regex-only redaction) are visible in the
>       README, not only in `ARCHITECTURE.md`
