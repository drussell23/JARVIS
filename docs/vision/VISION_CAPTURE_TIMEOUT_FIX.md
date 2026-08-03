# Vision Capture Timeout Fix

## Problem
When asking "can you see my screen?", JARVIS would get stuck processing the vision command indefinitely, causing the interface to hang.

## Root Cause
The screen capture operations in `backend/api/vision_command_handler.py` had **no timeout protection**, causing them to hang indefinitely when:
- Screen recording permissions were not granted
- System resources were constrained
- The capture API was unresponsive

## Solution Implemented

### 1. Added Timeout Protection to Primary Capture Method
**File**: `backend/api/vision_command_handler.py:2121`

```python
# BEFORE (no timeout):
screenshot = await self.vision_manager.vision_analyzer.capture_screen(
    multi_space=multi_space, space_number=space_number
)

# AFTER (with 15-second timeout):
screenshot = await asyncio.wait_for(
    self.vision_manager.vision_analyzer.capture_screen(
        multi_space=multi_space, space_number=space_number
    ),
    timeout=15.0  # 15 second timeout for screen capture
)
```

### 2. Added Timeout to Fallback Capture Method
**File**: `backend/api/vision_command_handler.py:2172`

```python
# Added timeout to subprocess.run for screencapture command
result = subprocess.run(
    ["screencapture", "-x", tmp_path],
    capture_output=True,
    text=True,
    timeout=10.0  # 10 second timeout for direct capture
)
```

### 3. Enhanced Error Handling and Logging

**Better error messages** (lines 2284-2296):
```python
if error_type == "screenshot_failed":
    if "timed out" in details.lower():
        error_message = (
            "The screen capture is taking longer than expected, Sir. "
            "This might be due to system resource constraints or screen recording permissions. "
            "Please ensure JARVIS has Screen Recording permissions in System Settings > Privacy & Security."
        )
```

**Improved logging** (lines 2144, 2152, 2155):
- `[VISION-CAPTURE]` prefix for all capture-related logs
- Detailed timing and status information
- Clear success/failure indicators with ✅/❌ emojis

### 4. Better Error Context
When capture fails, the system now provides specific details about the timeout:

```python
return await self._get_error_response(
    "screenshot_failed",
    command_text,
    details="Screen capture timed out or failed. This may be due to screen recording permissions or system resources."
)
```

## Benefits

1. **No More Hangs**: Vision commands now timeout gracefully after 15 seconds
2. **Better UX**: Users get clear error messages instead of indefinite waiting
3. **Diagnostic Information**: Logs help debug permission or resource issues
4. **Graceful Degradation**: System continues working even when screen capture fails

## Testing

Created `test_vision_capture_timeout.py` to verify:
- ✅ Screen capture completes or times out within 20 seconds
- ✅ Full command handling completes within 30 seconds
- ✅ Appropriate error messages are returned on timeout

## Files Modified

1. `backend/api/vision_command_handler.py`
   - Lines 2146-2156: Added timeout to primary capture
   - Lines 2172-2191: Added timeout to fallback capture
   - Lines 2284-2296: Enhanced error messages
   - Lines 2144, 2152, 2155, 2159, 2183, etc.: Improved logging

## Related Issue

This fixes the "processing..." stuck state that occurred when asking vision-related questions without proper screen recording permissions or when the system was under high load.

## Next Steps (Optional Enhancements)

1. Add retry logic with exponential backoff for transient failures
2. Implement progressive timeout (try fast methods first, slower methods if needed)
3. Add metrics to track capture performance and timeout frequency
4. Consider caching recent screenshots to provide faster responses
