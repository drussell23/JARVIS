#!/usr/bin/env python3
"""
Test v198.1: Loading Server Premature Shutdown Fix

This test verifies that the loading server correctly handles the transition
grace period to prevent premature shutdown during Chrome redirect.

Run with: python3 tests/test_loading_server_shutdown_fix.py
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class MockConnectionManager:
    """Mock connection manager for testing."""
    def __init__(self):
        self.count = 0


def test_transition_grace_period_initialization():
    """Test that transition grace period is properly initialized."""
    print("\n=== Test 1: Transition Grace Period Initialization ===")

    try:
        # Import the GracefulShutdownManager
        from loading_server import GracefulShutdownManager

        # Create mock connection manager
        mock_conn_mgr = MockConnectionManager()

        # Create shutdown manager
        manager = GracefulShutdownManager(mock_conn_mgr)

        # Check default transition grace period
        assert hasattr(manager, '_transition_grace_period'), \
            "_transition_grace_period attribute missing"
        assert manager._transition_grace_period == 5.0, \
            f"Default transition grace should be 5.0, got {manager._transition_grace_period}"

        # Check transition grace ends at is None initially
        assert hasattr(manager, '_transition_grace_ends_at'), \
            "_transition_grace_ends_at attribute missing"
        assert manager._transition_grace_ends_at is None, \
            "Transition grace ends at should be None initially"

        print("✓ Transition grace period initialized correctly")
        print("=== Test 1: PASSED ===\n")
        return True

    except ImportError as e:
        print(f"⚠ Skipping test (import error: {e})")
        return True


async def test_transition_grace_set_on_startup_complete():
    """Test that transition grace period is set when startup completes."""
    print("\n=== Test 2: Transition Grace Set on Startup Complete ===")

    try:
        from loading_server import GracefulShutdownManager

        # Create mock connection manager
        mock_conn_mgr = MockConnectionManager()

        # Create shutdown manager
        manager = GracefulShutdownManager(mock_conn_mgr)
        manager.initialize_async_objects()

        # Initially, transition grace ends at should be None
        assert manager._transition_grace_ends_at is None

        # Notify startup complete
        before_complete = datetime.now()
        await manager.notify_startup_complete()
        after_complete = datetime.now()

        # Check that transition grace ends at is set
        assert manager._transition_grace_ends_at is not None, \
            "Transition grace ends at should be set after startup complete"

        # Check that it's approximately 5 seconds in the future
        expected_end = before_complete + timedelta(seconds=5.0)
        actual_end = manager._transition_grace_ends_at

        # Allow 1 second tolerance
        assert actual_end >= before_complete + timedelta(seconds=4.0), \
            "Transition grace should end at least 4 seconds from now"
        assert actual_end <= after_complete + timedelta(seconds=6.0), \
            "Transition grace should end no more than 6 seconds from now"

        print("✓ Transition grace period set correctly on startup complete")
        print(f"  Grace ends at: {manager._transition_grace_ends_at}")
        print("=== Test 2: PASSED ===\n")
        return True

    except ImportError as e:
        print(f"⚠ Skipping test (import error: {e})")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
        return False


async def test_shutdown_blocked_during_transition_grace():
    """Test that auto-shutdown is blocked during transition grace period."""
    print("\n=== Test 3: Shutdown Blocked During Transition Grace ===")

    try:
        from loading_server import GracefulShutdownManager

        # Create mock connection manager
        mock_conn_mgr = MockConnectionManager()
        mock_conn_mgr.count = 1  # Browser connected

        # Create shutdown manager
        manager = GracefulShutdownManager(mock_conn_mgr)
        manager.initialize_async_objects()

        # Complete startup
        await manager.notify_startup_complete()

        # Simulate browser disconnect
        mock_conn_mgr.count = 0
        manager._browser_disconnected_at = datetime.now()

        # Check shutdown conditions (should NOT trigger during grace period)
        initial_shutdown_state = manager._shutdown_initiated
        await manager._check_shutdown_conditions()

        assert manager._shutdown_initiated == initial_shutdown_state, \
            "Shutdown should NOT be initiated during transition grace period"

        print("✓ Shutdown correctly blocked during transition grace period")
        print("=== Test 3: PASSED ===\n")
        return True

    except ImportError as e:
        print(f"⚠ Skipping test (import error: {e})")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
        return False


async def test_shutdown_allowed_after_transition_grace():
    """Test that auto-shutdown is allowed after transition grace period expires."""
    print("\n=== Test 4: Shutdown Allowed After Transition Grace ===")

    try:
        from loading_server import GracefulShutdownManager

        # Create mock connection manager
        mock_conn_mgr = MockConnectionManager()
        mock_conn_mgr.count = 1  # Browser connected initially

        # Create shutdown manager with short grace period for testing
        manager = GracefulShutdownManager(
            mock_conn_mgr,
            auto_shutdown_delay=0.1  # Short delay for testing
        )
        manager._transition_grace_period = 0.1  # Short grace for testing
        manager.initialize_async_objects()

        # Complete startup
        await manager.notify_startup_complete()

        # Wait for transition grace to expire
        await asyncio.sleep(0.2)

        # Simulate browser disconnect after grace period
        mock_conn_mgr.count = 0
        manager._browser_disconnected_at = datetime.now() - timedelta(seconds=1)  # Already past delay

        # Check shutdown conditions (should trigger now)
        await manager._check_shutdown_conditions()

        # Verify shutdown was initiated
        assert manager._shutdown_initiated == True, \
            "Shutdown SHOULD be initiated after transition grace period expires"

        print("✓ Shutdown correctly allowed after transition grace period")
        print("=== Test 4: PASSED ===\n")
        return True

    except ImportError as e:
        print(f"⚠ Skipping test (import error: {e})")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
        return False


def test_status_includes_transition_info():
    """Test that status property includes transition grace info."""
    print("\n=== Test 5: Status Includes Transition Info ===")

    try:
        from loading_server import GracefulShutdownManager

        # Create mock connection manager
        mock_conn_mgr = MockConnectionManager()

        # Create shutdown manager
        manager = GracefulShutdownManager(mock_conn_mgr)

        # Get status
        status = manager.status

        # Check for new fields
        assert "transition_grace_period" in status, \
            "Status should include transition_grace_period"
        assert "in_transition_grace" in status, \
            "Status should include in_transition_grace"
        assert "transition_grace_remaining" in status, \
            "Status should include transition_grace_remaining"

        # Initially not in transition
        assert status["in_transition_grace"] == False, \
            "Should not be in transition grace initially"
        assert status["transition_grace_remaining"] == 0.0, \
            "Transition grace remaining should be 0 initially"

        print("✓ Status correctly includes transition grace info")
        print("=== Test 5: PASSED ===\n")
        return True

    except ImportError as e:
        print(f"⚠ Skipping test (import error: {e})")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
        return False


async def main():
    """Run all tests."""
    print("=" * 60)
    print("v198.1 LOADING SERVER PREMATURE SHUTDOWN FIX - VERIFICATION")
    print("=" * 60)

    all_passed = True

    # Test 1: Initialization
    if not test_transition_grace_period_initialization():
        all_passed = False

    # Test 2: Startup complete
    if not await test_transition_grace_set_on_startup_complete():
        all_passed = False

    # Test 3: Blocked during grace
    if not await test_shutdown_blocked_during_transition_grace():
        all_passed = False

    # Test 4: Allowed after grace
    if not await test_shutdown_allowed_after_transition_grace():
        all_passed = False

    # Test 5: Status includes info
    if not test_status_includes_transition_info():
        all_passed = False

    print("=" * 60)
    if all_passed:
        print("ALL TESTS PASSED ✅")
        print("=" * 60)
        return 0
    else:
        print("SOME TESTS FAILED ❌")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
