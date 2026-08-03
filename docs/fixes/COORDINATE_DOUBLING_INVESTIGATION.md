# Coordinate Doubling Investigation

## Problem Statement
When you tell JARVIS "living room tv", the mouse moves to **(2475, 15)** instead of the correct Control Center position **(1236, 12)**. This is approximately double the intended coordinates.

## What We've Done

### 1. ✅ Verified Code Has Correct Coordinates
- `control_center_clicker.py`: Uses (1236, 12) ✓
- `control_center_clicker_simple.py`: Uses (1236, 12) ✓
- `advanced_display_monitor.py`: Imports the simple clicker ✓

### 2. ✅ Verified PyAutoGUI Works Correctly
- Direct PyAutoGUI tests show mouse goes to (1236, 12) correctly
- No system-level coordinate doubling in isolation
- The debug log shows correct coordinates being sent: (1236, 12)

### 3. ✅ Cleared All Cached Code
- Deleted all .pyc files
- Removed __pycache__ directories
- Ensured fresh imports

### 4. ✅ Disabled Problematic Coordinate Fix
- Disabled `apply_coordinate_fix()` in `adaptive_control_center_clicker.py`
- This was monkey-patching PyAutoGUI but not helping

### 5. ✅ Added Comprehensive Debugging
- `debug_jarvis_coordinates.py`: Logs every PyAutoGUI call
- `diagnose_coordinate_doubling.py`: System diagnostic
- `main.py`: Runs diagnostic on startup

## Key Findings

1. **The code is correct** - sends (1236, 12)
2. **PyAutoGUI works correctly** - when tested directly
3. **The debug log confirms** - (1236, 12) is being sent
4. **BUT the mouse still goes to (2475, 15)** - only when JARVIS runs

## The Mystery

The doubling happens **AFTER** the correct coordinates are sent to PyAutoGUI, which means:
- Not a code issue (we verified the values)
- Not a PyAutoGUI issue (it works in tests)
- Likely a **runtime/environment issue** specific to how JARVIS launches

## Possible Causes

1. **Something in JARVIS's startup sequence** modifies coordinate handling
2. **An imported module** that we haven't identified yet
3. **macOS accessibility permissions** applied differently when JARVIS runs
4. **Display configuration** that's different at runtime

## What To Do Next

### When You Start JARVIS:

1. **Check the startup output** for:
   ```
   [STARTUP-DEBUG] Running from: /path/to/main.py
   [STARTUP-DEBUG] Working directory: /path
   [STARTUP-DEBUG] Running coordinate diagnostic...
   ```

2. **Check the debug log**:
   ```bash
   cat /tmp/jarvis_coordinate_debug.log
   ```
   This will show exactly what coordinates are being passed

3. **Check the diagnostic log**:
   ```bash
   cat /tmp/jarvis_coordinate_diagnostic.log
   ```
   This shows system information at startup

4. **Tell JARVIS "living room tv"** and watch:
   - Where the mouse actually goes
   - What the debug log says

5. **Share the logs** with me so we can see:
   - The exact coordinates being sent
   - The call stack showing where it's called from
   - Any warnings about wrapped functions

## Test Commands

```bash
# Clear cache before starting
find /Users/derekjrussell/Documents/repos/JARVIS-AI-Agent -name "*.pyc" -delete
find /Users/derekjrussell/Documents/repos/JARVIS-AI-Agent -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

# Clear old logs
rm /tmp/jarvis_coordinate_debug.log
rm /tmp/jarvis_coordinate_diagnostic.log

# Start JARVIS
# (your normal start command)

# After telling JARVIS "living room tv", check logs:
cat /tmp/jarvis_coordinate_debug.log
cat /tmp/jarvis_coordinate_diagnostic.log
```

## Files Modified

1. `backend/main.py` - Added startup diagnostics
2. `backend/display/control_center_clicker.py` - Verified coordinates
3. `backend/display/control_center_clicker_simple.py` - Verified coordinates
4. `backend/display/adaptive_control_center_clicker.py` - Disabled coordinate_fix
5. `backend/display/advanced_display_monitor.py` - Uses simple clicker

## Files Created

1. `diagnose_coordinate_doubling.py` - Comprehensive system diagnostic
2. `debug_jarvis_coordinates.py` - PyAutoGUI call logger (already imported by clickers)
3. `test_exact_jarvis_flow.py` - Test script (works correctly)
4. `COORDINATE_DOUBLING_INVESTIGATION.md` - This file

## Current Status

✅ Code is correct
✅ Tests pass
❌ JARVIS still shows issue
🔍 **Need logs from actual JARVIS run to diagnose further**

The debugging infrastructure is now in place. When you run JARVIS and tell it "living room tv", we'll capture:
- Exact coordinates being sent
- Call stack
- System state
- Any modifications to PyAutoGUI

This should finally reveal where the doubling occurs!