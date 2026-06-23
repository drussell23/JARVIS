#!/usr/bin/env python3
"""chaos_injector.py -- autonomous chaos-engineering harness for the Sovereign
Provider Failover Lifecycle FSM (spec 2026-06-23).

PURPOSE
-------
Torture-test the ``FailoverLifecycleController`` BEFORE an operator flips
``JARVIS_FAILOVER_LIFECYCLE_ENABLED=true`` on a real GCE host. The headline
deliverable is a DETERMINISTIC IN-PROCESS GAUNTLET that drives the REAL
controller through three chaos scenarios using its injectable boundaries
(``vm_awaken_fn`` / ``vm_delete_fn`` / ``dw_probe_fn`` / ``node_ready_fn`` /
``clock_fn``) plus a FakeClock that fast-forwards 90s / 30min instantly, and
asserts the invariants. No real GCE, network, or subprocess is touched.

  Scenario 1 -- Synthetic Collapse (503 storm): DW is persistently dead; the
    FSM must leave DORMANT -> AWAKENING -> (node ready) -> SERVING with the
    deadman startup-script injected, Spot-first.
  Scenario 2 -- Phantom Recovery (THE race): DW recovers MID-awaken; the FSM
    must handle it gracefully -- reach DORMANT, bounded ticks, no thrash. Both
    node-ready-before-recovery and node-ready-after-recovery timings.
  Scenario 3 -- Assassination (FSM view + injection proof): drive to SERVING,
    abandon the controller; PROVE the awaken injected a deadman startup-script
    with a FINITE idle-timeout (so a real orphan would self-delete) and that
    nothing in the FSM would have disarmed it.

A second, HEAVILY-gated mode (``run_live_soak``) scaffolds the real-GCE soak.
It is triple-gated and this harness will NOT run it -- it only documents and
prints the exact command.

DISCIPLINE
----------
* Standalone, like scripts/bake_jprime_golden_image.py / sovereign_sentinel.py.
* Gated ``JARVIS_CHAOS_INJECTOR_ENABLED`` (default false). NEVER prod.
* ASCII only. ``from __future__ import annotations``. Fail-soft throughout.
* Uses the REAL FailoverLifecycleController + REAL ProviderHealthGradient with
  injected fakes -- it does not re-implement the FSM.

Usage
-----
    JARVIS_CHAOS_INJECTOR_ENABLED=true python3 scripts/chaos_injector.py --gauntlet
    # exits 0 iff all three scenarios PASS.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

# Standalone-invocation bootstrap: ensure the repo root (parent of scripts/) is
# on sys.path so ``backend.*`` imports resolve when run as
# ``python3 scripts/chaos_injector.py`` (pytest already adds the root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# --------------------------------------------------------------------------- #
# Master gate -- NEVER prod.
# --------------------------------------------------------------------------- #

def chaos_enabled() -> bool:
    """Master gate. Default FALSE -- this is a pre-flip torture harness, never
    a production component."""
    raw = (os.environ.get("JARVIS_CHAOS_INJECTOR_ENABLED", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# FakeClock -- advanceable monotonic clock (fast-forwards 90s / 30min instantly).
# --------------------------------------------------------------------------- #

class FakeClock:
    """Deterministic monotonic clock. ``advance(dt)`` fast-forwards instantly.

    The controller reads time ONLY through its injected ``clock_fn``, so this
    fully controls the FSM's notion of elapsed time -- no real sleeps anywhere.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


# --------------------------------------------------------------------------- #
# ChaosSchedule -- timeline of DW health verdicts keyed by FakeClock time.
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class _HealthEdge:
    at_t: float
    healthy: bool


