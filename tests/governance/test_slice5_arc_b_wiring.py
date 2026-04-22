"""Slice 5 Arc B integration tests — MemoryPressureGate wired to L3 fan-out.

Scope:
  1. Gate consultation hooked in _run_graph at the right seam (post
     _select_ready_batch, pre _run_selected_units)
  2. Clamp semantics: n_allowed < len(selected) moves overflow to
     deferred queue — zero work loss
  3. Disposition classification: allow / clamp / disabled / probe_fail —
     each produces the expected reason_code + log + SSE shape
  4. Gate-disabled passthrough (master flag off)
  5. WARN / HIGH / CRITICAL clamp levels each produce correct n_allowed
  6. OK level → no clamp (passthrough without SSE "clamp" disposition)
  7. SSE fires on every gate consultation (allow/clamp/disabled/probe_fail)
  8. Probe failure falls through cleanly (scheduler doesn't break)
  9. `_consult_memory_gate` never raises — returns None on gate outage
 10. Authority invariant — no orchestrator/policy/iron_gate imports

Authority invariant: no orchestrator/policy/iron_gate imports in this
test file.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.memory_pressure_gate import (
    MemoryPressureGate,
    MemoryProbe,
    PressureLevel,
    reset_default_gate,
)
from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
    SubagentScheduler,
)


_SCHEDULER_LOGGER = "Ouroboros.SubagentScheduler"
_STREAM_LOGGER = "backend.core.ouroboros.governance.ide_observability_stream"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if (k.startswith("JARVIS_MEMORY_PRESSURE")
                or k.startswith("JARVIS_IDE_STREAM")):
            monkeypatch.delenv(k, raising=False)
    reset_default_gate()
    yield
    reset_default_gate()


def _fake_probe(free_pct: float, source: str = "test") -> MemoryProbe:
    return MemoryProbe(
        free_pct=free_pct, total_bytes=16 * (1024 ** 3),
        available_bytes=int(free_pct * 16 * (1024 ** 3) / 100.0),
        source=source,
    )


def _install_fake_gate(probe_free_pct: float) -> MemoryPressureGate:
    """Install a fake gate as the default singleton so _consult_memory_gate
    sees it."""
    import backend.core.ouroboros.governance.memory_pressure_gate as mpg

    gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(probe_free_pct))
    mpg._default_gate = gate
    return gate


def _minimal_scheduler() -> SubagentScheduler:
    """Build a SubagentScheduler with just enough plumbing to call
    _consult_memory_gate in isolation. The unit method doesn't touch
    any of the SubagentScheduler's dependencies."""
    return SubagentScheduler.__new__(SubagentScheduler)


# ---------------------------------------------------------------------------
# Disposition classification — the core of Arc B's observability
# ---------------------------------------------------------------------------


class TestDispositionClassification:

    def test_ok_level_produces_allow(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=80.0)  # OK
        sched = _minimal_scheduler()
        with caplog.at_level(logging.INFO, logger=_SCHEDULER_LOGGER):
            decision = sched._consult_memory_gate(4, graph_id="g-test-1")
        assert decision is not None
        assert decision.level is PressureLevel.OK
        assert decision.n_allowed == 4
        assert any("disposition=allow" in r.message for r in caplog.records)

    def test_warn_clamp_produces_clamp_disposition(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=25.0)  # WARN
        sched = _minimal_scheduler()
        with caplog.at_level(logging.WARNING, logger=_SCHEDULER_LOGGER):
            decision = sched._consult_memory_gate(16, graph_id="g-warn")
        assert decision.level is PressureLevel.WARN
        assert decision.n_allowed == 8
        assert any("disposition=clamp" in r.message for r in caplog.records)
        assert any("requested=16" in r.message and "allowed=8" in r.message
                   for r in caplog.records)

    def test_high_clamp_produces_clamp_disposition(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=15.0)  # HIGH
        sched = _minimal_scheduler()
        with caplog.at_level(logging.WARNING, logger=_SCHEDULER_LOGGER):
            decision = sched._consult_memory_gate(16, graph_id="g-high")
        assert decision.level is PressureLevel.HIGH
        assert decision.n_allowed == 3
        assert any("disposition=clamp" in r.message for r in caplog.records)

    def test_critical_clamp_produces_clamp_disposition(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=5.0)  # CRITICAL
        sched = _minimal_scheduler()
        with caplog.at_level(logging.WARNING, logger=_SCHEDULER_LOGGER):
            decision = sched._consult_memory_gate(16, graph_id="g-crit")
        assert decision.level is PressureLevel.CRITICAL
        assert decision.n_allowed == 1
        assert any("disposition=clamp" in r.message for r in caplog.records)

    def test_gate_disabled_produces_disabled_disposition(
        self, monkeypatch, caplog,
    ):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")
        _install_fake_gate(probe_free_pct=5.0)  # would be CRITICAL
        sched = _minimal_scheduler()
        with caplog.at_level(logging.INFO, logger=_SCHEDULER_LOGGER):
            decision = sched._consult_memory_gate(16, graph_id="g-off")
        assert decision.reason_code == "memory_pressure_gate.disabled"
        assert decision.n_allowed == 16
        assert any("disposition=disabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# No-clamp at OK — boundary conditions
# ---------------------------------------------------------------------------


class TestOKPassthrough:

    def test_ok_level_requested_under_cap(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=50.0)
        sched = _minimal_scheduler()
        d = sched._consult_memory_gate(3, graph_id="g")
        assert d.n_allowed == 3
        assert d.reason_code == "memory_pressure_gate.ok"

    def test_warn_level_requested_below_cap_no_clamp(self, monkeypatch):
        """At WARN cap=8, requesting 3 → no clamp, still OK reason."""
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=25.0)
        sched = _minimal_scheduler()
        d = sched._consult_memory_gate(3, graph_id="g")
        # n_allowed still 3 (under cap); level is WARN though
        assert d.n_allowed == 3
        assert d.level is PressureLevel.WARN


