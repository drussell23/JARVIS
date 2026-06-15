#!/usr/bin/env python3
"""Slice 256 — Sovereign Transition Injector: wire the LiveKernelValidator into the
orchestrator VALIDATE phase, SAFELY + reversibly.

Design honesty (the same discipline the validator enforces):
  * The injection is EXACT-CONTEXT (a unique anchor string found exactly once), NOT
    fuzzy AST surgery — it refuses rather than guess if the anchor drifted.
  * The rollback gate is a REAL subprocess import of the modified orchestrator
    (reality), NOT ast.parse (syntax). ast.parse would pass a hook that references a
    nonexistent attr — the exact AttributeError class this engine hunts. If the live
    import fails, we auto-revert from the .bak.
  * The injected hook is FLAG-GATED (JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED, default OFF)
    and FAIL-SOFT (wrapped in try/except) so a subtly-wrong hook cannot crash VALIDATE
    — worst case it logs and no-ops. It reuses the EXISTING retry machinery by REBINDING
    validation via dataclasses.replace(passed=False) — VERIFIED-correct because
    op_context.ValidationResult is @dataclass(frozen=True), so a plain attribute set would
    raise FrozenInstanceError.

Run sandbox-off on a main+.env checkout:  python3 scripts/deploy_live_validator_fsm.py
Idempotent: refuses if the marker is already present.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ORCH = Path("backend/core/ouroboros/governance/orchestrator.py")
BAK = ORCH.with_suffix(".py.slice256.bak")
MARKER = "Slice 256: live-fire VALIDATE gate"
ANCHOR = "                    candidate, validation, _validate_duration_s = _vr\n"

WIRING = (
    "                    # --- Slice 256: live-fire VALIDATE gate (flag-gated, fail-soft) ---\n"
    "                    try:\n"
    "                        import os as _lf_os\n"
    "                        if (_lf_os.getenv('JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED', 'false')\n"
    "                                .strip().lower() in ('1', 'true', 'yes', 'on')) and getattr(validation, 'passed', False):\n"
    "                            import ast as _lf_ast\n"
    "                            from backend.core.ouroboros.governance.live_kernel_validator import (\n"
    "                                LiveKernelValidator as _LFV,\n"
    "                            )\n"
    "                            _lf_files = candidate.get('files') or [{\n"
    "                                'file_path': candidate.get('file_path', ''),\n"
    "                                'full_content': candidate.get('full_content', ''),\n"
    "                            }]\n"
    "                            _lf_changed = [f.get('file_path', '') for f in _lf_files if f.get('file_path')]\n"
    "                            if _LFV.affects_kernel(_lf_changed):\n"
    "                                _lf_syms = []\n"
    "                                for _lf_f in _lf_files:\n"
    "                                    try:\n"
    "                                        for _lf_n in _lf_ast.parse(_lf_f.get('full_content', '')).body:\n"
    "                                            if isinstance(_lf_n, (_lf_ast.FunctionDef, _lf_ast.AsyncFunctionDef, _lf_ast.ClassDef)):\n"
    "                                                _lf_syms.append(_lf_n.name)\n"
    "                                    except Exception:\n"
    "                                        pass\n"
    "                                _lf_res = await _LFV().validate_patch(changed_files=_lf_changed, affected_symbols=_lf_syms)\n"
    "                                if not _lf_res.ok:\n"
    "                                    logger.warning('[LiveFire] candidate FAILED live-fire boot: %s: %s',\n"
    "                                                   _lf_res.exception_type, (_lf_res.traceback or '')[-500:])\n"
    "                                    # VERIFIED: op_context.ValidationResult is @dataclass(frozen=True) —\n"
    "                                    # a plain `validation.passed = False` raises FrozenInstanceError. We\n"
    "                                    # REBIND the loop var via dataclasses.replace so downstream\n"
    "                                    # (`best_validation = validation`, the `if validation.passed` branch,\n"
    "                                    # ledger) sees the failed result and the EXISTING retry/escalation\n"
    "                                    # machinery handles route-back. failure_class='build' (the patch does\n"
    "                                    # not even import) routes it like a build break; tune if desired.\n"
    "                                    import dataclasses as _lf_dc\n"
    "                                    validation = _lf_dc.replace(\n"
    "                                        validation,\n"
    "                                        passed=False,\n"
    "                                        failure_class='build',\n"
    "                                        error='live-fire boot failure: ' + str(_lf_res.exception_type),\n"
    "                                        short_summary=('live-fire boot failure: ' + str(_lf_res.exception_type)\n"
    "                                                       + ': ' + (_lf_res.traceback or ''))[:300],\n"
    "                                    )\n"
    "                    except Exception as _lf_err:\n"
    "                        logger.warning('[LiveFire] gate error (fail-soft, skipped): %r', _lf_err)\n"
    "                    # --- end Slice 256 ---\n"
)


def _default_boot_check() -> tuple[bool, str]:
    """REAL validation: import the modified orchestrator in a subprocess. Catches
    syntax + import-time breakage the injection might introduce (ast.parse would not)."""
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
              f"refusing fuzzy injection. Re-derive the anchor.", file=sys.stderr)
        return 3

    shutil.copy2(orch, BAK)                      # .bak armor
    patched = src.replace(ANCHOR, ANCHOR + WIRING, 1)
    orch.write_text(patched, encoding="utf-8")
    print(f"Injected wiring after anchor; backup at {BAK}")

    try:
        ok, detail = boot_check()
    except Exception as err:  # noqa: BLE001
        ok, detail = False, f"boot check raised: {err!r}"

    if not ok:
        shutil.copy2(BAK, orch)                  # AUTO-REVERT
        print(f"BOOT CHECK FAILED → auto-reverted from {BAK}.\n--- detail ---\n{detail}",
              file=sys.stderr)
        return 4

    print("BOOT CHECK PASSED (orchestrator imports clean). Wiring deployed.\n"
          "NOTE: functional proof = the dogfood boot. The wiring is frozen-dataclass-safe "
          "(rebinds via dataclasses.replace). With JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED=true, "
          "a kernel-touching candidate that fails its live-fire import will log "
          "'[LiveFire] candidate FAILED live-fire boot' and route back through the existing "
          "retry machinery (failure_class=build).")
    return 0


if __name__ == "__main__":
    raise SystemExit(deploy())