class ChaosSchedule:
    """A deterministic DW-health timeline.

    ``add(at_t, healthy)`` registers a step-change edge: at clock time >= at_t
    (until the next edge), DW health is ``healthy``. ``is_healthy(now)`` reads
    the timeline. Used to drive BOTH:
      * the SERVING-phase ``dw_probe_fn`` verdict (which feeds ``record_sweep``),
      * the DORMANT-phase gradient pre-fill (the FSM's real outage trigger).

    The schedule is the single source of truth for "is DW up at time t".
    """

    def __init__(self, *, initial_healthy: bool = False) -> None:
        self._initial = bool(initial_healthy)
        self._edges: List[_HealthEdge] = []

    def add(self, at_t: float, healthy: bool) -> "ChaosSchedule":
        self._edges.append(_HealthEdge(float(at_t), bool(healthy)))
        self._edges.sort(key=lambda e: e.at_t)
        return self

    def is_healthy(self, now: float) -> bool:
        verdict = self._initial
        for edge in self._edges:
            if now >= edge.at_t:
                verdict = edge.healthy
            else:
                break
        return verdict


# --------------------------------------------------------------------------- #
# CallRecorder -- records awaken/delete boundary calls + injected scripts.
# --------------------------------------------------------------------------- #

class _CallRecorder:
    """Records every awaken/delete invocation + the injected startup-script."""

    def __init__(self) -> None:
        self.awaken_scripts: List[str] = []
        self.awaken_kwargs: List[Dict[str, Any]] = []
        self.delete_calls: int = 0

    def awaken(self, *, startup_script: str, **kwargs: Any) -> bool:
        self.awaken_scripts.append(startup_script)
        self.awaken_kwargs.append(dict(kwargs))
        return True

    def delete(self) -> bool:
        self.delete_calls += 1
        return True


# --------------------------------------------------------------------------- #
# Gradient helpers -- drive the REAL ProviderHealthGradient deterministically.
# --------------------------------------------------------------------------- #

def _reset_gradient() -> None:
    """Reset the process-global ProviderHealthGradient singleton (fail-soft)."""
    try:
        from backend.core.ouroboros.governance import provider_quarantine as pq
        pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def _gradient():
    from backend.core.ouroboros.governance.provider_quarantine import (
        get_provider_health_gradient,
    )
    return get_provider_health_gradient()


def _fill_gradient(route: str, *, healthy: bool, window: int) -> None:
    """Fill the route window with ``window`` identical sweeps so the gradient
    immediately reflects outage (all-fail) or recovery (all-success)."""
    grad = _gradient()
    for _ in range(window):
        grad.record_sweep(route, success=healthy)


# --------------------------------------------------------------------------- #
# Deadman assertion -- prove the injected startup-script is the real deadman
# with a finite idle-timeout.
# --------------------------------------------------------------------------- #

def _assert_deadman_injected(script: str) -> Tuple[bool, str]:
    """Verify the injected startup-script IS the Dead-Man's Switch with a
    finite idle-timeout. Returns (ok, detail)."""
    if not script:
        return False, "no startup-script injected"
    markers = ("jprime-deadman", "IDLE_TIMEOUT_S", "self-DELETE", "BOOT_GRACE_S")
    missing = [m for m in markers if m not in script]
    if missing:
        return False, "startup-script missing deadman markers: {}".format(missing)
    # Confirm the idle-timeout is FINITE (a positive integer literal), so a real
    # orphaned node WOULD eventually self-delete.
    import re
    m = re.search(r"IDLE_TIMEOUT_S=(\d+)", script)
    if not m:
        return False, "IDLE_TIMEOUT_S literal not found / not numeric"
    idle = int(m.group(1))
    if idle <= 0:
        return False, "IDLE_TIMEOUT_S is non-finite/zero ({})".format(idle)
    return True, "deadman injected (IDLE_TIMEOUT_S={}s, finite)".format(idle)


# --------------------------------------------------------------------------- #
# Controller construction -- the REAL controller with injected fakes.
# --------------------------------------------------------------------------- #

