#!/usr/bin/env python3
"""Live-fire battle test — Operator Trajectory View arc.

Resolves the gap quote:
  'No "what is O+V doing right now" one-screen view — status line is
   close, but there's no "trajectory": "currently: op-X, analyzing
   path Y because sensor Z fired, ETA W seconds, cost $C."'

Scenarios
---------
 1. Builder with no suppliers returns explicit idle frame.
 2. Builder composes full trajectory from all four suppliers.
 3. Any supplier raising → frame still builds with sentinels.
 4. Gap-quote narrative shape survives the full pipeline.
 5. Renderer per-surface variants differ.
 6. Stream emit_if_changed suppresses duplicate presentation frames.
 7. Stream listener exception doesn't break other subscribers.
 8. /trajectory REPL: status / expanded / json / sse / plain / watch / help.
 9. set_default_suppliers lets the default builder become
    production-wired without touching caller code.
10. Authority invariant grep.

Run::
    python3 scripts/livefire_trajectory.py
"""
from __future__ import annotations

import asyncio
import json
import re as _re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.trajectory_frame import (  # noqa: E402
    Confidence,
    TrajectoryFrame,
    TrajectoryPhase,
    idle_frame,
    phase_from_raw,
)
from backend.core.ouroboros.governance.trajectory_view import (  # noqa: E402
    TrajectoryBuilder,
    TrajectoryRenderer,
    TrajectoryStream,
    TrajectorySurface,
    dispatch_trajectory_command,
    reset_default_trajectory_singletons,
    set_default_suppliers,
)


