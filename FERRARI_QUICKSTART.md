# 🏎️ Ferrari Engine Quick Start Guide

## What is Ferrari Engine?

Ferrari Engine is JARVIS's GPU-accelerated window capture system, using Apple's ScreenCaptureKit for **60 FPS real-time surveillance** with sub-60ms latency.

## ✅ Verification Test

Run this to confirm Ferrari Engine is working:

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
PYTHONPATH="$PWD:$PWD/backend" python3 test_ferrari_integration_simple.py
```

**Expected output:**
```
🏁 FERRARI ENGINE INTEGRATION TEST: PASSED ✅
   🏎️  Ferrari Engine fully operational!
   • Window discovery via fast_capture: ✅
   • VideoWatcher spawning: ✅
   • GPU-accelerated frame streaming: ✅
   • ScreenCaptureKit integration: ✅
```

## 🚀 Quick Examples

### Example 1: Basic Window Monitoring

```python
from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent, VisualMonitorConfig

# Initialize agent
config = VisualMonitorConfig(default_fps=30)
agent = VisualMonitorAgent(config=config)
await agent.on_initialize()

# Find a window
window = await agent._find_window("Terminal")

# Spawn Ferrari watcher
watcher = await agent._spawn_ferrari_watcher(
    window_id=window['window_id'],
    fps=30,
    app_name="Terminal",
    space_id=1
)

# Get frames
for i in range(10):
    frame_data = await watcher.get_latest_frame(timeout=1.0)
    if frame_data:
        print(f"Frame {i}: {frame_data['method']} - {frame_data.get('capture_latency_ms', 0):.1f}ms")

# Cleanup
await watcher.stop()
await agent.on_stop()
```

### Example 2: God Mode (Multi-Window)

```python
# Monitor 3 windows simultaneously
apps = ["Terminal", "Cursor", "Safari"]
watchers = {}

for app_name in apps:
    window = await agent._find_window(app_name)
    if window and window.get('found'):
        watcher = await agent._spawn_ferrari_watcher(
            window_id=window['window_id'],
            fps=15,  # Lower FPS for multiple windows
            app_name=app_name,
            space_id=window.get('space_id', 1)
        )
        watchers[app_name] = watcher
        print(f"✅ Watching {app_name}")

# All 3 windows now streaming in parallel!
```

### Example 3: Watch & Act (with OCR)

```python
# Watch Terminal for "Done", then take action
from backend.neural_mesh.agents.visual_monitor_agent import WatchAndActRequest, ActionConfig, ActionType

request = WatchAndActRequest(
    app_name="Terminal",
    trigger_text="Done",
    action_config=ActionConfig(
        action_type=ActionType.SIMPLE_GOAL,
        goal="Click the Deploy button",
        narrate=True
    )
)

result = await agent.watch_and_act(request)
print(f"Detection: {result}")
```

## 📊 Check Status

```python
# Get Ferrari Engine stats
stats = agent.get_stats()

print(f"GPU Accelerated: {stats['gpu_accelerated']}")
print(f"Capture Method: {stats['capture_method']}")
print(f"Active Watchers: {stats['active_ferrari_watchers']}")
```

## 🔧 Configuration Options

```python
VisualMonitorConfig(
    default_fps=30,              # Target FPS (Ferrari adapts intelligently)
    max_parallel_watchers=5,     # Max concurrent watchers (God Mode)
    enable_action_execution=True,# Execute actions on detection
    enable_computer_use=True,    # Use Computer Use for actions
    auto_switch_to_window=True   # Auto-focus target window
)
```

## 🎯 Voice Commands (Future)

Once integrated with voice system:

```
"Watch the Terminal for 'Build Complete', then click Deploy"
"Monitor Safari until page loads"
"Alert me when the deployment status changes"
"Watch Cursor, Terminal, and Chrome simultaneously"
```

## 📦 Optional: Install OCR

For text detection capabilities:

```bash
# Install Tesseract OCR engine
brew install tesseract

