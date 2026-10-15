"""Five boot lines from 2026-10-14 11:32, each a wrong answer, not noise to hide.

* [KillGuard] refused terminate on bash.exe x2 -- the code-change sweep chose
  the operator's shells by the words on their command lines.
* Memory pressure 79.3% - MODERATE relief -- a 62 GB host holding J-Prime's
  17.6 GB model was judged as an idle "server"; the relief freed nothing.
* Memory tier changed: optimal -> abundant (a WARNING) -- the quantizer read
  macOS-only fields on Windows (0% in use), and an assumed starting tier made
  its first reading a "change".
* UAE / SAI / Learning DB: ❌ -- the quantizer was never connected to them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import types
from collections import namedtuple

import pytest


# -- a sweep chooses only what the kill guard would let it stop --------------------
def test_the_ownership_rule():
    from backend.core.process_identity import _owned
    lineage = {10, 1}                                  # our launcher chain
    never = lambda p: False                            # noqa: E731
    assert _owned(100, 100, lineage, [10, 1], never) is True        # ourselves
    assert _owned(10, 100, lineage, [1], lambda p: True) is False   # never our launcher
    assert _owned(200, 100, lineage, [100, 10, 1], never) is True   # our child
    assert _owned(300, 100, lineage, [7, 1], lambda p: p == 7) is True   # under a Trinity process
    assert _owned(400, 100, lineage, [10, 1], lambda p: p == 1) is False  # walk stops at our lineage
    assert _owned(500, 100, lineage, [7, 1], never) is False        # a stranger


def test_the_snapshot_owns_our_child_and_not_our_parent(monkeypatch):
    from backend.core import process_identity as pi
    monkeypatch.setattr(pi, "_snapshot", (float("-inf"), frozenset()))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             cwd=os.path.expanduser("~"))
    try:
        owned = pi.trinity_owned_pids()
        assert os.getpid() in owned and child.pid in owned
        assert os.getppid() not in owned
    finally:
        child.kill()
        child.wait()


def test_the_snapshot_builds_the_parent_map_once(monkeypatch):
    from backend.core import process_identity as pi
    import psutil
    calls = []
    real = psutil._ppid_map

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(psutil, "_ppid_map", counted)
    monkeypatch.setattr(pi, "_snapshot", (float("-inf"), frozenset()))
    pi.trinity_owned_pids()
    assert len(calls) == 1


def test_may_signal_follows_the_guard(monkeypatch):
    from backend.core import process_identity as pi
    monkeypatch.setattr(pi, "trinity_owned_pids", lambda: frozenset({1234}))
    monkeypatch.setenv("JARVIS_KILL_GUARD", "true")
    assert pi.may_signal(1234) is True and pi.may_signal(4321) is False
    monkeypatch.setenv("JARVIS_KILL_GUARD", "false")
    assert pi.may_signal(4321) is True


def test_the_cleanup_sweep_never_selects_what_it_may_not_stop(monkeypatch):
    from backend.core import process_identity as pi
    from backend.process_cleanup_manager import ProcessCleanupManager
    monkeypatch.setattr(pi, "may_signal", lambda pid: False)

    class Proc:
        pid = 4321

        def name(self):
            return "bash.exe"

        def cmdline(self):
            return ["bash", "-c", "tail supervisor.log # jarvis unified_supervisor.py"]

    manager = ProcessCleanupManager.__new__(ProcessCleanupManager)
    manager.config = {"jarvis_patterns": ["jarvis"], "jarvis_port_patterns": []}
    assert manager._is_jarvis_process(Proc()) is False


# -- memory: the quantizer measures Windows, and says it calmly --------------------
def test_used_memory_is_measured_on_every_platform():
    from backend.core.memory_quantizer import MemoryQuantizer
    Win = namedtuple("Win", "total available percent used free")
    Mac = namedtuple("Mac", "total available percent used free active inactive wired")
    assert MemoryQuantizer._true_used_bytes(Win(100, 20, 80.0, 80, 20)) == 80
    assert MemoryQuantizer._true_used_bytes(Mac(100, 60, 40.0, 40, 10, 15, 25, 5)) == 20


def test_tier_severity_is_the_declaration_order():
    from backend.core.memory_quantizer import MemoryTier
    assert [t.severity for t in MemoryTier] == list(range(len(MemoryTier)))
    assert MemoryTier.CRITICAL.severity > MemoryTier.OPTIMAL.severity


@pytest.mark.parametrize("old,new,level", [
    ("OPTIMAL", "ABUNDANT", logging.INFO),
    ("OPTIMAL", "CONSTRAINED", logging.WARNING),
])
def test_only_a_worse_tier_is_a_warning(caplog, old, new, level):
    from backend.core.memory_quantizer import MemoryQuantizer, MemoryTier
    q = MemoryQuantizer.__new__(MemoryQuantizer)
    q.tier_change_callbacks = []
    q.learning_db = None
    caplog.set_level(logging.INFO, logger="backend.core.memory_quantizer")
    asyncio.run(q._handle_tier_change(MemoryTier[old], MemoryTier[new]))
    line = [r for r in caplog.records if "Memory tier changed" in r.getMessage()][0]
    assert line.levelno == level


# -- the quantizer connects to its engines once they exist -------------------------
def test_engines_are_connected_when_they_come_up(monkeypatch):
    from backend.core.memory_quantizer import MemoryQuantizer
    q = MemoryQuantizer.__new__(MemoryQuantizer)
    q.uae_engine = q.sai_engine = q.learning_db = None
    seen = []
    q.pattern_learner = types.SimpleNamespace(set_learning_db=seen.append)

    db = types.SimpleNamespace(_initialized=False)
    monkeypatch.setitem(sys.modules, "backend.intelligence.learning_database",
                        types.SimpleNamespace(_db_instance=db))
    monkeypatch.setitem(sys.modules, "vision.situational_awareness.core_engine",
                        types.SimpleNamespace(_sai_engine=object()))
    monkeypatch.delitem(sys.modules, "intelligence.unified_awareness_engine", raising=False)
    monkeypatch.delitem(sys.modules, "backend.intelligence.unified_awareness_engine", raising=False)

    q._attach_collaborators()
    assert q.sai_engine is not None             # found under its other import name
    assert q.learning_db is None                # not until it has initialised
    assert q.uae_engine is None                 # never imported or created here
    db._initialized = True
    q._attach_collaborators()
    assert q.learning_db is db and seen == [db]


def test_patterns_reach_the_learning_db_in_its_own_shape():
    from backend.core.memory_quantizer import MemoryPatternLearner
    stored = []

    class DB:
        async def store_pattern(self, pattern, auto_merge=True):
            stored.append(pattern)
            return "p1"

    learner = MemoryPatternLearner()
    learner.set_learning_db(DB())
    for mb in (100.0, 120.0, 140.0):
        asyncio.run(learner.learn_pattern("backend", mb, {"tier": "elevated"}))
    assert stored and stored[-1]["pattern_type"] == "memory_usage"
    assert stored[-1]["pattern_data"]["component"] == "backend"


# -- pressure is judged beside the memory Trinity holds on purpose -----------------
def test_without_planned_memory_the_policy_is_unchanged():
    from backend.core.memory_types import PressurePolicy
    assert PressurePolicy.for_host(62.0) == PressurePolicy.for_ram_gb(62.0)


def test_planned_memory_rebases_the_policy_onto_the_rest_of_the_machine():
    from backend.core.memory_types import PressurePolicy, PressureTier
    pol = PressurePolicy.for_host(61.7, 17.6)
    base = PressurePolicy.for_ram_gb(61.7 - 17.6)          # 44 GB left: prosumer
    assert pol.version.startswith(base.version)
    share = 17.6 / 61.7 * 100
    for tier, pct in base.enter_thresholds.items():
        assert pol.enter_thresholds[tier] == pytest.approx(share + pct * (100 - share) / 100)
    assert 79.3 < pol.enter_thresholds[PressureTier.CONSTRAINED]    # the 11:32 boot
    assert pol.enter_thresholds[PressureTier.EMERGENCY] < 100       # a full machine still escalates


def test_the_controller_and_the_broker_share_one_policy(monkeypatch):
    from backend.core.ouroboros.governance import trinity_siblings as ts
    from backend.core.memory_types import PressureTier
    from backend.process_cleanup_manager import IntelligentMemoryController
    from backend.core.memory_budget_broker import MemoryBudgetBroker
    monkeypatch.setattr(ts, "planned_resident_gb", lambda: 17.6)
    monkeypatch.delenv("JARVIS_MEMORY_MODERATE_THRESHOLD", raising=False)
    controller = IntelligentMemoryController()
    broker = MemoryBudgetBroker(types.SimpleNamespace(), epoch=1)
    assert controller._moderate_threshold == pytest.approx(
        broker.policy.enter_thresholds[PressureTier.CONSTRAINED])
    monkeypatch.setattr(ts, "planned_resident_gb", lambda: 0.0)     # J-Prime stopped
    assert "+planned" not in broker.policy.version


# -- a policy snapshot taken before J-Prime served is not acted on -----------------
# supervisor-20261015-085516.log: the controller was built at 08:55:30 (nothing
# planned -> 65% moderate), J-Prime's model loaded 08:55:54, and "MODERATE
# relief" fired at 66.7% on 08:56:40, when the policy's own answer was 82%.
def test_the_controller_rereads_the_policy_before_acting_pre_baseline(monkeypatch):
    from backend.core.ouroboros.governance import trinity_siblings as ts
    from backend.process_cleanup_manager import IntelligentMemoryController
    for k in ("JARVIS_MEMORY_MODERATE_THRESHOLD", "JARVIS_MEMORY_HIGH_THRESHOLD",
              "JARVIS_MEMORY_CRITICAL_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ts, "planned_resident_gb", lambda: 0.0)       # boot: not serving yet
    controller = IntelligentMemoryController()
    controller._total_ram_gb = 61.7
    controller._rederive_policy_thresholds()
    assert controller._moderate_threshold < 66.7                      # the stale snapshot
    monkeypatch.setattr(ts, "planned_resident_gb", lambda: 17.3)      # the model loaded
    emit, _, level = controller.should_emit_event(66.7)
    assert (emit, level) == (False, None)
    assert controller._moderate_threshold > 80.0


def test_a_zero_planned_reading_is_not_held_for_the_full_ttl(monkeypatch):
    from backend.core.ouroboros.governance import trinity_siblings as ts
    readings = iter([0, 17 * 1024 ** 3])
    clock = {"t": 1000.0}
    monkeypatch.setattr(ts, "serving_siblings_resident_bytes", lambda: next(readings))
    monkeypatch.setattr(ts.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ts, "_planned", (float("-inf"), 0.0))
    monkeypatch.setenv("JARVIS_MEMORY_PLANNED_TTL_S", "60")
    monkeypatch.setenv("JARVIS_MEMORY_PLANNED_ZERO_TTL_S", "10")
    assert ts.planned_resident_gb() == 0.0
    clock["t"] += 11                                                   # < 60 s, > 10 s
    assert ts.planned_resident_gb() == pytest.approx(17.0)


def test_the_prediction_uses_the_host_policy():
    from backend.core.memory_quantizer import MemoryTier, PredictiveMemoryPlanner
    from backend.core.memory_types import PressurePolicy
    pol = PressurePolicy.for_host(61.7, 17.3)                          # critical from ~89%
    assert PredictiveMemoryPlanner._usage_to_tier(86.0) == MemoryTier.CRITICAL   # legacy table
    assert PredictiveMemoryPlanner._usage_to_tier(86.0, pol) == MemoryTier.CONSTRAINED