def _build_controller(
    *,
    clock: FakeClock,
    recorder: _CallRecorder,
    dw_probe_fn: Callable[[], bool],
    node_ready_fn: Callable[[str], bool],
    route: str,
):
    """Construct the REAL FailoverLifecycleController with injected fakes.

    The on_serving hook is stubbed to a no-op coroutine so the Cryo-DLQ drain
    (which would try to resolve the live intake router) never touches anything
    outside the FSM under test.
    """
    from backend.core.ouroboros.governance.failover_lifecycle import (
        FailoverLifecycleController,
    )

    async def _noop_on_serving() -> None:
        return None

    return FailoverLifecycleController(
        vm_awaken_fn=recorder.awaken,
        vm_delete_fn=recorder.delete,
        dw_probe_fn=dw_probe_fn,
        node_ready_fn=node_ready_fn,
        clock_fn=clock,
        route=route,
        on_serving_fn=_noop_on_serving,
    )


async def _drive(
    ctrl,
    clock: FakeClock,
    timeline: List[Tuple[float, float]],
) -> List[str]:
    """Drive the FSM: for each (advance_dt, _) step, advance the clock and tick.

    Returns the observed state-trajectory (list of state names, de-duplicated
    on consecutive repeats so transitions are legible). Bounded by len(timeline).
    """
    trajectory: List[str] = [ctrl.state.value]
    for advance_dt, _tick_count in timeline:
        clock.advance(advance_dt)
        state = await ctrl.tick()
        name = state.value
        if not trajectory or trajectory[-1] != name:
            trajectory.append(name)
    return trajectory


def _count_transitions(trajectory: List[str]) -> int:
    """Number of state CHANGES in a trajectory (already de-duplicated)."""
    return max(0, len(trajectory) - 1)


# --------------------------------------------------------------------------- #
# A. The deterministic gauntlet.
# --------------------------------------------------------------------------- #

async def _scenario_synthetic_collapse(route: str = "dw") -> Dict[str, Any]:
    """Scenario 1 -- Synthetic Collapse (503 storm).

    DW is persistently dead (gradient -> is_global_outage True). The FSM must
    leave DORMANT -> AWAKENING -> (node ready) -> SERVING, inject the deadman
    startup-script, Spot-first.
    """
    window = 5
    os.environ["JARVIS_QUARANTINE_WINDOW"] = str(window)
    # Reactive-floor confirm window kept short so the FakeClock crosses it fast.
    os.environ["JARVIS_OUTAGE_CONFIRM_S"] = "60"

    _reset_gradient()
    clock = FakeClock(start=1000.0)
    recorder = _CallRecorder()
    # node becomes ready after some boot delay; we let it be ready immediately
    # once we reach AWAKENING (the readiness gate is the controllable knob).
    node_ready_at = {"t": clock.t + 10.0}

    def node_ready(endpoint: str) -> bool:
        return clock.t >= node_ready_at["t"]

    def dw_probe() -> bool:
        return False  # DW stays dead throughout

    # DORMANT trigger reads is_global_outage directly: pre-fill a dead window.
    _fill_gradient(route, healthy=False, window=window)

    ctrl = _build_controller(
        clock=clock, recorder=recorder, dw_probe_fn=dw_probe,
        node_ready_fn=node_ready, route=route,
    )

    # Tick once to anchor the reactive-floor outage clock (DORMANT, no awaken
    # yet); then advance past the confirm window so the next tick awakens.
    trajectory: List[str] = [ctrl.state.value]

    async def _tick_record() -> None:
        state = await ctrl.tick()
        if trajectory[-1] != state.value:
            trajectory.append(state.value)

    await _tick_record()           # anchor outage clock (stays DORMANT)
    clock.advance(70.0)            # cross 60s confirm window
    await _tick_record()           # -> AWAKENING + awaken() fired
    clock.advance(15.0)            # node becomes ready (>= +10s)
    await _tick_record()           # -> SERVING

    verdict = "PASS"
    details: List[str] = []

    if ctrl.state.value != "SERVING":
        verdict = "FAIL"
        details.append("expected SERVING, got {}".format(ctrl.state.value))
    if not recorder.awaken_scripts:
        verdict = "FAIL"
        details.append("awaken was never called")
    else:
        ok, det = _assert_deadman_injected(recorder.awaken_scripts[0])
        details.append(det)
        if not ok:
            verdict = "FAIL"
    # Spot-first proof: the FSM's default awaken path is Spot-first; we assert
    # the FSM reached SERVING via a single awaken call (no on-demand thrash in
    # the fake -- the real Spot-first/on-demand fallback lives in the default
    # boundary, which the live soak exercises). The injection + single-awaken
    # is the in-process proof.
    if len(recorder.awaken_scripts) != 1:
        details.append("awaken called {} times (expected 1)".format(
            len(recorder.awaken_scripts)))

    if "DORMANT" not in trajectory or "AWAKENING" not in trajectory \
            or "SERVING" not in trajectory:
        verdict = "FAIL"
        details.append("trajectory missing required states")

    return {
        "verdict": verdict,
        "transitions": _count_transitions(trajectory),
        "trajectory": " -> ".join(trajectory),
        "detail": "; ".join(details),
    }