C_PASS, C_FAIL, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(t: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {t}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _pass(t: str) -> None:
    print(f"  {C_PASS}✓ {t}{C_END}")


def _fail(t: str) -> None:
    print(f"  {C_FAIL}✗ {t}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, d: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(d)
        (_pass if ok else _fail)(d)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _OpFake:
    def __init__(self, op: Optional[Dict[str, Any]]) -> None:
        self._op = op

    def current_op(self) -> Optional[Dict[str, Any]]:
        return self._op


class _CostFake:
    def __init__(self, s: Optional[Dict[str, Any]]) -> None:
        self._s = s

    def cost_snapshot(self, _op_id: str) -> Optional[Dict[str, Any]]:
        return self._s


class _EtaFake:
    def __init__(self, e: Optional[Dict[str, Any]]) -> None:
        self._e = e

    def eta_for(self, _op_id: str) -> Optional[Dict[str, Any]]:
        return self._e


class _SensorFake:
    def __init__(self, t: Optional[Dict[str, Any]]) -> None:
        self._t = t

    def trigger_for(self, _op_id: str) -> Optional[Dict[str, Any]]:
        return self._t


class _BoomFake:
    def current_op(self): raise RuntimeError("boom")
    def cost_snapshot(self, op_id): raise RuntimeError("boom")
    def eta_for(self, op_id): raise RuntimeError("boom")
    def trigger_for(self, op_id): raise RuntimeError("boom")


def _live_op() -> Dict[str, Any]:
    return {
        "op_id": "op-live01",
        "raw_phase": "apply",
        "subject": "fix import in auth",
        "target_paths": ["backend/auth.py"],
        "active_tools": ["edit_file"],
        "trigger_source": "test_failure",
        "trigger_reason": "pytest -q failed on test_login",
        "started_at_ts": time.time() - 30,
        "is_blocked": False,
        "next_step": "apply candidate, then run verify",
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_no_suppliers_idle() -> Scenario:
    """Builder with no suppliers returns idle frame."""
    s = Scenario("Idle when no suppliers wired")
    f = TrajectoryBuilder().build()
    s.check("is_idle=True", f.is_idle is True)
    s.check("phase=IDLE", f.phase is TrajectoryPhase.IDLE)
    s.check(
        "one-line summary = 'idle'",
        f.one_line_summary() == "idle",
    )
    return s


async def scenario_full_composition() -> Scenario:
    """Builder composes full trajectory from all four suppliers."""
    s = Scenario("Full composition with 4 suppliers")
    b = TrajectoryBuilder(
        op_state=_OpFake(_live_op()),
        cost=_CostFake({"spent_usd": 0.012, "budget_usd": 0.50}),
        eta=_EtaFake({"eta_seconds": 42.0, "confidence": 0.8}),
        sensor_trigger=_SensorFake({
            "source": "runtime_health", "reason": "backup",
        }),
    )
    f = b.build()
    s.check("has_op", f.has_op)
    s.check("phase=APPLYING", f.phase is TrajectoryPhase.APPLYING)
    s.check("eta=42s", f.eta_seconds == 42.0)
    s.check("cost=0.012", f.cost_spent_usd == 0.012)
    s.check("confidence=HIGH band", f.confidence_band is Confidence.HIGH)
    s.check(
        "trigger from op_info preserved over sensor fake",
        f.trigger_source == "test_failure",
    )
    return s


async def scenario_supplier_raise_fails_closed() -> Scenario:
    """Any supplier raising → frame still builds with sentinels."""
    s = Scenario("Supplier raise → fail-closed, no crash")
    # op_state raising → idle
    b1 = TrajectoryBuilder(op_state=_BoomFake())
    f1 = b1.build()
    s.check("op_state raise → idle frame", f1.is_idle is True)

    # Happy op_state, busted cost + eta + sensor → frame still builds
    b2 = TrajectoryBuilder(
        op_state=_OpFake(_live_op()),
        cost=_BoomFake(), eta=_BoomFake(), sensor_trigger=_BoomFake(),
    )
    f2 = b2.build()
    s.check("has_op despite supplier raises", f2.has_op is True)
    s.check("cost falls back to 0", f2.cost_spent_usd == 0.0)
    s.check("eta falls back to None", f2.eta_seconds is None)
    return s


async def scenario_gap_quote_shape() -> Scenario:
    """Gap-quote narrative shape survives the full pipeline."""
    s = Scenario("Gap quote: 'currently: op-X, ...' shape")
    b = TrajectoryBuilder(
        op_state=_OpFake(_live_op()),
        cost=_CostFake({"spent_usd": 0.012, "budget_usd": 0.50}),
        eta=_EtaFake({"eta_seconds": 42.0}),
    )
    f = b.build()
    narrative = f.narrative()
    for token in (
        "currently:",
        "op-live01",
        "applying",
        "backend/auth.py",
        "because sensor test_failure",
        "ETA 42s",
        "cost $0.012",
    ):
        s.check(
            f"narrative contains {token!r}",
            token in narrative,
        )
    return s


async def scenario_renderer_surfaces_differ() -> Scenario:
    """Each renderer surface produces a different output."""
    s = Scenario("Renderer per-surface variants differ")
    f = TrajectoryBuilder(
        op_state=_OpFake(_live_op()),
        cost=_CostFake({"spent_usd": 0.012, "budget_usd": 0.50}),
        eta=_EtaFake({"eta_seconds": 30.0, "confidence": 0.9}),
    ).build()
    r = TrajectoryRenderer()
    compact = r.render(f, surface=TrajectorySurface.REPL_COMPACT)
    expanded = r.render(f, surface=TrajectorySurface.REPL_EXPANDED)
    ide = r.render(f, surface=TrajectorySurface.IDE_JSON)
    sse = r.render(f, surface=TrajectorySurface.SSE)
    plain = r.render(f, surface=TrajectorySurface.PLAIN)
    outputs = {compact, expanded, ide, sse, plain}
    s.check(
        f"5 distinct rendered outputs (got {len(outputs)})",
        len(outputs) == 5,
    )
    # Sanity: IDE and SSE are valid JSON
    s.check("IDE parseable JSON", bool(json.loads(ide)))
    s.check("SSE parseable JSON", bool(json.loads(sse)))
    return s


async def scenario_stream_emit_if_changed() -> Scenario:
    """Stream emit_if_changed suppresses duplicates."""
    s = Scenario("Stream emit_if_changed suppresses duplicates")
    b = TrajectoryBuilder(op_state=_OpFake(_live_op()))
    stream = TrajectoryStream()
    received: List[TrajectoryFrame] = []
    stream.subscribe(received.append)
    stream.emit_if_changed(b.build())
    stream.emit_if_changed(b.build())
    stream.emit_if_changed(b.build())
    s.check(
        f"only 1 emit despite 3 build() calls (got {len(received)})",
        len(received) == 1,
    )
    # Different phase → emits again
    b2 = TrajectoryBuilder(
        op_state=_OpFake({**_live_op(), "raw_phase": "verify"}),
    )
    stream.emit_if_changed(b2.build())
    s.check(
        "phase change triggers emit",
        len(received) == 2,
    )
    return s


async def scenario_stream_listener_exception() -> Scenario:
    """Listener exception doesn't break other subscribers."""
    s = Scenario("Stream listener exception isolated")
    stream = TrajectoryStream()
    good: List[TrajectoryFrame] = []

    def _bad(_f):
        raise RuntimeError("intentional")

    stream.subscribe(_bad)
    stream.subscribe(good.append)
    stream.emit(idle_frame())
    s.check("good listener still got the frame", len(good) == 1)
    return s


async def scenario_repl_surfaces() -> Scenario:
    """/trajectory REPL: status / expanded / json / sse / plain / help."""
    s = Scenario("/trajectory REPL full surface coverage")
    reset_default_trajectory_singletons()
    set_default_suppliers(
        op_state=_OpFake(_live_op()),
        cost=_CostFake({"spent_usd": 0.012, "budget_usd": 0.50}),
        eta=_EtaFake({"eta_seconds": 42.0}),
    )
    for sub in ("", "status", "summary", "expanded", "full",
                "json", "ide", "sse", "plain", "help", "watch"):
        r = dispatch_trajectory_command(
            f"/trajectory {sub}".strip(),
        )
        s.check(f"/trajectory {sub!r} ok={r.ok}", r.ok is True)
    # Unknown subcmd returns ok=False but matched=True
    r_bad = dispatch_trajectory_command("/trajectory frobnicate")
    s.check("unknown subcommand → ok=False", r_bad.ok is False)
    reset_default_trajectory_singletons()
    return s


async def scenario_default_suppliers_injection() -> Scenario:
    """set_default_suppliers makes default builder production-wired."""
    s = Scenario("set_default_suppliers hot-wires the default")
    reset_default_trajectory_singletons()
    set_default_suppliers(op_state=_OpFake(_live_op()))
    r = dispatch_trajectory_command("/trajectory status")
    s.check("op-live01 surfaced in default pipeline",
            "op-live01" in r.text)
    # Replace suppliers with empty → default flips back to idle
    set_default_suppliers(op_state=_OpFake(None))
    r2 = dispatch_trajectory_command("/trajectory status")
    s.check("replacing suppliers → idle", r2.text == "idle")
    reset_default_trajectory_singletons()
    return s


async def scenario_authority_invariant() -> Scenario:
    """Arc modules import no gate/execution code."""
    s = Scenario("Authority invariant grep")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/trajectory_frame.py",
        "backend/core/ouroboros/governance/trajectory_view.py",
    ]
    for path in modules:
        src = Path(path).read_text()
        violations = []
        for mod in forbidden:
            if _re.search(
                rf"^\s*(from|import)\s+[^#\n]*{_re.escape(mod)}",
                src, _re.MULTILINE,
            ):
                violations.append(mod)
        s.check(
            f"{Path(path).name}: zero forbidden imports",
            not violations,
        )
    return s


ALL_SCENARIOS = [
    scenario_no_suppliers_idle,
    scenario_full_composition,
    scenario_supplier_raise_fails_closed,
    scenario_gap_quote_shape,
    scenario_renderer_surfaces_differ,
    scenario_stream_emit_if_changed,
    scenario_stream_listener_exception,
    scenario_repl_surfaces,
    scenario_default_suppliers_injection,
    scenario_authority_invariant,
]


async def main() -> int:
    print(f"{C_BOLD}Operator Trajectory View — live-fire{C_END}")
    print(f"{C_DIM}TrajectoryFrame + Builder + Renderer + Stream + /trajectory REPL{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.failed.append(f"raised: {type(exc).__name__}: {exc}")
            _fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for sc in results:
        status = f"{C_PASS}PASS{C_END}" if sc.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {sc.title}  ({len(sc.passed)} ✓, {len(sc.failed)} ✗)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}"
            f"OPERATOR TRAJECTORY VIEW GAP: CLOSED"
            f"{C_END}"
        )
        return 0
    print(
        f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed{C_END}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
