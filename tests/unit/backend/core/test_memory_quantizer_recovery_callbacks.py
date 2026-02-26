import pytest

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
