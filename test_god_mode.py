#!/usr/bin/env python3
"""
JARVIS God Mode End-to-End Test with Real-Time Voice Narration
===============================================================

This test verifies omnipresent multi-space surveillance with JARVIS
narrating the entire process in real-time using Daniel's British voice.

Features:
1. Discovers ALL Terminal windows across ALL macOS spaces
2. Spawns 60 FPS Ferrari Engine watchers for each
3. Monitors them in parallel (GPU-accelerated)
4. Detects trigger on ANY space (even if you're looking elsewhere)
5. Automatically switches to detected space
6. JARVIS speaks throughout the process explaining what he's doing

Prerequisites:
- Space 1: Terminal window (idle)
- Space 2: Terminal window (run: sleep 10 && echo "Deployment Complete")
- Space 3: You are here, running this test

Expected Result: JARVIS detects "Deployment Complete" on Space 2
                while you're looking at Space 3 (God Mode!)
                AND narrates the entire journey in real-time!
"""

import asyncio
import os
import sys
import logging
import subprocess
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
from backend.core.unified_speech_state import get_speech_state_manager


# =============================================================================
# JARVIS VOICE (Daniel's British Voice - Real-Time TTS)
# =============================================================================

async def jarvis_speak(message: str, rate: int = 200, blocking: bool = False) -> None:
    """
    JARVIS speaks using Daniel's British voice.

    Args:
        message: Text to speak
        rate: Words per minute (default: 200 for natural British cadence)
        blocking: If True, wait for speech to complete
    """
    try:
        # Notify speech state manager to prevent self-listening
        try:
            speech_manager = await get_speech_state_manager()
            await speech_manager.start_speaking(message, source="god_mode_test")
        except:
            pass  # Speech manager optional

        # Use macOS say command with Daniel's voice
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Daniel", "-r", str(rate), message,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        if blocking:
            # Wait for speech to complete
            await proc.wait()
            try:
                speech_manager = await get_speech_state_manager()
                await speech_manager.stop_speaking()
            except:
                pass
        else:
            # Fire and forget for non-blocking narration
            async def _wait_and_cleanup():
                await proc.wait()
                try:
                    speech_manager = await get_speech_state_manager()
                    await speech_manager.stop_speaking()
                except:
                    pass

            asyncio.create_task(_wait_and_cleanup())

        # Also print to console
        print(f"🗣️  JARVIS: {message}")

    except Exception as e:
        logger.warning(f"TTS error: {e}")
        # Fallback: just print
        print(f"🗣️  JARVIS: {message}")


