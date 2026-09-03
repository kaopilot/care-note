# Demo Script — round two

**Nine segments, ~10 minutes.** The brief asks to demonstrate as many of
scenarios 1–16 as possible, so this script is organised by scenario, not by
product tour. Every claim below is visible on screen at the moment it is said;
if a number differs from what you see, read what you see.

Two segments deliberately show the build **failing** — scenario 2's blast radius
and scenario 7's boundary. Do not cut them for time. "DOES NOT, and here is
exactly why" is the answer the brief says it rewards, and a nine-minute video of
green ticks makes every other claim in it worth less.

## Setup (before recording)

```bash
# terminal 1 — API
cd backend && source .venv/bin/activate
python init_db.py --reset && uvicorn app.main:app          # :8000

# terminal 2 — UI
cd frontend && npm run dev                                 # :5173

# terminal 3 — the one you record commands in
cd <repo root>                                             # NOT backend/
```

**About terminal 3.** Use `./run_tests.sh` for every test command below, not
bare `pytest`. Two things bite otherwise, and they produce different errors for
what is really one cause: the virtualenv lives at `backend/.venv`, so a fresh
terminal has no `pytest` on PATH (`command not found`); and `pytest.ini` sits at
the repository root and sets `pythonpath = backend`, so running from `backend/`
gives `file or directory not found: tests/...` even with the venv active.
`run_tests.sh` resolves both and works from any directory. Check it before you
record:

```bash
./run_tests.sh tests/test_survival_scenarios.py -q      # expect: 5 passed
```

**Tokens for the curl segments.** Segments 2 and 3 talk to the API directly,
because the controls they demonstrate are server-side and a browser cannot prove
that — enrolment has no screen, and clinic isolation shown in a UI that never
offers the button proves only that the button was not built. Export these in terminal 3 before recording — a login round-trip on camera
is dead air:

```bash
tok() { curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$1\",\"password\":\"carenote-demo\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }

export STAFF_A_TOKEN=$(tok staff_a)
export CLINICIAN_A_TOKEN=$(tok clinician_a)
echo "${STAFF_A_TOKEN:0:12}… ${CLINICIAN_A_TOKEN:0:12}…"    # both non-empty
```

Tokens expire after 60 minutes with no refresh (D-016), which is a real
constraint on a long recording session: re-run the two exports if a curl starts
returning 401 mid-take.

Two browser windows side by side at `localhost:5173` — **left `clinician_a`,
right `staff_a`**, password `carenote-demo`.

Patient **Amira Rahman** throughout. On a fresh `--reset` her card leads with a
penicillin allergy highlight marked **⚠ Always shown** — segment 6 depends on
that, so confirm it is there before you start.

---

## 1 — The table, honestly (~45s) · framing

```bash
./run_tests.sh tests/test_survival_scenarios.py -v
```

> "Sixteen scenarios, one test each. Nine survive, six are partial, one does
> not. Two of these moved *backwards* after we audited ourselves — clinic
> isolation and log hygiene were both SURVIVES until we measured them properly.
> This file fails if its verdicts drift from the published table, so the
> document and the tests cannot disagree."

> "One more thing before we start. We ran a final pass after this table was
> written and found four more defects, all of them live while every test passed.
> Two were misfiring on the two surfaces a patient can actually see. They are in
> segments 8 and 9, shown where the build was wrong rather than in a footnote."

Do not read the table aloud. Let it scroll and move on.

---

## 2 — She has no email (scenarios 1, 5) · ~75s

**Enrolment is an API, not a screen** — there is no "add patient" form, and
saying so on camera is better than fumbling for one. In the terminal, as
`staff_a`:

```bash
curl -s -X POST localhost:8000/patients \
  -H "Authorization: Bearer $STAFF_A_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Siti Rahman","identifier_type":"phone",
       "identifier":"0198887777","create_login":true}'
```

It returns her `patient_id`, `username: 0198887777`, and a `one_time_passcode`
shown exactly once.

> "She exists for the clinic as a phone number in a WhatsApp thread. Phone is a
> first-class identifier, not a workaround — and `dob` and `mrn` had to become
> nullable, because those `NOT NULL` columns were a second, quieter way the
> schema decided she was not a patient. There is no enrolment screen; front-desk
> UI was scope we cut, and the identity model is the part scenario 1 asks
> about."

Now the payoff, and it is in the UI: reload the right window as `staff_a` and
**Siti Rahman is in the patient list**. Then log in as her in a private window,
username `0198887777`, password the passcode — the patient view opens.

Run the same curl a second time to show it returns `409 That identifier is
already registered` rather than a second account on one phone number.

Then, terminal:

```bash
./run_tests.sh tests/test_clinic_config.py -v
```

> "Scenario 5 asks config versus schema. Schema: nothing — every table already
> carries `clinic_id`, so a third clinic needs no migration. Config was the real
> gap, and the useful half was deciding what stays global. Redaction, the
> protected clinical classes, contradiction severities — a clinic can change
> what it sees, never what it is protected from. Vocabulary is still global, so
> this one stays partial."

---

## 3 — Assume that line has a bug (scenario 2) · ~60s · **shows a failure**

