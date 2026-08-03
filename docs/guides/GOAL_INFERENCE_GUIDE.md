# 🧠 JARVIS Goal Inference & Learning System

## Overview

The Goal Inference system enables JARVIS to understand your intentions, learn your patterns, and make intelligent predictive decisions automatically. It combines **PyTorch neural networks**, **ChromaDB embeddings**, and **SQLite** for a powerful, self-improving AI assistant.

## 🚀 Quick Start

### Interactive Startup (Recommended)

```bash
# Interactive menu - choose your preset
python start_system.py
```

**What happens:**
- 🎯 Shows interactive menu with all 5 presets
- 🔧 Select preset (or press Enter for 'balanced')
- ⚙️ Choose automation on/off
- ✅ Automatically creates configuration on first run
- ✅ Loads learning database with your patterns
- ✅ Displays previous session metrics

### Quick Start (Skip Interactive Menu)

```bash
# Start with specific preset (skips menu)
python start_system.py --goal-preset learning

# Start with aggressive mode + automation (skips menu)
python start_system.py --goal-preset aggressive --enable-automation

# Start with balanced mode (skips menu)
python start_system.py --goal-preset balanced
```

### Environment Variable Configuration

```bash
# Set preset via environment variable (skips menu)
export JARVIS_GOAL_PRESET=aggressive
export JARVIS_GOAL_AUTOMATION=true
python start_system.py

# Or one-liner (skips menu)
JARVIS_GOAL_PRESET=learning python start_system.py
```

## 📊 Configuration Presets

### 1. **Aggressive** 🔥
**Best for**: Quick learning and high productivity

```bash
python start_system.py --goal-preset aggressive
```

- Goal Confidence: **0.65** (lower threshold = more suggestions)
- Automation: **ENABLED**
- Pattern Boost: **0.10** (learns faster)
- Proactive Threshold: **0.75**

**When to use**: When you want JARVIS to be highly proactive and learn quickly from your behavior.

### 2. **Balanced** ⚖️ (Default)
**Best for**: Daily use with reliable suggestions

```bash
python start_system.py --goal-preset balanced
# Or just: python start_system.py (then press Enter for default)
```

- Goal Confidence: **0.75**
- Automation: **DISABLED**
- Pattern Boost: **0.05**
- Proactive Threshold: **0.85**

**When to use**: Recommended for most users. Good balance between accuracy and proactivity.

### 3. **Conservative** 🛡️
**Best for**: High-stakes environments requiring certainty

```bash
python start_system.py --goal-preset conservative
```

- Goal Confidence: **0.85** (high threshold = only very confident suggestions)
- Automation: **DISABLED**
- Pattern Boost: **0.02** (slow, careful learning)
- Proactive Threshold: **0.90**

**When to use**: When you want only the most confident predictions and minimal interruptions.

### 4. **Learning** 📚
**Best for**: First-time setup or teaching JARVIS new patterns

```bash
python start_system.py --goal-preset learning
```

- Min Samples for Pattern: **2** (learns from fewer examples)
- Pattern Boost: **0.10** (rapid learning)
- Feedback Weight: **0.15** (heavily weighted feedback)
- Exploration Rate: **0.2** (tries new actions)

**When to use**: During the first few days or when establishing new routines.

### 5. **Performance** ⚡
**Best for**: Maximum speed and responsiveness

```bash
python start_system.py --goal-preset performance
```

- Cache Size: **200 entries** (vs 100 default)
- Cache TTL: **600 seconds** (vs 300 default)
- Parallel Processing: **ENABLED**
- Resource Preloading: **ENABLED**

**When to use**: When you need the fastest possible response times and have plenty of RAM.

## 🎛️ Manual Configuration

You can still use the configuration tool for fine-grained control:

```bash
# View current settings
python configure_goal_inference.py --show

# Apply a preset
python configure_goal_inference.py --preset aggressive

# Change specific settings
python configure_goal_inference.py --set goal_inference.min_goal_confidence 0.8

# Enable automation
python configure_goal_inference.py --enable-automation

# Interactive mode (menu-driven)
python configure_goal_inference.py --interactive
```

## 📈 How It Works

### 1. **Goal Inference**
JARVIS observes your actions and context to infer high-level goals:

```
You open Calendar + Keynote at 9 AM
  ↓
JARVIS infers: "meeting_preparation" (confidence: 0.92)
  ↓
Suggests: Connect to "Living Room TV"
```

