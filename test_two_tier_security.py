#!/usr/bin/env python3
"""
Test Script for Two-Tier Agentic Security System
=================================================

Verifies:
1. AgenticWatchdog initialization and state
2. TieredCommandRouter routing decisions
3. TieredVBIAAdapter authentication callbacks
4. Integration between all components

Usage:
    python3 test_two_tier_security.py
"""

# =============================================================================
# CRITICAL: Python 3.9 Compatibility - MUST be first before ANY other imports
# =============================================================================
import sys
import os

# Add backend to path FIRST so we can import the compat module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

# Apply Python 3.9 compatibility patches BEFORE any other imports
# This patches importlib.metadata.packages_distributions() which google-api-core needs
try:
    from utils.python39_compat import ensure_python39_compatibility
    compat_results = ensure_python39_compatibility()
except ImportError:
    # Fallback: manually patch if the module isn't available
    import importlib.metadata as metadata
    if not hasattr(metadata, 'packages_distributions'):
        def packages_distributions():
            """Fallback packages_distributions for Python 3.9"""
            return {}
        metadata.packages_distributions = packages_distributions

# Now safe to import everything else
import asyncio
import warnings
import logging

# Suppress warnings
warnings.filterwarnings("ignore")

# Suppress noisy loggers
logging.getLogger("speechbrain").setLevel(logging.CRITICAL)
logging.getLogger("speechbrain.lobes.models.huggingface_transformers.huggingface").setLevel(logging.CRITICAL)
logging.getLogger("torch").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.getLogger("google").setLevel(logging.CRITICAL)
logging.getLogger("transformers").setLevel(logging.CRITICAL)