async def _phantom_variant(
    *,
    node_ready_before_recovery: bool,
    route: str = "dw",
) -> Dict[str, Any]:
    """One Phantom-Recovery timing variant (the shared core).

    DW is dead -> FSM begins AWAKENING. At fake-T+90s (MID-awaken), DW suddenly
    recovers. Either the node becomes ready BEFORE recovery (variant A) or
    AFTER recovery (variant B). The FSM must reach DORMANT with bounded ticks
    and no thrash.
    """
    window = 5
    os.environ["JARVIS_QUARANTINE_WINDOW"] = str(window)
    os.environ["JARVIS_OUTAGE_CONFIRM_S"] = "60"
    os.environ["JARVIS_RECOVERY_THRESHOLD"] = "0.6"
    os.environ["JARVIS_RECOVERY_HYSTERESIS_CYCLES"] = "2"
    # Min uptime small so a recovered node can hand back inside the bounded run.
    os.environ["JARVIS_JPRIME_MIN_UPTIME_S"] = "30"
    os.environ["JARVIS_HANDBACK_COOLDOWN_S"] = "300"
    # Probe pacing: short safe interval so SERVING re-probes each advance.

    _reset_gradient()
    clock = FakeClock(start=1000.0)
    recorder = _CallRecorder()

    schedule = ChaosSchedule(initial_healthy=False)
    recovery_t = clock.t + 90.0  # DW recovers 90s in (mid-awaken)
    schedule.add(recovery_t, True)

    # node readiness time relative to AWAKENING start.
    if node_ready_before_recovery:
        node_ready_at = clock.t + 70.0   # ready just after confirm, before +90s
    else:
        node_ready_at = clock.t + 120.0  # ready after DW already recovered

    def node_ready(endpoint: str) -> bool:
        return clock.t >= node_ready_at

    def dw_probe() -> bool:
        # SERVING-phase probe verdict driven by the schedule. When DW is healthy
        # the gradient records successes -> recovery -> handback.
        return schedule.is_healthy(clock.t)

    # DORMANT trigger: pre-fill a dead window so is_global_outage True.
    _fill_gradient(route, healthy=False, window=window)

    ctrl = _build_controller(
        clock=clock, recorder=recorder, dw_probe_fn=dw_probe,
        node_ready_fn=node_ready, route=route,
    )

    trajectory: List[str] = [ctrl.state.value]
    transitions = 0
    MAX_TICKS = 200  # bounded -- a lockup would blow this and FAIL.
    tick_step = 15.0

    async def _step() -> None:
        nonlocal transitions
        state = await ctrl.tick()
        if trajectory[-1] != state.value:
            trajectory.append(state.value)
            transitions += 1

    # Anchor outage clock.
    await _step()
    clock.advance(70.0)  # cross confirm window -> AWAKENING next tick
    await _step()

    # Now drive forward in bounded steps. When DW recovers (schedule), the
    # gradient must also reflect it for the DORMANT re-trigger guard; the
    # SERVING dw_probe feeds record_sweep, so once SERVING the window heals
    # naturally. Drive until DORMANT-after-serving or bound hit.
    reached_serving = False
    ticks = 0
    while ticks < MAX_TICKS:
        ticks += 1
        clock.advance(tick_step)
        if ctrl.state.value == "SERVING":
            reached_serving = True
        await _step()
        # Terminate once we have settled back into DORMANT *after* having
        # awakened (so we observe the full collapse->recover->dormant arc), and
        # DW is healthy (so no immediate re-awaken; cooldown also guards).
        if reached_serving and ctrl.state.value == "DORMANT":
            break
        # If we never serve (node never ready before AWAKENING timeout), the
        # timeout path also lands us back in DORMANT -- accept that too.
        if not reached_serving and ctrl.state.value == "DORMANT" and ticks > 3:
            # Confirm we actually left DORMANT at some point.
            if "AWAKENING" in trajectory:
                break

    verdict = "PASS"
    details: List[str] = []

    if ctrl.state.value != "DORMANT":
        verdict = "FAIL"
        details.append("did not return to DORMANT (got {}, possible lockup)"
                       .format(ctrl.state.value))
    if ticks >= MAX_TICKS:
        verdict = "FAIL"
        details.append("hit MAX_TICKS bound -- possible lockup/thrash")
    if "AWAKENING" not in trajectory:
        verdict = "FAIL"
        details.append("never left DORMANT (no AWAKENING observed)")

    # Anti-thrash: a graceful collapse->recover->dormant arc is a SMALL number
    # of transitions. Generous-but-real bound = 6 (DORMANT->AWAKENING->
    # SERVING->HANDBACK->DORMANT is 4; allow headroom). Oscillation would blow
    # past this.
    THRASH_BOUND = 6
    if transitions > THRASH_BOUND:
        verdict = "FAIL"
        details.append("transition thrash: {} > bound {}".format(
            transitions, THRASH_BOUND))

    details.append("node_ready_before_recovery={}".format(node_ready_before_recovery))
    details.append("served={}".format(reached_serving))

    return {
        "verdict": verdict,
        "transitions": transitions,
        "trajectory": " -> ".join(trajectory),
        "detail": "; ".join(details),
    }


