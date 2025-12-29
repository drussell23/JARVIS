# JARVIS Stereoscopic Vision Test

## What This Tests

This is a **"Stereoscopic Vision Test"** that proves JARVIS has true omnipresent parallel vision across multiple macOS spaces.

Instead of testing static text detection (easy), we test **Dynamic State Monitoring** across multiple dimensions:

- **Space 1 (Vertical):** Ball bouncing Top ↕ Bottom - "BOUNCE COUNT: 1, 2, 3..."
- **Space 2 (Horizontal):** Ball bouncing Left ↔ Right - "BOUNCE COUNT: 1, 2, 3..."

If JARVIS can report both data streams simultaneously without mixing them up, we prove:

1. ✅ **True Parallel Processing** - Not sequential window switching
2. ✅ **Stream Identification** - Knows which space is which
3. ✅ **Real-Time Vision** - Dynamic data, not static snapshots
4. ✅ **Ferrari Engine** - 60 FPS GPU-accelerated capture working across spaces

## Quick Start

### 1. Open the Visual Stimulus

**Space 1 - Vertical Bouncing:**
```bash
open "file://$(pwd)/backend/tests/visual_test/bouncing_balls.html?mode=vertical"
```

**Space 2 - Horizontal Bouncing:**
```bash
open "file://$(pwd)/backend/tests/visual_test/bouncing_balls.html?mode=horizontal"
```

### 2. Arrange Windows

1. Move first browser window to **Space 1** (Mission Control)
2. Move second browser window to **Space 2**
3. Switch to **Space 3** (your terminal)

### 3. Run the Test

```bash
python3 test_stereo_vision.py
```

## Expected Output

```
🔬 JARVIS STEREOSCOPIC VISION TEST
   Dynamic Multi-Space Parallel Surveillance

✅ Found 2 browser window(s):
   - Space 1: Google Chrome (Window 12345)
   - Space 2: Google Chrome (Window 12346)

🏎️  Ferrari Engine watchers streaming OCR data:

    [Space 1] VERTICAL: Bounce 1
    [Space 2] HORIZONTAL: Bounce 1
    [Space 1] VERTICAL: Bounce 2
    [Space 2] HORIZONTAL: Bounce 2
    [Space 1] VERTICAL: Bounce 3
    [Space 2] HORIZONTAL: Bounce 3
    ...
    (Both streams updating independently in real-time)
```

## Full OCR Streaming (Optional)

For **real-time bounce count extraction**, install OCR support:

```bash
# Install Tesseract OCR
brew install tesseract

# Install Python packages
pip install pytesseract pillow
```

Once installed, the Ferrari Engine will extract and stream actual bounce counts from both windows in real-time.

## What Success Looks Like

🎯 **PASS:** JARVIS correctly identifies:
- Space 1 shows "STATUS: VERTICAL"
- Space 2 shows "STATUS: HORIZONTAL"
- Bounce counts update independently
- No cross-contamination between streams

❌ **FAIL:** JARVIS mixes up which window is which, or only monitors one at a time

## The "Stereoscopic" Metaphor

Just like human stereoscopic vision uses two eyes to see depth, this test uses **two parallel Ferrari Engine watchers** to prove JARVIS has true multi-space awareness - he's not "switching focus" between windows, he's genuinely **omnipresent** across your desktop.

---

**This is the ultimate stress test for God Mode surveillance!** 🚀