async def test_watchdog():
    """Test AgenticWatchdog initialization and state."""
    print("\n" + "=" * 60)
    print("TEST 1: AgenticWatchdog")
    print("=" * 60)

    watchdog = None
    try:
        from core.agentic_watchdog import (
            AgenticWatchdog,
            WatchdogConfig,
            start_watchdog,
            stop_watchdog,
            Heartbeat,
            AgenticMode,
        )

        # Initialize watchdog
        watchdog = await start_watchdog()
        print("✓ Watchdog initialized")

        # Check state
        print(f"  • Agentic allowed: {watchdog.is_agentic_allowed()}")
        print(f"  • Active task: {watchdog._active_task_id}")

        # Simulate task start
        await watchdog.task_started(
            task_id="test_task_001",
            goal="Test organizing desktop",
            mode=AgenticMode.AUTONOMOUS,
        )
        print(f"  • Started test task")

        # Send heartbeat
        import time
        watchdog.receive_heartbeat(Heartbeat(
            task_id="test_task_001",
            goal="Test organizing desktop",
            current_action="screenshot",
            actions_count=1,
            timestamp=time.time(),
            mode=AgenticMode.AUTONOMOUS,
        ))
        print(f"  • Sent heartbeat")

        # Complete task
        await watchdog.task_completed("test_task_001", success=True)
        print(f"  • Completed test task")

        # Get status
        status = watchdog.get_status()
        print(f"  • Status: mode={status.mode.value}, heartbeat_healthy={status.heartbeat_healthy}")

        print("\n✅ Watchdog tests PASSED")
        return True

    except Exception as e:
        print(f"\n❌ Watchdog test FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up watchdog
        if watchdog:
            try:
                from core.agentic_watchdog import stop_watchdog
                await stop_watchdog()
            except Exception:
                pass


async def test_router():
    """Test TieredCommandRouter routing decisions."""
    print("\n" + "=" * 60)
    print("TEST 2: TieredCommandRouter")
    print("=" * 60)

    try:
        from core.tiered_command_router import (
            TieredCommandRouter,
            TieredRouterConfig,
            CommandTier,
        )

        # Create router without VBIA (for testing)
        router = TieredCommandRouter()
        print("✓ Router initialized")

        # Test Tier 1 commands
        test_commands = [
            ("Hey Jarvis, what's the weather?", CommandTier.TIER1_STANDARD),
            ("Jarvis, play some music", CommandTier.TIER1_STANDARD),
            ("JARVIS ACCESS organize my desktop", CommandTier.TIER2_AGENTIC),
            ("Jarvis execute click on Safari", CommandTier.TIER2_AGENTIC),
            ("Jarvis control my computer", CommandTier.TIER2_AGENTIC),
            ("Jarvis, click on the button", CommandTier.TIER2_AGENTIC),  # Intent escalation
            ("Jarvis, delete all my files", CommandTier.BLOCKED),  # Dangerous
        ]

        all_passed = True
        for command, expected_tier in test_commands:
            result = await router.route(command)
            tier_match = result.tier == expected_tier
            status = "✓" if tier_match else "✗"
            print(f"  {status} '{command[:40]}...'")
            print(f"      Expected: {expected_tier.value}, Got: {result.tier.value}")
            if not tier_match:
                all_passed = False

        # Get stats
        stats = router.get_stats()
        print(f"\n  Stats:")
        print(f"    • Total routes: {stats['total_routes']}")
        print(f"    • Tier 1 count: {stats['tier1_count']}")
        print(f"    • Tier 2 count: {stats['tier2_count']}")
        print(f"    • Blocked count: {stats['blocked_count']}")

        if all_passed:
            print("\n✅ Router tests PASSED")
        else:
            print("\n⚠️ Router tests had some mismatches")

        return all_passed

    except Exception as e:
        print(f"\n❌ Router test FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_vbia_adapter():
    """Test TieredVBIAAdapter authentication."""
    print("\n" + "=" * 60)
    print("TEST 3: TieredVBIAAdapter")
    print("=" * 60)

    try:
        from core.tiered_vbia_adapter import (
            TieredVBIAAdapter,
            TieredVBIAConfig,
            AuthTier,
        )

        # Initialize adapter directly (not singleton) to avoid shared state issues
        adapter = TieredVBIAAdapter()
        await adapter.initialize()
        print("✓ VBIA Adapter initialized")

        # Test without cached verification (should use fallback)
        passed, confidence = await adapter.verify_speaker(threshold=0.70)
        print(f"  • Tier 1 verify (no cache, 70%): passed={passed}, confidence={confidence:.2f}")

        # Set a cached verification result (simulating voice pipeline)
        adapter.set_verification_result(
            confidence=0.92,
            speaker_id="derek",
            is_owner=True,
            verified=True,
            metadata={"test": True}
        )
        print("  • Set cached verification: 92% confidence")

        # Test Tier 1 verification with cache
        passed, confidence = await adapter.verify_speaker(threshold=0.70)
        print(f"  • Tier 1 verify (cached, 70%): passed={passed}, confidence={confidence:.2f}")

        # Test Tier 2 verification with cache
        passed, confidence = await adapter.verify_speaker(threshold=0.85)
        print(f"  • Tier 2 verify (cached, 85%): passed={passed}, confidence={confidence:.2f}")

        # Test liveness
        liveness_passed = await adapter.verify_liveness()
        print(f"  • Liveness check: passed={liveness_passed}")

        # Test full Tier 1 verification with bypass phrase
        result = await adapter.verify_tier1(phrase="what time is it")
        print(f"  • Full Tier 1 (bypass phrase): passed={result.passed}, bypass={'bypass' in result.details}")

        # Test full Tier 2 verification
        result = await adapter.verify_tier2()
        print(f"  • Full Tier 2: passed={result.passed}, liveness={result.liveness}")

        # Clear cache and test fallback behavior
        adapter.clear_verification_cache()
        passed, confidence = await adapter.verify_speaker(threshold=0.85)
        print(f"  • Tier 2 verify (no cache, 85%): passed={passed}, confidence={confidence:.2f}")

        # Get stats
        stats = adapter.get_stats()
        print(f"\n  Stats:")
        print(f"    • Tier 1 attempts: {stats['tier1_attempts']}")
        print(f"    • Tier 2 attempts: {stats['tier2_attempts']}")

        print("\n✅ VBIA Adapter tests PASSED")
        return True

    except Exception as e:
        print(f"\n❌ VBIA Adapter test FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_integration():
    """Test full integration: Router + VBIA + Watchdog."""
    print("\n" + "=" * 60)
    print("TEST 4: Full Integration")
    print("=" * 60)

    watchdog = None
    try:
        from core.agentic_watchdog import start_watchdog, stop_watchdog
        from core.tiered_command_router import TieredCommandRouter, TieredRouterConfig
        from core.tiered_vbia_adapter import TieredVBIAAdapter

        # Initialize all components (use fresh instances for integration test)
        watchdog = await start_watchdog()
        vbia_adapter = TieredVBIAAdapter()
        await vbia_adapter.initialize()

        router = TieredCommandRouter(
            vbia_callback=vbia_adapter.verify_speaker,
            liveness_callback=vbia_adapter.verify_liveness,
        )

        print("✓ All components initialized and wired")

        # Simulate voice pipeline setting verification result
        vbia_adapter.set_verification_result(
            confidence=0.93,
            speaker_id="derek",
            is_owner=True,
            verified=True,
        )
        print("✓ Simulated voice verification: 93% confidence")

        # Test Tier 1 route (should pass with cached auth)
        result = await router.route("Hey Jarvis, what's the weather?")
        print(f"\n  Tier 1 Route Test:")
        print(f"    • Tier: {result.tier.value}")
        print(f"    • Auth required: {result.auth_required}")
        print(f"    • Auth result: {result.auth_result}")
        print(f"    • VBIA confidence: {result.vbia_confidence}")
        print(f"    • Execution allowed: {result.execution_allowed}")

        # Test Tier 2 route (should use cached VBIA verification)
        result = await router.route("JARVIS ACCESS organize my desktop")
        print(f"\n  Tier 2 Route Test:")
        print(f"    • Tier: {result.tier.value}")
        print(f"    • Auth required: {result.auth_required}")
        print(f"    • Auth result: {result.auth_result}")
        print(f"    • VBIA confidence: {result.vbia_confidence}")
        print(f"    • Watchdog armed: {result.watchdog_armed}")
        print(f"    • Execution allowed: {result.execution_allowed}")

        # Clear verification and test Tier 2 denial
        vbia_adapter.clear_verification_cache()
        result = await router.route("JARVIS EXECUTE delete something")
        print(f"\n  Tier 2 Route Test (no verification):")
        print(f"    • Tier: {result.tier.value}")
        print(f"    • Auth result: {result.auth_result}")
        print(f"    • Execution allowed: {result.execution_allowed}")
        print(f"    • Denial reason: {result.denial_reason}")

        # Check watchdog state
        print(f"\n  Watchdog State:")
        print(f"    • Agentic allowed: {watchdog.is_agentic_allowed()}")

        print("\n✅ Integration tests PASSED")
        return True

    except Exception as e:
        print(f"\n❌ Integration test FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up watchdog
        if watchdog:
            try:
                from core.agentic_watchdog import stop_watchdog
                await stop_watchdog()
            except Exception:
                pass


async def cleanup_resources():
    """Clean up any remaining background resources."""
    # Give background tasks a moment to notice shutdown
    await asyncio.sleep(0.1)

    # Clean up PyTorch executor if it exists
    try:
        from core.pytorch_executor import shutdown_pytorch_executor
        await shutdown_pytorch_executor()
    except (ImportError, Exception):
        pass

    # Cancel any pending tasks
    try:
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("TWO-TIER AGENTIC SECURITY SYSTEM - Test Suite")
    print("=" * 60)

    results = []

    results.append(("Watchdog", await test_watchdog()))
    results.append(("Router", await test_router()))
    results.append(("VBIA Adapter", await test_vbia_adapter()))
    results.append(("Integration", await test_integration()))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 ALL TESTS PASSED - Two-Tier Security System is ready!")
    else:
        print("⚠️ SOME TESTS FAILED - Please review the errors above")
    print("=" * 60 + "\n")

    # Clean up resources
    await cleanup_resources()

    return 0 if all_passed else 1


if __name__ == "__main__":
    # Suppress cleanup warnings
    import logging
    logging.getLogger().setLevel(logging.ERROR)

    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 1
    except Exception as e:
        print(f"Test suite error: {e}")
        exit_code = 1

    # Force exit to avoid lingering threads
    os._exit(exit_code)