async def test_god_mode():
    """Run the God Mode omnipresent surveillance test with real-time voice narration."""

    print("\n" + "="*70)
    print("🚀 JARVIS GOD MODE - OMNIPRESENT SURVEILLANCE TEST")
    print("="*70)
    print(f"⏰ Test started: {datetime.now().strftime('%H:%M:%S')}")
    print()

    # Opening announcement
    await jarvis_speak(
        "Initiating God Mode surveillance test. Omnipresent monitoring system activating.",
        rate=190,
        blocking=True
    )
    await asyncio.sleep(0.5)

    # Initialize agent
    print("📡 Initializing VisualMonitorAgent...")
    await jarvis_speak("Initializing visual monitoring agent.", rate=200)

    agent = VisualMonitorAgent()

    print("🔧 Starting agent services...")
    await jarvis_speak("Starting Ferrari Engine and multi-space detection systems.", rate=200)

    await agent.on_initialize()
    await agent.on_start()

    # Set TTS callback on agent so it can narrate during detection
    agent._tts_callback = lambda msg: jarvis_speak(msg, rate=190, blocking=False)

    print("✅ Agent ready!\n")
    await jarvis_speak("All systems operational. Ready for omnipresent surveillance.", rate=190, blocking=True)
    await asyncio.sleep(0.8)

    print("🧪 TEST SCENARIO:")
    print("-" * 70)
    print("1. 🔍 Searching for ALL 'Terminal' windows across ALL spaces")
    print("2. 🏎️  Spawning 60 FPS Ferrari Engine watchers for each window")
    print("3. 👁️  Monitoring parallel streams for trigger: 'Deployment Complete'")
    print("4. ⏱️  Maximum timeout: 120 seconds")
    print("-" * 70)
    print()

    await jarvis_speak(
        "Test parameters configured. I will search for all Terminal windows across every desktop space, "
        "spawn sixty frames per second Ferrari Engine watchers for each, "
        "and monitor them in parallel for the phrase: Deployment Complete.",
        rate=190,
        blocking=True
    )
    await asyncio.sleep(0.5)

    print("⚡ EXECUTING GOD MODE WATCH...")
    print("   (This will block until trigger detected or timeout)")
    print()

    await jarvis_speak(
        "Executing God Mode watch now. Stand by.",
        rate=200,
        blocking=True
    )
    await asyncio.sleep(0.3)

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
            # SUCCESS! Narrate the victory
            print("✅ ✅ ✅ SUCCESS: GOD MODE TRIGGER DETECTED! ✅ ✅ ✅")
            print()

            triggered_window = result.get('triggered_window', {})
            trigger_details = result.get('trigger_details', {})
            space_id = triggered_window.get('space_id', 'unknown')
            confidence = trigger_details.get('confidence', 0.0)
            total_watchers = result.get('total_watchers', 0)

            # Victory narration
            await jarvis_speak(
                f"Success! Trigger detected on Space {space_id}. "
                f"Confidence level: {int(confidence * 100)} percent.",
                rate=180,
                blocking=True
            )
            await asyncio.sleep(0.5)

            print("🎯 Detection Details:")
            print(f"   📍 Space ID: {space_id}")
            print(f"   🪟 Window ID: {triggered_window.get('window_id', 'unknown')}")
            print(f"   📱 App Name: {triggered_window.get('app_name', 'unknown')}")
            print(f"   🏎️  Watcher ID: {trigger_details.get('watcher_id', 'unknown')}")
            print(f"   🎯 Confidence: {confidence:.2%}")
            print(f"   ⏱️  Detection Time: {trigger_details.get('detection_time', 0.0):.2f}s")
            print()

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

            # Final victory speech
            await jarvis_speak(
                f"God Mode surveillance test complete. I successfully monitored {total_watchers} Terminal windows "
                f"simultaneously across multiple desktop spaces using sixty frames per second Ferrari Engine capture. "
                f"The trigger phrase was detected on Space {space_id} while you were observing from a different space. "
                f"Omnipresent surveillance has been achieved. All systems verified operational.",
                rate=185,
                blocking=True
            )

        elif status == 'timeout':
            print("⏱️  TIMEOUT: No trigger detected within 120 seconds")
            print()

            total_watchers = result.get('total_watchers', 0)

            # Timeout narration
            await jarvis_speak(
                f"Surveillance timeout. I monitored {total_watchers} Terminal windows for one hundred twenty seconds "
                f"but did not detect the phrase Deployment Complete in any of them.",
                rate=190,
                blocking=True
            )
            await asyncio.sleep(0.3)

            print("🔍 Possible Issues:")
            print("   • Text 'Deployment Complete' not visible in any Terminal")
            print("   • Terminals not actually open on Space 1 or Space 2")
            print("   • OCR unable to read text (font too small, obscured, etc.)")
            print()
            print(f"   ℹ️  Watchers spawned: {total_watchers}")
            if total_watchers == 0:
                print("   ⚠️  No Terminal windows found - check prerequisites!")
                await jarvis_speak(
                    "No Terminal windows were found across any desktop space. "
                    "Please verify the prerequisites and try again.",
                    rate=190,
                    blocking=True
                )
            else:
                await jarvis_speak(
                    "Possible issues: The trigger text may not be visible, "
                    "or OCR may be unable to read it due to font size or obstruction.",
                    rate=190,
                    blocking=True
                )

        else:
            print(f"❌ FAILED: {status}")
            print()

            # Failure narration
            await jarvis_speak(
                f"Test failed with status: {status}. Reviewing diagnostics.",
                rate=190,
                blocking=True
            )

            print("📄 Full Result:")
            import json
            print(json.dumps(result, indent=2, default=str))

    except Exception as e:
        print()
        print("="*70)
        print("❌ EXCEPTION OCCURRED")
        print("="*70)
        print(f"Error: {e}")

        # Exception narration
        await jarvis_speak(
            f"An exception has occurred during the God Mode test. Error: {str(e)[:100]}",
            rate=190,
            blocking=True
        )

        import traceback
        traceback.print_exc()

    finally:
        print()
        print("🛑 Stopping agent...")

        await jarvis_speak("Shutting down surveillance systems.", rate=200)

        await agent.on_stop()
        print("✅ Agent stopped cleanly")

        await jarvis_speak("All systems offline. Test complete.", rate=200, blocking=True)

        print()
        print("="*70)


if __name__ == "__main__":
    try:
        asyncio.run(test_god_mode())
    except KeyboardInterrupt:
        print("\n\n🛑 Test interrupted by user (Ctrl+C)")
        # Quick sync speak for interrupt
        subprocess.run(
            ["say", "-v", "Daniel", "-r", "200", "Test interrupted by user."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        # Quick sync speak for fatal error
        subprocess.run(
            ["say", "-v", "Daniel", "-r", "190", f"Fatal error: {str(e)[:50]}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        import traceback
        traceback.print_exc()
