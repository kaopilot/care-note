# Demo Script

Three scenarios from the brief. Target **6–7 minutes total**. The rubric scores
conciseness and clarity, not length — narrate what is on screen and why it
matters, then move.

## Setup (do this before recording)

```bash
cd backend && python init_db.py --reset && uvicorn app.main:app   # :8000
cd frontend && npm run dev                                        # :5173
```

Two browser windows side by side, both at `localhost:5173`: **left signed in as
`clinician_a`, right as `staff_a`** (password `carenote-demo` for every account).
A third tab signed in as `patient_a` for the closing shot. Patient **Amira
Rahman** is the chart used throughout.

Have `docs/TECHNICAL_BRIEF.md` open in a tab for the architecture diagram if you
want to cut to it during Scenario C.

---

## Scenario A — Glance View + AI scribe integration (~2 min)

**Say:** *"A clinician opens a chart between consults. The question they walk in
with is 'what do I need to know in the next ten seconds' — not 'show me
everything'."*

1. From the patient list, click **Amira Rahman**. Let the Top Card land. Do not
   scroll — the point is what is readable without scrolling.
2. Walk the four zones in order, briefly: **New since your last visit** → **What
   matters now** (ranked, each with a reason) → **Risk flags** and **AI needs
   checking** → **Open actions**.
3. Point at the header timing: *"glance 14ms server / ~120ms round trip. Two
   numbers, deliberately — the 14ms is what the application controls, measured
   by middleware over 200 iterations; the round trip includes transit and
   render. Against a 300ms budget."*
4. Point at the **"AI needs checking"** flag: *"confidence is derived from
   hedging in the source transcript, not asserted by the model. This one is a
   patient session full of 'maybe' and 'I think', so it lands at 0.47 and gets
   flagged. The nurse consult, mostly measurements, lands at 0.77."*
5. **The provenance click.** Find a highlight tagged **◇ From AI note**. Say
   *"this is a claim, not a fact — so it has to be checkable in one click."*
   Click the span text.
6. Land in the timeline. Point out that the highlighted characters are marked —
   *"not 'jumps to the note', jumps to the words."* Point at the entry's dashed
   rail and monospace body: *"AI-authored, and it's carried by four independent
   signals so it survives in greyscale."*

**Do not** click Confirm/Dismiss yet — that is Scenario B's payoff.

---

## Scenario B — Collaboration + audit trail (~2.5 min)

**Say:** *"Now two roles working the same chart, and what the record remembers
about it."*

1. **(Right window, staff_a)** Add a staff note. Type something clinical with an
   angle bracket in it, e.g. `Home readings averaging BP <135/85 this fortnight.`
   Add to record.
   **Say:** *"Stored exactly as written. We deliberately don't HTML-escape on
   write — clinical prose contains `BP <120/80` and `dose <5mg`, and
   tag-stripping can silently eat a dose limit. Corrupting a note is worse than
   the XSS it would prevent, because untrusted content is never rendered as HTML
   at all."*
2. Open **Discussion** on that note, post a comment with `@clinician_a` in it,
   and assign a task to the clinician.
3. **(Left window, clinician_a)** Reload. The new note and the mention appear.
   Show the task in **Open actions**.
4. **Manual highlight inside an AI note.** Scroll to the AI-scribed consult
   summary, select a phrase with the mouse, and confirm the highlight.
   **Say:** *"That's the learning signal. Phase 4 records which features a
   clinician reaches for, and future ranking weights them up — bounded, so it
   can never exceed a quarter of the score, and floored so it can never learn to
   suppress an allergy."*
5. Click **Confirm** on a suggested highlight in the Top Card.
   **Say:** *"One click, inline, no navigation. That's a design constraint, not
   a nicety — a high-friction control produces a sparse signal and the learning
   loop starves."*
6. **Edit and revert.** Edit the clinician section (change the plan text). Open
   **History** → show the version diff → **Revert** to the prior version.
   **Say:** *"Revert appends a new version rather than rolling the number back.
   History is never destroyed — you can always see that a revert happened."*
7. Briefly show the **audit trail**: who changed what and when, metadata only.
   *"The log carries IDs, actions and timestamps. Never note bodies, never
   transcript text."*

---

## Scenario C — Longitudinal context, learning, decay (~2 min)

1. Scroll the timeline to the **older entries** — the 2025 and early-2026 dates
   are seeded specifically for this.
   **Say:** *"Date-grouped, not a flat feed, because a six-month gap should not
   look like a six-minute one."*
2. **Say how ranking prioritises:** *"Recent, unresolved, and clinician-confirmed
   beat old and settled. Every highlight shows its own arithmetic — recency,
   risk level, clinical entities, unresolved actions, and the learned term."*
   Point at a visible score breakdown showing **"Learned from this clinic"**.
3. Expand **"What this clinic pays attention to"** in the Glance View sidebar.
   **Say:** *"The learned weights, inspectable. Thirteen seeded interactions
   representing six months of real clinical attention — and they're seeded as
   *behaviour*, then aggregated through the same code path a live click uses.
   Seeding the weights directly would have been shorter and would have been a
   lie."*
4. **Data decay.** Point at the compressed 2026 history entry.
   **Say:** *"Hot, warm, cold. Older low-priority entries compress to an
   extractive summary with the original archived — byte-exact reversible, and
   provenance still resolves through to the archive, so a pointer into a
   compressed entry doesn't dangle. The 2025 entry next to it was held back
   because it documents an allergy: protection rules beat age."*
5. **(Patient tab)** Switch to `patient_a`.
   **Say:** *"Same record, different register. Plain language, no scores, no
   clinical shorthand, no internal comments and no raw AI notes — and that's
   enforced server-side, not by hiding things in this page."*
6. **Close on the thesis:** *"The brief asks how you build a system people trust
   only as far as they should. Three answers: nothing AI-generated is a fact
   until a clinician accepts it; every claim opens to its source in one click;
   and when a clinician disagrees with the AI, the clinician wins — but the
   disagreement stays visible instead of being quietly deleted."*

---

## Worth mentioning if there is time

- **What is stubbed:** the speech recogniser is a simulated stub — it does not
  transcribe, and it says so on every surface it touches. Being straight about
  this is worth more than implying a capability the build does not have.
- **The defect the tests missed:** superseded highlights were never deleted, so
  the Top Card rendered every claim twice. It was live through 334 passing tests
  and was found by looking at the screen. Fixed, pinned by regressions and a
  source scan (D-055). Reviewers respond well to a candidate who found their own
  bug and says so.

## Do not

- Read the security table aloud. Point at the brief and move on.
- Demo cross-clinic refusal in the UI — it is an API property. If asked, run
  `python scripts/phase1_smoke.py`.
- Apologise for the stub recogniser more than once.
