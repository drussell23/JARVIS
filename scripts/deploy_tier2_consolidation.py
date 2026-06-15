#!/usr/bin/env python3
"""Tier 2 deployer — wire the SemanticConsolidationMatrix into the orchestrator's lesson
choke point, SAFELY + reversibly. Same armor + discipline as deploy_live_validator_fsm.py.

Integration point (VERIFIED): every session lesson flows through ONE central append —
`self._session_lessons.append((lesson_type, lesson_text))` (unique anchor). We inject a
flag-gated, fail-soft `get_default_matrix(...).record(Lesson(...))` right after it, so the
matrix sees ALL failure lessons (incl. live-fire build failures) with a single edit and no
new method. The matrix is a process-wide lazy singleton (get_default_matrix), so no
instance plumbing is injected.

Armor (identical guarantees to the validator deployer):
  * EXACT-CONTEXT injection (anchor count must == 1) — refuses on drift.
  * Rollback gate = REAL subprocess import of the modified orchestrator (NOT ast.parse) —
    auto-revert from .bak on any boot failure OR exception (fail-closed).
  * Injected hook is FLAG-GATED (JARVIS_SEMANTIC_CONSOLIDATION_ENABLED, default OFF) +
    FAIL-SOFT — cannot perturb the lesson hot path (worst case: no-op).
  * Idempotent (marker check → no-op).

Run sandbox-off on a main+.env checkout:  python3 scripts/deploy_tier2_consolidation.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ORCH = Path("backend/core/ouroboros/governance/orchestrator.py")
BAK = ORCH.with_suffix(".py.tier2.bak")
MARKER = "Tier 2: semantic consolidation"
ANCHOR = "        self._session_lessons.append((lesson_type, lesson_text))\n"

WIRING = (
    "        # --- Tier 2: semantic consolidation (flag-gated, fail-soft) ---\n"
    "        try:\n"
    "            import os as _sc_os\n"
    "            if _sc_os.getenv('JARVIS_SEMANTIC_CONSOLIDATION_ENABLED', 'false').strip().lower() \\\n"
    "                    in ('1', 'true', 'yes', 'on'):\n"
    "                from backend.core.ouroboros.governance.semantic_consolidation import (\n"
    "                    get_default_matrix as _sc_matrix, Lesson as _sc_Lesson,\n"
    "                )\n"
    "                _sc_root = getattr(getattr(self, '_config', None), 'project_root', None)\n"
    "                _sc_matrix(_sc_root).record(\n"
    "                    _sc_Lesson(signature=str(lesson_text), kind=str(lesson_type))\n"
    "                )\n"
    "        except Exception:\n"
    "            pass\n"
    "        # --- end Tier 2 ---\n"
)


def _default_boot_check():
    proc = subprocess.run(
        [sys.executable, "-c",
         "import backend.core.ouroboros.governance.orchestrator; print('IMPORT_OK')"],
        capture_output=True, text=True, timeout=180,
    )
    ok = proc.returncode == 0 and "IMPORT_OK" in proc.stdout
    return ok, (proc.stderr or proc.stdout)[-3000:]


def deploy(*, orch: Path = ORCH, boot_check=_default_boot_check) -> int:
    if not orch.is_file():
        print(f"ERROR: {orch} not found (run from repo root)", file=sys.stderr)
        return 2
    src = orch.read_text(encoding="utf-8")
    if MARKER in src:
        print("Already deployed (marker present) — no-op.")
        return 0
    n = src.count(ANCHOR)
    if n != 1:
        print(f"ERROR: anchor found {n} times (expected 1) — orchestrator drifted; "
              f"refusing fuzzy injection.", file=sys.stderr)
        return 3

    shutil.copy2(orch, BAK)
    patched = src.replace(ANCHOR, ANCHOR + WIRING, 1)
    orch.write_text(patched, encoding="utf-8")
    print(f"Injected Tier-2 wiring after anchor; backup at {BAK}")

    try:
        ok, detail = boot_check()
    except Exception as err:  # noqa: BLE001
        ok, detail = False, f"boot check raised: {err!r}"

    if not ok:
        shutil.copy2(BAK, orch)
        print(f"BOOT CHECK FAILED → auto-reverted from {BAK}.\n--- detail ---\n{detail}",
              file=sys.stderr)
        return 4

    print("BOOT CHECK PASSED (orchestrator imports clean). Tier-2 wiring deployed.\n"
          "With JARVIS_SEMANTIC_CONSOLIDATION_ENABLED=true, recurring failure lessons cluster "
          "and distill into CORE_DIRECTIVE STYLE memories; on commit, the matrix retires the "
          "superseded episodes via the integrity-preserving episodic prune (RAM eviction + "
          "append-only supersession tombstone — the durable hash chain is never erased).")
    return 0


if __name__ == "__main__":
    raise SystemExit(deploy())