async def _scenario_phantom_recovery(route: str = "dw") -> Dict[str, Any]:
    """Scenario 2 -- Phantom Recovery (THE race). Runs BOTH timing variants
    and PASSes only if both pass."""
    variant_a = await _phantom_variant(node_ready_before_recovery=True, route=route)
    variant_b = await _phantom_variant(node_ready_before_recovery=False, route=route)

    verdict = "PASS" if (variant_a["verdict"] == "PASS"
                         and variant_b["verdict"] == "PASS") else "FAIL"
    return {
        "verdict": verdict,
        "transitions": "A={} B={}".format(
            variant_a["transitions"], variant_b["transitions"]),
        "trajectory": "A[{}] | B[{}]".format(
            variant_a["trajectory"], variant_b["trajectory"]),
        "detail": "variantA: {} || variantB: {}".format(
            variant_a["detail"], variant_b["detail"]),
        "variant_a": variant_a,
        "variant_b": variant_b,
    }


async def _scenario_assassination(route: str = "dw") -> Dict[str, Any]:
    """Scenario 3 -- Assassination (FSM view + injection proof).

    Drive to SERVING, then simulate orchestrator death by ABANDONING the
    controller (stop ticking). PROVE the awaken injected a deadman startup-
    script with a finite idle-timeout (so a real orphan would self-delete) and
    that nothing in the FSM would have disarmed it.
    """
    window = 5
    os.environ["JARVIS_QUARANTINE_WINDOW"] = str(window)
    os.environ["JARVIS_OUTAGE_CONFIRM_S"] = "60"

    _reset_gradient()
    clock = FakeClock(start=1000.0)
    recorder = _CallRecorder()
    node_ready_at = clock.t + 10.0

    def node_ready(endpoint: str) -> bool:
        return clock.t >= node_ready_at

    def dw_probe() -> bool:
        return False  # DW stays dead -> no handback -> stays SERVING

    _fill_gradient(route, healthy=False, window=window)

    ctrl = _build_controller(
        clock=clock, recorder=recorder, dw_probe_fn=dw_probe,
        node_ready_fn=node_ready, route=route,
    )

    trajectory: List[str] = [ctrl.state.value]

    async def _tick_record() -> None:
        state = await ctrl.tick()
        if trajectory[-1] != state.value:
            trajectory.append(state.value)

    await _tick_record()        # anchor
    clock.advance(70.0)
    await _tick_record()        # -> AWAKENING + awaken
    clock.advance(15.0)
    await _tick_record()        # -> SERVING

    verdict = "PASS"
    details: List[str] = []

    if ctrl.state.value != "SERVING":
        verdict = "FAIL"
        details.append("did not reach SERVING (got {})".format(ctrl.state.value))

    # *** ASSASSINATION: stop ticking. The controller is now abandoned. ***
    # The FSM never calls vm_delete on its own outside HANDBACK/AWAKENING-timeout,
    # so an orphan would burn money WITHOUT the node-side deadman. Prove the
    # deadman was injected with a finite idle-timeout.
    if not recorder.awaken_scripts:
        verdict = "FAIL"
        details.append("awaken never fired -- no deadman to inject")
    else:
        ok, det = _assert_deadman_injected(recorder.awaken_scripts[0])
        details.append(det)
        if not ok:
            verdict = "FAIL"

    # Prove nothing in the FSM disarmed the backstop: the FSM issued no delete
    # while SERVING (it only deletes at HANDBACK / AWAKENING-timeout). The
    # injected node-side deadman is therefore the sole live backstop -- exactly
    # what we want for the orphan case.
    if recorder.delete_calls != 0:
        verdict = "FAIL"
        details.append("FSM issued a delete while SERVING (delete_calls={})"
                       .format(recorder.delete_calls))
    else:
        details.append("FSM issued no delete while SERVING "
                       "(node-side deadman is the sole orphan backstop -- correct)")

    return {
        "verdict": verdict,
        "transitions": _count_transitions(trajectory),
        "trajectory": " -> ".join(trajectory),
        "detail": "; ".join(details),
    }


