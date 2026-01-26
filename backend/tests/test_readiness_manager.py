#!/usr/bin/env python3
"""
Test for ReadinessStateManager integration in health endpoints.

This validates that the v95.3 fix for /health/ready 503 error works correctly.
"""
import asyncio
import sys
import os

# Add backend directory to path (this file is in backend/tests/)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def test_readiness_manager_import():
    """Test that ReadinessStateManager can be imported and instantiated."""
    try:
        from core.readiness_state_manager import (
            get_readiness_manager,
            ComponentCategory,
            InitializationPhase,
            ProbeType,
        )
        print("✅ ReadinessStateManager imports successful")
        
        # Create a test manager
        manager = get_readiness_manager("test-component")
        assert manager is not None
        print("✅ Manager instantiation successful")
        
        # Check initial state
        assert manager.state.phase == InitializationPhase.NOT_STARTED
        print("✅ Initial phase is NOT_STARTED")
        
        return True
    except Exception as e:
        print(f"❌ Import test failed: {e}")
        return False


async def test_phase_transitions():
    """Test that phase transitions work correctly."""
    try:
        from core.readiness_state_manager import (
            get_readiness_manager,
            ComponentCategory,
            InitializationPhase,
        )
        
        # Get manager (singleton, so it may already exist from previous test)
        manager = get_readiness_manager("test-transitions")
        
        # Test start
        await manager.start()
        assert manager.state.phase == InitializationPhase.STARTING
        print("✅ Transition to STARTING successful")
        
        # Test mark_initializing
        await manager.mark_initializing()
        assert manager.state.phase == InitializationPhase.INITIALIZING
        print("✅ Transition to INITIALIZING successful")
        
        # Register and mark components
        await manager.register_component("test_component", ComponentCategory.CRITICAL)
        await manager.mark_component_ready("test_component", healthy=True)
        print("✅ Component registration and marking successful")
        
        # Test mark_ready
        await manager.mark_ready()
        assert manager.state.phase == InitializationPhase.READY
        print("✅ Transition to READY successful")
        
        # Test shutdown
        await manager.start_shutdown()
        assert manager.state.phase == InitializationPhase.SHUTTING_DOWN
        print("✅ Transition to SHUTTING_DOWN successful")
        
        return True
    except Exception as e:
        print(f"❌ Phase transition test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_probe_response():
    """Test that health probes return correct responses."""
    try:
        from core.readiness_state_manager import (
            get_readiness_manager,
            ProbeType,
            InitializationPhase,
        )
        
        # Create new manager in READY state
        manager = get_readiness_manager("test-probes")
        
        # Initialize to READY for this test
        if manager.state.phase == InitializationPhase.NOT_STARTED:
            await manager.start()
            await manager.mark_initializing()
            await manager.mark_ready()
        
        # Test readiness probe
        probe = manager.handle_probe(ProbeType.READINESS)
        # ProbeResponse uses 'success' attribute (to_dict returns 'ready')
        assert probe.success == True
        assert probe.status_code == 200
        print("✅ READINESS probe returns 200 when READY")
        
        # Test liveness probe (should always pass when not in error)
        liveness = manager.handle_probe(ProbeType.LIVENESS)
        assert liveness.success == True  # 'alive' is in to_dict(), attribute is 'success'
        print("✅ LIVENESS probe returns success=True")
        
        return True
    except Exception as e:
        print(f"❌ Probe response test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("Testing ReadinessStateManager Integration (v95.3)")
    print("=" * 60)
    
    results = []
    
    # Test 1: Imports
    result = test_readiness_manager_import()
    results.append(("Import Test", result))
    
    # Test 2: Phase transitions (async)
    result = asyncio.run(test_phase_transitions())
    results.append(("Phase Transitions", result))
    
    # Test 3: Probe responses (async)
    result = asyncio.run(test_probe_response())
    results.append(("Probe Responses", result))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("All tests passed! 🎉")
        return 0
    else:
        print("Some tests failed! ⚠️")
        return 1


if __name__ == "__main__":
    sys.exit(main())
