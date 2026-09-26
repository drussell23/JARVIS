# [Ouroboros] Modified by Ouroboros (op=op-01a0de84-) at 2026-09-26 16:21 UTC
# Reason: `backend/autonomy/action_queue.py` has no corresponding test module. CREATE `tests/test_action_queue.py` containing focu

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.autonomy.action_queue import (
    ActionQueueManager,
    AutonomousAction,
    ActionPriority,
    ActionCategory,
    ExecutionResult,
    ExecutionStatus,
    QueueStatus,
    QueuedAction
)

async def test_import_smoke():
    """Test that the action queue module can be imported without errors."""
    print("Testing import smoke...")
    try:
        # This should not raise any import errors
        from backend.autonomy.action_queue import (
            ActionQueueManager,
            AutonomousAction,
            ActionPriority,
            ActionCategory,
            ExecutionResult,
            ExecutionStatus,
            QueueStatus,
            QueuedAction,
            test_action_queue
        )
        print("✅ Import smoke test passed")
        return True
    except Exception as e:
        print(f"❌ Import smoke test failed: {e}")
        return False


async def test_queue_manager_initialization():
    """Test that ActionQueueManager initializes correctly."""
    print("Testing queue manager initialization...")
    
    try:
        # Test default initialization
        queue = ActionQueueManager()
        assert queue is not None
        assert queue.action_queue == []
        assert queue.status == QueueStatus.IDLE
        assert queue.active_executions == 0
        print("✅ Queue manager initialization test passed")
        return True
    except Exception as e:
        print(f"❌ Queue manager initialization test failed: {e}")
        return False


async def test_add_action_basic():
    """Test basic action addition to the queue."""
    print("Testing add action basic...")
    
    try:
        queue = ActionQueueManager()
        
        # Create a simple action
        action = AutonomousAction(
            action_type="test",
            priority=ActionPriority.HIGH,
            confidence=0.8,
            category=ActionCategory.MAINTENANCE,
            data={"message": "test action"}
        )
        
        # Add action to queue
        result = await queue.add_action(action)
        
        assert result is True
        assert len(queue.action_queue) == 1
        
        queued_action = queue.action_queue[0]
        assert queued_action.action == action
        assert queued_action.priority_score is not None
        print("✅ Add action basic test passed")
        return True
    except Exception as e:
        print(f"❌ Add action basic test failed: {e}")
        return False


async def test_add_action_full_queue():
    """Test that adding to full queue returns False."""
    print("Testing add action full queue...")
    
    try:
        # Create queue with small capacity
        queue = ActionQueueManager()
        queue.max_queue_size = 1
        
        # Add two actions - first should succeed, second should fail
        action1 = AutonomousAction(
            action_type="test1",
            priority=ActionPriority.HIGH,
            confidence=0.8,
            category=ActionCategory.MAINTENANCE,
            data={"message": "test action 1"}
        )
        
        action2 = AutonomousAction(
            action_type="test2",
            priority=ActionPriority.HIGH,
            confidence=0.8,
            category=ActionCategory.MAINTENANCE,
            data={"message": "test action 2"}
        )
        
        # Add first action
        result1 = await queue.add_action(action1)
        assert result1 is True
        
        # Add second action - should fail due to full queue
        result2 = await queue.add_action(action2)
        assert result2 is False
        assert len(queue.action_queue) == 1
        
        print("✅ Add action full queue test passed")
        return True
    except Exception as e:
        print(f"❌ Add action full queue test failed: {e}")
        return False


