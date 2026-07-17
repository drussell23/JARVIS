"""ConceptionBridge — structured decision telemetry + IncubationStore.

Closes the "observability black hole": every routing decision now emits a
deterministic, structured RoutingOutcome carrying the FULL EV matrix
(``substance`` / ``feasibility`` / ``alignment``) and the disposition — no
forensic log reconstruction (Mandate 1). A sub-threshold blueprint is NOT
discarded: it routes to a bounded IncubationStore, tagged with an incubating
state vector, and is re-scored on later events as repo state evolves (Mandate 2).
The store reuses the existing blueprint artifact + snapshot()/observability
emission (Mandate 3) and is bounded/never-raise (Mandate 4).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Tuple

import backend.core.ouroboros.governance.conception_proposal_bridge as cpb


# ---------------------------------------------------------------------------
# Fakes — mirror the real ExpectedValue contract (full EV matrix)
# ---------------------------------------------------------------------------


@dataclass
class _BP:
    blueprint_id: str
    target_files: Tuple[str, ...]
    description: str = "improve X"
    title: str = "X"
    category: str = "performance"
    repo: str = "repo"


@dataclass
class _EV:
    ev: float
    scope: str = "svc"
    alignment: float = 0.5
    substance: float = 0.5
    feasibility: float = 0.5
    rationale: str = "r"

    def to_dict(self):
        return {
            "ev": self.ev, "scope": self.scope, "alignment": self.alignment,
            "substance": self.substance, "feasibility": self.feasibility,
        }


class _FakeRouter:
    def __init__(self, result="enqueued"):
        self.ingested: List[object] = []
        self._result = result

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return self._result


def _bridge(ev_map: Dict[str, _EV], **kw):
    """A bridge whose scorer maps blueprint_id → a full EV matrix object.

    ev_map values may be an ``_EV`` (full matrix) or a bare float (ev only)."""
    def _score(bp):
        v = ev_map[bp.blueprint_id]
        return v if isinstance(v, _EV) else _EV(float(v))
    return cpb.ConceptionProposalBridge(
        value_scorer=_score,
        ledger_emit=lambda **k: None,
        **kw,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _enable(monkeypatch, floor=0.5):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_EV_FLOOR", str(floor))


# ---------------------------------------------------------------------------
# Mandate 1 — structured telemetry: full EV matrix on every outcome
# ---------------------------------------------------------------------------


def test_routed_outcome_carries_full_ev_matrix(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = _bridge({"hi": _EV(0.9, alignment=0.8, substance=0.7, feasibility=0.6),
                 "lo": _EV(0.1)})
    out = _run(b.route([_BP("hi", ("a.py",)), _BP("lo", ("b.py",))], r))
    hi = next(o for o in out if o.blueprint_id == "hi")
    assert hi.routed and hi.reason == "routed"
    assert hi.alignment == 0.8 and hi.substance == 0.7 and hi.feasibility == 0.6
    # to_dict() is the structured payload — carries the whole matrix.
    d = hi.to_dict()
    for k in ("blueprint_id", "ev", "substance", "feasibility", "alignment",
              "threshold", "reason", "routed", "incubation_attempts"):
        assert k in d


def test_every_decision_lands_in_the_ring(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = _bridge({"hi": _EV(0.9), "lo": _EV(0.1)})
    _run(b.route([_BP("hi", ("a.py",)), _BP("lo", ("b.py",))], r))
    snap = b.snapshot()
    decisions = snap["recent_decisions"]
    assert len(decisions) == 2
    reasons = {d["blueprint_id"]: d["reason"] for d in decisions}
    assert reasons == {"hi": "routed", "lo": "incubated"}
    # Each decision record is fully structured (EV matrix present).
    for d in decisions:
        assert {"substance", "feasibility", "alignment", "ev"} <= set(d)


def test_structured_log_emitted_per_decision(monkeypatch, caplog):
    _enable(monkeypatch, floor=0.0)
    import logging
    caplog.set_level(logging.INFO, logger=cpb.logger.name)
    r = _FakeRouter()
    _run(_bridge({"hi": _EV(0.9), "lo": _EV(0.1)}).route(
        [_BP("hi", ("a.py",)), _BP("lo", ("b.py",))], r))
    lines = [m for m in caplog.messages if "decision blueprint=" in m]
    assert len(lines) == 2
    assert any("disposition=routed" in m for m in lines)
    assert any("disposition=incubated" in m for m in lines)


# ---------------------------------------------------------------------------
# Mandate 2 — sub-threshold routes to the IncubationStore (not discarded)
# ---------------------------------------------------------------------------


def test_below_threshold_ev_incubates_not_discarded(monkeypatch):
    # EV 0.4 < neutral floor 0.5 → the "cold organism" case. It must NOT
    # vanish — it must land in the IncubationStore.
    _enable(monkeypatch, floor=0.5)
    r = _FakeRouter()
    b = _bridge({"cold": _EV(0.4, alignment=0.3, substance=0.4, feasibility=0.5)})
    out = _run(b.route([_BP("cold", ("f.py",))], r))
    assert r.ingested == []                       # nothing routed
    assert out[0].reason == "incubated"
    assert out[0].incubation_attempts == 1
    snap = b.snapshot()
    assert snap["incubator_size"] == 1
    assert snap["counters"]["incubated_total"] == 1
    inc = snap["incubating"][0]
    assert inc["blueprint_id"] == "cold"
    # The incubating state vector preserves the full EV matrix for re-eval.
    assert inc["alignment"] == 0.3 and inc["substance"] == 0.4


def test_incubated_blueprint_re_evaluated_on_later_event(monkeypatch):
    # A temporally-misaligned proposal: cold now, warm later as repo evolves.
    _enable(monkeypatch, floor=0.5)
    r = _FakeRouter()
    ev_map = {"seed": _EV(0.4)}
    b = _bridge(ev_map)
    _run(b.route([_BP("seed", ("f.py",))], r))
    assert b.snapshot()["incubator_size"] == 1     # incubating

    # Repo state shifts → the SAME blueprint now scores above the floor. A new
    # event carrying an unrelated blueprint still re-scores the incubating one.
    ev_map["seed"] = _EV(0.95)
    ev_map["other"] = _EV(0.1)
    out = _run(b.route([_BP("other", ("g.py",))], r))
    routed = {o.blueprint_id for o in out if o.routed}
    assert "seed" in routed                        # graduated + routed
    assert len(r.ingested) == 1
    snap = b.snapshot()
    assert snap["counters"]["graduated_total"] == 1
    assert all(i["blueprint_id"] != "seed" for i in snap["incubating"])


def test_incubation_attempts_increment_across_events(monkeypatch):
    _enable(monkeypatch, floor=0.9)   # keep it cold across passes
    r = _FakeRouter()
    b = _bridge({"cold": _EV(0.4)})
    bp = _BP("cold", ("f.py",))
    _run(b.route([bp], r))
    _run(b.route([bp], r))
    out = _run(b.route([bp], r))
    assert out[0].incubation_attempts == 3
    assert b.snapshot()["incubating"][0]["attempts"] == 3
    assert b.snapshot()["counters"]["incubated_total"] == 1   # one distinct bp


def test_incubator_retires_chronically_cold(monkeypatch):
    _enable(monkeypatch, floor=0.9)
    monkeypatch.setenv("JARVIS_CONCEPTION_INCUBATOR_MAX_ATTEMPTS", "2")
    r = _FakeRouter()
    b = _bridge({"cold": _EV(0.4)})
    bp = _BP("cold", ("f.py",))
    _run(b.route([bp], r))                  # attempt 1
    _run(b.route([bp], r))                  # attempt 2 → hits ceiling, retired
    assert b.snapshot()["incubator_size"] == 0


def test_incubator_capacity_bounded_drop_oldest(monkeypatch):
    _enable(monkeypatch, floor=0.9)
    monkeypatch.setenv("JARVIS_CONCEPTION_INCUBATOR_SIZE", "2")
    r = _FakeRouter()
    b = _bridge({"a": _EV(0.1), "b": _EV(0.1), "c": _EV(0.1)})
    # One event, three sub-threshold blueprints incubated in order a,b,c —
    # capacity 2 evicts the oldest first-seen ("a").
    _run(b.route(
        [_BP("a", ("a.py",)), _BP("b", ("b.py",)), _BP("c", ("c.py",))], r))
    snap = b.snapshot()
    assert snap["incubator_size"] == 2
    ids = {i["blueprint_id"] for i in snap["incubating"]}
    assert ids == {"b", "c"}                # "a" dropped (oldest)


# ---------------------------------------------------------------------------
# Mandate 4 — bounded ring + never-raise
# ---------------------------------------------------------------------------


def test_decision_ring_bounded(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    monkeypatch.setenv("JARVIS_CONCEPTION_DECISION_RING_SIZE", "3")
    r = _FakeRouter()
    b = _bridge({f"n{i}": _EV(0.9) for i in range(6)})
    for i in range(6):
        _run(b.route([_BP(f"n{i}", ("f.py",))], r))
    assert len(b.snapshot()["recent_decisions"]) == 3   # ring capped


def test_duplicate_graduates_and_emits(monkeypatch):
    _enable(monkeypatch, floor=0.0)
    r = _FakeRouter()
    b = _bridge({"a": _EV(0.9)})
    bp = _BP("a", ("f.py",))
    _run(b.route([bp], r))                  # routed → in the dedup registry
    out = _run(b.route([bp], r))            # second event → duplicate
    assert out[0].reason == "duplicate"
    # duplicate decisions are structured telemetry too
    assert any(d["reason"] == "duplicate" for d in b.snapshot()["recent_decisions"])


def test_reset_clears_incubation_and_decisions(monkeypatch):
    _enable(monkeypatch, floor=0.9)
    r = _FakeRouter()
    b = _bridge({"cold": _EV(0.4)})
    _run(b.route([_BP("cold", ("f.py",))], r))
    assert b.snapshot()["incubator_size"] == 1
    b.reset_for_tests()
    snap = b.snapshot()
    assert snap["incubator_size"] == 0
    assert snap["recent_decisions"] == []
    assert snap["counters"]["incubated_total"] == 0
