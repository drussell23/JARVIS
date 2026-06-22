from __future__ import annotations
import importlib, pytest
from backend.core.ouroboros.governance import convergence_watchdog as cw

def _fresh():
    importlib.reload(cw); return cw.ReductionTracker()

def test_thresholds_defaults(monkeypatch):
    monkeypatch.delenv("JARVIS_WATCHDOG_STALL_RATIO", raising=False)
    monkeypatch.delenv("JARVIS_WATCHDOG_STALL_PASSES", raising=False)
    assert cw.stall_ratio_threshold() == 0.95 and cw.stall_passes_threshold() == 2
    assert cw.watchdog_enabled() is True

def test_good_reduction_no_stall():
    t = _fresh()
    v = t.record_pass("g1", parent_chars=1000, max_child_chars=400)  # ratio 0.4
    assert v.stalled is False and v.ratio < 0.5 and v.consecutive_stalls == 0

def test_two_consecutive_stalls_trips():
    t = _fresh()
    t.record_pass("g1", 1000, 980)            # 0.98 stall #1
    v = t.record_pass("g1", 1000, 990)        # 0.99 stall #2 -> stalled
    assert v.consecutive_stalls >= 2 and v.stalled is True

def test_good_pass_resets_run():
    t = _fresh()
    t.record_pass("g1", 1000, 980)            # stall
    v = t.record_pass("g1", 1000, 300)        # good -> resets
    assert v.consecutive_stalls == 0 and v.stalled is False

def test_lineages_independent():
    t = _fresh()
    t.record_pass("a", 1000, 990); t.record_pass("a", 1000, 990)
    v = t.record_pass("b", 1000, 200)
    assert v.stalled is False

def test_failsoft_bad_input():
    t = _fresh()
    v = t.record_pass("g", parent_chars=0, max_child_chars=0)
    assert isinstance(v.stalled, bool)

def test_emit_sovereign_yield_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        cw.emit_sovereign_yield("op1", lineage_id="g1", ratio=0.97, consecutive_stalls=2,
                                parent_chars=5000, child_chars=4850, tier="tier2")
    assert any("[SOVEREIGN YIELD]" in r.getMessage() for r in caplog.records)