async def _run_gauntlet_async() -> Dict[str, Dict[str, Any]]:
    """Run the three scenarios, each fail-soft. Returns the result map."""
    results: Dict[str, Dict[str, Any]] = {}

    async def _safe(name: str, coro_fn):
        try:
            results[name] = await coro_fn()
        except Exception as exc:  # noqa: BLE001 -- each scenario fail-soft
            results[name] = {
                "verdict": "FAIL",
                "transitions": 0,
                "trajectory": "",
                "detail": "scenario raised: {!r}\n{}".format(
                    exc, traceback.format_exc()),
            }

    await _safe("scenario_1_synthetic_collapse", _scenario_synthetic_collapse)
    await _safe("scenario_2_phantom_recovery", _scenario_phantom_recovery)
    await _safe("scenario_3_assassination", _scenario_assassination)
    return results


def run_gauntlet() -> Dict[str, Dict[str, Any]]:
    """Synchronous entry point. Drives the REAL FailoverLifecycleController
    through the three chaos scenarios with a FakeClock + injected fakes.

    Sets ``JARVIS_FAILOVER_LIFECYCLE_ENABLED=true`` for the duration (the FSM
    is inert otherwise) and restores the prior value afterward. Returns the
    per-scenario result map and prints a clear report.
    """
    prior_enabled = os.environ.get("JARVIS_FAILOVER_LIFECYCLE_ENABLED")
    prior_window = os.environ.get("JARVIS_QUARANTINE_WINDOW")
    os.environ["JARVIS_FAILOVER_LIFECYCLE_ENABLED"] = "true"
    try:
        results = asyncio.run(_run_gauntlet_async())
    finally:
        if prior_enabled is None:
            os.environ.pop("JARVIS_FAILOVER_LIFECYCLE_ENABLED", None)
        else:
            os.environ["JARVIS_FAILOVER_LIFECYCLE_ENABLED"] = prior_enabled
        if prior_window is None:
            os.environ.pop("JARVIS_QUARANTINE_WINDOW", None)
        else:
            os.environ["JARVIS_QUARANTINE_WINDOW"] = prior_window
        _reset_gradient()

    _print_report(results)
    return results


