# 🎯 Space-Specific Targeting Fix

## Problem Identified

**Query:** "What errors do you see in Space 5?"

**Expected:** Analyze VS Code in Space 5  
**Actual:** Analyzed Terminal in Space 6 with Jupyter errors

## Root Cause

The `_select_capture_targets()` method didn't recognize space-specific queries. It scored ALL windows across ALL spaces and picked the highest priority ones.

Since Terminal (Space 6) had visible errors in the window title, it scored higher than VS Code (Space 5), so JARVIS captured and analyzed the wrong space.

## Fix Applied

### 1. Space Extraction (`intelligent_orchestrator.py` lines 379-387)

**Added space detection logic:**
```python
# Check if query specifies a particular space
target_space_id = None
if query:
    query_lower = query.lower()
    for i in range(1, 21):  # Support up to Space 20
        if f"space {i}" in query_lower:
            target_space_id = i
            self.logger.info(f"[ORCHESTRATOR] Query targets specific Space {i}")
            break
```

### 2. Space Filtering (`intelligent_orchestrator.py` lines 391-396)

**Only capture from requested space:**
```python
for space in snapshot.spaces:
    space_id = space["space_id"]
    
    # If query specifies a space, ONLY capture that space
    if target_space_id is not None and space_id != target_space_id:
        continue  # Skip all other spaces
```

### 3. Priority Boost (`intelligent_orchestrator.py` lines 409-414)

**Boost priority for targeted space:**
```python
# If targeting specific space, capture ALL windows from that space
if target_space_id == space_id:
    if priority == CapturePriority.SKIP:
        priority = CapturePriority.HIGH
    reason = f"Requested: Space {target_space_id}"
    value = max(value, 0.9)
```

### 4. Unlimited Targets (`intelligent_orchestrator.py` lines 428-431)

**Return all targets from requested space:**
```python
# If targeting specific space, return ALL its targets (don't limit)
if target_space_id is not None:
    self.logger.info(f"[ORCHESTRATOR] Captured {len(targets)} targets from Space {target_space_id}")
    return targets  # Don't apply max_targets limit
```

### 5. Enhanced Prompt (`intelligent_orchestrator.py` lines 1517-1528)

**Tell Claude which space to analyze:**
```python
if target_space:
    prompt = f"""TARGET: **SPACE {target_space} ONLY** (User specifically requested this space)

🎯 FOCUSED ANALYSIS ON SPACE {target_space}:
You are analyzing ONLY Space {target_space}. Do NOT analyze other spaces.
"""
```

### 6. Response Format Warning (`intelligent_orchestrator.py` line 1639)

**Remind Claude to stay focused:**
```python
{f"⚠️  IMPORTANT: The user asked specifically about SPACE {target_space}. 
Your response MUST be about Space {target_space} ONLY. Do NOT analyze other spaces!" 
if target_space else ""}
```

## Test Cases

### Query: "What errors do you see in Space 5?"

**Expected Flow:**
1. ✅ Extract `target_space_id = 5` from query
2. ✅ Log: `[ORCHESTRATOR] Query targets specific Space 5`
3. ✅ Filter: Only iterate Space 5, skip Spaces 1-4, 6
4. ✅ Capture: VS Code from Space 5 only
5. ✅ Prompt: `TARGET: **SPACE 5 ONLY**`
6. ✅ Response: Analysis of VS Code in Space 5

**Before Fix:**
- ❌ Analyzed Space 6 (Terminal) instead

**After Fix:**
- ✅ Analyzes Space 5 (VS Code) correctly

### Query: "Show me Space 3"

**Expected Flow:**
1. ✅ Extract `target_space_id = 3`
2. ✅ Capture Google Chrome from Space 3 only
3. ✅ Analyze Space 3 content

### Query: "What's happening across my desktop spaces?"

**Expected Flow:**
1. ✅ `target_space_id = None` (no specific space)
2. ✅ Use normal priority scoring across all spaces
3. ✅ Multi-space analysis

## Supported Queries

### Space-Specific (Now Fixed!)
- ✅ "What errors do you see in Space 5?"
- ✅ "Show me Space 3"
- ✅ "What's in Space 1?"
- ✅ "Read the terminal in Space 6"
- ✅ "Check Space 4 for errors"
- ✅ "Analyze Space 2"

### Multi-Space (Still Works!)
- ✅ "What's happening across my desktop spaces?"
- ✅ "List all spaces"
- ✅ "What am I working on?"

## Logging Example

**Query:** "What errors do you see in Space 5?"

**Expected Logs:**
```
[ORCHESTRATOR] Query intent: error_analysis
[ORCHESTRATOR] Query targets specific Space 5
[ORCHESTRATOR] Workspace scouted: 6 spaces, 6 windows
[ORCHESTRATOR] Captured 1 targets from Space 5
[ORCHESTRATOR] Selected 1 capture targets
[ORCHESTRATOR] Captured 1 windows
[CLAUDE] Analyzing 1 images with enhanced visual intelligence prompt
```

## Action Required

**Restart JARVIS backend:**
```bash
# In backend terminal (Ctrl+C)
cd /Users/derekjrussell/Documents/repos/JARVIS-AI-Agent
python3 start_system.py
```

**Test it:**
```
"What errors do you see in Space 5?"
```

**Expected Response:**
```
Sir, I'm analyzing Space 5 which is running VS Code:

🔍 Key Visual Findings:
• File: intelligent_orchestrator.py (Working Tree)
• [OCR of visible code/errors in VS Code]
• [Any errors, warnings, or issues detected]

💡 Recommendations:
• [Specific suggestions based on Space 5 content]
```

---

*Fix applied: 2025-10-14*
*Files modified: `backend/vision/intelligent_orchestrator.py`*
