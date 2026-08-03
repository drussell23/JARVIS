# Desktop Space ValueError Fix

## Problem
When asking "What's happening across my desktop spaces?", JARVIS was encountering a `ValueError` and displaying the message:
```
JARVIS: I encountered an error analyzing your screen: ValueError. Please try again.
```

## Root Cause
The error occurred when multi-space screenshot capture failed or returned an empty dictionary. The code flow was:

1. User query "What's happening across my desktop spaces?" triggers multi-space capture
2. `vision_command_handler.py` calls `_capture_screen(multi_space=True)`
3. Multi-space capture engine (`MultiSpaceCaptureEngine`) attempts to capture all spaces
4. If capture fails completely, it returns an empty dict `{}`
5. The empty dict was passed to `_analyze_multi_space_screenshots()` in `pure_vision_intelligence.py`
6. This function called `_get_multi_space_claude_response()` without validating the screenshots dict
7. `_get_multi_space_claude_response()` raised `ValueError("No screenshots provided for multi-space analysis")` on line 2174

## Solution

### 1. Added validation in `_analyze_multi_space_screenshots()` (pure_vision_intelligence.py)
- Added check for empty screenshots dict before attempting Claude API call
- If screenshots are empty, now falls back to window data from Yabai/system
- Provides helpful response with available information or clear error message about permissions

### 2. Improved error handling in async_pipeline.py
- Added more specific error message for screenshot capture failures
- Now detects ValueError related to screenshots and provides actionable guidance
- Directs users to check Screen Recording permissions in System Preferences

## Changes Made

### File: `backend/api/pure_vision_intelligence.py`
**Lines 1238-1280**: Added validation at the start of `_analyze_multi_space_screenshots()`:
- Checks if screenshots dict is empty
- Falls back to window data if available
- Provides basic workspace overview from Yabai data
- Shows clear permission error message if needed

### File: `backend/core/async_pipeline.py`
**Lines 1664-1684**: Enhanced error handling in vision command processing:
- Added traceback logging for better debugging
- Detects ValueError types specifically
- Provides user-friendly permission guidance for screenshot-related errors
- Generic error message for other ValueError types

## Testing
To test the fix:
1. Ask JARVIS: "What's happening across my desktop spaces?"
2. Instead of a generic ValueError, you should now receive either:
   - A basic workspace overview from window data (if Yabai is available)
   - A clear message about enabling Screen Recording permissions
   - A helpful error message if something else goes wrong

## Benefits
1. **No more cryptic errors**: Users get actionable information instead of "ValueError"
2. **Graceful degradation**: Falls back to window-data-only response when screenshots fail
3. **Better debugging**: Full traceback is logged for developers
4. **User guidance**: Clear instructions on fixing permission issues

## Related Files
- `backend/api/pure_vision_intelligence.py` - Core vision intelligence
- `backend/core/async_pipeline.py` - Command processing pipeline
- `backend/vision/multi_space_capture_engine.py` - Screenshot capture engine
- `backend/api/vision_command_handler.py` - Vision command routing
