"""Conception Proposal Bridge — ranked blueprints → intake router (Gap 3 unify).

Proves the bridge is a pure, event-driven translation layer that routes
high-EV blueprints into the existing router and NOTHING more: a dynamic
batch-relative threshold (no hardcoded cutoff), a bulletproof durable dedup so
an already-routed / in-execution blueprint is dropped (the mandate-4 constraint),
DRY reuse of the value model's emitted EV + the make_envelope schema +
router.ingest, master-off inertness, and never-raise.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Tuple

import backend.core.ouroboros.governance.conception_proposal_bridge as cpb


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _BP:
    blueprint_id: str
    target_files: Tuple[str, ...]
    description: str = "improve X"
    title: str = "X"
    category: str = "performance"
    repo: str = "repo"
    estimated_cost_usd: float = 0.0


@dataclass
class _EV:
    ev: float
    scope: str = "svc"
    alignment: float = 0.5
    substance: float = 0.5
    feasibility: float = 0.5
    rationale: str = "r"

    def to_dict(self):
        return {"ev": self.ev, "scope": self.scope}


class _FakeRouter:
    """Records ingested envelopes; returns 'enqueued' (or a scripted result)."""

    def __init__(self, result="enqueued"):
        self.ingested: List[object] = []
        self._result = result

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return self._result


def _bridge(ev_map, **kw):
    """A bridge whose scorer maps blueprint_id → a fixed EV."""
    return cpb.ConceptionProposalBridge(
        value_scorer=lambda bp: _EV(ev_map[bp.blueprint_id]),
        ledger_emit=lambda **k: None,   # silence the ledger in unit tests
        **kw,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _enable(monkeypatch, floor=0.5):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_EV_FLOOR", str(floor))


# ---------------------------------------------------------------------------
# Master gating / inertness
# ---------------------------------------------------------------------------


def test_master_default_off():
    import os
    os.environ.pop("JARVIS_CONCEPTION_BRIDGE_ENABLED", None)
    assert cpb.master_enabled() is False


def test_disabled_routes_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "false")
    r = _FakeRouter()
    out = _run(_bridge({"a": 0.99}).route([_BP("a", ("f.py",))], r))
    assert out == [] and r.ingested == []


# ---------------------------------------------------------------------------
# Dynamic, batch-relative threshold — no hardcoded cutoff
# ---------------------------------------------------------------------------


def test_dynamic_threshold_routes_above_batch_mean(monkeypatch):
    _enable(monkeypatch, floor=0.0)   # isolate the batch-mean behavior
    r = _FakeRouter()
    bps = [_BP("hi", ("a.py",)), _BP("lo", ("b.py",))]
    out = _run(_bridge({"hi": 0.9, "lo": 0.1}).route(bps, r))
    routed = {o.blueprint_id for o in out if o.routed}
    below = {o.blueprint_id for o in out if o.reason == "incubated"}
    assert routed == {"hi"} and below == {"lo"}         # mean=0.5 → only hi clears
    assert len(r.ingested) == 1


def test_ev_floor_blocks_cold_organism(monkeypatch):
    _enable(monkeypatch, floor=0.5)
    r = _FakeRouter()
    # A whole batch at the neutral prior — nothing clears the floor.
    bps = [_BP("a", ("a.py",)), _BP("b", ("b.py",))]
    out = _run(_bridge({"a": 0.4, "b": 0.45}).route(bps, r))
    assert all(not o.routed for o in out) and r.ingested == []


def test_threshold_rises_with_batch_quality(monkeypatch):
    _enable(monkeypatch, floor=0.5)
    r = _FakeRouter()
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_MAX_PER_ROUTE", "5")
    # strong batch: mean 0.8 → only the 0.95 clears, the 0.7/0.75 do not.
    bps = [_BP("x", ("x.py",)), _BP("y", ("y.py",)), _BP("z", ("z.py",))]
    out = _run(_bridge({"x": 0.95, "y": 0.75, "z": 0.70}).route(bps, r))
    routed = {o.blueprint_id for o in out if o.routed}
    assert routed == {"x"}


def test_max_per_route_caps_survivors(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_MAX_PER_ROUTE", "2")
    r = _FakeRouter()
    bps = [_BP(str(i), (f"{i}.py",)) for i in range(5)]
    out = _run(_bridge({str(i): 0.9 for i in range(5)}).route(bps, r))
    assert sum(o.routed for o in out) == 2 and len(r.ingested) == 2


# ---------------------------------------------------------------------------
# Bulletproof dedup — the mandate-4 constraint
# ---------------------------------------------------------------------------


def test_already_routed_blueprint_is_dropped(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = _bridge({"a": 0.9})
    # First event routes it.
    out1 = _run(b.route([_BP("a", ("a.py",))], r))
    assert sum(o.routed for o in out1) == 1 and len(r.ingested) == 1
    # Second event with the SAME blueprint_id — dropped, NOT re-ingested.
    out2 = _run(b.route([_BP("a", ("a.py",))], r))
    assert all(not o.routed for o in out2)
    assert any(o.reason == "duplicate" for o in out2)
    assert len(r.ingested) == 1   # router never saw it twice


def test_router_side_dedup_result_still_marks_routed(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter(result="deduplicated")   # router already had it
    b = _bridge({"a": 0.9})
    out = _run(b.route([_BP("a", ("a.py",))], r))
    assert any(o.routed for o in out)                 # in-queue counts as routed
    # subsequent pass is deduped by our durable registry, not re-sent
    out2 = _run(b.route([_BP("a", ("a.py",))], r))
    assert len(r.ingested) == 1 and any(o.reason == "duplicate" for o in out2)


def test_dedup_registry_ttl_and_capacity(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_DEDUP_TTL_S", "100")
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_DEDUP_CAPACITY", "16")
    reg = cpb._RoutedRegistry()
    reg.mark("a", now=1000.0)
    assert reg.is_routed("a", now=1050.0) is True     # within TTL
    assert reg.is_routed("a", now=1200.0) is False    # expired
    # capacity bound
    for i in range(40):
        reg.mark(f"b{i}", now=2000.0 + i)
    assert len(reg) <= 16


def test_envelope_signature_is_blueprint_id(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    _run(_bridge({"bp-42": 0.9}).route([_BP("bp-42", ("a.py",))], r))
    env = r.ingested[0]
    assert env.evidence["signature"] == "bp-42"       # drives router dedup_key
    assert env.evidence["blueprint_id"] == "bp-42"
    assert env.source == "auto_proposed"
    assert env.target_files == ("a.py",)
    assert "conception_value" in env.evidence


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_blueprint_without_target_files_is_skipped(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    out = _run(_bridge({"a": 0.9}).route([_BP("a", ())], r))
    assert out == [] and r.ingested == []


def test_never_raises_on_bad_router(monkeypatch):
    _enable(monkeypatch, floor=0.0)

    class _Boom:
        async def ingest(self, e):
            raise RuntimeError("boom")

    out = _run(_bridge({"a": 0.9}).route([_BP("a", ("a.py",))], _Boom()))
    assert any(o.reason == "error" for o in out)      # captured, not raised


def test_null_router_is_inert(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    assert _run(_bridge({"a": 0.9}).route([_BP("a", ("a.py",))], None)) == []


def test_scorer_fault_skips_blueprint(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = cpb.ConceptionProposalBridge(
        value_scorer=lambda bp: (_ for _ in ()).throw(ValueError("x")),
        ledger_emit=lambda **k: None,
    )
    out = _run(b.route([_BP("a", ("a.py",))], r))
    assert out == [] and r.ingested == []


# ---------------------------------------------------------------------------
# Event wiring + observability
# ---------------------------------------------------------------------------


def test_observer_pulls_batch_and_routes(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = _bridge({"a": 0.9, "b": 0.1})
    batch = [_BP("a", ("a.py",)), _BP("b", ("b.py",))]
    obs = b.make_observer(r, blueprint_source=lambda: batch)
    _run(obs(_BP("a", ("a.py",))))   # event carries one bp; observer pulls batch
    assert len(r.ingested) == 1 and r.ingested[0].evidence["blueprint_id"] == "a"


def test_observer_faulty_source_is_safe(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    obs = _bridge({"a": 0.9}).make_observer(
        r, blueprint_source=lambda: (_ for _ in ()).throw(RuntimeError("src")))
    _run(obs(_BP("a", ("a.py",))))   # must not raise
    assert r.ingested == []


def test_snapshot_shape(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = _bridge({"a": 0.9})
    _run(b.route([_BP("a", ("a.py",))], r))
    snap = b.snapshot()
    assert snap["schema_version"] == cpb.CONCEPTION_BRIDGE_SCHEMA_VERSION
    assert snap["enabled"] is True
    assert snap["counters"]["routed_total"] == 1
    assert snap["dedup_registry_size"] == 1


def test_module_is_authority_free():
    import inspect
    src = inspect.getsource(cpb)
    banned = ("iron_gate", "risk_tier_floor", "semantic_guardian", "policy_engine",
              "orchestrator", "tool_executor", "scoped_tool_backend")
    for b in banned:
        assert f"import {b}" not in src and f"governance.{b}" not in src, b