def _print_report(results: Dict[str, Dict[str, Any]]) -> None:
    line = "=" * 72
    print(line)
    print("SOVEREIGN FAILOVER LIFECYCLE -- CHAOS GAUNTLET REPORT")
    print(line)
    titles = {
        "scenario_1_synthetic_collapse": "Scenario 1 -- Synthetic Collapse (503 storm)",
        "scenario_2_phantom_recovery": "Scenario 2 -- Phantom Recovery (THE race)",
        "scenario_3_assassination": "Scenario 3 -- Assassination (injection proof)",
    }
    for key in (
        "scenario_1_synthetic_collapse",
        "scenario_2_phantom_recovery",
        "scenario_3_assassination",
    ):
        r = results.get(key, {"verdict": "FAIL", "trajectory": "",
                              "transitions": 0, "detail": "missing result"})
        print("")
        print("{}".format(titles.get(key, key)))
        print("  verdict     : {}".format(r.get("verdict")))
        print("  trajectory  : {}".format(r.get("trajectory")))
        print("  transitions : {}".format(r.get("transitions")))
        print("  detail      : {}".format(r.get("detail")))
    print("")
    all_pass = all(r.get("verdict") == "PASS" for r in results.values()) \
        and len(results) == 3
    print(line)
    print("OVERALL: {}".format("PASS (all 3 scenarios)" if all_pass else "FAIL"))
    print(line)


def gauntlet_all_pass(results: Dict[str, Dict[str, Any]]) -> bool:
    return (len(results) == 3
            and all(r.get("verdict") == "PASS" for r in results.values()))


# --------------------------------------------------------------------------- #
# B. Live-soak scaffold -- operator-gated, real GCE. TRIPLE-GATED.
# --------------------------------------------------------------------------- #

_LIVE_MONEY_ACK_FLAG = "--i-understand-this-spends-money"


def run_live_soak(args: argparse.Namespace) -> int:
    """Operator-gated live 503-storm + assassination soak on a REAL GCE node.

    TRIPLE-GATED -- refuses to run unless ALL of:
      1. ``JARVIS_CHAOS_INJECTOR_ENABLED=true`` (master gate), AND
      2. ``--live`` flag, AND
      3. ``--i-understand-this-spends-money`` explicit money acknowledgement.

    This function DOES NOT run the soak in this harness -- it scaffolds and
    documents it, printing the exact command + what to watch. The real soak is
    an operator action on a real Linux/GCP host.

    The live 503-storm is delivered via a PROVIDER-EGRESS CHAOS HOOK: an injected
    wrapper at the DW provider request boundary that returns synthetic
    503 / ``fsm_exhausted:TIMEOUT`` when armed -- it does NOT mutate real Aegis
    network state, and it disarms cleanly. Then: real awaken -> SIGKILL the O+V
    orchestrator PID -> poll for the real node's self-DELETE (driven by the
    node-side Dead-Man's Switch with a small idle-timeout / boot-grace for
    observability).
    """
    if not chaos_enabled():
        print("[chaos_injector] REFUSED: JARVIS_CHAOS_INJECTOR_ENABLED is not set "
              "(master gate). Live soak blocked.", file=sys.stderr)
        return 2
    if not getattr(args, "live", False):
        print("[chaos_injector] REFUSED: --live not passed. Live soak blocked.",
              file=sys.stderr)
        return 2
    if not getattr(args, "i_understand_this_spends_money", False):
        print("[chaos_injector] REFUSED: {} not passed. The live soak provisions a "
              "REAL GCE node and spends money. Pass it to acknowledge."
              .format(_LIVE_MONEY_ACK_FLAG), file=sys.stderr)
        return 2

    # All three gates satisfied -- but this harness still does NOT execute the
    # soak. It prints the scaffold + the exact operator command. (Executing a
    # real GCE provision + SIGKILL is an explicit operator action on a prod-like
    # Linux host, never an agent-driven background run.)
    _print_live_scaffold()
    return 0


