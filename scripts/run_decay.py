"""Run the data-decay policy from the command line.

In production this is a nightly cron entry. Here it is an explicit script,
because a prototype that silently rewrote stored clinical text on a timer would
be much harder to reason about during a demo — and the policy is the
interesting part, not the scheduler.

    python scripts/run_decay.py                     # preview every clinic
    python scripts/run_decay.py --clinic clinic-a   # preview one
    python scripts/run_decay.py --clinic clinic-a --apply

Preview is the default. This is the only operation in the system that rewrites
the content of a stored entry, so it does not run by accident.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import SessionLocal  # noqa: E402
from app.services import decay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the Care Note decay policy.")
    parser.add_argument("--clinic", default=None, help="limit to one clinic id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually compress (default is a preview that changes nothing)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = decay.run(db, clinic_id=args.clinic, dry_run=not args.apply)
    finally:
        db.close()

    mode = "APPLIED" if args.apply else "PREVIEW (nothing written)"
    print(f"Decay pass — {mode}")
    print(
        f"  evaluated {report['evaluated']}, "
        f"changing {report['changed']}, unchanged {report['unchanged']}"
    )
    print(
        f"  policy: warm after {report['policy']['warm_after_days']}d, "
        f"cold after {report['policy']['cold_after_days']}d"
    )

    for change in report["changes"]:
        marker = "HELD" if change["protected"] else "    "
        print(
            f"  {marker} {change['entry_id']:<28} "
            f"{change['current_state']:>4} -> {change['target_state']:<4} "
            f"({change['age_days']}d) {change['reason']}"
        )

    if report["hot_bytes_before"]:
        print(
            f"  read path: {report['hot_bytes_before']}B -> "
            f"{report['hot_bytes_after']}B on compressed entries"
        )
        print(
            f"  archive:   +{report['archive_bytes']}B stored, fully recoverable "
            f"(net {report['net_storage_delta']:+d}B)"
        )
    if not args.apply and report["changed"]:
        print("\n  Re-run with --apply to make these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
