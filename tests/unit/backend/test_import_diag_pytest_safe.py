"""Spine: the import-chain diagnostic must never abort a pytest run.

Live-fire a1-brain-20260706-014931: ``test_jarvis_import.py`` (a manual
diagnostic script collected by pytest because of its ``test_`` name) did
``import torch`` + ``sys.exit(1)`` at MODULE level. On the lean brain venv
(no torch) pytest's mainloop caught the SystemExit mid-collection ->
INTERNALERROR rc=3 -> the ENTIRE suite aborted, byte-identically, on every
poll -> the A1 vector's FAILED line never reached TestWatcher -> the
stability streak could never complete -> no test_failure signal, session
after session.

Contract: importing the diagnostic module must be side-effect free (no
prints, no imports of heavy deps, no sys.exit); its body runs only under
``__main__``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_DIAG = _REPO / "tests" / "unit" / "backend" / "test_jarvis_import.py"


def test_diag_module_import_is_side_effect_free():
    """Importing the module must not exit, print, or require torch."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('diag', %r)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print('IMPORT_OK')\n" % str(_DIAG)
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, (
        "diagnostic module exited at import (rc=%d) — this is the exact "
        "suite-killer class from a1-brain-20260706-014931:\n%s"
        % (r.returncode, (r.stderr or r.stdout)[-500:])
    )
    assert "IMPORT_OK" in r.stdout
    # No diagnostic chatter at import time (body must be __main__-guarded).
    assert "Testing JARVIS import chain" not in r.stdout


def test_diag_keeps_manual_entrypoint():
    """The manual diagnostic purpose is preserved (main + __main__ guard)."""
    src = _DIAG.read_text(encoding="utf-8")
    assert "def main(" in src
    assert '__name__ == "__main__"' in src or "__name__ == '__main__'" in src
