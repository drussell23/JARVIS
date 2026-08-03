# Goal Inference + Autonomous Decision Engine - User Guide

## 🎯 What This Does For You

Instead of just executing commands, JARVIS now:
1. **Predicts what you need** before you ask
2. **Learns your patterns** and automates them
3. **Proactively suggests actions** based on context
4. **Connects displays faster** through prediction

---

## 🚀 How to Activate

### Option 1: Integrate with Unified Command Processor

Add this to your main JARVIS initialization:

```python
# In your main.py or jarvis startup
from backend.intelligence.goal_autonomous_uae_integration import get_integration

# Initialize the integration
goal_autonomous_integration = get_integration()

# Use it in your command flow
async def process_command(command, context):
    # First, check for predictive suggestions
    decisions = await goal_autonomous_integration.process_context(context)

    if decisions:
        # Present proactive suggestions to user
        for decision in decisions:
            if decision.integrated_confidence > 0.8:
                print(f"JARVIS: {decision.reasoning}")
                print(f"         Shall I {decision.action.action_type}?")

    # Then process the actual command normally
    # ... your existing command processing
```

### Option 2: Add to Voice Command Handler

```python
# In backend/voice/jarvis_agent_voice.py

from backend.intelligence.goal_autonomous_uae_integration import get_integration

class JARVISVoice:
    def __init__(self):
        self.integration = get_integration()

    async def process_voice_command(self, command, context):
        # Check for predictive display connection
        if "tv" in command.lower() or "display" in command.lower():
            display_prediction = await self.integration.predict_display_connection(context)

            if display_prediction:
                # Execute immediately (already predicted)
                await self.integration.execute_decision(display_prediction, context)
                return f"Connected to {display_prediction.action.target}, sir. I anticipated your request."

        # Continue with normal processing
        # ...
```

---

## 👀 What You'll See as a User

### **Scenario 1: Meeting Preparation**

**Before (without integration):**
```
You: "connect to living room tv"
JARVIS: *connects in 0.7s*
JARVIS: "Connected to Living Room TV"
```

**After (with integration):**
```
[2:55 PM - You open Keynote for 3:00 PM meeting]

JARVIS: "Sir, I notice you have a team meeting in 5 minutes.
         Your presentation is open in Keynote.
         Shall I connect to Living Room TV?"

You: "yes"

JARVIS: *connects in 0.2s*
JARVIS: "Living Room TV connected, sir. Your presentation is ready."
```

### **Scenario 2: Daily Standup Pattern**

**Week 1:**
```
Monday 9:00 AM
You: "living room tv"
JARVIS: *connects*
```

**Week 2:**
```
Monday 8:58 AM
JARVIS: "Sir, it's almost 9:00 AM. Your daily standup is in 2 minutes.
         Shall I connect to Living Room TV?"

You: "yes please"
JARVIS: *connects instantly*
```

**Week 3:**
```
Monday 8:58 AM
JARVIS: "Sir, I've noticed you connect to Living Room TV every Monday
         at 9 AM for your standup. Would you like me to do this
         automatically?"

You: "yes, automate it"
JARVIS: "Understood. I'll connect automatically from now on, sir."

[Next Monday, 8:59 AM]
JARVIS: "Automatically connecting to Living Room TV for your standup, sir."
*connects without asking*
```

### **Scenario 3: Context-Aware Defaults**

**Before:**
```
You: "connect display"
JARVIS: "Which display would you like to connect to?"
You: "living room tv"
JARVIS: *connects*
```

**After:**
```
[You have Keynote + Calendar meeting in context]

You: "connect display"
JARVIS: "Connecting to Living Room TV based on your presentation
         preparation, sir."
*connects instantly*
```

---

## 🔍 How to Verify It's Working

### 1. Check the Logs

Look for these messages in your JARVIS logs:

```bash
# Enable debug logging
export JARVIS_LOG_LEVEL=DEBUG

# Run JARVIS and look for:
[Goal Inference] Inferred goals: meeting_preparation (confidence: 0.92)
[Autonomous Engine] Generated 3 decisions from goals
[Integration] Predictive display connection: Living Room TV (confidence: 0.89)
[UAE] High confidence element position for Living Room TV
[Integration] Executing integrated decision: connect_display
```

### 2. Monitor Metrics

Add this to your code to see metrics:

```python
from backend.intelligence.goal_autonomous_uae_integration import get_integration

integration = get_integration()
metrics = integration.get_metrics()

print(f"Goals Inferred: {metrics['goals_inferred']}")
print(f"Predictions Made: {metrics['total_predictions']}")
print(f"Accuracy: {metrics['prediction_accuracy']:.0%}")
```

### 3. Test Scenarios

Try these test scenarios:

