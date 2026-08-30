"""A bounded queue rejects by capacity, and capacity is priority-blind.

`BackgroundAgentPool.submit` computes a rich priority -- sovereign human,
resurrection, route tier -- and then calls `put_nowait`. On a full bounded
queue that raises regardless of priority, so the tiers govern DEQUEUE ORDER
and cannot help an op that was never admitted.

Measured, soak bt-2026-08-30-093912: an operator's signed goal reached the
HEAD of the WAL re-drain (`wal_replay_reordered head_moved=True`, batches of
178/71/144) and still never dispatched -- 215 `pool_capacity_full` parks.
Being first in line does not help when the line never moves.

Ordinary work is now capped BELOW the hard bound, leaving a reserve only
authority-backed work may enter.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)


class _Pool(BackgroundAgentPool):
    """Only the sizing surface is needed; no event loop, no workers."""
    def __init__(self, queue_size):
        self._queue_size = queue_size


@pytest.mark.parametrize(
    "size,reserved,soft",
    [(16, 2, 14), (8, 1, 7), (4, 1, 3), (2, 1, 1)],
)
def test_headroom_scales_with_configured_capacity(size, reserved, soft):
    """The reserve is a RATIO of the configured queue, never a fixed count --
    it tracks the pool as the operator sizes it."""
    p = _Pool(size)
    assert p._authority_headroom_slots() == reserved
    assert p._ordinary_soft_capacity() == soft


def test_a_reserve_always_leaves_room_for_ordinary_work():
    """The reservation must never starve background traffic to a standstill:
    soft capacity stays >= 1 at every size."""
    for size in range(1, 65):
        p = _Pool(size)
        assert p._ordinary_soft_capacity() >= 1
        assert p._authority_headroom_slots() <= max(0, size - 1)


def test_a_queue_too_small_to_partition_degrades_to_old_behaviour():
    """size < 2 cannot be split; reserve 0 so nothing changes."""
    p = _Pool(1)
    assert p._authority_headroom_slots() == 0


def test_ratio_is_env_tunable(monkeypatch):
    """No hardcoded policy: the operator sets the split."""
    monkeypatch.setenv("JARVIS_BG_AUTHORITY_HEADROOM_RATIO", "0.5")
    p = _Pool(16)
    assert p._authority_headroom_slots() == 8
    assert p._ordinary_soft_capacity() == 8


def test_ratio_zero_disables_the_reservation(monkeypatch):
    """An explicit opt-out restores the pre-existing admission exactly."""
    monkeypatch.setenv("JARVIS_BG_AUTHORITY_HEADROOM_RATIO", "0")
    p = _Pool(16)
    assert p._authority_headroom_slots() == 0
    assert p._ordinary_soft_capacity() == 16


@pytest.mark.parametrize("bad", ["", "abc", "-1"])
def test_a_malformed_ratio_never_raises(monkeypatch, bad):
    """Admission must survive a typo in an env var."""
    monkeypatch.setenv("JARVIS_BG_AUTHORITY_HEADROOM_RATIO", bad)
    p = _Pool(16)
    slots = p._authority_headroom_slots()
    assert 0 <= slots <= 15


def test_the_submit_path_consults_the_soft_cap():
    """WIRING PIN. The helpers are useless unless `submit` gates on them, and
    every test above calls them directly -- blind to an unwired gate."""
    import ast
    import io as _io

    import backend.core.ouroboros.governance.background_agent_pool as M

    src = _io.open(M.__file__, encoding="utf-8").read()
    called = {
        n.func.attr
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_ordinary_soft_capacity" in called, "soft cap is unwired"
    assert "operator_authority" in src, "authority stamp never read"


def test_the_router_stamps_authority_before_submitting():
    """The other half of the contract: the pool reads a stamp the router must
    actually write, from the finding it already computed at intake."""
    import io as _io

    import backend.core.ouroboros.governance.intake.unified_intake_router as R

    src = _io.open(R.__file__, encoding="utf-8").read()
    assert "operator_authority" in src, "router never stamps authority"
    assert src.index("operator_authority") < src.index(
        "_submit_fn = getattr(self._gls"
    ), "stamp must be written before the submit call"
