"""Every `D-0xx` reference in the repo points at a decision that exists.

Why this is a test and not a convention
---------------------------------------
Renumbering three decision records that collided with existing ones was done
with a find-and-replace. `"D-083)" -> "D-089)"` was meant to fix the new
records; it also rewrote two pre-existing, correct references in
`SCENARIO_COVERAGE.md`. Both still pointed at a real decision, so nothing was
broken in any way a reader would notice quickly — they simply pointed at the
*wrong* one, and the same document then contradicted itself two lines apart:
scenario 3 said "a phone number was in a query string until D-083" while the
audit section below called the same defect D-089.

**A cross-reference that silently points at the wrong decision is worse than no
cross-reference.** It reads as a citation, and a reviewer following it lands on
an unrelated record and concludes the argument does not hold.

This file catches three failure modes a human proof-read will not:

1. a reference to a decision number that was never written;
2. a duplicated decision heading, which is what caused the collision — two
   `### D-083` records existed at once;
3. headings that skip or repeat numbers, so the next person appending a record
   can trust the last one they see.

It does not check that a reference is *apposite* — no test can. It checks that
the target exists and is unique, which is the part that can be mechanised.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

DECISION_HEADING = re.compile(r"^### (D-\d{3})\s*·", re.M)
DECISION_REFERENCE = re.compile(r"\bD-(\d{3})\b")

# Everything a reader might follow a reference from. Source comments count:
# `contradictions.py` cites D-089 to explain why a whole comparison class is
# gated, and a reader chasing that citation deserves the right record.
SEARCHED = [
    "*.md",
    "backend/app/**/*.py",
    "tests/*.py",
    "scripts/*.py",
    "docs/*.md",
]


def _decisions_text() -> str:
    return (REPO_ROOT / "DECISIONS.md").read_text(encoding="utf-8")


def _defined_numbers() -> list[str]:
    return DECISION_HEADING.findall(_decisions_text())


def _searchable_files() -> list[pathlib.Path]:
    seen: set[pathlib.Path] = set()
    for pattern in SEARCHED:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file() and "node_modules" not in path.parts:
                seen.add(path)
    return sorted(seen)


def test_decision_headings_are_unique():
    """The collision itself.

    Three records were appended as D-083, D-084 and D-085 while records with
    those numbers already existed. Everything still rendered; the numbers just
    stopped identifying anything.
    """
    numbers = _defined_numbers()
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, (
        f"duplicate decision headings: {duplicates}. Two records with one "
        "number means every reference to it is ambiguous."
    )


def test_decision_numbers_run_without_gaps():
    """Not pedantry — a gap usually means a record was deleted rather than
    superseded, and a decision log that quietly loses entries is not a log.

    Superseded decisions should stay and say what replaced them.
    """
    numbers = sorted(int(n.split("-")[1]) for n in _defined_numbers())
    assert numbers, "no decision headings found — has the format changed?"
    expected = list(range(numbers[0], numbers[-1] + 1))
    missing = sorted(set(expected) - set(numbers))
    assert not missing, f"decision numbers missing from DECISIONS.md: {missing}"


@pytest.mark.parametrize(
    "path", _searchable_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_every_decision_reference_resolves(path):
    """No file may cite a decision that was never written."""
    defined = set(_defined_numbers())
    text = path.read_text(encoding="utf-8", errors="replace")

    dangling = sorted(
        {
            f"D-{number}"
            for number in DECISION_REFERENCE.findall(text)
            if f"D-{number}" not in defined
        }
    )
    assert not dangling, (
        f"{path.relative_to(REPO_ROOT)} cites {dangling}, which "
        "DECISIONS.md does not define."
    )


def test_the_reference_scan_is_actually_reading_files():
    """The guard on the guard.

    Every assertion above is a search. If `_searchable_files` returned an empty
    list, or the reference regex stopped matching, the parametrised test would
    pass by running against nothing.
    """
    files = _searchable_files()
    assert len(files) > 20, f"only {len(files)} files searched — the glob is wrong"

    joined = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in files
    )
    assert len(DECISION_REFERENCE.findall(joined)) > 50, (
        "almost no decision references found — the regex has stopped matching"
    )
    assert len(_defined_numbers()) > 50, "almost no decision headings found"
