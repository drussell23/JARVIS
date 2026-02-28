import pytest

from backend.core import memory_quantizer as mq_mod
from backend.core.memory_quantizer import MemoryQuantizer, MemoryTier


@pytest.mark.asyncio
async def test_recovery_callback_fires_on_critical_to_optimal_transition():
    quantizer = MemoryQuantizer(config={})
    events = []

    async def _on_recovery(old_tier, new_tier):
        events.append((old_tier.value, new_tier.value))

    quantizer.register_recovery_callback(_on_recovery)
    await quantizer._handle_tier_change(MemoryTier.CRITICAL, MemoryTier.OPTIMAL)

    assert events == [("critical", "optimal")]


@pytest.mark.asyncio
async def test_recovery_callback_not_fired_for_non_recovery_transition():
    quantizer = MemoryQuantizer(config={})
    events = []

    def _on_recovery(old_tier, new_tier):
        events.append((old_tier.value, new_tier.value))

    quantizer.register_recovery_callback(_on_recovery)
    await quantizer._handle_tier_change(MemoryTier.ELEVATED, MemoryTier.OPTIMAL)

    assert events == []


def test_register_unregister_recovery_callback_is_idempotent():
    quantizer = MemoryQuantizer(config={})

    def _callback(*_args, **_kwargs):
        return None

    quantizer.register_recovery_callback(_callback)
    quantizer.register_recovery_callback(_callback)
    assert len(quantizer._recovery_callbacks) == 1

    quantizer.unregister_recovery_callback(_callback)
    quantizer.unregister_recovery_callback(_callback)
    assert quantizer._recovery_callbacks == []


@pytest.mark.asyncio
async def test_thrash_emergency_requires_sustained_signal(monkeypatch):
    quantizer = MemoryQuantizer(config={})
    states = []

    async def _on_thrash(state):
        states.append(state)

    quantizer.register_thrash_callback(_on_thrash)
    monkeypatch.setattr(mq_mod, "THRASH_PAGEIN_WARNING", 500)
    monkeypatch.setattr(mq_mod, "THRASH_PAGEIN_EMERGENCY", 2000)
    monkeypatch.setattr(mq_mod, "THRASH_SUSTAINED_SECONDS", 10)
    monkeypatch.setattr(mq_mod, "THRASH_EMERGENCY_SUSTAINED_SECONDS", 10)
    monkeypatch.setattr(mq_mod, "THRASH_RECOVERY_SUSTAINED_SECONDS", 5)
    monkeypatch.setattr(mq_mod, "THRASH_PAGEIN_PANIC_MULTIPLIER", 99.0)

    # First emergency-level sample should not jump straight to emergency.
    quantizer._pagein_rate = 2400
    quantizer._pagein_rate_ema = 2400
    quantizer._thrash_warning_since = 100.0
    quantizer._thrash_emergency_since = 100.0
    monkeypatch.setattr(mq_mod.time, "time", lambda: 105.0)
    await quantizer._check_thrash_state()
    assert quantizer._thrash_state == "healthy"
    assert states == []

    # A sustained second sample should escalate to emergency.
    quantizer._pagein_rate = 2500
    quantizer._pagein_rate_ema = 2500
    monkeypatch.setattr(mq_mod.time, "time", lambda: 122.0)
    await quantizer._check_thrash_state()
    assert quantizer._thrash_state == "emergency"
    assert states[-1] == "emergency"
