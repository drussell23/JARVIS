# [Ouroboros] Written by Ouroboros (op=op-019d9368-) at 2026-04-15 UTC
# [Ouroboros] Modified by Ouroboros (op=op-019d9368-) at 2026-04-15 23:23 UTC
# Reason: Write four focused sensor-level test modules for the TestFailureSensor in-flight dedup mechanism shipped in commit 20baa

# Reason: Module C - concurrent signals for different target files are not cross-suppressed.

"""Module C: Isolation - concurrent signals for different target files are
not cross-suppressed. Dedup is keyed per target_file, so an in-flight op
for file A must never block a signal for file B.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


def _make_signal(
    target: str,
    stable: bool = True,
    streak: int = 2,
) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=(target,),
        repo="jarvis",
        description=f"Stable test failure: {target}::test_x",
        evidence={
            "signature": f"AssertionError:{target}",
            "test_id": f"{target}::test_x",
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
async def test_different_targets_are_not_cross_suppressed() -> None:
    """An in-flight op for file A must not suppress a signal for file B.
    Both targets must be independently tracked and independently admitted.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    target_a = "tests/test_auth.py"
    target_b = "tests/test_billing.py"

    # Enqueue file A
    env_a = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_a, streak=2))
    assert env_a is not None
    assert router.ingest.await_count == 1
    assert target_a in sensor._pending_target_keys

    # File B must still be admitted even though A is in-flight
    env_b = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_b, streak=2))
    assert env_b is not None, "signal for a different target must not be suppressed"
    assert router.ingest.await_count == 2
    assert target_b in sensor._pending_target_keys


@pytest.mark.asyncio
async def test_repeated_signal_for_in_flight_target_is_suppressed_not_other() -> None:
    """After A is in-flight, a repeat of A is suppressed but a fresh
    signal for C still goes through. Verifies per-key isolation.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    target_a = "tests/test_auth.py"
    target_c = "tests/test_cache.py"

    # Enqueue A
    await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_a, streak=2))
    assert router.ingest.await_count == 1

    # Repeat of A - suppressed
    env_a2 = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_a, streak=3))
    assert env_a2 is None, "repeat of in-flight target A must be suppressed"
    assert router.ingest.await_count == 1

    # Fresh signal for C - must go through
    env_c = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_c, streak=2))
    assert env_c is not None, "signal for unrelated target C must not be suppressed"
    assert router.ingest.await_count == 2


@pytest.mark.asyncio
async def test_many_concurrent_targets_all_admitted() -> None:
    """N distinct targets submitted sequentially all reach router.ingest
    exactly once each - no cross-contamination between keys.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    targets = [
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/test_gamma.py",
        "tests/test_delta.py",
        "tests/test_epsilon.py",
    ]

    for i, target in enumerate(targets):
        env = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target, streak=2))
        assert env is not None, f"signal for {target} must be admitted"
        assert router.ingest.await_count == i + 1

    # All targets tracked independently
    for target in targets:
        assert target in sensor._pending_target_keys, (
            f"{target} must be tracked as in-flight after enqueue"
        )


@pytest.mark.asyncio
async def test_release_one_target_does_not_affect_others() -> None:
    """Releasing target A via release_target() must not affect the
    in-flight status of target B. Per-key isolation of the release path.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    target_a = "tests/test_auth.py"
    target_b = "tests/test_billing.py"

    await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_a, streak=2))
    await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_b, streak=2))
    assert router.ingest.await_count == 2

    # Release only A
    sensor.release_target(target_a)
    assert target_a not in sensor._pending_target_keys
    assert target_b in sensor._pending_target_keys, (
        "releasing A must not affect B's in-flight status"
    )

    # A can now be re-admitted
    env_a2 = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_a, streak=3))
    assert env_a2 is not None, "A must be re-admitted after explicit release"
    assert router.ingest.await_count == 3

    # B is still suppressed
    env_b2 = await sensor._signal_to_envelope_and_ingest(_make_signal(target=target_b, streak=3))
    assert env_b2 is None, "B must still be suppressed - it was not released"
    assert router.ingest.await_count == 3
