# 🧪 Voice Narration Testing Guide

Quick reference for testing enhanced voice narration with God Mode surveillance.

---

## 🚀 Quick Start

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 run_supervisor.py
```

---

## 📝 Test Scenarios

### 1️⃣ First Operation (Learning Acknowledgment)

**Say:**
```
"JARVIS, watch Terminal for Build Complete"
```

**Expected Response:**
```
"On it, Derek. Watching Terminal for 'Build Complete'."

[After detection]

"Found it, Derek! 'Build Complete' just appeared in Terminal.

First time monitoring Terminal, Derek. I've learned its visual
characteristics now."
```

**What to Check:**
- ✅ Natural start message
- ✅ Learning acknowledgment for first Terminal surveillance
- ✅ No stats mentioned (single window mode)

---

### 2️⃣ God Mode (Multi-Window Parallel Surveillance)

**Say:**
```
"JARVIS, watch all Chrome windows for Error"
```

**Expected Response:**
```
"Got it, Derek. I'll scan every Chrome window across all your desktop
spaces for 'Error' until I find it."

[After detection]

"Success! I spotted 'Error' in Chrome on Space 3. Confidence: 94%.
I was watching 5 Chrome windows in parallel - this one triggered first."
```

**What to Check:**
- ✅ God Mode acknowledgment ("all Chrome windows")
- ✅ Window count mentioned (5 windows)
- ✅ Space ID mentioned (Space 3)
- ✅ Confidence percentage shown
- ✅ Parallel surveillance noted

---

### 3️⃣ With Duration (Time-Limited Surveillance)

**Say:**
```
"JARVIS, watch Terminal for 2 minutes when it says DONE"
```

**Expected Response:**
```
"Sure. Monitoring Terminal for 'DONE' for the next 2 minutes."

[If timeout]

"I watched Terminal for 2 minutes, Derek, but didn't see 'DONE'.
Want me to keep looking?"
```

**What to Check:**
- ✅ Duration mentioned in start message
- ✅ Helpful timeout message offers to continue
- ✅ Non-accusatory tone

---

### 4️⃣ High Confidence Detection (Clear Text)

**Setup:** Use large, clear text trigger like bouncing ball test page

**Say:**
```
"JARVIS, watch Chrome for BOUNCE COUNT"
```

**Expected Response:**
```
"Found it, Derek! 'BOUNCE COUNT' just appeared in Chrome.
Confidence: 96%."
```

**What to Check:**
- ✅ Enthusiastic tone ("Found it!")
- ✅ High confidence shown (>90%)
- ✅ Simple, confident message

---

### 5️⃣ Medium Confidence Detection (Borderline Clear)

**Setup:** Use smaller text or partially occluded trigger

**Say:**
```
"JARVIS, watch Terminal for warning"
```

**Expected Response:**
```
"I detected 'warning' in Terminal, Derek. Confidence: 87%.
Looks good."
```

**What to Check:**
- ✅ Confidence shown (85-90%)
- ✅ Reassuring tone ("Looks good")
- ✅ Brief acknowledgment

---

### 6️⃣ Borderline Confidence (Unclear Text)

**Setup:** Use very small text or overlapping windows

**Say:**
```
"JARVIS, watch Chrome for success"
```

**Expected Response:**
```
"I detected what appears to be 'success' in Chrome, Derek.
Confidence is 82% - slightly lower than usual, possibly due to
partial occlusion or font rendering. Want me to keep watching
to confirm?"
```

**What to Check:**
- ✅ Shows thought process
- ✅ Explains why confidence is lower
- ✅ Asks for confirmation
- ✅ Helpful, not accusatory

---

### 7️⃣ Low Confidence (Very Unclear)

**Setup:** Use tiny text or severe occlusion

**Say:**
```
"JARVIS, watch Terminal for error"
```

**Expected Response:**
```
"I detected something that might be 'error' in Terminal, Derek.
OCR confidence is only 74%, which is below my usual threshold.
This could be due to small font size, partial visibility, or text overlap.
Want me to try again with enhanced OCR?"
```

**What to Check:**
- ✅ Multi-factor explanation
- ✅ Suggests potential causes
- ✅ Offers retry options
- ✅ Educational tone

---

### 8️⃣ 10th Operation (Milestone Celebration)

**Setup:** Run 10 surveillance operations

**Expected Response (on 10th):**
```
"Got it! 'DONE' showed up in Terminal.

Fun milestone: That's 10 surveillance operations, Derek!
8 successful detections so far. I'm learning your patterns."
```

**What to Check:**
- ✅ Milestone celebration
- ✅ Success rate mentioned (8/10)
- ✅ Encouraging tone ("I'm learning your patterns")

---

### 9️⃣ 25th Operation (Major Milestone)

**Setup:** Run 25 surveillance operations

**Expected Response (on 25th):**
```
"Found it, Derek! 'Error' just appeared in Chrome on Space 2.
Confidence: 93%.

