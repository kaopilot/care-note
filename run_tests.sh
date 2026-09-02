#!/usr/bin/env bash
#
# Run the test suite from anywhere, with the right interpreter.
#
# Two things trip people up, and both are our fault rather than theirs:
#
#   1. The virtualenv lives at `backend/.venv`, because that is where
#      `requirements.txt` is. A third terminal opened for the demo has not
#      activated it, so `pytest` is simply not on PATH.
#   2. `pytest.ini` sits at the repository root and sets `pythonpath = backend`,
#      so the suite must be invoked from the root. Running it from `backend/`
#      gives "file or directory not found: tests/..." even with the venv active.
#
# Together those produce two different confusing errors for what is really one
# setup detail. This script resolves both and passes any arguments through:
#
#   ./run_tests.sh                                  # everything
#   ./run_tests.sh tests/test_survival_scenarios.py -v
#   ./run_tests.sh -k rbac
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV_PYTHON="$ROOT/backend/.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  VENV_PYTHON="$ROOT/backend/.venv/Scripts/python.exe"   # Windows layout
fi

if [ -x "$VENV_PYTHON" ]; then
  PYTHON="$VENV_PYTHON"
elif python3 -c "import pytest" >/dev/null 2>&1; then
  # No virtualenv, but pytest is importable — someone installed globally.
  PYTHON="python3"
else
  cat >&2 <<'MSG'
Could not find pytest.

The virtualenv is expected at backend/.venv. To create it:

    cd backend
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

Then run this script again from anywhere:

    ./run_tests.sh
MSG
  exit 1
fi

# `python -m pytest` rather than the `pytest` binary: it puts the repository
# root on sys.path, which is what conftest.py and `pythonpath = backend` expect.
exec "$PYTHON" -m pytest "${@:-tests/}"
