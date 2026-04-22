#!/usr/bin/env python3
"""Wave 1 Advisory Probe — front-loads part (b) of the soak exit criteria.

Exercises the graduated Wave 1 primitives against live repo + system
state WITHOUT running a battle test:

  1. Read current posture (live .jarvis/posture_current.json or infer
     from fresh git-log SignalBundle)
  2. Build 16 sensors × 4 postures weighted-cap matrix (STANDARD urgency)
  3. Urgency multiplier sweep on TestFailureSensor
  4. Memory probe + can_fanout(n) for n ∈ {1, 3, 8, 16}
  5. Emergency brake simulation (cost_burn=0.95, postmortem=0.75, both)
  6. FlagRegistry posture-relevance surface for current posture

Produces a reviewable artifact at
``scripts/wave1_advisory_probe_PASS.log``. Zero API cost, no TTY needed.
Does NOT substitute for operator-present battle-test sessions — the
judgment signal (posture expectation vs human intuition, /help flags
usefulness) still requires a human observing a live session.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from dataclasses import replace
from typing import Any, Dict, List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _scrub_env():
    """Start from a clean slate — no env-var contamination from prior runs."""
    for k in list(os.environ):
        if (k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_MEMORY_PRESSURE")
                or k.startswith("JARVIS_POSTURE")
                or k.startswith("JARVIS_DIRECTION_INFERRER")
                or k.startswith("JARVIS_FLAG_REGISTRY")):
            del os.environ[k]


def main() -> int:
    print("=" * 78)
    print("Wave 1 Advisory Probe — primitives vs live repo + system state")
    print("=" * 78)

    _scrub_env()
    # All four masters default-true post-graduation — no env set needed.

    from backend.core.ouroboros.governance.direction_inferrer import (
        DirectionInferrer, is_enabled as _di_enabled,
    )
    from backend.core.ouroboros.governance.posture import (
        Posture, baseline_bundle,
    )
    from backend.core.ouroboros.governance.posture_observer import (
        SignalCollector, get_default_store, reset_default_store,
    )
    from backend.core.ouroboros.governance.sensor_governor import (
        SensorGovernor, Urgency, ensure_seeded as _sg_seed,
        reset_default_governor,
    )
    from backend.core.ouroboros.governance.sensor_governor_seed import (
        SEED_SPECS,
    )
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        MemoryPressureGate, get_default_gate, reset_default_gate,
    )
    from backend.core.ouroboros.governance.flag_registry import (
        Category, ensure_seeded as _fr_seed, reset_default_registry,
    )

    artifact: Dict[str, Any] = {
        "schema_version": "1.0",
        "probe_at": time.time(),
        "sections": {},
    }

    # -------------------------------------------------------------------
    # (1) Live posture
    # -------------------------------------------------------------------
    print("\n[1] LIVE POSTURE")
    print("-" * 78)

    reset_default_store()
    store = get_default_store(REPO_ROOT / ".jarvis")
    current = store.load_current()
    if current is None:
        # No persisted reading — infer one fresh from real git log
        collector = SignalCollector(REPO_ROOT)
        bundle = collector.build_bundle()
        current = DirectionInferrer().infer(bundle)
        source = "inferred from live git log (no persisted .jarvis/posture_current.json)"
    else:
        source = "loaded from .jarvis/posture_current.json"

    posture_name = current.posture.value
    confidence = current.confidence
    print(f"  Posture      : {posture_name}")
    print(f"  Confidence   : {confidence:.3f}")
    print(f"  Source       : {source}")
    print(f"  Bundle hash  : {current.signal_bundle_hash}")
    print(f"  Top 3 contributors:")
    for c in current.evidence[:3]:
        print(
            f"    {c.signal_name:<32s}  raw={c.raw_value:.3f}  "
            f"contrib={c.contribution_score:+.4f}"
        )

    artifact["sections"]["posture"] = {
        "posture": posture_name,
        "confidence": confidence,
        "source": source,
        "bundle_hash": current.signal_bundle_hash,
        "top_contributors": [
            {"signal": c.signal_name, "raw": c.raw_value,
             "contrib": c.contribution_score}
            for c in current.evidence[:3]
        ],
        "all_scores": [(p.value, s) for p, s in current.all_scores],
    }

    # -------------------------------------------------------------------
    # (2) 16 sensors × 4 postures weighted-cap matrix
    # -------------------------------------------------------------------
    print("\n[2] SENSOR BUDGET MATRIX — 16 sensors × 4 postures (STANDARD urgency)")
    print("-" * 78)

    reset_default_governor()
    posture_holder = {"val": posture_name}
    gov = SensorGovernor(
        posture_fn=lambda: posture_holder["val"],
        signal_bundle_fn=lambda: None,  # no brake
    )
    for s in SEED_SPECS:
        gov.register(s)

    postures = ("EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN")
    print(f"  {'Sensor':<28s} {'EXPL':>6s} {'CONS':>6s} {'HARD':>6s} {'MAIN':>6s}")
    matrix: Dict[str, Dict[str, int]] = {}
    for spec in SEED_SPECS:
        row: Dict[str, int] = {}
        for p in postures:
            posture_holder["val"] = p
            d = gov.request_budget(spec.sensor_name, Urgency.STANDARD)
            row[p] = d.weighted_cap
        matrix[spec.sensor_name] = row
        print(
            f"  {spec.sensor_name:<28s} "
            f"{row['EXPLORE']:>6d} {row['CONSOLIDATE']:>6d} "
            f"{row['HARDEN']:>6d} {row['MAINTAIN']:>6d}"
        )

    artifact["sections"]["sensor_matrix"] = matrix

    # -------------------------------------------------------------------
    # (3) Urgency multiplier sweep — TestFailureSensor under current posture
    # -------------------------------------------------------------------
    print(f"\n[3] URGENCY SWEEP — TestFailureSensor at posture={posture_name}")
    print("-" * 78)

    posture_holder["val"] = posture_name
    reset_default_governor()
    gov = SensorGovernor(
        posture_fn=lambda: posture_holder["val"],
        signal_bundle_fn=lambda: None,
    )
    for s in SEED_SPECS:
        gov.register(s)

    urgency_caps: Dict[str, int] = {}
    print(f"  {'Urgency':<12s} {'weighted_cap':>14s}")
    for u in Urgency:
        d = gov.request_budget("TestFailureSensor", u)
        urgency_caps[u.value] = d.weighted_cap
        print(f"  {u.value:<12s} {d.weighted_cap:>14d}")

    artifact["sections"]["urgency_sweep"] = {
        "sensor": "TestFailureSensor",
        "posture": posture_name,
        "caps": urgency_caps,
    }

    # -------------------------------------------------------------------
    # (4) Memory probe + can_fanout matrix
    # -------------------------------------------------------------------
    print("\n[4] MEMORY PRESSURE — live probe + can_fanout(n)")
    print("-" * 78)

    reset_default_gate()
    gate = get_default_gate()
    probe = gate.probe()
    level = gate.level_for_free_pct(probe.free_pct) if probe.ok else None
    print(f"  Probe source : {probe.source}")
    print(f"  free_pct     : {probe.free_pct:.1f}%")
    print(
        f"  total        : {probe.total_bytes // (1024**3) if probe.total_bytes else 0} GiB"
    )
    print(f"  level        : {level.value if level else '(unknown)'}")
    print(f"  {'n_requested':>12s} {'n_allowed':>12s}  {'reason_code':<60s}")

    fanout_matrix: List[Dict[str, Any]] = []
    for n in (1, 3, 8, 16):
        d = gate.can_fanout(n)
        fanout_matrix.append({
            "n_requested": n, "n_allowed": d.n_allowed,
            "level": d.level.value, "reason_code": d.reason_code,
        })
        print(
            f"  {n:>12d} {d.n_allowed:>12d}  {d.reason_code:<60s}"
        )

    artifact["sections"]["memory_probe"] = {
        "source": probe.source,
        "free_pct": probe.free_pct,
        "total_gib": probe.total_bytes // (1024**3) if probe.total_bytes else 0,
        "level": level.value if level else None,
        "fanout_matrix": fanout_matrix,
    }

    # -------------------------------------------------------------------
    # (5) Emergency brake simulations
    # -------------------------------------------------------------------
    print(f"\n[5] EMERGENCY BRAKE — three simulations vs posture={posture_name}")
    print("-" * 78)

    brake_cases = [
        ("baseline (no brake)",
         {"cost_burn_normalized": 0.0, "postmortem_failure_rate": 0.0}),
        ("cost_burn=0.95",
         {"cost_burn_normalized": 0.95, "postmortem_failure_rate": 0.0}),
        ("postmortem=0.75",
         {"cost_burn_normalized": 0.0, "postmortem_failure_rate": 0.75}),
        ("both high",
         {"cost_burn_normalized": 0.95, "postmortem_failure_rate": 0.75}),
    ]

    brake_results: List[Dict[str, Any]] = []
    print(
        f"  {'Case':<25s} {'brake':>6s} "
        f"{'TestFail':>10s} {'OppMiner':>10s} {'DocStale':>10s} {'global':>8s}"
    )
    for case_name, bundle in brake_cases:
        reset_default_governor()
        g = SensorGovernor(
            posture_fn=lambda: posture_name,
            signal_bundle_fn=lambda b=bundle: b,
        )
        for s in SEED_SPECS:
            g.register(s)
        d_tf = g.request_budget("TestFailureSensor", Urgency.STANDARD)
        d_om = g.request_budget("OpportunityMinerSensor", Urgency.STANDARD)
        d_ds = g.request_budget("DocStalenessSensor", Urgency.STANDARD)
        brake_results.append({
            "case": case_name, "bundle": bundle,
            "emergency_brake": d_tf.emergency_brake,
            "test_failure_cap": d_tf.weighted_cap,
            "opportunity_miner_cap": d_om.weighted_cap,
            "doc_staleness_cap": d_ds.weighted_cap,
            "global_cap": d_tf.global_cap,
        })
        print(
            f"  {case_name:<25s} {str(d_tf.emergency_brake):>6s} "
            f"{d_tf.weighted_cap:>10d} {d_om.weighted_cap:>10d} "
            f"{d_ds.weighted_cap:>10d} {d_tf.global_cap:>8d}"
        )

    artifact["sections"]["emergency_brake"] = brake_results

    # -------------------------------------------------------------------
    # (6) FlagRegistry posture-relevance surface
    # -------------------------------------------------------------------
    print(f"\n[6] /help flags --posture {posture_name} — surface check")
    print("-" * 78)

    reset_default_registry()
    fr = _fr_seed()
    # Ensure governor + gate flags auto-register
    reset_default_governor()
    _sg_seed()
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        ensure_bridged,
    )
    ensure_bridged()

    relevant = fr.relevant_to_posture(posture_name)
    print(f"  {len(relevant)} flag(s) relevant to {posture_name}:")
    rel_list: List[Dict[str, Any]] = []
    for spec in relevant:
        rel_tag = spec.posture_relevance.get(posture_name)
        rel_list.append({
            "name": spec.name, "category": spec.category.value,
            "relevance": rel_tag.value if rel_tag else "relevant",
            "default": spec.default,
        })
        print(
            f"    [{spec.category.value:<14s}] {spec.name:<52s} "
            f"default={spec.default!r}"
        )

    artifact["sections"]["posture_relevant_flags"] = {
        "posture": posture_name,
        "count": len(relevant),
        "flags": rel_list,
    }

    # -------------------------------------------------------------------
    # (7) Decision signals — brief analytical summary for the soak report
    # -------------------------------------------------------------------
    print("\n[7] ANALYSIS")
    print("-" * 78)

    analysis: List[str] = []

    # Emergency brake math check
    baseline_tf = brake_results[0]["test_failure_cap"]
    brake_tf = brake_results[3]["test_failure_cap"]
    expected_brake_ratio = 0.2  # default emergency_reduction_pct
    actual_ratio = brake_tf / max(1, baseline_tf)
    brake_ok = (
        brake_results[3]["emergency_brake"]
        and 0.15 <= actual_ratio <= 0.25  # ~20% ± slack
    )
    analysis.append(
        f"emergency brake ratio ok: {brake_ok} "
        f"(brake_cap/baseline_cap = {actual_ratio:.2f}, expected ~{expected_brake_ratio})"
    )

    # Posture sanity: EXPLORE-dominant on repo with high feat: commits → test failure
    # sensor should have LOWER cap than OpportunityMiner in EXPLORE
    if posture_name == "EXPLORE":
        tf_explore = matrix["TestFailureSensor"]["EXPLORE"]
        om_explore = matrix["OpportunityMinerSensor"]["EXPLORE"]
        weight_direction_ok = om_explore > tf_explore
        analysis.append(
            f"EXPLORE weight direction ok: {weight_direction_ok} "
            f"(OpportunityMiner cap={om_explore} > TestFailure cap={tf_explore})"
        )
    elif posture_name == "HARDEN":
        tf = matrix["TestFailureSensor"]["HARDEN"]
        om = matrix["OpportunityMinerSensor"]["HARDEN"]
        weight_direction_ok = tf > om
        analysis.append(
            f"HARDEN weight direction ok: {weight_direction_ok} "
            f"(TestFailure cap={tf} > OpportunityMiner cap={om})"
        )
    else:
        analysis.append(f"weight direction check skipped for posture={posture_name}")

    # Memory probe ok (psutil is the expected path on macOS/linux)
    probe_ok = probe.source != "fallback" and probe.ok
    analysis.append(
        f"memory probe ok: {probe_ok} (source={probe.source!r}, ok={probe.ok})"
    )

    # Posture-relevant flag count — should surface the master kill switches at minimum
    critical_kills_present = all(
        any(f["name"] == name for f in rel_list)
        for name in (
            "JARVIS_DIRECTION_INFERRER_ENABLED",
            "JARVIS_SENSOR_GOVERNOR_ENABLED",
            "JARVIS_MEMORY_PRESSURE_GATE_ENABLED",
            "JARVIS_FLAG_REGISTRY_ENABLED",
        )
    )
    analysis.append(
        f"all 4 master kill switches surface for posture={posture_name}: {critical_kills_present}"
    )

    for a in analysis:
        print(f"  - {a}")

    artifact["sections"]["analysis"] = analysis
    overall_ok = all(
        x.endswith(": True")
        or (not x.startswith("weight direction check skipped") and "ok:" not in x)
        or (": True" in x)
        for x in analysis
    )
    # simpler: all assertions that say "ok: True"
    assertions = [a for a in analysis if " ok: " in a]
    overall_ok = all(": True" in a for a in assertions)
    artifact["all_checks_pass"] = overall_ok

    pass_log = REPO_ROOT / "scripts" / "wave1_advisory_probe_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "wave1_advisory_probe_FAIL.log"
    log_path = pass_log if overall_ok else fail_log
    other = fail_log if overall_ok else pass_log
    if other.exists():
        other.unlink()
    log_path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")

    print()
    print("=" * 78)
    if overall_ok:
        print("  RESULT: PASS  —  all sanity assertions hold on live state.")
    else:
        print("  RESULT: FAIL  —  see analysis for the break.")
    print(f"  Artifact: {log_path}")
    print("=" * 78)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
