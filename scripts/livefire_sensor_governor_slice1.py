#!/usr/bin/env python3
"""Slice 1 live-fire: SensorGovernor on real process state.

Proves:
  1. ensure_seeded() installs 16 sensors
  2. All 4 postures coverable via posture_fn injection
  3. Posture-weighted caps match expected formula per sensor
  4. Emergency brake activates when cost_burn injected high
  5. Emergency brake activates when postmortem_rate injected high
  6. Global cap enforcement under saturation
  7. Rolling window eviction on timestamp push
  8. FlagRegistry auto-registration (Wave 1 #2 consumer)
  9. Wave 1 #1 consumer: posture filter reads real store
 10. Authority invariants on both arc files
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


def _scrub_env():
    for k in list(os.environ):
        if k.startswith("JARVIS_SENSOR_GOVERNOR"):
            del os.environ[k]


def main() -> int:
    print("=" * 72)
    print("SensorGovernor Slice 1 — Live-Fire on Real Process State")
    print("=" * 72)
    _scrub_env()

    from backend.core.ouroboros.governance.sensor_governor import (
        SensorGovernor, Urgency,
        ensure_seeded, reset_default_governor,
        get_default_governor, is_enabled,
    )
    from backend.core.ouroboros.governance.sensor_governor_seed import (
        SEED_SPECS,
    )

    checks = []

    # (1) Master flag default off
    checks.append(("is_enabled() default False (Slice 1)",
                   is_enabled() is False))

    # (2) Enable + seed
    os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"] = "true"
    reset_default_governor()
    gov = ensure_seeded()
    specs = gov.list_specs()
    print(f"[seed] installed {len(specs)} sensors")
    checks.append(("seed installed 16 sensors", len(specs) == 16))
    checks.append(("SEED_SPECS count matches governor",
                   len(SEED_SPECS) == len(specs)))

    # (3) Posture matrix — inject each posture + verify expected cap
    reset_default_governor()
    posture_holder = {"val": "EXPLORE"}
    gov = SensorGovernor(
        posture_fn=lambda: posture_holder["val"],
        signal_bundle_fn=lambda: None,
    )
    for s in SEED_SPECS:
        gov.register(s)

    print("\n[posture matrix] TestFailureSensor:")
    for posture in ("EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"):
        posture_holder["val"] = posture
        d = gov.request_budget("TestFailureSensor", Urgency.STANDARD)
        print(f"  {posture:13s} weighted_cap={d.weighted_cap:3d}")

    # TestFailure: base=20, HARDEN=1.8 → 36
    posture_holder["val"] = "HARDEN"
    d = gov.request_budget("TestFailureSensor", Urgency.STANDARD)
    checks.append(("TestFailure HARDEN cap = 36 (20 * 1.8)",
                   d.weighted_cap == 36))

    # OpportunityMiner: base=15, EXPLORE=1.5 → 22
    posture_holder["val"] = "EXPLORE"
    d = gov.request_budget("OpportunityMinerSensor", Urgency.STANDARD)
    checks.append(("OpportunityMiner EXPLORE cap = 22 (15 * 1.5)",
                   d.weighted_cap == 22))

    # OpportunityMiner: base=15, HARDEN=0.3 → 4
    posture_holder["val"] = "HARDEN"
    d = gov.request_budget("OpportunityMinerSensor", Urgency.STANDARD)
    checks.append(("OpportunityMiner HARDEN cap = 4 (15 * 0.3)",
                   d.weighted_cap == 4))

    # DocStaleness: base=6, CONSOLIDATE=1.3 → 7
    posture_holder["val"] = "CONSOLIDATE"
    d = gov.request_budget("DocStalenessSensor", Urgency.STANDARD)
    checks.append(("DocStaleness CONSOLIDATE cap = 7 (6 * 1.3)",
                   d.weighted_cap == 7))

    # (4) Emergency brake — high cost_burn
    bundle_holder = {"val": None}
    gov_brake = SensorGovernor(
        posture_fn=lambda: "MAINTAIN",
        signal_bundle_fn=lambda: bundle_holder["val"],
    )
    for s in SEED_SPECS:
        gov_brake.register(s)

    bundle_holder["val"] = {"cost_burn_normalized": 0.95, "postmortem_failure_rate": 0.0}
    d = gov_brake.request_budget("TestFailureSensor", Urgency.STANDARD)
    print(f"\n[brake cost] TestFailure cap={d.weighted_cap} brake={d.emergency_brake}")
    checks.append(("emergency brake activates on cost_burn 0.95",
                   d.emergency_brake is True))
    # base 20 * 1.0 (MAINTAIN) * 0.2 (brake) = 4
    checks.append(("cost-brake cap reduced to 4", d.weighted_cap == 4))

    # (5) Emergency brake — high postmortem
    bundle_holder["val"] = {"cost_burn_normalized": 0.0, "postmortem_failure_rate": 0.75}
    d = gov_brake.request_budget("TestFailureSensor", Urgency.STANDARD)
    print(f"[brake pm] TestFailure cap={d.weighted_cap} brake={d.emergency_brake}")
    checks.append(("emergency brake activates on postmortem 0.75",
                   d.emergency_brake is True))

    # (6) Global cap under saturation
    os.environ["JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR"] = "10"
    bundle_holder["val"] = None  # no brake
    gov_global = SensorGovernor(
        posture_fn=lambda: "MAINTAIN",
        signal_bundle_fn=lambda: None,
    )
    for s in SEED_SPECS:
        gov_global.register(s)
    for _ in range(10):
        gov_global.record_emission("TestFailureSensor")
    d = gov_global.request_budget("OpportunityMinerSensor", Urgency.STANDARD)
    print(f"\n[global cap] request after saturation: allowed={d.allowed} reason={d.reason_code}")
    checks.append(("global cap exhausted denies cross-sensor request",
                   d.allowed is False
                   and d.reason_code == "governor.global_cap_exhausted"))
    del os.environ["JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR"]

    # (7) Rolling window eviction
    os.environ["JARVIS_SENSOR_GOVERNOR_WINDOW_S"] = "60"
    gov_win = SensorGovernor(
        posture_fn=lambda: None, signal_bundle_fn=lambda: None,
    )
    gov_win.register(SEED_SPECS[0])
    now = time.monotonic()
    # 2 old + 1 fresh timestamp
    gov_win._per_sensor[SEED_SPECS[0].sensor_name].extend(
        [now - 120, now - 90, now - 30],
    )
    gov_win._global.extend([now - 120, now - 90, now - 30])
    d = gov_win.request_budget(SEED_SPECS[0].sensor_name, Urgency.STANDARD)
    checks.append(("rolling window evicts 2 of 3 old timestamps",
                   d.current_count == 1))
    del os.environ["JARVIS_SENSOR_GOVERNOR_WINDOW_S"]

    # (8) FlagRegistry auto-registration (Wave 1 #2 consumer)
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"
    from backend.core.ouroboros.governance.flag_registry import (
        ensure_seeded as _fr_seed, reset_default_registry,
    )
    reset_default_registry()
    reset_default_governor()
    ensure_seeded()  # seeds governor + registers flags into Wave 1 #2
    fr = _fr_seed()
    gov_flags_registered = all(
        fr.get_spec(name) is not None
        for name in (
            "JARVIS_SENSOR_GOVERNOR_ENABLED",
            "JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR",
            "JARVIS_SENSOR_GOVERNOR_WINDOW_S",
            "JARVIS_SENSOR_GOVERNOR_EMERGENCY_REDUCTION_PCT",
            "JARVIS_SENSOR_GOVERNOR_EMERGENCY_COST_THRESHOLD",
            "JARVIS_SENSOR_GOVERNOR_EMERGENCY_POSTMORTEM_THRESHOLD",
        )
    )
    print(f"\n[wave 1 #2 bridge] 6 governor flags registered in FlagRegistry: {gov_flags_registered}")
    checks.append(("Wave 1 #2 bridge: 6 flags auto-registered",
                   gov_flags_registered))

    # (9) Wave 1 #1 consumer: default posture_fn reads real store
    os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
    from backend.core.ouroboros.governance.posture_observer import (
        reset_default_store, get_default_store,
    )
    from backend.core.ouroboros.governance.direction_inferrer import (
        DirectionInferrer,
    )
    from backend.core.ouroboros.governance.posture import baseline_bundle
    from dataclasses import replace
    import tempfile

    with tempfile.TemporaryDirectory(prefix="livefire_gov_slice1_") as tmp:
        reset_default_store()
        store = get_default_store(pathlib.Path(tmp) / ".jarvis")
        # Prime with HARDEN reading
        harden_bundle = replace(
            baseline_bundle(), fix_ratio=0.75,
            postmortem_failure_rate=0.55,
            iron_gate_reject_rate=0.45,
            session_lessons_infra_ratio=0.80,
        )
        store.write_current(DirectionInferrer().infer(harden_bundle))

        reset_default_governor()
        gov = ensure_seeded()  # uses default posture_fn
        d = gov.request_budget("TestFailureSensor", Urgency.STANDARD)
        print(f"[wave 1 #1 bridge] TestFailure on live HARDEN store: posture={d.posture} cap={d.weighted_cap}")
        checks.append(("Wave 1 #1 bridge: TestFailure uses HARDEN posture",
                       d.posture == "HARDEN" and d.weighted_cap == 36))

    reset_default_store()

    # (10) Authority invariants
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    for relpath in (
        "backend/core/ouroboros/governance/sensor_governor.py",
        "backend/core/ouroboros/governance/sensor_governor_seed.py",
    ):
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

    pass_log = REPO_ROOT / "scripts" / "livefire_sensor_governor_slice1_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_sensor_governor_slice1_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 1,
        "feature": "SensorGovernor + 16-sensor seed",
        "timestamp": time.time(),
        "all_pass": all_pass,
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