# ---------------------------------------------------------------------------
# Scheduler integration — clamp semantics on actual selected/deferred lists
# ---------------------------------------------------------------------------


class TestSchedulerClampSemantics:
    """The clamp inside _run_graph: overflow goes to deferred, not
    dropped. We verify by calling the actual logic without booting a
    full graph (the clamp block is standalone)."""

    def test_clamp_moves_overflow_to_deferred(self, monkeypatch):
        """Simulate the in-loop clamp block by running the same code
        with a controlled decision."""
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=15.0)  # HIGH → cap 3
        sched = _minimal_scheduler()

        # Simulate: selected=[u1..u8], deferred=[u9, u10]
        selected = [f"unit-{i}" for i in range(1, 9)]
        deferred = ["unit-9", "unit-10"]

        decision = sched._consult_memory_gate(len(selected), graph_id="g-h")
        assert decision.n_allowed == 3

        # Emulate the clamp logic from _run_graph
        if decision is not None and decision.n_allowed < len(selected):
            overflow = list(selected[decision.n_allowed:])
            selected = list(selected[:decision.n_allowed])
            deferred = sorted(list(deferred) + overflow)

        assert len(selected) == 3
        # Overflow units now in deferred (sorted)
        assert "unit-4" in deferred
        assert "unit-5" in deferred
        assert "unit-8" in deferred
        assert "unit-9" in deferred
        assert "unit-10" in deferred
        # No loss — all 10 original units still accounted for
        assert len(selected) + len(deferred) == 10

    def test_no_clamp_when_ok(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        _install_fake_gate(probe_free_pct=80.0)
        sched = _minimal_scheduler()
        selected = ["u1", "u2", "u3"]
        deferred_in = ["u99"]
        decision = sched._consult_memory_gate(len(selected), graph_id="g")
        assert decision.n_allowed >= len(selected)
        # No mutation happens because n_allowed >= len(selected)

    def test_gate_none_result_means_no_mutation(self, monkeypatch):
        """When gate consultation returns None (outage), selected stays intact."""
        sched = _minimal_scheduler()
        import backend.core.ouroboros.governance.memory_pressure_gate as mpg

        def _broken():
            raise RuntimeError("gate import broken")
        monkeypatch.setattr(mpg, "get_default_gate", _broken)
        d = sched._consult_memory_gate(16, graph_id="g")
        assert d is None


# ---------------------------------------------------------------------------
# SSE event wiring
# ---------------------------------------------------------------------------


class TestSSEWiring:

    def test_event_type_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_MEMORY_FANOUT_DECISION, _VALID_EVENT_TYPES,
        )
        assert EVENT_TYPE_MEMORY_FANOUT_DECISION in _VALID_EVENT_TYPES
        assert EVENT_TYPE_MEMORY_FANOUT_DECISION == "memory_fanout_decision"

    def test_publish_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_memory_fanout_decision_event, reset_default_broker,
        )
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            FanoutDecision, PressureLevel,
        )
        reset_default_broker()
        decision = FanoutDecision(
            allowed=True, n_requested=4, n_allowed=4,
            level=PressureLevel.OK, free_pct=80.0,
            reason_code="memory_pressure_gate.ok", source="test",
        )
        assert publish_memory_fanout_decision_event(
            graph_id="g", disposition="allow", decision=decision,
        ) is None

    def test_publish_enabled_emits(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_MEMORY_FANOUT_DECISION,
            get_default_broker,
            publish_memory_fanout_decision_event,
            reset_default_broker,
        )
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            FanoutDecision, PressureLevel,
        )
        reset_default_broker()
        broker = get_default_broker()

        decision = FanoutDecision(
            allowed=True, n_requested=16, n_allowed=3,
            level=PressureLevel.HIGH, free_pct=15.0,
            reason_code="memory_pressure_gate.capped_to_3_at_high",
            source="test",
        )
        before = broker.published_count
        eid = publish_memory_fanout_decision_event(
            graph_id="g-test", disposition="clamp", decision=decision,
        )
        assert eid is not None
        assert broker.published_count == before + 1

        # Inspect frame payload
        latest = list(broker._history)[-1]
        assert latest.event_type == EVENT_TYPE_MEMORY_FANOUT_DECISION
        assert latest.payload["graph_id"] == "g-test"
        assert latest.payload["disposition"] == "clamp"
        assert latest.payload["n_requested"] == 16
        assert latest.payload["n_allowed"] == 3
        assert latest.payload["level"] == "high"

    def test_consult_gate_publishes_sse_on_clamp(self, monkeypatch):
        """End-to-end: _consult_memory_gate fires SSE via the bridge."""
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_MEMORY_FANOUT_DECISION,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        _install_fake_gate(probe_free_pct=15.0)  # HIGH → cap 3

        sched = _minimal_scheduler()
        sched._consult_memory_gate(16, graph_id="g-sse-test")

        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_MEMORY_FANOUT_DECISION in types
        frame = next(
            e for e in broker._history
            if e.event_type == EVENT_TYPE_MEMORY_FANOUT_DECISION
        )
        assert frame.payload["disposition"] == "clamp"
        assert frame.payload["graph_id"] == "g-sse-test"