```bash
# Scenario 1: Meeting preparation
1. Open Calendar app
2. Create a meeting in 10 minutes
3. Open Keynote or PowerPoint
4. Wait 30 seconds
5. JARVIS should suggest display connection

# Scenario 2: Pattern learning
1. Connect to TV at the same time for 3 days in a row
2. On day 4, JARVIS should offer to automate
3. Accept automation
4. Day 5 should auto-connect

# Scenario 3: Context understanding
1. Just say "display" without specifying which one
2. JARVIS should choose Living Room TV if context matches
```

---

## 📊 Understanding the Confidence Scores

The system uses confidence scores to decide when to act:

| Confidence | Behavior |
|------------|----------|
| **95%+** | Auto-execute (if user allowed automation) |
| **85-95%** | Proactive suggestion with high confidence |
| **75-85%** | Suggest, but wait for confirmation |
| **60-75%** | Mention as possibility |
| **<60%** | Don't suggest (too uncertain) |

---

## 🎓 Training the System

The system learns from:

1. **Your patterns**: When you connect, what apps are open, time of day
2. **Your confirmations**: When you say "yes" to suggestions
3. **Your corrections**: When you say "no" or choose different option
4. **Your feedback**: Explicit feedback like "don't do this again"

To train it faster:

```python
# After an action, provide feedback
from backend.intelligence.goal_autonomous_uae_integration import get_integration

integration = get_integration()

# If JARVIS did something good
integration.autonomous_engine.learn_from_feedback(
    action,
    success=True,
    user_feedback="This was exactly what I needed"
)

# If JARVIS was wrong
integration.autonomous_engine.learn_from_feedback(
    action,
    success=False,
    user_feedback="Don't connect display during meetings"
)
```

---

## 🔧 Configuration

Create `backend/config/integration_config.json`:

```json
{
  "min_goal_confidence": 0.75,
  "min_decision_confidence": 0.70,
  "enable_predictive_display": true,
  "max_concurrent_goals": 10,
  "learning_enabled": true,
  "auto_connect_threshold": 0.85,
  "proactive_suggestions": true,
  "automation_allowed": false
}
```

Adjust these settings:
- `min_goal_confidence`: How sure must we be about a goal? (lower = more suggestions, higher = fewer but more accurate)
- `auto_connect_threshold`: How confident before auto-executing? (higher = safer)
- `automation_allowed`: Allow full automation after learning patterns?

---

## 🐛 Troubleshooting

### "No proactive suggestions appearing"

**Check:**
1. Is `enable_predictive_display` true in config?
2. Are confidence thresholds too high?
3. Is the context rich enough? (need apps, time info, etc.)
4. Check logs for goal inference results

**Fix:**
```python
# Lower the thresholds temporarily
integration.config['min_goal_confidence'] = 0.65
integration.config['auto_connect_threshold'] = 0.75
```

### "Wrong display suggested"

**Cause:** System hasn't learned your preferences yet

**Fix:**
```python
# Teach it your preference
integration.autonomous_engine.learned_patterns['display_preferences'] = {
    'meeting_preparation': 'Living Room TV',
    'project_completion': 'External Monitor',
    'presentation': 'Living Room TV'
}
```

### "Too many suggestions"

**Fix:**
```python
# Increase confidence thresholds
integration.config['min_goal_confidence'] = 0.85
integration.config['min_decision_confidence'] = 0.80
```

---

## 📈 Expected Improvements Over Time

| Week | Expected Behavior |
|------|-------------------|
| **Week 1** | Basic goal inference, occasional suggestions |
| **Week 2** | Pattern recognition starts, more relevant suggestions |
| **Week 3** | Learns your daily routines, offers automation |
| **Week 4** | High accuracy predictions, faster responses |
| **Month 2** | Proactive automation, anticipates your needs |
| **Month 3** | Fully integrated into workflow, seamless operation |

---

## 🎯 Key Differences: Before vs After

| Aspect | Before | After |
|--------|--------|-------|
| **Speed** | 0.7s | <0.3s (predicted) |
| **Proactivity** | None | Suggests before you ask |
| **Learning** | Static | Learns and improves |
| **Context** | Command-based | Goal-based understanding |
| **Automation** | Manual every time | Can automate patterns |
| **Intelligence** | Reactive | Predictive + Adaptive |

---

## ✅ Success Indicators

You'll know it's working when:

✓ JARVIS suggests display connection before you ask
✓ Connections happen noticeably faster
✓ Suggestions are contextually relevant
✓ System learns and remembers your patterns
✓ Automation offers appear after repeated actions
✓ Fewer words needed ("display" instead of "connect to living room tv")
✓ JARVIS explains reasoning ("based on your meeting...")

---

## 🚀 Next Steps

1. **Activate** the integration in your main JARVIS flow
2. **Use it** normally for a few days
3. **Watch** for proactive suggestions
4. **Confirm** good suggestions to reinforce learning
5. **Provide feedback** on wrong predictions
6. **Enable automation** once accuracy is high

The more you use it, the smarter it gets! 🧠✨
