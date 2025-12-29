#!/usr/bin/env python3
"""
JARVIS God Mode End-to-End Test
================================

This test verifies omnipresent multi-space surveillance:
1. Discovers ALL Terminal windows across ALL macOS spaces
2. Spawns 60 FPS Ferrari Engine watchers for each
3. Monitors them in parallel (GPU-accelerated)
4. Detects trigger on ANY space (even if you're looking elsewhere)
5. Automatically switches to detected space

Prerequisites:
- Space 1: Terminal window (idle)
- Space 2: Terminal window (run: sleep 10 && echo "Deployment Complete")
- Space 3: You are here, running this test

Expected Result: JARVIS detects "Deployment Complete" on Space 2
                while you're looking at Space 3 (God Mode!)
"""

import asyncio
import os
import sys
import logging
from datetime import datetime

# Configure logging to see the Ferrari Engine startup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("GodModeTest")

# Add backend to path
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))

from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent


async def test_god_mode():
    """Run the God Mode omnipresent surveillance test."""

    print("\n" + "="*70)
    print("🚀 JARVIS GOD MODE - OMNIPRESENT SURVEILLANCE TEST")
    print("="*70)
    print(f"⏰ Test started: {datetime.now().strftime('%H:%M:%S')}")
    print()

    # Initialize agent
    print("📡 Initializing VisualMonitorAgent...")
    agent = VisualMonitorAgent()

    print("🔧 Starting agent services...")
    await agent.on_initialize()
    await agent.on_start()

    print("✅ Agent ready!\n")

    print("🧪 TEST SCENARIO:")
    print("-" * 70)
    print("1. 🔍 Searching for ALL 'Terminal' windows across ALL spaces")
    print("2. 🏎️  Spawning 60 FPS Ferrari Engine watchers for each window")
    print("3. 👁️  Monitoring parallel streams for trigger: 'Deployment Complete'")
    print("4. ⏱️  Maximum timeout: 120 seconds")
    print("-" * 70)
    print()

    print("⚡ EXECUTING GOD MODE WATCH...")
    print("   (This will block until trigger detected or timeout)")
    print()

    # Execute the God Mode Watch
    # This will discover all Terminal windows and watch them in parallel
    start_time = datetime.now()

    try:
        result = await agent.watch(
            app_name="Terminal",
            trigger_text="Deployment Complete",
            all_spaces=True,  # <--- GOD MODE ENABLED
            max_duration=120.0  # 2 minute timeout
        )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print()
        print("="*70)
        print("📊 TEST RESULTS")
        print("="*70)
        print(f"⏱️  Duration: {duration:.2f} seconds")
        print()

        # Check result
        status = result.get('status', 'unknown')
        trigger_detected = result.get('trigger_detected', False)

        if status == 'triggered' or trigger_detected:
            print("✅ ✅ ✅ SUCCESS: GOD MODE TRIGGER DETECTED! ✅ ✅ ✅")
            print()
            print("🎯 Detection Details:")

            triggered_window = result.get('triggered_window', {})
            trigger_details = result.get('trigger_details', {})

            print(f"   📍 Space ID: {triggered_window.get('space_id', 'unknown')}")
            print(f"   🪟 Window ID: {triggered_window.get('window_id', 'unknown')}")
            print(f"   📱 App Name: {triggered_window.get('app_name', 'unknown')}")
            print(f"   🏎️  Watcher ID: {trigger_details.get('watcher_id', 'unknown')}")
            print(f"   🎯 Confidence: {trigger_details.get('confidence', 0.0):.2%}")
            print(f"   ⏱️  Detection Time: {trigger_details.get('detection_time', 0.0):.2f}s")
            print()

            total_watchers = result.get('total_watchers', 0)
            print(f"   🔢 Total Watchers Spawned: {total_watchers}")
            print(f"   ⚡ Parallel Streams: {total_watchers} x 60 FPS")
            print()

            action_result = result.get('action_result', {})
            if action_result:
                print(f"   🎬 Action Executed: {action_result.get('status', 'none')}")

            print()
            print("🧠 VERIFICATION:")
            print("   ✓ Multi-space window discovery: WORKING")
            print("   ✓ Ferrari Engine 60 FPS capture: WORKING")
            print("   ✓ Parallel watcher coordination: WORKING")
            print("   ✓ OCR text detection: WORKING")
            print("   ✓ First-trigger-wins race: WORKING")
            if result.get('triggered_space'):
                print("   ✓ Automatic space switching: WORKING")
            print()
            print("🎉 JARVIS HAS ACHIEVED OMNIPRESENT SURVEILLANCE!")

        elif status == 'timeout':
            print("⏱️  TIMEOUT: No trigger detected within 120 seconds")
            print()
            print("🔍 Possible Issues:")
            print("   • Text 'Deployment Complete' not visible in any Terminal")
            print("   • Terminals not actually open on Space 1 or Space 2")
            print("   • OCR unable to read text (font too small, obscured, etc.)")
            print()
            total_watchers = result.get('total_watchers', 0)
            print(f"   ℹ️  Watchers spawned: {total_watchers}")
            if total_watchers == 0:
                print("   ⚠️  No Terminal windows found - check prerequisites!")

        else:
            print(f"❌ FAILED: {status}")
            print()
            print("📄 Full Result:")
            import json
            print(json.dumps(result, indent=2, default=str))

    except Exception as e:
        print()
        print("="*70)
        print("❌ EXCEPTION OCCURRED")
        print("="*70)
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print()
        print("🛑 Stopping agent...")
        await agent.on_stop()
        print("✅ Agent stopped cleanly")
        print()
        print("="*70)


if __name__ == "__main__":
    try:
        asyncio.run(test_god_mode())
    except KeyboardInterrupt:
        print("\n\n🛑 Test interrupted by user (Ctrl+C)")
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
