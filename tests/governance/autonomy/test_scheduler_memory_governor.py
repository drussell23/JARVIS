from __future__ import annotations

from backend.core.ouroboros.governance.autonomy import subagent_scheduler as ss


class _FakeProbe:
    def __init__(self, available_mb: float):
        self.available_bytes = int(available_mb * 1024 * 1024)
        self.total_bytes = 16 * 1024 * 1024 * 1024
        self.ok = True


class _FakeGate:
    def __init__(self, available_mb: float):
        self._p = _FakeProbe(available_mb)

    def probe(self):
        return self._p


def _make_scheduler():
    # Construct with minimal stubs; only _consult_memory_governor is exercised.
    return ss.SubagentScheduler.__new__(ss.SubagentScheduler)


def test_governor_clamps_on_low_ram(monkeypatch):
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_L3_WORKTREE_RAM_BUDGET_MB", "1500")
    monkeypatch.setattr(
        ss, "get_default_gate", lambda: _FakeGate(available_mb=4500.0),
    )
    sched = _make_scheduler()
    decision = sched._consult_memory_governor(
        8, graph_id="g1", level_cap=8,
    )
    assert decision is not None
    assert decision.n_allowed == 3
    assert decision.disposition == "clamp"


def test_governor_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "false")
    sched = _make_scheduler()
    assert sched._consult_memory_governor(8, graph_id="g1", level_cap=8) is None


def test_governor_probe_failure_is_non_fatal(monkeypatch):
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ss, "get_default_gate", _boom)
    sched = _make_scheduler()
    # Must swallow and return None — scheduler never breaks on probe failure.
    assert sched._consult_memory_governor(8, graph_id="g1", level_cap=8) is None


def test_run_graph_clamp_composition(monkeypatch):
    """The governor clamp composes after the fan-out clamp: selected is
    truncated to the governor's n_allowed and overflow is deferred.

    This simulates the composition `_run_graph` performs rather than
    driving the async `_run_graph` end-to-end; it pins the arithmetic
    contract the wiring relies on.
    """
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_L3_WORKTREE_RAM_BUDGET_MB", "1500")
    monkeypatch.setattr(
        ss, "get_default_gate", lambda: _FakeGate(available_mb=3000.0),
    )
    sched = _make_scheduler()
    selected = ["u1", "u2", "u3", "u4"]
    deferred = []
    gov = sched._consult_memory_governor(
        len(selected), graph_id="g1", level_cap=len(selected),
    )
    assert gov.n_allowed == 2  # 3000/1500 = 2
    # Simulate the composition the _run_graph edit performs:
    overflow = list(selected[gov.n_allowed:])
    selected = list(selected[:gov.n_allowed])
    deferred = sorted(deferred + overflow)
    assert selected == ["u1", "u2"]
    assert deferred == ["u3", "u4"]


def test_disabled_governor_is_byte_identical_passthrough(monkeypatch):
    """With the master flag off, _consult_memory_governor returns None
    and the _run_graph composition leaves `selected` untouched."""
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "false")
    sched = _make_scheduler()
    selected = ["u1", "u2", "u3"]
    gov = sched._consult_memory_governor(
        len(selected), graph_id="g1", level_cap=len(selected),
    )
    assert gov is None
    # Composition guard: None -> no truncation.
    if gov is not None and gov.n_allowed < len(selected):
        selected = selected[:gov.n_allowed]
    assert selected == ["u1", "u2", "u3"]
