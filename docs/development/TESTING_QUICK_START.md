# 🚀 Quick Start: Testing Feedback Learning & Command Safety

**5-minute verification that everything works**

---

## Step 1: Run Automated Tests (2 minutes)

```bash
cd /Users/derekjrussell/Documents/repos/JARVIS-AI-Agent

# Run the live test suite
python -m backend.examples.live_feedback_test
```

**✅ Expected output:**
```
✓ Feedback Loop Basic: PASSED
✓ Command Safety: PASSED
✓ Terminal Intelligence: PASSED
✓ Persistence: PASSED
✓ Integration: PASSED

Results: 5/6 passed
✨ SUCCESS! All tests passed!
```

---

## Step 2: Run Interactive Demo (3 minutes)

```bash
python -m backend.examples.demo_feedback_and_safety
```

**You'll see:**
- Demo 1: Feedback learning in action
- Demo 2: Command safety classification
- Demo 3: Terminal error analysis
- Demo 4: Complete integrated workflow

**✅ Success if:** Demo completes without errors

---

## Step 3: Verify Files Exist

```bash
# Check that all new files were created
ls -lh backend/core/learning/feedback_loop.py
ls -lh backend/system_control/command_safety.py
ls -lh backend/vision/handlers/terminal_command_intelligence.py
ls -lh backend/vision/intelligence/feedback_aware_vision.py
```

**✅ Success if:** All 4 files exist

---

## Quick Manual Test (Optional)

### Test Feedback Loop

```python
python3 -c "
import asyncio
from backend.core.learning.feedback_loop import *
from pathlib import Path

async def test():
    loop = FeedbackLearningLoop(storage_path=Path('/tmp/test.json'))
    await loop.record_feedback(
        NotificationPattern.TERMINAL_ERROR,
        UserResponse.ENGAGED,
        'Test',
        {}
    )
    print('✓ Feedback loop works!')

asyncio.run(test())
"
```

### Test Command Safety

```python
python3 -c "
from backend.system_control.command_safety import get_command_classifier

c = get_command_classifier()
r = c.classify('rm -rf /tmp')
print(f'✓ Command safety works! (rm -rf = {r.tier.value} tier)')
"
```

### Test Terminal Intelligence

```python
python3 -c "
import asyncio
from backend.vision.handlers.terminal_command_intelligence import get_terminal_intelligence

async def test():
    intel = get_terminal_intelligence()
    ctx = await intel.analyze_terminal_context('Error: ModuleNotFoundError')
    print(f'✓ Terminal intelligence works! (found {len(ctx.errors)} error)')

asyncio.run(test())
"
```

---

## What to Test with Live JARVIS

**Once JARVIS is running:**

### 1. Trigger a Terminal Error
```bash
# In a visible terminal
python -c "import nonexistent_module"
```

**Expected:** JARVIS detects error, offers help

### 2. Respond to JARVIS
```
You: "yes" or "what does it say?"
```

**Expected:** JARVIS suggests `pip install nonexistent_module` with safety warning

### 3. Check Feedback Was Recorded
```bash
cat ~/.jarvis/learning/feedback.json | python -m json.tool | tail -20
```

**Expected:** See recent feedback event

---

## Success Criteria ✅

### Systems are working if:

- [ ] Automated tests pass (5/6 or 6/6)
- [ ] Demo runs without errors
- [ ] All 4 new files exist
- [ ] Feedback loop records events
- [ ] Commands are classified correctly
- [ ] Terminal errors generate suggestions
- [ ] Data persists to `~/.jarvis/learning/feedback.json`

---

## Troubleshooting

### "Module not found" error
```bash
# Make sure you're in the right directory
cd /Users/derekjrussell/Documents/repos/JARVIS-AI-Agent

# Check Python path
export PYTHONPATH="${PWD}:${PYTHONPATH}"
```

### "Permission denied" for feedback.json
```bash
mkdir -p ~/.jarvis/learning
chmod 755 ~/.jarvis/learning
```

### Tests fail
```bash
# Run pytest for detailed errors
pytest backend/tests/test_feedback_learning_and_safety.py -v -s
```

---

## Next Steps

Once verified working:

1. ✅ **Read the docs:**
   - `docs/FEEDBACK_LEARNING_AND_COMMAND_SAFETY.md` (comprehensive)
   - `docs/MANUAL_TESTING_GUIDE.md` (detailed testing)
   - `docs/INTEGRATION_GUIDE_FEEDBACK_AND_SAFETY.md` (how to integrate)

2. ✅ **Integrate with JARVIS:**
   - Follow integration guide
   - Add to `main.py` initialization
   - Test with live terminal errors

3. ✅ **Monitor in production:**
   - Watch `~/.jarvis/learning/feedback.json`
   - Check learned patterns
   - Verify suppression works

---

## Quick Reference

### Check Learning Data
```bash
cat ~/.jarvis/learning/feedback.json | python -m json.tool | less
```

### Reset Learning
```bash
rm ~/.jarvis/learning/feedback.json
# Or use API: await loop.reset_learning()
```

### View Pattern Insights
```python
from backend.core.learning.feedback_loop import get_feedback_loop, NotificationPattern
loop = get_feedback_loop()
print(loop.get_pattern_insights(NotificationPattern.TERMINAL_ERROR))
```

### Classify a Command
```python
from backend.system_control.command_safety import get_command_classifier
c = get_command_classifier()
r = c.classify("YOUR_COMMAND_HERE")
print(f"{r.tier.value}: {r.reasoning}")
```

---

**Total Testing Time:** 5-15 minutes
**Status:** ✅ Ready to test
**Support:** See docs/ for detailed guides

