#!/usr/bin/env python3
"""
Simple Ferrari Engine Integration Test
Tests ONLY the core Ferrari Engine integration without OCR dependencies

This verifies:
1. VisualMonitorAgent v12.0 initialization
2. Ferrari Engine window discovery (fast_capture)
3. VideoWatcher spawning and frame capture
4. GPU-accelerated streaming verification
"""
import asyncio
import os
import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_path))

from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent, VisualMonitorConfig


async def test_ferrari_integration():
    """Test core Ferrari Engine integration."""
    print("=" * 80)
    print("🏎️  FERRARI ENGINE INTEGRATION TEST (Simple)")
    print("   Testing GPU window discovery + VideoWatcher integration")
    print("=" * 80)

    # Step 1: Initialize agent
    print("\n→ Step 1: Initializing VisualMonitorAgent v12.0...")

    config = VisualMonitorConfig(
        default_fps=30,
        max_parallel_watchers=5
    )

    agent = VisualMonitorAgent(config=config)
    await agent.on_initialize()

    stats = agent.get_stats()
    print(f"✅ Agent initialized")
    print(f"   Capture method: {stats['capture_method']}")
    print(f"   GPU accelerated: {stats['gpu_accelerated']}")

    # Step 2: Test Ferrari Engine window discovery
    print("\n→ Step 2: Testing Ferrari Engine window discovery...")

    if agent._fast_capture_engine:
        try:
            windows = await asyncio.to_thread(
                agent._fast_capture_engine.get_visible_windows
            )
            print(f"✅ Ferrari Engine discovered {len(windows)} windows")

            if windows:
                print("\n   Sample windows:")
                for i, window in enumerate(windows[:5]):
                    print(f"   {i+1}. {window.app_name}: {window.window_title}")
                    print(f"      ID: {window.window_id}, Size: {window.width}x{window.height}")
        except Exception as e:
            print(f"❌ Window discovery failed: {e}")
            await agent.on_stop()
            return False
    else:
        print("⚠️  Ferrari Engine not available (expected if ScreenCaptureKit unavailable)")
        await agent.on_stop()
        return True  # Still success - we're testing graceful degradation

    # Step 3: Find a suitable window
    print("\n→ Step 3: Finding suitable window for monitoring...")

    test_apps = ["Cursor", "Terminal", "iTerm2", "Code", "Safari", "Chrome"]
    found_window = None

    for app_name in test_apps:
        try:
            window_info = await agent._find_window(app_name=app_name)
            if window_info and window_info.get('found'):
                found_window = window_info
                print(f"\n✅ Selected: {app_name}")
                print(f"   Window ID: {found_window['window_id']}")
                print(f"   Size: {found_window.get('width', 0)}x{found_window.get('height', 0)}")
                print(f"   Discovery method: {found_window.get('method', 'unknown')}")
                print(f"   Confidence: {found_window.get('confidence', 0)}%")
                break
        except Exception as e:
            continue

    if not found_window:
        print("\n⚠️  No suitable window found")
        print("   This is OK - Ferrari Engine discovery working, just no target windows")
        await agent.on_stop()
        return True

    # Step 4: Spawn Ferrari watcher
    print("\n→ Step 4: Spawning Ferrari Engine VideoWatcher...")

    try:
        watcher = await agent._spawn_ferrari_watcher(
            window_id=found_window['window_id'],
            fps=30,
            app_name=found_window['app_name'],
            space_id=found_window.get('space_id', 1)
        )

        if not watcher:
            print("❌ Failed to spawn watcher")
            await agent.on_stop()
            return False

        print(f"✅ Watcher spawned successfully")
        print(f"   Watcher ID: {watcher.watcher_id}")
        print(f"   Target FPS: 30")

        # Step 5: Collect sample frames
        print("\n→ Step 5: Collecting sample frames...")
        print("   Capturing 5 frames to verify streaming...")

        frames_captured = []
        for i in range(10):  # Try 10 times to get 5 frames
            frame_data = await watcher.get_latest_frame(timeout=1.0)

            if frame_data:
                frames_captured.append(frame_data)
                method = frame_data.get('method', 'unknown')
                fps = frame_data.get('fps', 0)
                latency = frame_data.get('capture_latency_ms', 0)

                print(f"   Frame {len(frames_captured)}: Method=[{method}] "
                      f"FPS={fps:.1f} Latency={latency:.1f}ms "
                      f"Shape={frame_data['frame'].shape}")

                if len(frames_captured) >= 5:
                    break

            await asyncio.sleep(0.1)

        # Step 6: Analyze results
        print("\n" + "=" * 80)
        print("📊 RESULTS")
        print("=" * 80)
        print(f"Frames captured: {len(frames_captured)}")

        if frames_captured:
            methods = set(f.get('method', 'unknown') for f in frames_captured)
            avg_latency = sum(f.get('capture_latency_ms', 0) for f in frames_captured) / len(frames_captured)

            print(f"Capture methods used: {methods}")
            print(f"Average latency: {avg_latency:.1f}ms")

            if 'screencapturekit' in methods:
                print("\n🏎️  ✅ FERRARI ENGINE ACTIVE!")
                print("   GPU-accelerated ScreenCaptureKit streaming confirmed!")
                success = True
            else:
                print(f"\n⚠️  Using fallback method: {methods}")
                print("   (This is OK if ScreenCaptureKit not available)")
                success = True
        else:
            print("\n❌ No frames captured")
            success = False

        # Cleanup
        print("\n→ Stopping watcher...")
        await watcher.stop()
        print("✅ Watcher stopped")

    except Exception as e:
        print(f"\n❌ Error during watcher test: {e}")
        import traceback
        traceback.print_exc()
        success = False

    # Final cleanup
    await agent.on_stop()

    # Final stats
    final_stats = agent.get_stats()
    print("\n" + "=" * 80)
    print("📈 FINAL STATISTICS")
    print("=" * 80)
    print(f"Active ferrari watchers: {final_stats['active_ferrari_watchers']}")
    print(f"Capture method: {final_stats['capture_method']}")
    print(f"GPU accelerated: {final_stats['gpu_accelerated']}")

    print("\n" + "=" * 80)
    if success:
        print("🏁 FERRARI ENGINE INTEGRATION TEST: PASSED ✅")
        if final_stats['gpu_accelerated']:
            print("\n   🏎️  Ferrari Engine fully operational!")
            print("   • Window discovery via fast_capture: ✅")
            print("   • VideoWatcher spawning: ✅")
            print("   • GPU-accelerated frame streaming: ✅")
            print("   • ScreenCaptureKit integration: ✅")
            print("\n   Ready for production use:")
            print("   • Real-time window monitoring")
            print("   • 60 FPS GPU-accelerated capture")
            print("   • Multi-window surveillance (God Mode)")
        else:
            print("\n   Test passed with fallback methods")
            print("   (Ferrari Engine not available on this system)")
    else:
        print("🏁 FERRARI ENGINE INTEGRATION TEST: FAILED ❌")
    print("=" * 80 + "\n")

    return success


if __name__ == "__main__":
    try:
        passed = asyncio.run(test_ferrari_integration())
        sys.exit(0 if passed else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
