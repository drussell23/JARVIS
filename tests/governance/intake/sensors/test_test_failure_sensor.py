"""Tests for TestFailureSensor (Sensor B)."""
import importlib
import time
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors import (
    test_failure_sensor as tfs,
)
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


def _make_signal(stable: bool = True, streak: int = 2) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_auth.py",),
        repo="jarvis",
        description="Stable test failure: test_auth::test_login",
        evidence={
            "signature": "AssertionError:tests/test_auth.py",
            "test_id": "tests/test_auth.py::test_login",
            "streak": streak,
            "error_text": "AssertionError",
        },
        confidence=min(0.95, 0.7 + 0.1 * streak),
        stable=stable,
    )


async def test_stable_signal_produces_envelope():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=True)
    envelope = await sensor._signal_to_envelope_and_ingest(signal)
    assert envelope is not None
    assert envelope.source == "test_failure"
    assert envelope.target_files == ("tests/test_auth.py",)
    assert envelope.urgency == "high"
    assert envelope.evidence["test_id"] == "tests/test_auth.py::test_login"
    router.ingest.assert_called_once_with(envelope)


async def test_unstable_signal_is_skipped():
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=False)
    result = await sensor._signal_to_envelope_and_ingest(signal)
    assert result is None
    router.ingest.assert_not_called()


async def test_handle_signals_batch():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signals = [_make_signal(stable=True), _make_signal(stable=False)]
    results = await sensor.handle_signals(signals)
    # Returns one entry per input signal (None for the unstable one)
    assert len(results) == 2
    assert len([r for r in results if r is not None]) == 1


async def test_confidence_preserved_from_signal():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=True, streak=5)
    envelope = await sensor._signal_to_envelope_and_ingest(signal)
    assert envelope is not None
    # confidence should reflect streak: min(0.95, 0.7 + 0.1*5) = 0.95 (capped at 1.0 by envelope)
    assert 0.9 <= envelope.confidence <= 1.0


# ---------------------------------------------------------------------------
# In-flight dedup (bt-2026-04-15-010727 v5 test_failure concurrency storm)
# ---------------------------------------------------------------------------


