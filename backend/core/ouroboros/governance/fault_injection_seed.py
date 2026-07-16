"""
Fault-Injection Scenario Seed — the concrete scenarios (autonomy Gap 1)
======================================================================

Seeds ``fault_injection_matrix`` with reversible fault scenarios, each over a
CLEAN injectable seam of the defense it targets (pure decision fn / first-class
kwarg / documented reset seam — NEVER ``unittest.mock.patch``). Mirrors
``flag_registry_seed``: this module is data, the matrix runner is control flow.

Each scenario proves ONE stability defense fires + contains its fault:
  * ORPHAN_WORKER      — worker_lifeline ppid-drift → EXIT_CODE_ORPHANED
  * ORPHAN_CHILD       — child_reaper cascade reaps a registered child
  * WATCHDOG_FALSE_POSITIVE — evaluate_kill kills genuine stale, SUPPRESSES suspend
  * WATCHDOG_BEAT_DEATH — beat() write fault is COUNTED, not silent (Slice 29)
  * WATCHDOG_MONITOR_DEATH — supervisor restarts on death, NOT on teardown
  * MEMORY_EXHAUSTION  — MemoryPressureGate clamps fanout under CRITICAL pressure
  * PROVIDER_OUTAGE    — provider_quarantine detects a full window of failures
  * SEVERED_CAPABILITY — the Gap 2 oracle SEES an induced dormant capability

Classes with a defense but no deterministic scenario yet (EMBED_CORRELATED_
FAILURE, LOOP_STARVATION, COST_RUNAWAY, TEARDOWN_WEDGE) surface as NO_SCENARIO
coverage gaps — honestly, not silently — since they need a real-fault (soak /
live-loop) lane the matrix's deterministic lane does not cover.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict

from backend.core.ouroboros.governance.fault_injection_matrix import (
    FaultScenario, ResilienceFaultClass,
)


def seed_scenarios(register: Callable[[FaultScenario], None]) -> None:
    """Register every seed scenario. NEVER raises (a bad scenario is skipped)."""
    for factory in (
        _orphan_worker, _orphan_child, _watchdog_false_positive,
        _watchdog_beat_death, _watchdog_monitor_death, _memory_exhaustion,
        _provider_outage, _severed_capability,
    ):
        try:
            register(factory())
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------
# 1. Orphan worker — worker_lifeline ppid-drift (pure decision fn)
# ---------------------------------------------------------------------------


def _orphan_worker() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.governance.worker_lifeline import (
            _tick_decision, EXIT_CODE_ORPHANED,
        )
        # armed under ppid 1000, now reparented to init (1) = the orphan fault.
        drift = _tick_decision(1000, 1, None, None)
        healthy = _tick_decision(5, 5, None, None)
        return {"drift": drift, "healthy": healthy, "code": EXIT_CODE_ORPHANED}

    def verify(h: Any) -> Dict[str, bool]:
        drift, healthy, code = h["drift"], h["healthy"], h["code"]
        return {
            "detects_orphan": drift is not None and drift[0] == code,
            "reason_names_drift": drift is not None and "orphan" in str(drift[1]).lower(),
            "healthy_does_not_exit": healthy is None,
        }

    return FaultScenario(
        id="orphan_worker.ppid_drift",
        fault_class=ResilienceFaultClass.ORPHAN_WORKER,
        defense="worker_lifeline._tick_decision",
        description="A worker whose parent died (ppid drift) must self-exit "
                    "with EXIT_CODE_ORPHANED; a healthy worker must not.",
        inject=inject, verify=verify,
    )


# ---------------------------------------------------------------------------
# 2. Orphan child — child_reaper cascade (real sacrificial subprocess)
# ---------------------------------------------------------------------------


def _orphan_child() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.governance import child_reaper
        child_reaper.reset_for_tests()
        p = subprocess.Popen(["sleep", "60"])
        child_reaper.register_child(p.pid, role="fault-test-child")
        return p

    def verify(p: Any) -> Dict[str, bool]:
        from backend.core.ouroboros.governance import child_reaper
        n = child_reaper.cascade_terminate(grace_s=0.4)
        # give the OS a beat to reap
        try:
            p.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        return {
            "reaped_at_least_one": n >= 1,
            "registry_drained": child_reaper.registered_children() == (),
            "child_terminated": p.poll() is not None,
        }

    def revert(p: Any) -> None:
        from backend.core.ouroboros.governance import child_reaper
        try:
            if p is not None and p.poll() is None:
                p.kill()
        finally:
            child_reaper.reset_for_tests()

    return FaultScenario(
        id="orphan_child.cascade_reap",
        fault_class=ResilienceFaultClass.ORPHAN_CHILD,
        defense="child_reaper.cascade_terminate",
        description="A registered child must be reaped by cascade_terminate and "
                    "removed from the registry.",
        inject=inject, verify=verify, revert=revert,
    )


# ---------------------------------------------------------------------------
# 3. Watchdog false-positive guard — evaluate_kill (pure fn)
# ---------------------------------------------------------------------------


def _watchdog_false_positive() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.governance.external_watchdog import evaluate_kill
        base = dict(now_wall=1000.0, armed_wall=0.0, last_beat_wall=800.0,
                    budget_s=1e12, stale_window_s=120.0)
        return {
            "genuine": evaluate_kill(**base, suspended=False),
            "suspend": evaluate_kill(**base, suspended=True),
        }

    def verify(h: Any) -> Dict[str, bool]:
        return {
            "kills_genuine_stale": h["genuine"] == (True, "heartbeat_stale"),
            "suppresses_on_suspend": h["suspend"] == (False, ""),
        }

    return FaultScenario(
        id="watchdog.suspend_false_positive_guard",
        fault_class=ResilienceFaultClass.WATCHDOG_FALSE_POSITIVE,
        defense="external_watchdog.evaluate_kill",
        description="A stale heartbeat must SIGKILL when real time elapsed, but "
                    "a host-suspend interval must NOT be forged as a wedge.",
        inject=inject, verify=verify,
    )


# ---------------------------------------------------------------------------
# 4. Watchdog beat-writer death — beat() visibility (Slice 29 counters)
# ---------------------------------------------------------------------------


def _watchdog_beat_death() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.governance.external_watchdog import (
            ExternalProcessWatchdog,
        )
        # A heartbeat path whose parent is a FILE → mkdir fails → beat() OSError.
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.close()
        bad_path = Path(tf.name) / "hb.tick"  # parent is a file, not a dir
        wd = ExternalProcessWatchdog(
            target_pid=os.getpid(), heartbeat_path=bad_path,
            budget_s=1e12, stale_window_s=120.0,
        )
        wd.beat()
        wd.beat()
        return {"wd": wd, "tmp": tf.name}

    def verify(h: Any) -> Dict[str, bool]:
        wd = h["wd"]
        return {
            "write_failure_counted": getattr(wd, "beat_failures", 0) >= 1,
            "no_false_success": getattr(wd, "beat_successes", 1) == 0,
        }

    def revert(h: Any) -> None:
        try:
            os.unlink(h["tmp"])
        except Exception:  # noqa: BLE001
            pass

    return FaultScenario(
        id="watchdog.beat_write_failure_visible",
        fault_class=ResilienceFaultClass.WATCHDOG_BEAT_DEATH,
        defense="external_watchdog.ExternalProcessWatchdog.beat",
        description="A silently-failing heartbeat write (the bt-233357 false-kill "
                    "root cause) must be COUNTED, not swallowed invisibly.",
        inject=inject, verify=verify, revert=revert,
    )


# ---------------------------------------------------------------------------
# 5. Watchdog monitor death — Slice 29 supervisor (pure decision fn)
# ---------------------------------------------------------------------------


def _watchdog_monitor_death() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.battle_test.harness import (
            _should_restart_wall_clock_monitor as sr,
        )
        return {
            "death": sr(cancelled=False, exc=RuntimeError("x"), restarts=0,
                        max_restarts=5, closing=False),
            "teardown": sr(cancelled=False, exc=RuntimeError("x"), restarts=0,
                           max_restarts=5, closing=True),
            "bounded": sr(cancelled=False, exc=RuntimeError("x"), restarts=5,
                          max_restarts=5, closing=False),
            "cancelled": sr(cancelled=True, exc=None, restarts=0,
                            max_restarts=5, closing=False),
        }

    def verify(h: Any) -> Dict[str, bool]:
        return {
            "restarts_on_unexpected_death": h["death"][0] is True,
            "no_restart_during_teardown": h["teardown"][0] is False,
            "bounded_by_restart_budget": h["bounded"][0] is False,
            "no_restart_on_cancellation": h["cancelled"][0] is False,
        }

    return FaultScenario(
        id="watchdog.monitor_supervisor_restart",
        fault_class=ResilienceFaultClass.WATCHDOG_MONITOR_DEATH,
        defense="harness._should_restart_wall_clock_monitor",
        description="The beat-writer monitor task must be restarted on unexpected "
                    "death but NOT on teardown/cancellation, bounded by budget.",
        inject=inject, verify=verify,
    )


# ---------------------------------------------------------------------------
# 6. Memory exhaustion — MemoryPressureGate fanout clamp (probe_fn seam)
# ---------------------------------------------------------------------------


def _memory_exhaustion() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            MemoryPressureGate, MemoryProbe,
        )
        prev = os.environ.get("JARVIS_MEMORY_PRESSURE_GATE_ENABLED")
        os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"] = "true"
        # Force process-tree + reservation dims off so the free-% dim is the
        # deterministic driver of this scenario.
        prev_proc = os.environ.get("JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED")
        prev_res = os.environ.get("JARVIS_MEMORY_PRESSURE_RESERVATIONS_ENABLED")
        os.environ["JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED"] = "false"
        os.environ["JARVIS_MEMORY_PRESSURE_RESERVATIONS_ENABLED"] = "false"
        total = 16 * (1024 ** 3)
        probe = MemoryProbe(free_pct=3.0, total_bytes=total,
                            available_bytes=int(0.03 * total), source="fault",
                            ok=True, error=None)
        gate = MemoryPressureGate(probe_fn=lambda: probe)
        return {"gate": gate, "prev": prev, "prev_proc": prev_proc, "prev_res": prev_res}

    def verify(h: Any) -> Dict[str, bool]:
        gate = h["gate"]
        d = gate.can_fanout(8)
        return {
            "clamps_fanout_under_pressure": d.n_allowed < 8,
            "level_is_critical": d.level.value == "critical",
            "still_allows_some_progress": d.n_allowed >= 1,
        }

    def revert(h: Any) -> None:
        for key, val in (
            ("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", h.get("prev")),
            ("JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED", h.get("prev_proc")),
            ("JARVIS_MEMORY_PRESSURE_RESERVATIONS_ENABLED", h.get("prev_res")),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    return FaultScenario(
        id="memory.fanout_clamp_under_pressure",
        fault_class=ResilienceFaultClass.MEMORY_EXHAUSTION,
        defense="memory_pressure_gate.MemoryPressureGate.can_fanout",
        description="Under CRITICAL free-memory pressure the gate must clamp "
                    "parallel fan-out (while still allowing some progress).",
        inject=inject, verify=verify, revert=revert,
    )


# ---------------------------------------------------------------------------
# 7. Provider outage — provider_quarantine detection (record/reset seam)
# ---------------------------------------------------------------------------


def _provider_outage() -> FaultScenario:
    _ROUTE = "__fault_injection_probe__"

    def inject() -> Any:
        prev = os.environ.get("JARVIS_PROVIDER_QUARANTINE_ENABLED")
        os.environ["JARVIS_PROVIDER_QUARANTINE_ENABLED"] = "true"
        from backend.core.ouroboros.governance.provider_quarantine import (
            get_provider_health_gradient,
        )
        g = get_provider_health_gradient()
        g.reset(_ROUTE)
        for _ in range(64):  # saturate any reasonable rolling window
            g.record_sweep(_ROUTE, success=False)
        return {"g": g, "prev": prev}

    def verify(h: Any) -> Dict[str, bool]:
        g = h["g"]
        return {
            "window_full": g.window_full(_ROUTE),
            "detects_global_outage": g.is_global_outage(_ROUTE),
            "success_rate_zero": g.success_rate(_ROUTE) == 0.0,
        }

    def revert(h: Any) -> None:
        try:
            h["g"].reset(_ROUTE)
        finally:
            if h.get("prev") is None:
                os.environ.pop("JARVIS_PROVIDER_QUARANTINE_ENABLED", None)
            else:
                os.environ["JARVIS_PROVIDER_QUARANTINE_ENABLED"] = h["prev"]

    return FaultScenario(
        id="provider.quarantine_detects_outage",
        fault_class=ResilienceFaultClass.PROVIDER_OUTAGE,
        defense="provider_quarantine.ProviderHealthGradient",
        description="A full rolling window of failed sweeps on a route must be "
                    "detected as a global outage.",
        inject=inject, verify=verify, revert=revert,
    )


# ---------------------------------------------------------------------------
# 8. Severed capability — the Gap 2 oracle SEES a dormant capability
# ---------------------------------------------------------------------------


def _severed_capability() -> FaultScenario:
    def inject() -> Any:
        from backend.core.ouroboros.governance.capability_firing import (
            FiringEvidence, firing_verdict,
        )
        # A capability whose ONLY observable channel is a ledger it writes.
        src = 'x = ".jarvis/probe_history.jsonl"\n'
        dormant_ev = FiringEvidence(present_markers=set(), active_ledgers=set())
        active_ev = FiringEvidence(active_ledgers={"probe_history"})
        return {
            "dormant": firing_verdict(src, dormant_ev),
            "active": firing_verdict(src, active_ev),
        }

    def verify(h: Any) -> Dict[str, bool]:
        d_verdict, d_hits, d_chan = h["dormant"]
        a_verdict, _a_hits, _a_chan = h["active"]
        return {
            "detects_dormancy": d_verdict == "SILENT",
            "dormancy_is_ledger_backed": "ledger" in d_chan,
            "does_not_false_flag_active": a_verdict == "FIRING",
        }

    return FaultScenario(
        id="capability.gap2_oracle_sees_dormancy",
        fault_class=ResilienceFaultClass.SEVERED_CAPABILITY,
        defense="capability_firing.firing_verdict",
        description="The Gap 2 self-perception oracle must flag a reachable "
                    "capability whose evidence-of-work ledger went quiet as "
                    "SILENT/dormant, and must NOT false-flag one still firing.",
        inject=inject, verify=verify,
    )
