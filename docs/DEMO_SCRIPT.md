# Demo Script

Three scenarios, **7–8 minutes total**. Narrate what is on screen and why it
matters, then move on — this is scored on clarity, not length. Every claim in
the narration below is something visible on screen at that moment; if a number
does not match what you see, read what you see.

## Setup (do this before recording)

```bash
cd backend && python init_db.py --reset && uvicorn app.main:app   # :8000
cd frontend && npm run dev                                        # :5173
```

Two browser windows side by side, both at `localhost:5173`: **left signed in as
`clinician_a`, right as `staff_a`** (password `carenote-demo` for every account).
A third tab signed in as `patient_a` for the closing shot. Patient **Amira
Rahman** is the chart used throughout.

**One thing to know about the seed:** a fresh `--reset` contains a single
AI-scribed note and it scores 0.82, which is high confidence, so the **"AI needs
checking"** panel starts empty. Scenario A runs the scribe live to produce a
low-confidence one, which is the better demo anyway. If you would rather not run
it on camera, click **Patient session** under *Capture a consult* once before
recording and the flag will already be there.

Have `docs/TECHNICAL_BRIEF.md` open in a tab for the architecture diagram if you
want to cut to it during Scenario C.

---

## Scenario A — Glance View + AI scribe integration (~2.5 min)

**Say:** *"A clinician opens a chart between consults. The question they walk in
with is 'what do I need to know in the next ten seconds' — not 'show me
everything'."*

1. From the patient list, click **Amira Rahman**. Let the Top Card land. Do not
   scroll — the point is what is readable without scrolling.
2. Walk the four zones in order, briefly: **New since your last visit** → **What
   matters now** (ranked, each with a reason) → **Risk flags** and **AI needs
   checking** → **Open actions**.
3. Point at the header timing and **read the two numbers off the screen** —
   they vary run to run, so do not quote a figure from memory.
   **Say:** *"Two numbers, deliberately. The first is what the application
   controls, measured by middleware; the second is the full browser round trip.
   The benchmark over 200 iterations puts the P95 in the low teens of
   milliseconds against a 300ms budget."*
4. **Run the scribe live.** Under *Capture a consult*, click **Patient session**.
   **Say:** *"A synthetic transcript, redacted, then summarised. The processing
   state is real — this is the pipeline, not a fixture."*
   When it lands, point at the new entry's confidence chip and at **"AI needs
   checking"**.
   **Say:** *"Confidence is measured from hedging in the source transcript, not
   reported by the model. This is a patient session full of 'maybe' and 'I
   think', so it comes out around 0.47 — low. The nurse consult, mostly
   measurements, comes out around 0.77. The chip shows the word and the number,
   because 'medium' on its own means whatever the reader assumes it means."*
5. **The provenance click.** Find a highlight tagged **◇ From AI note**. Say
   *"this is a claim, not a fact — so it has to be checkable in one click."*
   Click the span text.
6. Land in the timeline. Point out that the highlighted characters are marked —
   *"not 'jumps to the note', jumps to the words."* Point at the entry's dashed
   rail and monospace body: *"AI-authored, and it's carried by four independent
   signals so it survives in greyscale."*
7. If a **"Risk set by rule"** chip is visible on an entry, point at it.
   **Say:** *"Deterministic rules set a floor under the risk level. A model can
   raise it — it might notice something our keyword list misses — but it can
   never lower it, and this chip says which one decided. Model-assigned severity
   labels drift between runs; a rule does not."*

**Do not** click Confirm/Dismiss yet — that is Scenario B's payoff.

---

## Scenario B — Collaboration, audit trail, contradictions (~3.5 min)

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
   **Say:** *"That's the learning signal. The system records which kinds of
   content a clinician reaches for and weights similar content up in future —
   bounded, so it can never exceed a quarter of the score, and floored, so no
   amount of dismissing can teach it to stop mentioning an allergy."*
5. Click **Confirm** on a suggested highlight in the Top Card.
   **Say:** *"One click, inline, no navigation away. That is a design
   constraint rather than a nicety — this decision is also the training signal,
   and a control with friction on it produces a sparse one."*
6. **Edit and revert.** Edit the clinician section (change the plan text). Open
   **History** → show the version diff → **Revert** to the prior version.
   **Say:** *"Revert appends a new version rather than rolling the number back.
   History is never destroyed — you can always see that a revert happened."*
7. Briefly show the **audit trail**: who changed what and when, metadata only.
   *"The log carries IDs, actions and timestamps. Never note bodies, never
   transcript text."*