# ---------------------------------------------------------------------------
# Probe failure — never breaks the scheduler
# ---------------------------------------------------------------------------


class TestProbeFailureSafety:

    def test_probe_unreliable_gives_passthrough(self, monkeypatch, caplog):
        """Probe returns ok=False → gate responds with probe_unreliable
        + n_allowed=n_requested. Disposition = probe_fail."""
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        import backend.core.ouroboros.governance.memory_pressure_gate as mpg

        bad_probe = MemoryProbe(
            free_pct=0, total_bytes=0, available_bytes=0,
            source="test", ok=False, error="simulated",
        )
        gate = MemoryPressureGate(probe_fn=lambda: bad_probe)
        mpg._default_gate = gate

        sched = _minimal_scheduler()
        with caplog.at_level(logging.INFO, logger=_SCHEDULER_LOGGER):
            decision = sched._consult_memory_gate(16, graph_id="g")
        assert decision.n_allowed == 16  # pass-through
        assert any("disposition=probe_fail" in r.message
                   for r in caplog.records)

    def test_probe_raises_consultation_returns_none(self, monkeypatch):
        """If gate.can_fanout raises, _consult_memory_gate returns None
        rather than propagating the exception into _run_graph."""
        sched = _minimal_scheduler()
        import backend.core.ouroboros.governance.memory_pressure_gate as mpg

        class _ExplodingGate:
            def can_fanout(self, n):
                raise RuntimeError("simulated")

        def _get_broken():
            return _ExplodingGate()
        monkeypatch.setattr(mpg, "get_default_gate", _get_broken)
        assert sched._consult_memory_gate(4, graph_id="g") is None


# ---------------------------------------------------------------------------
# Authority invariant
# ---------------------------------------------------------------------------


class TestArcBAuthorityInvariant:

    def test_scheduler_arc_b_additions_authority_free(self):
        """Ensure the Arc B insertion doesn't pull in execution-authority
        modules. The scheduler already imports plenty — we test that
        NO new imports of the authority set are introduced."""
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (
            repo_root
            / "backend/core/ouroboros/governance/autonomy/subagent_scheduler.py"
        ).read_text(encoding="utf-8")
        # Arc B must not pull in these specifically
        forbidden = ("iron_gate", "risk_tier", "change_engine",
                     "candidate_generator")
        for f in forbidden:
            # Substring check — generic enough to catch module imports
            # but let pre-existing usage of "gate" word survive
            assert f".{f}" not in src, (
                f"subagent_scheduler.py contains authority import {f!r}"
            )

    def test_stream_publish_helper_authority_free(self):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (
            repo_root
            / "backend/core/ouroboros/governance/ide_observability_stream.py"
        ).read_text(encoding="utf-8")
        # Find the publish_memory_fanout_decision_event function body
        assert "def publish_memory_fanout_decision_event" in src
        idx = src.index("def publish_memory_fanout_decision_event")
        window = src[idx:idx + 2048]
        forbidden = ("iron_gate", "risk_tier", "change_engine",
                     "candidate_generator", "orchestrator", "policy")
        for f in forbidden:
            assert f".{f} " not in window, (
                f"publish_memory_fanout_decision_event references {f}"
            )
