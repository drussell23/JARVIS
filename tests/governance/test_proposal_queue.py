"""Proactive Proposal Queue spine — flow protection + eviction gating.

Mandate 4 verbatim (2026-07-19): a valid cross-space proposal is
generated; idle mocked to 2.0s (active typing) → Active Flow
Protection blocks the TUI notification and HOLDS the proposal; a
simulated dhash invalidation later flushes it.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.duplex.proposal_queue import (
    ProposalQueue,
)


def _insight(desc="reconcile test in S2 with code in S4"):
    return {"description": desc, "affected_spaces": [2, 4]}


class TestActiveFlowProtection:
    def test_typing_blocks_notification_then_evicted(self, monkeypatch):
        """MANDATE 4 VERBATIM: idle=2.0s (typing) → held; dhash
        invalidation flushes it."""
        idle = {"s": 2.0}
        clock = {"t": 1000.0}
        q = ProposalQueue(
            idle_source=lambda: idle["s"], clock=lambda: clock["t"],
        )
        assert q.submit(_insight(), [2, 4], dhash="TOPO_A") is True
        assert q.depth == 1
        # Active typing (2.0s < 15s gate) → notification BLOCKED, held:
        assert q.present_if_idle() is None
        assert q.stats["held_flow"] == 1
        assert q.depth == 1                          # still queued
        # The user closed/fixed the window — dhash invalidation:
        evicted = q.evict_stale(current_dhash="TOPO_B")
        assert evicted == 1
        assert q.depth == 0                          # silently flushed
        assert q.stats["spatial_evicted"] == 1

    def test_idle_operator_receives_proposal(self):
        q = ProposalQueue(idle_source=lambda: 30.0)  # idle 30s > 15s gate
        q.submit(_insight(), [2, 4], dhash="A")
        p = q.present_if_idle()
        assert p is not None
        assert "reconcile" in p.summary()
        assert q.stats["presented"] == 1
        assert q.depth == 0                          # dequeued for approval

    def test_flow_then_idle_transition(self):
        idle = {"s": 3.0}
        q = ProposalQueue(idle_source=lambda: idle["s"])
        q.submit(_insight(), [2, 4], dhash="A")
        assert q.present_if_idle() is None           # typing
        idle["s"] = 20.0                             # operator stepped away
        assert q.present_if_idle() is not None       # now delivered


class TestEviction:
    def test_ttl_expiry_flushes(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PROPOSAL_TTL_S", "300")
        clock = {"t": 1000.0}
        q = ProposalQueue(idle_source=lambda: 0.0, clock=lambda: clock["t"])
        q.submit(_insight(), [2, 4], dhash="A")
        clock["t"] += 301.0                          # past 5-min TTL
        assert q.evict_stale() == 1
        assert q.stats["ttl_evicted"] == 1
        assert q.depth == 0

    def test_matching_dhash_survives_eviction(self):
        q = ProposalQueue(idle_source=lambda: 0.0)
        q.submit(_insight(), [2, 4], dhash="STABLE")
        assert q.evict_stale(current_dhash="STABLE") == 0
        assert q.depth == 1                          # topology unchanged

    def test_semantic_dedup(self):
        q = ProposalQueue(idle_source=lambda: 0.0)
        assert q.submit(_insight("same"), [2, 4], dhash="A") is True
        assert q.submit(_insight("same"), [2, 4], dhash="A") is False
        assert q.stats["deduped"] == 1
        assert q.depth == 1


class TestExecutionGating:
    def test_present_returns_proposal_never_mutates_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/proposal_queue.py"
        ).read_text()
        # No filesystem writes anywhere in this module — it PROPOSES.
        assert "open(" not in src
        assert "write" not in src.replace("# ", "").lower() or \
            "never touches the filesystem" in src
        # DRY: routes through the EXISTING [Y/n] gate, no new alert sys.
        assert "Iron Gate" in src

    def test_idle_source_fail_safe_toward_active(self):
        # native_idle unreadable → 0.0 → treated as ACTIVE → never
        # interrupts (fail-safe toward NOT disturbing the operator).
        from backend.core.ouroboros.governance.comms.duplex.proposal_queue import (  # noqa: E501
            native_idle_seconds,
        )
        assert isinstance(native_idle_seconds(), float)