### 2. **Pattern Learning**
After seeing the same pattern 3 times, it becomes automatic:

```
Monday 9 AM: Connect Living Room TV ✓
Tuesday 9 AM: Connect Living Room TV ✓
Wednesday 9 AM: Connect Living Room TV ✓
  ↓
Pattern learned! (auto_connect enabled)
  ↓
Thursday 9 AM: JARVIS connects automatically
```

### 3. **Confidence Boosting**
Successful predictions increase pattern confidence:

```
Initial confidence: 0.70
After success 1: 0.73 (+0.03 adaptive boost)
After success 2: 0.75 (+0.02 adaptive boost)
After success 3: 0.77 (pattern strengthening)
```

## 🗄️ Learning Database

### Architecture

**Hybrid System**:
- **SQLite**: Structured data (goals, actions, patterns, preferences)
- **ChromaDB**: Embeddings for semantic similarity search
- **Adaptive Cache**: LRU cache with 70-90% hit rate

### Storage Location

```
~/.jarvis/learning/
  ├── jarvis_learning.db        # SQLite database
  └── chroma_embeddings/         # ChromaDB vector store
```

### Database Features

1. **Async Operations**: Non-blocking, concurrent access
2. **Batch Processing**: Flushes every 5 seconds or 100 items
3. **Smart Merging**: Automatically deduplicates similar patterns
4. **Auto-Optimization**: Vacuums and analyzes every hour
5. **Confidence Tracking**: Multiple boost strategies (LINEAR, EXPONENTIAL, LOGARITHMIC, ADAPTIVE)

## 📊 Monitoring & Metrics

### Startup Metrics

When JARVIS starts, you'll see:

```
✅ Goal Inference + Learning Database loaded
   • Goal Confidence: 0.75
   • Proactive Suggestions: True
   • Automation: False
   • Learning: True
   • Database Cache: 1000 entries
   • Previous session: 45 goals, 67 actions
   • Success rate: 89.2%
```

### Runtime Metrics API

Access metrics programmatically:

```python
from backend.intelligence.learning_database import get_learning_database

db = await get_learning_database()
metrics = await db.get_learning_metrics()

print(f"Total patterns: {metrics['patterns']['total_patterns']}")
print(f"Cache hit rate: {metrics['cache_performance']['pattern_cache_hit_rate']:.2%}")
```

## 🎯 Use Cases

### 1. Display Connection Prediction

**Scenario**: You connect to "Living Room TV" every morning at 9 AM for meetings.

**What happens**:
1. **Day 1-2**: JARVIS observes and logs the pattern
2. **Day 3**: Pattern recognized (frequency: 3)
3. **Day 4+**: Auto-connect suggestion appears (confidence: 85%)

**Configuration**: Use `learning` preset for faster pattern recognition.

### 2. Application Workspace Organization

**Scenario**: You always open Slack + VS Code + Terminal for coding sessions.

**What happens**:
1. JARVIS learns the app sequence
2. Infers goal: "development_session"
3. Suggests: Auto-arrange windows in optimal layout

**Configuration**: Use `aggressive` preset for quick workspace automation.

### 3. Context-Aware Assistance

**Scenario**: You're working on a presentation and need to share your screen.

**What happens**:
1. JARVIS detects: Keynote open + Calendar showing meeting in 10 min
2. Infers goal: "presentation_preparation" (confidence: 0.92)
3. Suggests: Connect to display + Enable Do Not Disturb

**Configuration**: Use `balanced` preset for reliable suggestions.

## 🔒 Safety Features

### Automation Limits

```json
"safety": {
  "require_confirmation_for_automation": true,
  "max_automation_actions_per_day": 50,
  "whitelist_actions": ["connect_display", "open_application"],
  "risk_tolerance": 0.5
}
```

### Fallback Behavior

- ❌ Low confidence (<0.70): No suggestion
- ⚠️ Medium confidence (0.70-0.85): Suggestion only
- ✅ High confidence (>0.85): Auto-execute if automation enabled

## 🧪 Testing

### Test the Database

```bash
cd backend/intelligence
python learning_database.py
```