8. **Two people contradicting each other.** This is the sharpest thing to show,
   so leave time for it.
   **(Right window, staff_a)** add a staff note:
   `Patient reports allergic to penicillin, rash on forearms.`
   **(Left window, clinician_a)** add to the clinician section:
   `Chest infection. Started on amoxicillin 500mg TDS for seven days.`
   Reload the clinician window. A **critical band appears above everything else
   on the card**, quoting both notes.
   **Say:** *"Neither of them is being careless. The nurse recorded an allergy,
   the clinician prescribed from a different part of a fragmented record, and
   under the old way of working nobody sees both. Penicillin and amoxicillin are
   not the same word — they are the same drug class, which is what the detection
   is actually matching on."*
   Then the important half: *"Notice what the system does not do. It does not
   pick a winner. There is no precedence rule between two clinicians, and
   deciding the more recent note wins would throw away an allergy recorded last
   year. Both entries are quoted, both are one click away, and a person
   decides."*
   If asked how it avoids crying wolf: no model is involved, it is deterministic
   pattern matching over three classes — allergies, doses, medication status —
   `1g` and `1000mg` are not treated as a conflict, and *"denies allergy to
   aspirin"* does not register as an allergy.

---

## Scenario C — Longitudinal context, learning, decay (~2.5 min)

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
4. **Code-switched capture.** Scroll to the patient note titled **"Kaki saya"**
   — "Kaki bengkak again this week... Kebas sikit waktu pagi."
   **Say:** *"This is what a patient in a Singapore or Malaysian clinic actually
   writes — two languages in one sentence. For most of this build that entry
   produced no clinical tags at all, so it scored nothing and never reached the
   Top Card, even though it describes exactly the oedema the consult is about.
   The patients least likely to be understood in English were the ones the
   system quietly stopped surfacing."*
   Point at its highlight in the Top Card: reason reads **"Oedema (Malay:
   bengkak)"**.
   **Say:** *"`bengkak` emits the same tag `swelling` emits — not a separate
   one. That matters for the learning layer: one concept has to be one feature,
   or a clinic's learned attention wouldn't transfer across whichever language
   the patient happened to use. And nothing is translated — the record still
   shows exactly what she wrote."*
   If asked what's missing: only Malay, only fourteen terms, needs a
   native-speaker review, and negation is unhandled in both languages.
5. **Data decay.** Point at the compressed 2026 history entry.
   **Say:** *"Hot, warm, cold. Older low-priority entries compress to an
   extractive summary with the original archived — byte-exact reversible, and
   provenance still resolves through to the archive, so a pointer into a
   compressed entry doesn't dangle. The 2025 entry next to it was held back
   because it documents an allergy: protection rules beat age."*
6. **(Patient tab)** Switch to `patient_a`.
   **Say:** *"Same record, different register. Plain language, no scores, no
   clinical shorthand, no internal comments and no raw AI notes — and that's
   enforced server-side, not by hiding things in this page."*
7. **Close on the argument:** *"The hard part here is not summarising a
   consult. It is building something clinical staff should trust exactly as far
   as it deserves, and no further. Our answer is that AI output enters this
   system as a claim rather than a fact. Nothing AI-generated counts until a
   clinician accepts it. Every claim opens to its source in one click. When a
   clinician disagrees with the AI the clinician wins, but the disagreement
   stays on the record instead of being quietly deleted. When two people
   disagree, the system surfaces it and refuses to choose. And nothing the AI
   writes can ever reach the patient — not because we review it, but because
   there is no code path that would let it."*

---

## Worth mentioning if there is time

- **What is stubbed:** the speech recogniser is a simulated stub — it does not
  transcribe, and it says so on every surface it touches. Being straight about
  this is worth more than implying a capability the build does not have.
- **The defects the tests missed, and why they all look alike:** superseded
  highlights were never deleted, so the Top Card rendered every claim twice —
  live through 334 passing tests, found by looking at the screen (D-055). Five
  more surfaced afterwards from using the thing (D-059–D-062): a clinician's own
  highlight vanished from the card, confirming one suggestion 404'd the rest,
  "new since your last visit" stayed empty for a whole session, a task could
  never be closed, and every timestamp shipped without a UTC offset so a note
  written seconds ago read "8h ago" in SGT. Each lives in the seam between two
  pieces of individually correct code, which is the class a component-level
  suite cannot see — so the regressions are end-to-end sequences, and ten of
  fifteen fail against the previous commit. Say this plainly if asked what you
  would fix next; a candidate who can name the shape of their own blind spot is
  worth more than one with nothing to report.
- **The three numbers, if they ask about evaluation:** the risk badge has a
  deterministic floor a model can raise but never lower, and the row records
  which one set it. Confidence is measured from hedging in the source on every
  path, banded high ≥0.75 / medium 0.60–0.75 / low <0.60 — a live model's
  self-report is stored for calibration and never shown, because a number the
  model chose about itself cannot be checked by the person reading it. The
  importance score shows its own arithmetic term by term. Each abstains: an
  unparseable risk falls back to `low` with the floor still applied, confidence
  never claims 1.0, and a span with no clinical reason produces no highlight at
  all.

## Do not

- Read the security table aloud. Say it is in the brief and move on.
- Demo cross-clinic refusal in the UI — it is an API property. If asked, run
  `python scripts/phase1_smoke.py`.
- Apologise for the stub recogniser more than once.