Milestone achieved! 25 surveillance operations completed, Derek.
22 successful detections (88% success rate). You've used God Mode
15 times across 4 different apps."
```

**What to Check:**
- ✅ Detailed statistics
- ✅ Success rate percentage
- ✅ God Mode count
- ✅ App diversity mentioned

---

### 🔟 100th Operation (Major Celebration)

**Setup:** Run 100 surveillance operations (or mock the counter)

**Expected Response (on 100th):**
```
"Success! I spotted 'Build Complete' in Terminal.

🎯 Major milestone: 100 surveillance operations completed, Derek!
Stats: 94/100 successful (94%), God Mode used 67 times,
342 total windows monitored, average confidence 91%.
Fastest detection: 2.3s. You're a surveillance pro!"
```

**What to Check:**
- ✅ Full statistics breakdown
- ✅ Total windows monitored
- ✅ Average confidence
- ✅ Fastest detection time
- ✅ Enthusiastic celebration

---

### 1️⃣1️⃣ First God Mode (Special Acknowledgment)

**Say (first time using "all"):**
```
"JARVIS, watch all Terminal windows for DONE"
```

**Expected Response:**
```
"Understood. Spawning watchers for every Terminal window.
Looking for 'DONE' until I find it."

[After detection]

"Found it! 'DONE' appeared in Terminal on Space 5. I was watching
3 Terminal windows in parallel - this one triggered first.

First God Mode operation activated, Derek. Parallel surveillance
is now part of my skill set."
```

**What to Check:**
- ✅ God Mode start message
- ✅ Learning acknowledgment for first God Mode
- ✅ "Parallel surveillance" mentioned

---

### 1️⃣2️⃣ Confidence Improvement (Learning)

**Setup:** After 5+ operations with improving confidence

**Expected Response:**
```
"Found it, Derek! 'Error' just appeared in Chrome.
Confidence: 96%.

Detection confidence is improving, Derek. This one was 96% -
well above my average of 88%."
```

**What to Check:**
- ✅ Acknowledges improvement
- ✅ Shows current vs average
- ✅ Encouraging tone

---

### 1️⃣3️⃣ Time-Aware (Early Morning)

**Setup:** Test at 5 AM or 6 AM

**Say:**
```
"JARVIS, watch Terminal for DONE"
```

**Expected Response:**
```
"Quietly monitoring Terminal for 'DONE'."

[After detection]

"Found it, Derek! 'DONE' just appeared in Terminal."
```

**What to Check:**
- ✅ Subdued start message
- ✅ No loud stats at early morning
- ✅ Simple, quiet acknowledgment

---

### 1️⃣4️⃣ Time-Aware (Late Night)

**Setup:** Test at 11 PM or later

**Say:**
```
"JARVIS, watch Chrome for Error"
```

**Expected Response:**
```
"Quietly monitoring Chrome for 'Error'."

[After detection]

"There it is - 'Error' appeared in Chrome."
```

**What to Check:**
- ✅ Respectful of late hour
- ✅ Minimal noise/stats
- ✅ Subdued tone

---

### 1️⃣5️⃣ No Windows Found (Helpful Error)

**Say (with Chrome closed):**
```
"JARVIS, watch Chrome for Error"
```

**Expected Response:**
```
"On it, Derek. Watching Chrome for 'Error'."

[Immediately after]

"I don't see any Chrome windows open right now, Derek.
Could you open Chrome first?"
```

**What to Check:**
- ✅ Helpful, not accusatory
- ✅ Suggests solution
- ✅ Friendly tone

---

### 1️⃣6️⃣ Surveillance Timeout (Duration Expired)

**Say:**
```
"JARVIS, watch Terminal for 30 seconds when it says NEVER_APPEARS"
```

**Expected Response:**
```
"Sure. Monitoring Terminal for 'NEVER_APPEARS' for the next 30 seconds."

[After 30 seconds]