# Install Python dependencies
pip install pytesseract pillow opencv-python
```

## 🐛 Troubleshooting

### "Ferrari Engine not available"

**Check 1:** macOS version
```bash
sw_vers  # Should be 12.3+
```

**Check 2:** Screen Recording permission
- System Preferences → Privacy → Screen Recording
- Enable for Terminal/Python

**Check 3:** Native extensions compiled
```bash
cd backend/native_extensions
ls -la fast_capture.*.so  # Should exist
```

### "No windows found"

**Solution:** Open some applications (Terminal, Cursor, Safari, etc.)

### "Frames not capturing"

**Check:** Screen Recording permission (see above)

## 📈 Performance Tips

1. **Lower FPS for multiple windows:**
   ```python
   # God Mode with 3 watchers
   fps=15  # Each window at 15 FPS = 45 FPS total
   ```

2. **Adaptive FPS is automatic:**
   - Static content: ~5-10 FPS
   - Dynamic content: Up to 60 FPS
   - No configuration needed!

3. **OCR optimization:**
   - OCR runs ~5 times/second regardless of capture FPS
   - Higher capture FPS = more frames to choose from
   - Lower FPS = less GPU/battery usage

## 🎓 Key Concepts

### Ferrari Engine = ScreenCaptureKit + Native C++

- **ScreenCaptureKit:** Apple's GPU-accelerated capture API (macOS 12.3+)
- **fast_capture:** Native C++ bridge for window enumeration
- **VideoWatcher:** High-level Python interface with adaptive FPS
- **Result:** 60 FPS capable, sub-60ms latency, zero-copy Metal textures

### Adaptive FPS

Ferrari Engine doesn't always run at max FPS:

- **Static screen:** Low FPS (saves power)
- **Active changes:** Ramps up to 60 FPS
- **You set target:** Ferrari adapts within 0-target range

### God Mode

Multiple windows, single process:

```python
_active_video_watchers = {
    "watcher_8230_12345": {...},  # Terminal
    "watcher_9104_12346": {...},  # Cursor
    "watcher_7721_12347": {...},  # Safari
}
```

All streaming in parallel, non-blocking async.

## 📚 Documentation

- **Full Documentation:** `FERRARI_ENGINE_INTEGRATION_SUCCESS.md`
- **Test Files:**
  - `test_ferrari_integration_simple.py` - Core test
  - `test_visual_monitor_ferrari.py` - Full OCR test
- **Source Code:**
  - `backend/neural_mesh/agents/visual_monitor_agent.py` - Main agent
  - `backend/vision/macos_video_capture_advanced.py` - VideoWatcher
  - `backend/native_extensions/fast_capture.*` - Native bridge

## ✨ Success Indicators

When Ferrari Engine is working:

```python
stats = agent.get_stats()

assert stats['gpu_accelerated'] == True
assert stats['capture_method'] == 'ferrari_engine'
assert 'screencapturekit' in frame_data['method']
assert frame_data['capture_latency_ms'] < 100  # Usually ~50ms
```

## 🚦 Current Status

| Component | Status | Notes |
|-----------|--------|-------|
| Ferrari Engine Core | ✅ READY | ScreenCaptureKit active |
| Window Discovery | ✅ READY | fast_capture working |
| VideoWatcher Integration | ✅ READY | Spawning functional |
| Frame Streaming | ✅ READY | 60 FPS capable |
| God Mode (Multi-Window) | ✅ READY | 5 concurrent watchers |
| OCR Text Detection | ⚠️ Optional | Requires `tesseract` |
| Voice Commands | 🔜 Coming | Neural Mesh integration |

---

**Ready to go! 🏎️💨**

The Ferrari Engine is production-ready and integrated into VisualMonitorAgent v12.0. Start with the verification test, then explore the examples above.

For questions or issues, see the full documentation in `FERRARI_ENGINE_INTEGRATION_SUCCESS.md`.
