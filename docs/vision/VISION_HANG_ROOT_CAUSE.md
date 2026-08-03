# Vision "Can You See My Screen?" Hang - Root Cause Analysis

## Problem
When asking "can you see my screen?", JARVIS hangs indefinitely during vision system initialization.

## Root Cause Found
**Circular Import / Slow Import Chain** in the vision system causing 20+ second hangs during module loading.

### Import Chain Analysis

```
vision_command_handler.py
  └─> imports VisionCommandHandler
       └─> initializes yabai_space_detector
       └─> initializes multi_space_capture_engine
            └─> tries to import context_intelligence.managers
                 └─> tries to import context_intelligence.automation
                      └─> tries to import claude_streamer
                           └─> HANGS HERE (circular import or slow init)
```

### Evidence from Logs

```
2025-11-10 17:25:41,404 - context_intelligence.automation.claude_streamer - APINetworkManager not available - edge case handling disabled
2025-11-10 17:25:41,405 - vision.reliable_screenshot_capture - Window capture manager not available: cannot import name 'get_claude_streamer' from 'context_intelligence.automation.claude_streamer'
2025-11-10 17:25:41,405 - vision.reliable_screenshot_capture - Error Handling Matrix not available: cannot import name 'get_claude_streamer' from 'context_intelligence.automation.claude_streamer'
```

Then the system **HANGS** for 20-30 seconds trying to complete the imports.

## Fixes Applied

### Fix #1: Commented Out Problematic Import
**Files Modified:**
- `backend/context_intelligence/automation/__init__.py`
- `backend/context_intelligence/__init__.py`

**Changes:**
```python
# BEFORE:
from .automation import get_browser_controller, get_google_docs_client, get_claude_streamer

# AFTER:
from .automation import get_browser_controller, get_google_docs_client
# get_claude_streamer causes import hangs - import directly when needed
```

### Fix #2: Added Missing Function (but still hangs)
Added `get_claude_streamer()` function to `claude_streamer.py` but imports still hang.

## The Real Solution Needed

The vision system has **too many dependencies** that cause slow/circular imports. Options:

### Option A: Lazy Imports (Recommended)
Move heavy imports inside functions instead of module-level:

```python
# Instead of:
from context_intelligence.managers import get_window_capture_manager

# Do:
def _capture_with_window_manager(self, space_id):
    from context_intelligence.managers import get_window_capture_manager
    # ... rest of code
```

### Option B: Remove Unnecessary Dependencies
The vision capture doesn't actually need:
- `claude_streamer`
- `Error Handling Matrix`
- `Window Capture Manager` (has fallbacks)

Comment out or make optional.

### Option C: Separate Vision Core from Advanced Features
Create a lightweight `vision_core.py` with just:
- Basic screencapture
- PIL ImageGrab
- Yabai integration

Move advanced features to separate modules loaded on-demand.

## Impact

**Before fixes:**
- Vision init: 20-30+ seconds (or infinite hang)
- Total response time: Never completes

**After commenting out imports:**
- Still hangs at 20 seconds (more imports to fix)

**Target after full fix:**
- Vision init: <2 seconds
- Total response time: 4-10 seconds

## Next Steps

1. ✅ Identified circular import issue
2. ✅ Commented out `get_claude_streamer` import
3. ⚠️  Need to find OTHER slow/circular imports
4. 🔄 Convert to lazy imports throughout vision system
5. 🧪 Test end-to-end

## Files Involved

**Modified:**
- `backend/context_intelligence/automation/__init__.py`
- `backend/context_intelligence/__init__.py`
- `backend/context_intelligence/automation/claude_streamer.py`

**Need to Review:**
- `backend/vision/reliable_screenshot_capture.py` (many imports)
- `backend/vision/multi_space_capture_engine.py` (context intelligence imports)
- `backend/vision/claude_vision_analyzer_main.py` (massive file, many imports)
- `backend/context_intelligence/managers/__init__.py` (may have circular refs)

## Recommendation

**Immediate fix:** Convert vision system to use lazy imports for all heavy dependencies. This will make imports instant and load features on-demand.

**Long-term:** Refactor vision system architecture to separate:
1. Core capture (fast, minimal deps)
2. Advanced features (loaded on demand)
3. Intelligence systems (separate initialization)