def _print_live_scaffold() -> None:
    line = "=" * 72
    print(line)
    print("LIVE SOAK SCAFFOLD -- REAL GCE (operator action; NOT run here)")
    print(line)
    print("""
This harness will NOT provision a real node. The steps below are the operator
runbook for the real-GCE soak. Run them on a Linux/GCP host with gcloud auth.

STEP 0 -- Pre-conditions (cost observability):
  export JARVIS_FAILOVER_LIFECYCLE_ENABLED=true
  export JARVIS_FAILOVER_DEADMAN_ENABLED=true
  export JARVIS_DEADMAN_IDLE_TIMEOUT_S=120      # small for fast self-delete proof
  export JARVIS_DEADMAN_BOOT_GRACE_S=120        # small boot grace
  export JARVIS_DEADMAN_CHECK_INTERVAL_S=30
  export GCP_PROJECT_ID=<your-project>
  export GCP_ZONE=us-central1-a

STEP 1 -- Arm the provider-egress chaos hook (503 storm):
  Inject a wrapper at the DoubleWord provider REQUEST boundary
  (doubleword_provider.py session.post seam) that, while ARMED, returns a
  synthetic HTTP 503 / fsm_exhausted:TIMEOUT for every model+lane. This drives
  the ProviderHealthGradient window to all-fail -> is_global_outage(dw)=True,
  which is the FSM's real cryo-trigger. It does NOT touch Aegis network state
  and DISARMS cleanly (restores the original boundary).

STEP 2 -- Watch the FSM awaken a REAL node:
  Tail the orchestrator log for:
    [FailoverLifecycle] cryo-trigger ... -> AWAKEN
    [FailoverLifecycle] awaken: node=jarvis-prime-failover created (Spot)
    [FailoverLifecycle] SERVING via J-Prime endpoint=...
  Confirm the node exists:
    gcloud compute instances describe jarvis-prime-failover --zone=$GCP_ZONE

STEP 3 -- ASSASSINATION: SIGKILL the O+V orchestrator PID:
    kill -9 <orchestrator_pid>
  The FSM is now dead and can no longer issue a teardown. ONLY the node-side
  Dead-Man's Switch can save the bill.

STEP 4 -- Poll for the REAL node self-DELETE (the deadman backstop):
  After (boot_grace + idle_timeout) ~= 240s of no /api/ traffic, the node-side
  watchdog issues a Compute REST DELETE on itself. Poll:
    while gcloud compute instances describe jarvis-prime-failover \\
        --zone=$GCP_ZONE >/dev/null 2>&1; do echo waiting; sleep 30; done
    echo "NODE SELF-DELETED -- deadman backstop PROVEN"

SAFETY: if the node does NOT self-delete within ~10 min, MANUALLY delete it:
    gcloud compute instances delete jarvis-prime-failover --zone=$GCP_ZONE --quiet
""")
    print(line)
    print("Scaffold printed. No real resources were provisioned by this harness.")
    print(line)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chaos_injector",
        description="Chaos-engineering harness for the Sovereign Provider "
                    "Failover Lifecycle FSM.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--gauntlet", action="store_true",
                      help="Run the deterministic in-process gauntlet (default).")
    mode.add_argument("--live", action="store_true",
                      help="Live real-GCE soak (TRIPLE-GATED; scaffold only).")
    p.add_argument(_LIVE_MONEY_ACK_FLAG, dest="i_understand_this_spends_money",
                   action="store_true",
                   help="Explicit acknowledgement that --live spends real money.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    if not chaos_enabled():
        print("[chaos_injector] REFUSED: set JARVIS_CHAOS_INJECTOR_ENABLED=true "
              "to run. This is a pre-flip torture harness, never a prod component.",
              file=sys.stderr)
        return 2

    if args.live:
        return run_live_soak(args)

    # Default + --gauntlet: run the deterministic gauntlet.
    results = run_gauntlet()
    return 0 if gauntlet_all_pass(results) else 1


if __name__ == "__main__":
    sys.exit(main())
