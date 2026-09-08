"""Self-signed work does not queue behind work nobody chose.

Replays session ``bt-2026-09-08-193049``: eight harness batch ops submitted at
12:31:24, the Sentinel's own signed goal at 12:31:37 landing at
``queue_depth=8`` with the SAME ``priority=3``, on a one-worker lane retiring
roughly one op per 24 minutes — about three hours down a queue inside a
40-minute session wall.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import background_agent_pool as P

#: The Sentinel's real goal id from that session.
SENTINEL_GOAL = "ov-auto-uncovered-module-apply-emergency-cpu-fix"


class _Ctx:
    """The two context shapes the live session actually produced."""

    def __init__(self, evidence=None, *, signal_source="roadmap"):
        self.intake_evidence = evidence or {}
        self.signal_source = signal_source
        self.provider_route = "standard"


def _batch_op():
    """op-01a08280-f4f2 — source='roadmap', and its own telemetry says
    'this op carries no signed-goal pointer'."""
    return _Ctx({})


def _sentinel_op():
    """op-01a08281-27f1 — 'DelegatedProvenance VERIFIED goal_id=...'."""
    return _Ctx({"provenance": {"goal_id": SENTINEL_GOAL}})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        "JARVIS_AUTONOMOUS_PRIMACY_ENABLED",
        "JARVIS_RESURRECTION_PRIMACY_MARGIN",
        "JARVIS_SOVEREIGN_PRIMACY_MARGIN",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# --------------------------------------------------------------------------
# The discriminator
# --------------------------------------------------------------------------

def test_signal_source_alone_does_NOT_discriminate():
    """The trap. Both the batch ops and the Sentinel's goal carried
    source='roadmap' in the live session, so boosting on it would boost the
    entire queue and reorder nothing."""
    assert _batch_op().signal_source == _sentinel_op().signal_source == "roadmap"


def test_the_signed_goal_pointer_DOES_discriminate():
    assert P._self_directed_goal_id(_batch_op()) == ""
    assert P._self_directed_goal_id(_sentinel_op()) == SENTINEL_GOAL


def test_the_pointer_is_read_through_the_one_shared_accessor():
    """A second reader of the contract is a second opinion about which ops are
    self-directed."""
    import inspect

    src = inspect.getsource(P._self_directed_goal_id)
    assert "goal_pointer_for" in src


@pytest.mark.parametrize("hostile", [None, object(), 42, "str", b"bytes"])
def test_the_discriminator_never_raises(hostile):
    assert P._self_directed_goal_id(hostile) == ""


def test_the_discriminator_survives_a_raising_property():
    class _Hostile:
        @property
        def intake_evidence(self):
            raise RuntimeError("boom")

    assert P._self_directed_goal_id(_Hostile()) == ""


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------

def test_the_full_precedence_ordering():
    """human < resurrected < self-signed autonomous < normal routes."""
    assert (
        P._sovereign_pool_priority()
        < P._resurrection_pool_priority()
        < P._autonomous_pool_priority()
        < min(P._ROUTE_PRIORITY.values())
    )


def test_autonomous_outranks_the_batch_tier_it_was_starved_behind():
    assert P._autonomous_pool_priority() < P._ROUTE_PRIORITY["standard"]


def test_autonomous_never_outranks_a_human():
    assert P._autonomous_pool_priority() > P._sovereign_pool_priority()


def test_autonomous_never_outranks_a_survivor():
    """A resurrected op already paid for its progress once."""
    assert P._autonomous_pool_priority() > P._resurrection_pool_priority()


def test_the_tier_is_derived_and_follows_its_neighbours(monkeypatch):
    """A midpoint, never a literal — the property `_resurrection_pool_priority`
    established and this tier has to preserve."""
    seen = set()
    for margin in ("10", "100", "500", "4000"):
        monkeypatch.setenv("JARVIS_RESURRECTION_PRIMACY_MARGIN", margin)
        value = P._autonomous_pool_priority()
        seen.add(value)
        assert P._resurrection_pool_priority() < value < min(P._ROUTE_PRIORITY.values())
    assert len(seen) > 1, "the tier is pinned, not derived"


def test_a_degenerate_span_ties_rather_than_inverting(monkeypatch):
    """Adjacent neighbours leave no integer between them. A tie is broken by
    the queue's submission_order; an INVERSION would put self-directed work
    ahead of a survivor, which the ladder forbids."""
    monkeypatch.setenv("JARVIS_RESURRECTION_PRIMACY_MARGIN", "1")
    assert P._autonomous_pool_priority() >= P._resurrection_pool_priority()
    assert P._autonomous_pool_priority() < min(P._ROUTE_PRIORITY.values())


def test_the_boost_is_monotone_never_a_demotion():
    """min() in submit(): a route that already outranks the tier keeps its
    place, so arming this cannot delay an op that was going to run sooner."""
    import inspect

    src = inspect.getsource(P.BackgroundAgentPool.submit)
    assert "if _boosted < _priority:" in src, "the boost can demote an op"


def test_the_boost_cannot_lift_past_the_primacy_tiers():
    """Structural: the autonomous branch lives in the ELSE of the sovereign and
    resurrection checks, so it can only ever lift an op out of the
    undifferentiated route tier."""
    import inspect

    src = inspect.getsource(P.BackgroundAgentPool.submit)
    sovereign = src.index("_sovereign_pool_priority()")
    resurrection = src.index("_resurrection_pool_priority()")
    autonomous = src.index("_autonomous_pool_priority()")
    assert sovereign < resurrection < autonomous


def test_the_kill_switch_restores_the_old_ordering(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTONOMOUS_PRIMACY_ENABLED", "false")
    assert not P.autonomous_primacy_enabled()
    monkeypatch.setenv("JARVIS_AUTONOMOUS_PRIMACY_ENABLED", "true")
    assert P.autonomous_primacy_enabled()


def test_default_is_on():
    """This closes a live starvation; shadow-first would leave the next run
    exactly as starved as the last one."""
    assert P.autonomous_primacy_enabled()


# --------------------------------------------------------------------------
# The live queue, replayed
# --------------------------------------------------------------------------

def test_the_sentinel_goal_now_sorts_ahead_of_the_batch():
    """The whole fix, as the PriorityQueue would order it: 8 batch ops
    submitted first, the Sentinel's goal ninth."""
    entries = []
    for i in range(8):
        ctx = _batch_op()
        entries.append((P._ROUTE_PRIORITY["standard"], i, f"batch-{i}", ctx))
    sentinel = _sentinel_op()
    assert P._self_directed_goal_id(sentinel)
    entries.append((P._autonomous_pool_priority(), 8, "sentinel", sentinel))

    order = [name for _, _, name, _ in sorted(entries, key=lambda e: (e[0], e[1]))]
    assert order[0] == "sentinel", order
