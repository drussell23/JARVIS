"""C3 — Coalesce absorbed-lease ack test.

Verifies that when ``_flush_coalesced`` merges N envelopes into 1,
the N-1 absorbed leases (envelopes[1:]) are acked via WAL
``update_status(lease_id, "acked")`` so they do not re-play on restart.

Contract:
- base lease (envelopes[0].lease_id) flows through on the merged envelope — NOT acked here.
- absorbed leases (envelopes[1:].lease_id) each get ``update_status(lease_id, "acked")``.
- ack is fail-soft: a WAL error for an absorbed lease must NOT propagate.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, call

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope_with_lease(
    *,
    target: str = "a.py",
    lease_id: str | None = None,
    urgency: str = "normal",
) -> IntentEnvelope:
    """Create a valid envelope and stamp a lease_id onto it."""
    env = make_envelope(
        source="backlog",
        description=f"test-coalesce-{target}",
        target_files=(target,),
        repo="jarvis",
        confidence=0.8,
        urgency=urgency,
        evidence={"signature": f"sig-{target}"},
        requires_human_ack=False,
    )
    lid = lease_id or str(uuid.uuid4())
    return env.with_lease(lid)


def _make_router(tmp_path: Path) -> UnifiedIntakeRouter:
    """Build a minimal router with a stub GLS. Not started — drive directly."""
    gls = MagicMock()
    gls.submit = MagicMock(return_value=None)
    config = IntakeRouterConfig(
        project_root=tmp_path,
        wal_path=tmp_path / ".jarvis" / "intake_wal.jsonl",
        lock_path=tmp_path / ".jarvis" / "intake_router.lock",
        max_queue_size=100,
    )
    return UnifiedIntakeRouter(gls=gls, config=config)


def _seed_coalesce_buffer(
    router: UnifiedIntakeRouter, key: str, envelopes: List[IntentEnvelope]
) -> None:
    """Directly inject envelopes into the router's coalesce buffer."""
    router._coalesce_buffer[key] = list(envelopes)
    import time
    router._coalesce_timestamps[key] = time.monotonic()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCoalesceLeaseAck:
    """Verify absorbed leases are acked on coalesce flush (C3)."""

    def test_absorbed_leases_are_acked(self, tmp_path: Path) -> None:
        """3 coalesced envelopes → 2 absorbed leases are acked; base is NOT acked."""
        router = _make_router(tmp_path)

        lease_ids = [str(uuid.uuid4()) for _ in range(3)]
        envelopes = [
            _make_envelope_with_lease(target="x.py", lease_id=lease_ids[0]),
            _make_envelope_with_lease(target="y.py", lease_id=lease_ids[1]),
            _make_envelope_with_lease(target="z.py", lease_id=lease_ids[2]),
        ]

        # Replace WAL with an inspectable mock
        fake_wal = MagicMock()
        router._wal = fake_wal

        coalesce_key = "test-key"
        _seed_coalesce_buffer(router, coalesce_key, envelopes)

        merged = router._flush_coalesced(coalesce_key)

        # Merged envelope carries base's lease_id
        assert merged is not None
        assert merged.lease_id == lease_ids[0], "base lease must flow through"

        # Only absorbed leases (indices 1 and 2) are acked
        acked_calls = [
            c for c in fake_wal.update_status.call_args_list
        ]
        acked_lease_ids = [c.args[0] for c in acked_calls]
        acked_statuses = [c.args[1] for c in acked_calls]

        assert lease_ids[1] in acked_lease_ids, "absorbed lease[1] must be acked"
        assert lease_ids[2] in acked_lease_ids, "absorbed lease[2] must be acked"
        for status in acked_statuses:
            assert status == "acked", f"unexpected status: {status!r}"

        # Base lease must NOT appear in acked calls
        assert lease_ids[0] not in acked_lease_ids, "base lease must NOT be acked here"

        # Exactly 2 ack calls (one per absorbed lease)
        assert len(acked_calls) == 2, f"expected 2 ack calls, got {len(acked_calls)}"

    def test_single_envelope_no_ack(self, tmp_path: Path) -> None:
        """Single-envelope flush (no merge) — no ack calls emitted."""
        router = _make_router(tmp_path)
        fake_wal = MagicMock()
        router._wal = fake_wal

        env = _make_envelope_with_lease(target="solo.py")
        _seed_coalesce_buffer(router, "solo-key", [env])

        merged = router._flush_coalesced("solo-key")
        assert merged is env
        fake_wal.update_status.assert_not_called()

    def test_ack_fail_soft_does_not_raise(self, tmp_path: Path) -> None:
        """WAL error on absorbed-lease ack must NOT propagate — flush succeeds."""
        router = _make_router(tmp_path)
        fake_wal = MagicMock()
        fake_wal.update_status.side_effect = RuntimeError("disk full")
        router._wal = fake_wal

        lease_ids = [str(uuid.uuid4()) for _ in range(2)]
        envelopes = [
            _make_envelope_with_lease(target="a.py", lease_id=lease_ids[0]),
            _make_envelope_with_lease(target="b.py", lease_id=lease_ids[1]),
        ]
        _seed_coalesce_buffer(router, "fail-key", envelopes)

        # Must not raise even though update_status raises
        merged = router._flush_coalesced("fail-key")
        assert merged is not None
        assert merged.lease_id == lease_ids[0]