**Do this in the terminal, not the browser.** The app has no URL routing —
patient selection is client state — and a UI that never offers the button is not
evidence of anything. The claim is that the *server* refuses, so ask the server:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/patients/patient-b1/glance \
  -H "Authorization: Bearer $CLINICIAN_A_TOKEN"     # 404
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/patients/patient-a1/glance \
  -H "Authorization: Bearer $CLINICIAN_A_TOKEN"     # 200
```

> "404, not 403 — a clinician in Clinic A is not told that patient exists.
> Isolation is enforced in one place: `AccessScope.query`. Routes never get a
> user, they get a scope already narrowed to their clinic, so forgetting the
> check is not a mistake you can make. And it has to be shown at the API,
> because showing it in a UI that never renders the button proves only that we
> did not build the button."

Terminal:

```bash
./run_tests.sh tests/test_survival_scenarios.py::test_breaking_the_single_line_exposes_every_clinic -v
```

> "The scenario asks what happens when that line has a bug. So we drop the
> clinic filter and ask again — and every patient in both clinics comes back.
> Nothing else catches it. No row-level security, no per-tenant connection, no
> check at the serialisation boundary. It is the strongest single control we
> have and it is *singular*, which is a different property. That is why this one
> is partial, not survives."

---

## 4 — The doors nobody guards (scenarios 3, 4) · ~75s

```bash
./run_tests.sh tests/test_llm_chokepoint.py tests/test_url_surface.py -q
```

> "Scenario 4 asks us to prove redaction runs before the model. It is not a
> code comment — a test scans the source and fails if any module but the wrapper
> can reach a provider."

> "Scenario 3 asks about the other doors. Two were open. Crash logs: SQLAlchemy
> puts bound parameters in exception messages, so one unhandled error put a
> name, an NRIC and note content in a single line. Then the one we found
> auditing ourselves — the access log records the full request line before our
> code runs, and our *own* enrolment route was passing the patient's phone
> number as a query parameter. The feature built for scenario 1, leaking through
> scenario 3. It is in the body now, and a test pins that no route can take
> patient data in a URL again."

---

## 5 — The model hangs, then dies (scenarios 8, 9) · ~75s

Restart the API with the outage flag (terminal 1, venv already active):

```bash
CARENOTE_LLM_FORCE_UNAVAILABLE=true uvicorn app.main:app
```

Left window → **Capture a consult** → **Doctor consult**.

> "Provider returns 503 for an hour. The clinician still gets a summary — rule
> extracted from the transcript, every sentence the speakers' own — and the card
> says **written without the AI**."

Point at the chip.

> "That badge is the fix from our own audit. The label existed before, but only
> as a model string in ten-pixel grey next to the pointer. The data was right
> and the card read like an ordinary AI summary. It is also deliberately
> separate from the confidence chip: 'the model was unsure' and 'no model read
> this consult' need different responses."

> "The timeout is 8 seconds, not 60. A 45-second hang is not a timeout a
> clinician standing next to a patient can use."

Restart without the flag before continuing.

---

## 6 — A tired Tuesday (scenario 15) · ~75s

Left window, Amira's card. The penicillin highlight is top, chipped **⚠ Always
shown**.

> "Scenario 15 asks what stops the ranking learning to bury an allergy because a
> tired clinician swiped one away."

Click **reject** on it. It drops to the bottom of the card and re-renders as
**Dismissed — kept visible**.

> "It doesn't leave. Our first answer was a floor on the learned weight, and
> that floors the wrong quantity — surfacing is a top-six cut, so other tags
> rising displaces an allergy with its own weight untouched, and one dismissal
> removed it permanently. Now protected classes bypass ranking entirely.
> Learning still orders them; it cannot decide whether they appear."

> "And the protected list *is* the never-dampened list, imported — not a second
> list that would drift silently."

---

## 7 — The record disagrees with itself (scenarios 13, 6, 14) · ~90s

Right window as `staff_a`, add a staff note: `Patient reports allergy to
penicillin — rash as a child.` Left window as clinician, add: `Patient states she
has no known drug allergies.` Reload the Glance View.

> "The nurse recorded penicillin. The patient told the AI she has none. Both are
> in the timeline, both are cited, and the system does not choose — there is no
> precedence rule between two people and inventing one would be a clinical
> decision we have no standing to make."

Add the same allergy in two more notes, reload.

> "Here is what our own audit caught. Detection is pairwise, so re-recording an
> allergy at every visit used to produce a card per pair — and because the list
> is capped, those copies filled it and pushed a real metformin dose
> disagreement off the card entirely. One clinical disagreement is now one card,
> and every entry behind it keeps its own link."

Point to the Malay entry (`Kebas sikit waktu pagi`).

> "Scenario 6 — the risk floor works on canonical tags, not English strings, so
> the same symptom rates the same in Malay. It was `high` in English and
> `medium` in Malay until we fixed it. Hokkien we cannot read, and it is flagged
> as unread rather than silently scored — abstention beats confident silence."

---

## 8 — Provenance, and two people typing (scenarios 16, 10) · ~90s

Click a highlight → jumps to the exact span in the source entry.

Now edit that source note in the right window. Reload the left.

> "Scenario 16: the highlight cites a note that has since changed. It does not
> silently point at different text — that is the worst of the three options
> because it looks fine. Highlights anchor to a version number, so it resolves
> against the text it was made from and shows both side by side."

**Then keep going, because this is where our final audit found a defect.** The
seed already ran the decay policy, so `entry-a1-hist-2026` is archived and one
highlight points into it — no command needed. Show the policy if you like, but
say what it is:

```bash
python scripts/run_decay.py --clinic clinic-a      # preview: 0 changing, 12 unchanged
```

Scroll the Glance View to that highlight. It carries the stale marker and says
*"the source note has been shortened for archiving"* — not a version change,
because there isn't one. Click it: the timeline scrolls to the note and draws no
box.

> "Our answer to scenario 16 was version numbers, and it was right for edits.
> Decay compresses an old note to a summary — and archival is deliberately not
> an authorship event, so it creates no version. Staleness was a version-number
> comparison, so a compressed note reported *not stale* while the highlight's
> offsets pointed into a summary they no longer describe. Note the wording: an
> archived note gets its own sentence, because the edit one renders as 'v1 to
> v1' here, which reads like a bug. That is the silently-wrong
> case, reached by a route the mechanism never watched. Both routes are closed
> now. Watch what the timeline does here: it scrolls to the note and highlights
> nothing, because pointing confidently at the wrong words is worse than
> pointing at the note. That half was wrong for edits too."

Click **Restore full note**.

> "Stale is not lost. The original is archived byte for byte, the highlighted
> words still resolve against their version snapshot, and restoring the note
> makes its highlights current again."

Both windows, same note, same section, type in both, save left then right.

> "Scenario 10. The second save is rejected with a 409, not silently merged and
> not silently lost — the version they edited is no longer current. No lost
> updates. But this is not real-time: they find out at save, not at 09:14. No
> presence, no cursors."

---

## 9 — What reaches the patient (scenarios 12, 11) · ~90s

First, a note that must go through untouched: `Continue metformin 1g BD,
amlodipine 5mg OD, atorvastatin 20mg ON.` It saves without a prompt, and the
Glance View shows no contradiction.

> "That ordinary line used to do two wrong things at once. The contradiction
> detector gave the first dose in the sentence to every drug in it, so it
> reported amlodipine at one gram — a dose that does not exist for that drug —
> as a high-severity disagreement against a later note that agreed. And the
> dosage gate's window ran past the next drug name, so 'metformin and
> amlodipine 5mg' read as metformin 5mg and blocked the write. A safety check
> that is confidently wrong about an easy case teaches people the check means
> nothing, which disarms it for the one that matters."

Now attempt a patient-facing note containing `metformin 5000mg`.

> "Patient-facing content is a higher severity class — you cannot audit
> something already on someone's phone. An implausible dose gates the write, and
> the override is recorded rather than blocked outright, because a hard block
> teaches people to route around the check."

Patient tab → correction banner.

> "Scenario 11 asks why the link never arrives. Ours never sends — there is no
> email, SMS or push, and `dispatched` is deliberately not modelled rather than
> faked. We report reach honestly: unread, read, corrected."

Still in the patient tab, add a patient note, then edit it. **Nothing happens** —
no banner on her page, and no row appears on the clinician's delivery panel.
(The unread count may fall, because opening the portal marks the *clinician's*
instructions read. That is the panel working.)

> "This banner is the loudest thing we say to a patient, and it means the clinic
> changed something you already acted on — possibly a dose. Until our last audit
> it also fired when she edited her own note, because two modules had a constant
> with the same name and different contents. She would have been told to stop
> following her own words. The one reader in this system with no way to check a
> warning against anything was the one getting a false one."

---

## Closing (~30s)

> "Scenario 7 we do not survive, and it is worth saying plainly. The scribe is
> post-hoc — it consumes a whole transcript — so a drug allergy at minute two is
> not in the Glance View until the consult ends. The deterministic extractors
> are pure functions and would run on partial transcript in about ten seconds;
> what we could not answer is when it is acceptable to interrupt a doctor
> mid-sentence, and that needs a clinician, not an engineer."

> "Nine survive, six partial, one does not. Two moved backwards because we
> measured our own claims instead of restating them. The full table, with the
> test behind every row, is in `SCENARIO_COVERAGE.md`."

> "And scenario 16 kept its verdict but not the reason it deserved it — decay
> reached the same content by a route we had never checked. Three of the four
> defects in that final pass sat in a seam between two modules that were each
> correct alone. Every module in this repo argues for its own behaviour at
> length; none says what it assumes about the one beside it. That is where we
> would look next, and it is written down in D-103 rather than left for someone
> else to find."

---

## Do not

- Do not claim real-time collaboration, streaming ASR, acoustic diarization, a
  message sender, or a real drug database. None exist.
- Do not skip segment 3 or the closing. The failures are the argument.
- Do not read verdict tables aloud — show them scrolling and narrate the point.
- Do not say "secure" or "HIPAA-compliant". Synthetic data, prototype, and the
  README says plainly it is not safe for real PHI as-is.