Expected output:
```
🗄️ Testing Advanced JARVIS Learning Database
============================================================
✅ Stored goal: goal_1234567890_abc123
✅ Queued 5 goals for batch insert
✅ Stored action: action_1234567890_def456
✅ Learned display pattern
✅ Learned preference

📊 Learning Metrics:
   Total Goals: 6
   Total Actions: 1
   Total Patterns: 1
   Pattern Cache Hit Rate: 0.00%

🔍 Analyzed 1 patterns
✅ Advanced database test complete!
```

### Test Goal Inference Integration

```python
from backend.intelligence.goal_autonomous_uae_integration import get_integration

integration = get_integration()
result = await integration.process_command("connect to living room tv")

print(f"Goal inferred: {result['goal']}")
print(f"Confidence: {result['confidence']}")
print(f"Suggested action: {result['action']}")
```

## 📝 Configuration Reference

### Full Configuration Structure

```json
{
  "goal_inference": {
    "min_goal_confidence": 0.75,
    "max_active_goals": 10,
    "goal_timeout_minutes": 30,
    "pattern_learning_enabled": true
  },
  "autonomous_decisions": {
    "min_decision_confidence": 0.70,
    "enable_predictive_display": true,
    "auto_connect_threshold": 0.85,
    "learning_rate": 0.01,
    "exploration_rate": 0.1
  },
  "integration": {
    "enable_proactive_suggestions": true,
    "proactive_suggestion_threshold": 0.85,
    "enable_automation": false,
    "automation_threshold": 0.95
  },
  "learning": {
    "enabled": true,
    "min_samples_for_pattern": 3,
    "pattern_confidence_boost": 0.05,
    "success_rate_threshold": 0.7,
    "feedback_weight": 0.1
  },
  "performance": {
    "max_prediction_cache_size": 100,
    "cache_ttl_seconds": 300,
    "parallel_processing": true,
    "max_workers": 4
  }
}
```

## 🚨 Troubleshooting

### Issue: No patterns being learned

**Solution**:
```bash
# Use learning preset with lower thresholds
python start_system.py --goal-preset learning

# Or manually lower min_samples
python configure_goal_inference.py --set learning.min_samples_for_pattern 2
```

### Issue: Too many suggestions

**Solution**:
```bash
# Use conservative preset
python start_system.py --goal-preset conservative

# Or increase threshold
python configure_goal_inference.py --set integration.proactive_suggestion_threshold 0.90
```

### Issue: Database too large

**Solution**:
```python
from backend.intelligence.learning_database import get_learning_database

db = await get_learning_database()
await db.cleanup_old_patterns(days=30)  # Keep only last 30 days
await db.optimize()  # Vacuum and optimize
```

## 📚 Advanced Topics

### Custom Confidence Boost Strategy

```python
from backend.intelligence.learning_database import ConfidenceBoostStrategy

db = await get_learning_database()
await db.boost_pattern_confidence(
    pattern_id="pattern_123",
    boost=0.1,
    strategy=ConfidenceBoostStrategy.ADAPTIVE  # or LINEAR, EXPONENTIAL, LOGARITHMIC
)
```

### Pattern Analysis

```python
db = await get_learning_database()
patterns = await db.analyze_patterns()

for pattern in patterns:
    print(f"Pattern: {pattern['pattern_id']}")
    print(f"  Strength: {pattern['strength_score']:.2f}")
    print(f"  Should decay: {pattern['should_decay']}")
```

### Batch Pattern Storage

```python
# Queue patterns for batch insert (10x faster)
for pattern in patterns:
    await db.store_pattern(pattern, batch=True)

# Auto-flushes every 5 seconds or when batch reaches 100 items
```

## 🎓 Best Practices

1. **Start with `learning` preset** for the first week
2. **Switch to `balanced`** after JARVIS has learned your routines
3. **Use `aggressive`** only if you want maximum automation
4. **Review metrics** weekly to see learning progress
5. **Clear old patterns** monthly to keep database lean

## 🤝 Contributing

The Goal Inference system is designed to be extensible:

- **Add new pattern types**: Extend `PatternType` enum
- **Add new boost strategies**: Extend `ConfidenceBoostStrategy` enum
- **Add new presets**: Modify `_apply_preset_to_config()` in main.py
- **Custom metrics**: Add to `get_learning_metrics()` in learning_database.py

## 📄 License

Part of the JARVIS AI Assistant project.

---

**Questions?** Check the logs at `~/.jarvis/logs/` or run with `--debug` flag for verbose output.
