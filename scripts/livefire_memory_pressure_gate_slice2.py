#!/usr/bin/env python3
"""Slice 2 live-fire: MemoryPressureGate on real system state.

  1. Default gate uses cascade + real-system probe succeeds
  2. Pressure level reported on real free memory
  3. can_fanout(16) on real system returns sensible decision
  4. Forced CRITICAL via high threshold env → 1-unit cap
  5. Forced OK via low threshold env → unclamped
  6. Snapshot shape with real probe
  7. FlagRegistry auto-registration (Wave 1 #2 bridge)
  8. Master-off revert: gate always returns OK + allow
  9. Authority invariants
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _scrub():
    for k in list(os.environ):
        if k.startswith("JARVIS_MEMORY_PRESSURE"):
            del os.environ[k]


def main() -> int:
    print("=" * 72)
    print("MemoryPressureGate Slice 2 — Live-Fire on Real System")
    print("=" * 72)
    _scrub()

    from backend.core.ouroboros.governance.memory_pressure_gate import (
        MEMORY_PRESSURE_SCHEMA_VERSION,
        MemoryPressureGate, PressureLevel,
        ensure_bridged, get_default_gate, is_enabled, reset_default_gate,
    )

    checks = []

    # (1) Master default false
    checks.append(("is_enabled() default False (Slice 1-3)",
                   is_enabled() is False))

    # (2) Master-off: always OK + allow
    gate_off = MemoryPressureGate()
    d_off = gate_off.can_fanout(16)
    checks.append(("master off: can_fanout unclamped",
                   d_off.allowed is True and d_off.n_allowed == 16
                   and d_off.reason_code == "memory_pressure_gate.disabled"))
    checks.append(("master off: pressure() returns OK",
                   gate_off.pressure() is PressureLevel.OK))

    # Enable
    os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"] = "true"
    reset_default_gate()
    gate = get_default_gate()

    # (3) Default gate uses cascade + real probe
    probe = gate.probe()
    print(f"[probe] source={probe.source} free_pct={probe.free_pct:.1f}% "
          f"total={probe.total_bytes // (1024**3)}GiB")
    checks.append(("cascade probe returns ok result", probe.ok is True))
    checks.append(("probe source is not fallback",
                   probe.source in ("psutil", "proc_meminfo", "vm_stat")))
    checks.append(("probe has sensible total_bytes",
                   probe.total_bytes > 0))

    # (4) Current pressure level
    level = gate.pressure()
    print(f"[level] current pressure = {level.value}")
    checks.append(("pressure() returns valid level",
                   level in (PressureLevel.OK, PressureLevel.WARN,
                             PressureLevel.HIGH, PressureLevel.CRITICAL)))

    # (5) can_fanout(16) on real system
    d = gate.can_fanout(16)
    print(f"[fanout(16)] level={d.level.value} n_allowed={d.n_allowed} "
          f"reason={d.reason_code}")
    checks.append(("can_fanout(16) on real system produces valid decision",
                   d.n_allowed >= 1))
    checks.append(("can_fanout(16) clamps correctly for level",
                   (d.level is PressureLevel.OK and d.n_allowed == 16)
                   or (d.level is PressureLevel.WARN and d.n_allowed <= 8)
                   or (d.level is PressureLevel.HIGH and d.n_allowed <= 3)
                   or (d.level is PressureLevel.CRITICAL and d.n_allowed == 1)))

    # (6) Force CRITICAL via threshold env override
    free_pct_now = probe.free_pct
    # Set warn threshold above current free_pct so all levels trip
    os.environ["JARVIS_MEMORY_PRESSURE_WARN_PCT"] = str(free_pct_now + 50.0)
    os.environ["JARVIS_MEMORY_PRESSURE_HIGH_PCT"] = str(free_pct_now + 40.0)
    os.environ["JARVIS_MEMORY_PRESSURE_CRITICAL_PCT"] = str(free_pct_now + 30.0)
    d_crit = gate.can_fanout(16)
    print(f"[forced-critical] level={d_crit.level.value} n_allowed={d_crit.n_allowed}")
    checks.append(("forced CRITICAL threshold → CRITICAL level",
                   d_crit.level is PressureLevel.CRITICAL))
    checks.append(("forced CRITICAL → n_allowed clamped to 1",
                   d_crit.n_allowed == 1))

    # Restore thresholds
    for k in ("JARVIS_MEMORY_PRESSURE_WARN_PCT",
              "JARVIS_MEMORY_PRESSURE_HIGH_PCT",
              "JARVIS_MEMORY_PRESSURE_CRITICAL_PCT"):
        os.environ.pop(k, None)

    # (7) Force OK via ultra-low thresholds
    os.environ["JARVIS_MEMORY_PRESSURE_WARN_PCT"] = "0.1"
    os.environ["JARVIS_MEMORY_PRESSURE_HIGH_PCT"] = "0.05"
    os.environ["JARVIS_MEMORY_PRESSURE_CRITICAL_PCT"] = "0.01"
    d_ok = gate.can_fanout(16)
    print(f"[forced-ok] level={d_ok.level.value} n_allowed={d_ok.n_allowed}")
    checks.append(("forced OK thresholds → OK + unclamped",
                   d_ok.level is PressureLevel.OK and d_ok.n_allowed == 16))
    for k in ("JARVIS_MEMORY_PRESSURE_WARN_PCT",
              "JARVIS_MEMORY_PRESSURE_HIGH_PCT",
              "JARVIS_MEMORY_PRESSURE_CRITICAL_PCT"):
        os.environ.pop(k, None)

    # (8) Snapshot on real probe
    snap = gate.snapshot()
    checks.append(("snapshot schema_version=1.0",
                   snap["schema_version"] == MEMORY_PRESSURE_SCHEMA_VERSION))
    checks.append(("snapshot probe + level + thresholds + caps present",
                   all(k in snap for k in ("probe", "level", "thresholds",
                                            "fanout_caps"))))
    checks.append(("snapshot enabled flag matches master",
                   snap["enabled"] is True))

    # (9) FlagRegistry bridge
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"
    from backend.core.ouroboros.governance.flag_registry import (
        ensure_seeded as _fr_seed, reset_default_registry,
    )
    reset_default_registry()
    reset_default_gate()
    ensure_bridged()
    fr = _fr_seed()
    expected_flags = (
        "JARVIS_MEMORY_PRESSURE_GATE_ENABLED",
        "JARVIS_MEMORY_PRESSURE_WARN_PCT",
        "JARVIS_MEMORY_PRESSURE_HIGH_PCT",
        "JARVIS_MEMORY_PRESSURE_CRITICAL_PCT",
        "JARVIS_MEMORY_PRESSURE_WARN_FANOUT_CAP",
        "JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP",
        "JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP",
    )
    registered = all(fr.get_spec(n) is not None for n in expected_flags)
    print(f"\n[wave 1 #2 bridge] 7 gate flags registered: {registered}")
    checks.append(("Wave 1 #2 bridge: 7 gate flags auto-registered",
                   registered))

    # (10) Authority invariants
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    relpath = "backend/core/ouroboros/governance/memory_pressure_gate.py"
    src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    bad = []
    for line in src.splitlines():
        if line.startswith(("from ", "import ")):
            for forbidden in authority_forbidden:
                if f".{forbidden}" in line:
                    bad.append(line)
    checks.append((f"authority-free: {relpath}", not bad))

    # Report
    print()
    print("-" * 72)
    print(f"Checks ({len(checks)}):")
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}")
    print("-" * 72)

    pass_log = REPO_ROOT / "scripts" / "livefire_memory_pressure_gate_slice2_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_memory_pressure_gate_slice2_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 2,
        "feature": "MemoryPressureGate",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "probe_source": probe.source,
        "probe_free_pct": probe.free_pct,
        "total_checks": len(checks),
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print(f"\n  RESULT: PASS  —  {len(checks)}/{len(checks)} checks green.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
