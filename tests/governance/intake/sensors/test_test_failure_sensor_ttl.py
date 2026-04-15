# [Ouroboros] Written by Ouroboros (op=op-019d9368-) at 2026-04-15 UTC
# [Ouroboros] Modified by Ouroboros (op=op-019d9368-) at 2026-04-15 23:22 UTC
# Reason: Write four focused sensor-level test modules for the TestFailureSensor in-flight dedup mechanism shipped in commit 20baa

# Reason: Module B - TTL expiry re-admits the same target for a new op.

"""Module B: TTL expiry - after TTL expiry the same target is re-admitted
and a new op is created.

The TTL default is read from the env var JARVIS_TEST_FAILURE_INFLIGHT_TTL_S
(default 300s). Tests manipulate time.monotonic via monkeypatching the
pending_target_keys timestamp directly to avoid real sleeps.
"""
from __future__ import annotations

import importlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    test_failure_sensor as tfs,
)
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


def _make_signal(
    target: str = "tests/test_auth.py",
    stable: bool = True,
    streak: int = 2,
) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=(target,),
        repo="jarvis",
        description=f"Stable test failure: {target}::test_login",
        evidence={
            "signature": f"AssertionError:{target}",
            "test_id": f"{target}::test_login",
            "streak": streak,
            "error_text": "AssertionError",
        },
        confidence=min(0.95, 0.7 + 0.1 * streak),
        stable=stable,
    )


def _make_router(return_value: str = "enqueued") -> MagicMock:
    router = MagicMock()
    router.ingest = AsyncMock(return_value=return_value)
    return router


@pytest.mark.asyncio
async def test_after_ttl_expiry_target_is_readmitted() -> None:
    """After the TTL window elapses the stale entry is pruned and the
    next signal for the same target reaches router.ingest, producing a
    new op. No real sleep is used - we backdate the timestamp directly.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    # First emission - marks target in-flight
    sig1 = _make_signal(streak=2)
    env1 = await sensor._signal_to_envelope_and_ingest(sig1)
    assert env1 is not None
    assert router.ingest.await_count == 1
    assert "tests/test_auth.py" in sensor._pending_target_keys

    # Simulate TTL expiry by backdating the recorded timestamp
    ttl = tfs._INFLIGHT_TTL_S
    sensor._pending_target_keys["tests/test_auth.py"] = time.monotonic() - ttl - 1.0

    # Second emission after TTL - must be re-admitted
    sig2 = _make_signal(streak=3)
    env2 = await sensor._signal_to_envelope_and_ingest(sig2)

    assert env2 is not None, "target must be re-admitted after TTL expiry"
    assert router.ingest.await_count == 2, "router.ingest must be called for the re-admitted signal"


@pytest.mark.asyncio
async def test_stale_entry_pruned_from_pending_keys() -> None:
    """_prune_stale_pending removes entries whose age exceeds the TTL,
    keeping the dict bounded even when release_target is never called.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    # Manually insert a stale entry
    ttl = tfs._INFLIGHT_TTL_S
    sensor._pending_target_keys["tests/test_stale.py"] = time.monotonic() - ttl - 5.0

    # Trigger pruning
    sensor._prune_stale_pending()

    assert "tests/test_stale.py" not in sensor._pending_target_keys, (
        "stale entry must be removed by _prune_stale_pending"
    )


@pytest.mark.asyncio
async def test_fresh_entry_not_pruned() -> None:
    """An entry recorded just now must survive _prune_stale_pending."""
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    sensor._pending_target_keys["tests/test_fresh.py"] = time.monotonic()
    sensor._prune_stale_pending()

    assert "tests/test_fresh.py" in sensor._pending_target_keys, (
        "fresh entry must not be pruned before TTL elapses"
    )


@pytest.mark.asyncio
async def test_ttl_read_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module-level _INFLIGHT_TTL_S must reflect the env var
    JARVIS_TEST_FAILURE_INFLIGHT_TTL_S when the module is (re)loaded.
    """
    monkeypatch.setenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", "42")
    importlib.reload(tfs)
    try:
        assert tfs._INFLIGHT_TTL_S == 42.0, (
            "_INFLIGHT_TTL_S must equal the value from JARVIS_TEST_FAILURE_INFLIGHT_TTL_S"
        )
    finally:
        monkeypatch.delenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", raising=False)
        importlib.reload(tfs)


@pytest.mark.asyncio
async def test_zero_ttl_disables_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting JARVIS_TEST_FAILURE_INFLIGHT_TTL_S=0 disables dedup entirely:
    every emission reaches router.ingest regardless of prior state.
    """
    monkeypatch.setenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", "0")
    importlib.reload(tfs)
    try:
        assert tfs._INFLIGHT_TTL_S == 0.0

        router = _make_router("enqueued")
        sensor = tfs.TestFailureSensor(repo="jarvis", router=router)

        await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))
        await sensor._signal_to_envelope_and_ingest(_make_signal(streak=3))

        assert router.ingest.await_count == 2, (
            "both signals must reach router when dedup is disabled via TTL=0"
        )
        assert not sensor._pending_target_keys, (
            "nothing must be tracked when dedup is disabled"
        )
    finally:
        monkeypatch.delenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", raising=False)
        importlib.reload(tfs)
