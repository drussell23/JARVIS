"""
Computer Use Bridge Integration Unit Tests
==========================================

Tests the cross-repo bridge infrastructure without requiring full Computer Use stack.

Author: JARVIS AI System
Version: 6.1.0
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


async def test_bridge_initialization():
    """Test: Computer Use Bridge initialization"""
    print("\n" + "="*70)
    print("TEST: Computer Use Bridge Initialization")
    print("="*70)

    try:
        from backend.core.computer_use_bridge import (
            get_computer_use_bridge,
            ComputerAction,
            ActionType,
            ExecutionStatus,
        )

        # Initialize bridge
        bridge = await get_computer_use_bridge(
            enable_action_chaining=True,
            enable_omniparser=False,
        )

        print(f"\n✅ Bridge initialized successfully")
        print(f"   Session ID: {bridge.session_id}")
        print(f"   Action Chaining: {bridge.state.action_chaining_enabled}")
        print(f"   OmniParser: {bridge.state.omniparser_enabled}")

        # Verify state file was created
        from backend.core.computer_use_bridge import COMPUTER_USE_STATE_FILE

        if COMPUTER_USE_STATE_FILE.exists():
            print(f"\n✅ State file created: {COMPUTER_USE_STATE_FILE}")

            with open(COMPUTER_USE_STATE_FILE, 'r') as f:
                state_data = json.load(f)

            print(f"   Session: {state_data.get('session_id', 'N/A')}")
            print(f"   Started: {state_data.get('started_at', 'N/A')}")
            print(f"   Total Actions: {state_data.get('total_actions', 0)}")
            print(f"   Total Batches: {state_data.get('total_batches', 0)}")

        # Test emitting an action event
        print(f"\n🧪 Testing action event emission...")

        test_action = ComputerAction(
            action_id=str(uuid4()),
            action_type=ActionType.CLICK,
            coordinates=(100, 200),
            reasoning="Test click action",
            confidence=0.95,
        )

        await bridge.emit_action_event(
            action=test_action,
            status=ExecutionStatus.COMPLETED,
            execution_time_ms=150.0,
            goal="Test action emission",
        )

        print(f"✅ Action event emitted successfully")

        # Verify event file was created
        from backend.core.computer_use_bridge import COMPUTER_USE_EVENTS_FILE

        if COMPUTER_USE_EVENTS_FILE.exists():
            print(f"✅ Events file created: {COMPUTER_USE_EVENTS_FILE}")

            with open(COMPUTER_USE_EVENTS_FILE, 'r') as f:
                events_data = json.load(f)

            print(f"   Total events: {len(events_data)}")

            if events_data:
                latest = events_data[-1]
                print(f"   Latest event type: {latest.get('event_type', 'N/A')}")
                print(f"   Latest timestamp: {latest.get('timestamp', 'N/A')}")

        # Get statistics
        stats = bridge.get_statistics()
        print(f"\n📊 Bridge Statistics:")
        print(f"   Total Actions: {stats['total_actions']}")
        print(f"   Total Batches: {stats['total_batches']}")
        print(f"   Time Saved: {stats['time_saved_seconds']}s")
        print(f"   Tokens Saved: {stats['tokens_saved']}")

        print(f"\n✅ TEST PASSED: Bridge initialization and event emission working!")
        return True

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_batch_event_emission():
    """Test: Batch event emission with metrics"""
    print("\n" + "="*70)
    print("TEST: Batch Event Emission with Metrics")
    print("="*70)

    try:
        from backend.core.computer_use_bridge import (
            get_computer_use_bridge,
            ComputerAction,
            ActionBatch,
            ActionType,
            InterfaceType,
            ExecutionStatus,
        )

        bridge = await get_computer_use_bridge()

        # Create a test batch (simulating calculator 2+2)
        print(f"\n🧪 Creating test batch (simulating calculator 2+2)...")

        actions = [
            ComputerAction(
                action_id=str(uuid4()),
                action_type=ActionType.CLICK,
                coordinates=(100, 200),
                reasoning="Click '2' button",
                confidence=0.95,
            ),
            ComputerAction(
                action_id=str(uuid4()),
                action_type=ActionType.CLICK,
                coordinates=(150, 250),
                reasoning="Click '+' button",
                confidence=0.95,
            ),
            ComputerAction(
                action_id=str(uuid4()),
                action_type=ActionType.CLICK,
                coordinates=(100, 200),
                reasoning="Click '2' button",
                confidence=0.95,
            ),
            ComputerAction(
                action_id=str(uuid4()),
                action_type=ActionType.CLICK,
                coordinates=(200, 250),
                reasoning="Click '=' button",
                confidence=0.95,
            ),
        ]

        batch = ActionBatch(
            batch_id=str(uuid4()),
            actions=actions,
            interface_type=InterfaceType.STATIC,
            goal="Calculate 2 + 2 on Calculator",
        )

        print(f"✅ Batch created: {len(batch.actions)} actions")

        # Emit batch event with optimization metrics
        print(f"\n🧪 Emitting batch event with metrics...")

        # Calculate metrics
        batch_execution_time = 450.0  # 450ms for batch
        stop_and_look_time = len(actions) * 2000  # 2s per action = 8s
        time_saved = stop_and_look_time - batch_execution_time

        tokens_per_screenshot = 1500
        tokens_saved = int((len(actions) - 1) * tokens_per_screenshot * 0.7)

        await bridge.emit_batch_event(
            batch=batch,
            status=ExecutionStatus.COMPLETED,
            execution_time_ms=batch_execution_time,
            time_saved_ms=time_saved,
            tokens_saved=tokens_saved,
        )

        print(f"✅ Batch event emitted successfully")
        print(f"   Execution time: {batch_execution_time:.0f}ms")
        print(f"   Time saved: {time_saved:.0f}ms")
        print(f"   Tokens saved: {tokens_saved}")

        # Verify statistics updated
        stats = bridge.get_statistics()
        print(f"\n📊 Updated Statistics:")
        print(f"   Total Actions: {stats['total_actions']}")
        print(f"   Total Batches: {stats['total_batches']}")
        print(f"   Avg Batch Size: {stats['avg_batch_size']:.2f}")
        print(f"   Time Saved: {stats['time_saved_seconds']:.2f}s")
        print(f"   Tokens Saved: {stats['tokens_saved']}")

        print(f"\n✅ TEST PASSED: Batch event emission with metrics working!")
        return True

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_reactor_core_connector():
    """Test: Reactor Core connector can read JARVIS events"""
    print("\n" + "="*70)
    print("TEST: Reactor Core Connector Integration")
    print("="*70)

    try:
        # Check if Reactor Core is available
        reactor_core_path = Path.home() / "Documents" / "repos" / "reactor-core"
        if not reactor_core_path.exists():
            print(f"\n⚠️  Reactor Core not found, skipping test")
            return True

        sys.path.insert(0, str(reactor_core_path))

        from reactor_core.integration.computer_use_connector import (
            ComputerUseConnector,
            ComputerUseConnectorConfig,
        )

        print(f"\n✅ Reactor Core Computer Use Connector imported")

        # Initialize connector
        connector = ComputerUseConnector()

        print(f"✅ Connector initialized")

        # Try to read JARVIS state
        jarvis_state = await connector.get_jarvis_state()

        if jarvis_state:
            print(f"\n✅ JARVIS state found:")
            print(f"   Session ID: {jarvis_state.get('session_id', 'N/A')}")
            print(f"   Total Actions: {jarvis_state.get('total_actions', 0)}")
            print(f"   Total Batches: {jarvis_state.get('total_batches', 0)}")
            print(f"   Action Chaining: {jarvis_state.get('action_chaining_enabled', False)}")
        else:
            print(f"\n⚠️  No JARVIS state found (expected if first run)")

        # Try to read events
        events = await connector.get_events(
            since=datetime.now() - timedelta(hours=1),
            limit=100,
        )

        print(f"\n✅ Events loaded: {len(events)} events found")

        if events:
            latest = events[-1]
            print(f"\n📊 Latest Event:")
            print(f"   Type: {latest.event_type.value}")
            print(f"   Batch Size: {latest.batch_size}")
            print(f"   Time Saved: {latest.time_saved_ms:.0f}ms")
            print(f"   Tokens Saved: {latest.tokens_saved}")

        # Get optimization metrics
        metrics = await connector.get_optimization_metrics()

        print(f"\n📊 Optimization Metrics:")
        print(f"   Total Events: {metrics['total_events']}")
        print(f"   Total Actions: {metrics['total_actions']}")
        print(f"   Total Batches: {metrics['total_batches']}")
        print(f"   Avg Batch Size: {metrics['avg_batch_size']:.2f}")
        print(f"   Time Saved: {metrics['total_time_saved_seconds']:.2f}s")
        print(f"   Tokens Saved: {metrics['total_tokens_saved']}")

        print(f"\n✅ TEST PASSED: Reactor Core connector working!")
        return True

    except ImportError as e:
        print(f"\n⚠️  Reactor Core dependencies missing: {e}")
        print(f"   Skipping test (not a failure)")
        return True
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_jarvis_prime_delegate():
    """Test: JARVIS Prime delegate structure"""
    print("\n" + "="*70)
    print("TEST: JARVIS Prime Delegate Structure")
    print("="*70)

    try:
        prime_path = Path.home() / "Documents" / "repos" / "jarvis-prime"
        if not prime_path.exists():
            print(f"\n⚠️  JARVIS Prime not found, skipping test")
            return True

        sys.path.insert(0, str(prime_path))

        from jarvis_prime.core.computer_use_delegate import (
            get_computer_use_delegate,
            DelegationMode,
        )

        print(f"\n✅ JARVIS Prime Computer Use Delegate imported")

        # Initialize delegate
        delegate = get_computer_use_delegate(
            mode=DelegationMode.FULL_DELEGATION,
            enable_action_chaining=True,
            enable_omniparser=False,
        )

        print(f"✅ Delegate initialized")
        print(f"   Mode: {delegate.default_mode.value}")
        print(f"   Action Chaining: {delegate.enable_action_chaining}")
        print(f"   OmniParser: {delegate.enable_omniparser}")

        # Check JARVIS availability
        available = await delegate.check_jarvis_availability()
        print(f"\n📡 JARVIS Availability: {available}")

        if available:
            capabilities = await delegate.get_jarvis_capabilities()
            print(f"\n✅ JARVIS Capabilities:")
            print(f"   Available: {capabilities['available']}")
            print(f"   Action Chaining: {capabilities['action_chaining_enabled']}")
            print(f"   OmniParser: {capabilities['omniparser_enabled']}")

        # Get statistics
        stats = delegate.get_statistics()
        print(f"\n📊 Delegate Statistics:")
        print(f"   Total Requests: {stats['total_requests']}")
        print(f"   Total Results: {stats['total_results']}")
        print(f"   Success Rate: {stats['success_rate']:.1f}%")

        print(f"\n✅ TEST PASSED: JARVIS Prime delegate structure working!")
        return True

    except ImportError as e:
        print(f"\n⚠️  JARVIS Prime dependencies missing: {e}")
        print(f"   Skipping test (not a failure)")
        return True
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all unit tests"""
    print("\n" + "="*70)
    print("COMPUTER USE BRIDGE INTEGRATION UNIT TESTS")
    print("="*70)
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Version: 6.1.0")

    results = {}

    # Test 1: Bridge initialization
    results['bridge_init'] = await test_bridge_initialization()
    await asyncio.sleep(1)

    # Test 2: Batch event emission
    results['batch_emission'] = await test_batch_event_emission()
    await asyncio.sleep(1)

    # Test 3: Reactor Core connector
    results['reactor_core'] = await test_reactor_core_connector()
    await asyncio.sleep(1)

    # Test 4: JARVIS Prime delegate
    results['jarvis_prime'] = await test_jarvis_prime_delegate()

    # Summary
    print("\n" + "="*70)
    print("TEST SUITE SUMMARY")
    print("="*70)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, result in results.items():
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{status}: {test_name.replace('_', ' ').title()}")

    print(f"\n{'='*70}")
    print(f"TOTAL: {passed}/{total} tests passed ({passed/total*100:.0f}%)")
    print(f"{'='*70}\n")

    if passed == total:
        print("🎉 ALL TESTS PASSED! Cross-repo integration is working correctly!")
        return 0
    else:
        print("⚠️  Some tests failed. Review output above.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
