#!/usr/bin/env python
"""Print the exposure-bias report for every clinic in the database.

    python scripts/eval_learning.py

Run it after `init_db.py` and after any simulated interaction, and the numbers
move. That is the point: the brief can quote a measured figure instead of
asserting that a mitigation exists.

Reading the output
------------------
* `displacement_rate` — share of visible slots the learned term changed.
  0.0 means learning is decoration. 1.0 means the rules stopped mattering.
  Anything in between is the loop doing its job at the strength `W_LEARNED`
  allows (0.25, capped by construction).

* `exposure_concentration` — share of visible slots held by tags this clinic
  has already given feedback on. **This is the bias, as a number.** Near 1.0
  means the loop only ever hears about what it already believed.

* `blind_tag_rate` — tags present in the record that have never reached a
  visible slot. The loop has had no chance to learn these matter. This is the
  population D-069's exploration slot draws from, and it is the honest measure
  of how far from closed the loop is.

* `protected_tags_displaced` — a `NEVER_DAMPENED` tag that was visible without
  learning and is not visible with it. **This should always be empty.** The
  floor stops a protected tag's own weight going negative; it does not stop
  something else being promoted past it, and that gap is exactly the "tired
  clinician buries an allergy on a Tuesday" failure the reviewers asked about.

See DECISIONS.md D-092.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import SessionLocal  # noqa: E402
from app.models import Clinic  # noqa: E402
from app.services import learning_eval  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        clinics = db.query(Clinic).all()
        if not clinics:
            print("No clinics. Run `python backend/init_db.py` first.")
            return 1

        exit_code = 0
        for clinic in clinics:
            report = learning_eval.evaluate(db, clinic.id)
            print(f"\n=== {clinic.id} ===")
            print(json.dumps(report.as_dict(), indent=2))

            if report.protected_tags_displaced:
                # Loud, and a non-zero exit: a protected class losing its slot
                # is the one result here that is a defect rather than a
                # measurement.
                print(
                    "\n!! A protected tag lost a visible slot to a promoted one. "
                    "The NEVER_DAMPENED floor does not cover this case.",
                    file=sys.stderr,
                )
                exit_code = 2

            if report.slots_total and report.displacement_rate == 0.0:
                print(
                    "   note: learning changed nothing visible in this clinic — "
                    "either no feedback yet, or the learned term is too small "
                    "to move this data."
                )
        return exit_code
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
