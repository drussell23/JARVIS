# tests/governance/test_context_governor.py
from __future__ import annotations
from backend.core.ouroboros.governance import context_governor as cg


class _Floors:
    def __init__(self, met, missing=()):
        self._met = met
        self._missing = tuple(missing)
    def is_satisfied(self, ledger):
        return self._met
    def missing_categories(self, ledger):
        return self._missing


def _gov(**kw):
    return cg.InformationGainGovernor(
        prefetch_excerpts=kw.get("prefetch", ["def helper(): return 1"]),
        floors=kw.get("floors", _Floors(met=True)),
        enabled=kw.get("enabled", True),
        min_gain=kw.get("min_gain", 0.15),
        decay_rounds=kw.get("decay_rounds", 2),
    )


def test_disabled_always_continues():
    g = _gov(enabled=False)
    v = g.observe_round(0, ["totally new content xyz"], ledger=None)
    assert v.action == "continue"


def test_high_gain_continues():
    g = _gov()
    v = g.observe_round(0, ["completely unrelated brand new tokens alpha beta"],
                        ledger=None)
    assert v.action == "continue"
    assert v.info_gain > 0.15


def test_round0_baseline_is_prefetch_not_empty():
    g = _gov(prefetch=["def helper(): return 1"])
    v = g.observe_round(0, ["def helper(): return 1"], ledger=None)
    assert v.info_gain < 0.15


def test_decay_triggers_converge_when_floor_met():
    g = _gov(floors=_Floors(met=True), decay_rounds=2, prefetch=["aaa bbb ccc"])
    g.observe_round(0, ["aaa bbb ccc"], ledger=None)
    v = g.observe_round(1, ["aaa bbb ccc"], ledger=None)
    assert v.action == "converge"


def test_decay_with_floor_unmet_emits_deadlock_break():
    g = _gov(floors=_Floors(met=False, missing=("CALL_GRAPH", "HISTORY")),
             decay_rounds=2, prefetch=["aaa bbb"])
    g.observe_round(0, ["aaa bbb"], ledger=None)
    v = g.observe_round(1, ["aaa bbb"], ledger=None)
    assert v.action == "deadlock_break"
    assert set(v.missing_categories) == {"CALL_GRAPH", "HISTORY"}
    assert "get_callers" in v.directive
    assert "git_" in v.directive


def test_elastic_budget_warm_compresses_cold_expands():
    warm = _gov(prefetch=["seed content present"])
    cold = _gov(prefetch=[])
    vw = warm.observe_round(0, ["new alpha"], ledger=None)
    vc = cold.observe_round(0, ["new alpha"], ledger=None)
    assert vw.budget_scale < 1.0
    assert vc.budget_scale >= 1.0


def test_deadlock_breaker_is_one_shot():
    g = _gov(floors=_Floors(met=False, missing=("CALL_GRAPH",)),
             decay_rounds=1, prefetch=["aaa"])
    v1 = g.observe_round(0, ["aaa"], ledger=None)
    assert v1.action == "deadlock_break"
    g.mark_deadlock_round_consumed()
    v2 = g.observe_round(1, ["aaa"], ledger=None)
    assert v2.action == "deadlock_failed"