class TestInFlightDedup:
    """Exercises ``_pending_target_keys`` and the suppression gate in
    ``_signal_to_envelope_and_ingest``. Motivating scenario: v5 battle
    test emitted 3 distinct test_failure ops for the same broken file
    over 88 seconds while Claude first-token was stalled at 85s. Each
    op ran to exhaustion, collectively driving the ExhaustionWatcher to
    hibernation. Sensor-side dedup rejects the re-emissions before any
    router / WAL / queue / op_id resources are burned.
    """

    async def test_first_emission_enqueues_and_marks_target(self):
        router = MagicMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = TestFailureSensor(repo="jarvis", router=router)
        signal = _make_signal(stable=True, streak=2)

        envelope = await sensor._signal_to_envelope_and_ingest(signal)

        assert envelope is not None
        router.ingest.assert_called_once()
        # Target file should now be marked in-flight
        assert "tests/test_auth.py" in sensor._pending_target_keys

    async def test_second_emission_for_same_target_is_suppressed(self):
        """The canonical v5 repro: same target_files, second signal
        arrives while the first op is still in flight. The second
        ingest must never reach router.ingest — short-circuited at the
        sensor gate.
        """
        router = MagicMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = TestFailureSensor(repo="jarvis", router=router)

        # First signal — enqueued
        sig1 = _make_signal(stable=True, streak=2)
        env1 = await sensor._signal_to_envelope_and_ingest(sig1)
        assert env1 is not None
        assert router.ingest.await_count == 1

        # Second signal — same target, different streak (sensor would
        # normally emit a fresh signal on the next poll cycle)
        sig2 = _make_signal(stable=True, streak=3)
        env2 = await sensor._signal_to_envelope_and_ingest(sig2)

        assert env2 is None, "second emission must be suppressed"
        assert router.ingest.await_count == 1, (
            "router.ingest must not be called for the suppressed signal"
        )

    async def test_different_target_files_are_independent(self):
        """Dedup is per-target-file. A signal for a different file goes
        through even when another target is in-flight.
        """
        router = MagicMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = TestFailureSensor(repo="jarvis", router=router)

        # First file in-flight
        sig1 = _make_signal(stable=True, streak=2)
        await sensor._signal_to_envelope_and_ingest(sig1)

        # Different file — should go through
        sig2 = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_unrelated.py",),
            repo="jarvis",
            description="Stable test failure: test_unrelated::test_x",
            evidence={
                "signature": "AssertionError:tests/test_unrelated.py",
                "test_id": "tests/test_unrelated.py::test_x",
                "streak": 2,
                "error_text": "AssertionError",
            },
            confidence=0.9,
            stable=True,
        )
        env2 = await sensor._signal_to_envelope_and_ingest(sig2)

        assert env2 is not None
        assert router.ingest.await_count == 2
        assert "tests/test_auth.py" in sensor._pending_target_keys
        assert "tests/test_unrelated.py" in sensor._pending_target_keys

    async def test_ttl_expiry_re_allows_emission(self, monkeypatch):
        """Stale entries past the TTL are pruned, allowing a fresh signal
        for the same target. Simulates a stuck op that never released.
        """
        router = MagicMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = TestFailureSensor(repo="jarvis", router=router)

        # First emission
        sig1 = _make_signal(stable=True, streak=2)
        await sensor._signal_to_envelope_and_ingest(sig1)
        assert router.ingest.await_count == 1

        # Force the entry to be stale
        sensor._pending_target_keys["tests/test_auth.py"] = (
            time.monotonic() - tfs._INFLIGHT_TTL_S - 1.0
        )

        # Second emission should pass because the stale entry gets pruned
        sig2 = _make_signal(stable=True, streak=3)
        env2 = await sensor._signal_to_envelope_and_ingest(sig2)

        assert env2 is not None
        assert router.ingest.await_count == 2

    async def test_disabled_via_env(self, monkeypatch):
        """Setting TTL to 0 disables the dedup entirely — every emission
        reaches router.ingest regardless of prior state.
        """
        monkeypatch.setenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", "0")
        importlib.reload(tfs)
        try:
            assert tfs._INFLIGHT_TTL_S == 0.0

            router = MagicMock()
            router.ingest = AsyncMock(return_value="enqueued")
            sensor = tfs.TestFailureSensor(repo="jarvis", router=router)

            sig1 = _make_signal(stable=True, streak=2)
            await sensor._signal_to_envelope_and_ingest(sig1)
            sig2 = _make_signal(stable=True, streak=3)
            await sensor._signal_to_envelope_and_ingest(sig2)
            sig3 = _make_signal(stable=True, streak=4)
            await sensor._signal_to_envelope_and_ingest(sig3)

            # All three reach the router when dedup is disabled
            assert router.ingest.await_count == 3
            # Nothing was tracked either
            assert not sensor._pending_target_keys
        finally:
            monkeypatch.setenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", "300")
            importlib.reload(tfs)

    async def test_non_enqueued_ingest_does_not_mark(self):
        """When router.ingest returns ``"queued_behind"`` / ``"deduplicated"``
        / ``"pending_ack"``, the sensor must NOT mark the target as
        in-flight — the envelope is still pending, and the router will
        re-ingest it later (in the queued_behind case) or has already
        dropped it (deduplicated). Marking would cause the sensor to
        wrongly suppress the eventual retry.
        """
        router = MagicMock()
        router.ingest = AsyncMock(return_value="queued_behind")
        sensor = TestFailureSensor(repo="jarvis", router=router)

        sig = _make_signal(stable=True, streak=2)
        env = await sensor._signal_to_envelope_and_ingest(sig)

        # Envelope still returned (router accepted the conversion)
        assert env is not None
        # But target is NOT marked — router will handle the queued re-ingest
        assert "tests/test_auth.py" not in sensor._pending_target_keys

    async def test_release_target_is_idempotent(self):
        sensor = TestFailureSensor(repo="jarvis", router=MagicMock())
        # No-op on unknown key
        sensor.release_target("tests/never_seen.py")

        # After mark, release clears
        sensor._pending_target_keys["tests/test_auth.py"] = time.monotonic()
        sensor.release_target("tests/test_auth.py")
        assert "tests/test_auth.py" not in sensor._pending_target_keys

        # Second release is a no-op (doesn't raise)
        sensor.release_target("tests/test_auth.py")

    async def test_handle_signals_suppresses_duplicates_in_batch(self):
        """When ``handle_signals`` receives a batch containing multiple
        signals for the same target, only the first one reaches the
        router. Covers the rare case where the plugin path produces
        two signals in one call.
        """
        router = MagicMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = TestFailureSensor(repo="jarvis", router=router)

        batch = [
            _make_signal(stable=True, streak=2),
            _make_signal(stable=True, streak=3),  # same target
            _make_signal(stable=True, streak=4),  # same target
        ]
        results = await sensor.handle_signals(batch)

        # First signal ingested, rest suppressed
        assert results[0] is not None
        assert results[1] is None
        assert results[2] is None
        assert router.ingest.await_count == 1