"I watched Terminal for 30 seconds, Derek, but didn't see
'NEVER_APPEARS'. Want me to keep looking?"
```

**What to Check:**
- ✅ Duration acknowledged
- ✅ Helpful timeout message
- ✅ Offers to continue
- ✅ Non-judgmental tone

---

### 1️⃣7️⃣ System Error (Graceful Failure)

**Setup:** Trigger internal error (e.g., VisualMonitorAgent unavailable)

**Expected Response:**
```
"I'm sorry, Derek. My visual surveillance system isn't responding
right now. Try again in a moment?"
```

**What to Check:**
- ✅ Apologetic tone
- ✅ Explains what's wrong
- ✅ Suggests retry
- ✅ No technical jargon

---

## 🎯 Key Testing Objectives

### ✅ Progressive Confidence
- [ ] High confidence (>90%) = enthusiastic, minimal explanation
- [ ] Medium confidence (85-90%) = shows confidence %
- [ ] Borderline (80-85%) = explains thought process
- [ ] Low (<80%) = multi-factor explanation, offers retry

### ✅ Context Awareness
- [ ] God Mode = mentions window count, space ID
- [ ] Single window = simple, direct
- [ ] Duration = mentions time limit
- [ ] Time of day = subdued at night/early morning

### ✅ Learning & Milestones
- [ ] First app = learning acknowledgment
- [ ] First God Mode = skill acknowledgment
- [ ] 10th operation = fun milestone
- [ ] 25th operation = detailed stats
- [ ] 100th operation = major celebration
- [ ] Confidence improvement = acknowledges growth

### ✅ Natural Variations
- [ ] Multiple start messages (never same twice)
- [ ] Multiple success messages (varies each time)
- [ ] Multiple error messages (not repetitive)
- [ ] Random selection ensures variety

### ✅ Helpful Communication
- [ ] Errors are friendly, not technical
- [ ] Timeouts offer to continue
- [ ] Low confidence explains causes
- [ ] All messages voice-friendly (TTS-ready)

---

## 🐛 Known Edge Cases

### Case 1: Rapid Sequential Operations
**What:** User runs 10 operations in quick succession
**Expected:** Milestone celebrates on 10th operation only, not every operation
**Status:** ✅ Handled - `last_milestone_announced` prevents duplicates

### Case 2: Same App Multiple Times
**What:** Monitor Chrome 5 times in a row
**Expected:** Learning acknowledgment only on FIRST Chrome surveillance
**Status:** ✅ Handled - `apps_monitored` set prevents duplicates

### Case 3: Zero Confidence Result
**What:** OCR completely fails, confidence = 0%
**Expected:** Low confidence message (<80%) triggers
**Status:** ✅ Handled - Else clause catches all <80%

### Case 4: Missing Result Fields
**What:** Result dict missing `window_count` or `space_id`
**Expected:** Graceful degradation, stats just omitted
**Status:** ✅ Handled - `.get()` with defaults

### Case 5: Failed Operation Recording
**What:** Surveillance fails, stats should still update
**Expected:** Operation recorded with `success=False`
**Status:** ✅ Handled - `_record_surveillance_operation()` in all paths

---

## 📊 Metrics to Verify

After testing, check:

```python
# Access from handler instance:
handler.surveillance_stats

# Should show:
{
    'total_operations': 25,
    'successful_detections': 22,
    'god_mode_operations': 15,
    'apps_monitored': {'Terminal', 'Chrome', 'Safari', 'VSCode'},
    'total_windows_watched': 67,
    'fastest_detection_time': 2.3,
    'average_confidence': 0.91,
}
```

---

## 🎤 Voice Testing

For TTS integration:

1. **Record responses** during testing
2. **Play back via `say` command:**
   ```bash
   say -v Daniel "Found it, Derek! I detected Error in Chrome on Space 3."
   ```
3. **Check for:**
   - Natural pauses (commas, periods)
   - Rising intonation (question marks)
   - No weird TTS artifacts
   - Appropriate emphasis

---

## ✨ Success Criteria

Test is successful when:

✅ **All 17 test scenarios pass**
✅ **Milestones celebrate at 10, 25, 50, 100**
✅ **Learning acknowledgments appear for first app, first God Mode**
✅ **Confidence levels adapt responses appropriately**
✅ **Time-aware messaging works (test at different hours)**
✅ **God Mode shows statistics (window count, space ID)**
✅ **Single window mode is simple and direct**
✅ **Errors are helpful and non-technical**
✅ **No repetitive messages (variations work)**
✅ **Stats tracking works correctly**

---

## 🚀 Final Check

Before declaring production-ready:

```bash
# 1. Compile check
python3 -m py_compile backend/voice/intelligent_command_handler.py

# 2. Run test suite
python3 test_voice_god_mode.py

# 3. Live test with supervisor
python3 run_supervisor.py

# 4. Try 5+ different commands
# 5. Verify milestones work (mock counter if needed)
# 6. Check stats are accurate
```

---

## 📝 Notes

- **Milestone testing:** May need to mock `surveillance_stats['total_operations']` to test 100th operation quickly
- **Learning testing:** Clear `apps_monitored` set to re-test first app acknowledgment
- **Confidence testing:** Use different trigger text clarity levels
- **Time testing:** Mock `datetime.now().hour` for different times
- **God Mode testing:** Requires multiple windows of same app across spaces

---

🎉 **Happy Testing! JARVIS is ready to communicate like a sophisticated AI assistant.**