async def test_priority_calculation():
    """Test that priority scores are calculated correctly."""
    print("Testing priority calculation...")
    
    try:
        queue = ActionQueueManager()
        
        # Create actions with different priorities and confidence
        action1 = AutonomousAction(
            action_type="test",
            priority=ActionPriority.LOW,
            confidence=0.9,
            category=ActionCategory.SECURITY,
            data={"message": "high priority test"}
        )
        
        action2 = AutonomousAction(
            action_type="test",
            priority=ActionPriority.HIGH,
            confidence=0.5,
            category=ActionCategory.COMMUNICATION,
            data={"message": "low priority test"}
        )
        
        # Add actions to queue
        await queue.add_action(action1)
        await queue.add_action(action2)
        
        # Check that actions are ordered by priority score (lower is higher)
        assert len(queue.action_queue) == 2
        
        # The first action should have lower priority score (higher priority)
        queued_action1 = queue.action_queue[0]
        queued_action2 = queue.action_queue[1]
        
        print(f"First action priority score: {queued_action1.priority_score}")
        print(f"Second action priority score: {queued_action2.priority_score}")
        
        # Should be ordered by priority score (lower = higher priority)
        assert queued_action1.priority_score <= queued_action2.priority_score
        
        print("✅ Priority calculation test passed")
        return True
    except Exception as e:
        print(f"❌ Priority calculation test failed: {e}")
        return False


async def test_queue_state_getter():
    """Test that queue state getter works correctly."""
    print("Testing queue state getter...")
    
    try:
        queue = ActionQueueManager()
        
        # Add an action to the queue
        action = AutonomousAction(
            action_type="test",
            priority=ActionPriority.HIGH,
            confidence=0.8,
            category=ActionCategory.MAINTENANCE,
            data={"message": "test action"}
        )
        
        await queue.add_action(action)
        
        # Get state
        state = queue.get_queue_state()
        
        assert isinstance(state, dict)
        assert 'status' in state
        assert 'queue_length' in state
        assert 'active_executions' in state
        assert 'stats' in state
        
        print("✅ Queue state getter test passed")
        return True
    except Exception as e:
        print(f"❌ Queue state getter test failed: {e}")
        return False


async def test_pause_resume():
    """Test queue pause and resume functionality."""
    print("Testing queue pause/resume...")
    
    try:
        queue = ActionQueueManager()
        
        # Test initial state
        assert queue.status == QueueStatus.IDLE
        
        # Pause the queue
        queue.pause()
        assert queue.status == QueueStatus.PAUSED
        
        # Resume the queue
        queue.resume()
        assert queue.status == QueueStatus.IDLE
        
        print("✅ Queue pause/resume test passed")
        return True
    except Exception as e:
        print(f"❌ Queue pause/resume test failed: {e}")
        return False


async def test_clear_queue():
    """Test that queue clearing works correctly."""
    print("Testing queue clear...")
    
    try:
        queue = ActionQueueManager()
        
        # Add some actions
        action1 = AutonomousAction(
            action_type="test1",
            priority=ActionPriority.HIGH,
            confidence=0.8,
            category=ActionCategory.MAINTENANCE,
            data={"message": "test action 1"}
        )
        
        action2 = AutonomousAction(
            action_type="test2",
            priority=ActionPriority.HIGH,
            confidence=0.8,
            category=ActionCategory.MAINTENANCE,
            data={"message": "test action 2"}
        )
        
        await queue.add_action(action1)
        await queue.add_action(action2)
        
        assert len(queue.action_queue) == 2
        
        # Clear the queue
        queue.clear_queue()
        
        assert len(queue.action_queue) == 0
        
        print("✅ Queue clear test passed")
        return True
    except Exception as e:
        print(f"❌ Queue clear test failed: {e}")
        return False


async def run_all_tests():
    """Run all tests"""
    print("\n" + "="*80)
    print(" Action Queue - Comprehensive Test Suite")
    print("="*80)

    tests = [
        ("Import Smoke Test", test_import_smoke),
        ("Queue Manager Initialization", test_queue_manager_initialization),
        ("Add Action Basic", test_add_action_basic),
        ("Add Action Full Queue", test_add_action_full_queue),
        ("Priority Calculation", test_priority_calculation),
        ("Queue State Getter", test_queue_state_getter),
        ("Pause/Resume", test_pause_resume),
        ("Clear Queue", test_clear_queue),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = await test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Test '{name}' FAILED with exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # Summary
    print("\n" + "="*80)
    print(" TEST SUMMARY")
    print("="*80 + "\n")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED!\n")
    else:
        print(f"\n⚠️  {total - passed} test(s) failed\n")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)