"""Tests for the fault injection framework itself."""
import asyncio
import pytest


class TestFaultInjector:
    def test_register_and_trigger_fault(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType
        injector = FaultInjector()
        injector.register(boundary="prime_client.request", fault_type=FaultType.NETWORK_PARTITION)
        fault = injector.check("prime_client.request")
        assert fault is not None
        assert fault.fault_type == FaultType.NETWORK_PARTITION
        # Second check: fault consumed (one-shot by default)
        assert injector.check("prime_client.request") is None

    def test_probabilistic_fault(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType
        injector = FaultInjector(seed=42)
        injector.register_probabilistic("health_check.*", FaultType.TIMEOUT_AFTER_SUCCESS, probability=1.0)
        fault = injector.check("health_check.prime")
        assert fault is not None

    def test_no_fault_when_unregistered(self):
        from tests.adversarial.fault_injector import FaultInjector
        injector = FaultInjector()
        assert injector.check("unknown_boundary") is None

    @pytest.mark.asyncio
    async def test_inject_timeout_after_success(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType, apply_fault
        injector = FaultInjector()
        injector.register("my_op", FaultType.TIMEOUT_AFTER_SUCCESS, params={"delay_s": 0.01})

        call_count = 0
        async def my_operation():
            nonlocal call_count
            call_count += 1
            return "success"

        fault = injector.check("my_op")
        with pytest.raises(asyncio.TimeoutError):
            await apply_fault(fault, my_operation(), timeout=0.005)
        assert call_count == 1

    def test_clock_jump_fault(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType, MockClock
        injector = FaultInjector()
        clock = MockClock()
        injector.register("timer_check", FaultType.CLOCK_JUMP_FORWARD, params={"jump_s": 60})
        fault = injector.check("timer_check")
        assert fault is not None
        clock.apply_fault(fault)
        assert clock.wall_offset == 60
