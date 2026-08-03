# 🚀 JARVIS Quick Start Guide

## The Simple Way: Just Run It! ✨

```bash
python start_system.py
```

**That's it!** JARVIS automatically:
1. 🤖 **Detects the best preset** based on your usage history
2. 🧠 **Learns from your patterns** and adapts over time
3. ⚙️ **Smart automation** - enables when you're ready (80%+ success rate)
4. 🚀 **No configuration needed** - works perfectly out of the box!

---

## How Auto-Detection Works

JARVIS intelligently chooses the best configuration for you:

### 🆕 First Run (No Database)
```
🎯 Auto-detected Goal Inference Preset: learning
   → First run detected, using 'learning' preset for fast adaptation
⚠️ Goal Inference Automation: DISABLED
```
**Why?** You're new - JARVIS needs to learn your patterns first!

### 📚 Early Learning Phase (< 50 goals)
```
🎯 Auto-detected Goal Inference Preset: learning
   → Early learning phase (27 goals), using 'learning' preset
⚠️ Goal Inference Automation: DISABLED
```
**Why?** Still learning - fast adaptation mode for quick pattern recognition.

### ⚖️ Building Patterns (50-200 goals, < 10 patterns)
```
🎯 Auto-detected Goal Inference Preset: balanced
   → Building patterns (7 patterns), using 'balanced' preset
⚠️ Goal Inference Automation: DISABLED
```
**Why?** Patterns emerging - balanced mode for reliability.

### 🔥 Experienced User (20+ patterns)
```
🎯 Auto-detected Goal Inference Preset: aggressive
   → Experienced user (23 patterns), using 'aggressive' preset
   → High pattern success (87%), automation recommended
✓ Goal Inference Automation: ENABLED
```
**Why?** You're experienced - JARVIS trusts your patterns and enables automation!

---

## Override Auto-Detection (Advanced Users)

Want to manually choose a preset? Just use flags:

```bash
# Specific preset
python start_system.py --goal-preset learning

# With automation enabled
python start_system.py --goal-preset aggressive --enable-automation

# With other flags
python start_system.py --goal-preset balanced --no-browser --verbose
```

---

## Set Permanent Defaults

Add to `~/.bashrc` or `~/.zshrc`:

```bash
export JARVIS_GOAL_PRESET=balanced
export JARVIS_GOAL_AUTOMATION=false
```

Then just run:
```bash
python start_system.py
```

---

## Preset Quick Reference

| Preset | Best For | Confidence | Learning Speed | Automation Default |
|--------|----------|------------|----------------|-------------------|
| **aggressive** | Power users, quick learning | 0.65 | Fast ⚡ | ✅ ON |
| **balanced** | Daily use (default) | 0.75 | Normal | ❌ OFF |
| **conservative** | High-stakes work | 0.85 | Slow 🐌 | ❌ OFF |
| **learning** | First week usage | 0.75 | Fast ⚡ | ❌ OFF |
| **performance** | Speed-critical | 0.75 | Normal | ❌ OFF |

---

## What Gets Started?

JARVIS includes **10 core components** + **6 intelligent systems**:

### Core Components
1. **CHATBOTS** - Claude Vision AI
2. **VISION** - Multi-Space Desktop Monitoring
3. **MEMORY** - M1-optimized memory management
4. **VOICE** - Voice activation ("Hey JARVIS")
5. **ML_MODELS** - NLP and sentiment analysis
6. **MONITORING** - System health tracking
7. **VOICE_UNLOCK** - Advanced Screen Unlock
8. **WAKE_WORD** - Hands-free activation
9. **DISPLAY_MONITOR** - External Display Management
10. **GOAL_INFERENCE** - ML-Powered Goal Understanding ⭐

### Intelligent Systems v2.0
1. TemporalQueryHandler v3.0
2. ErrorRecoveryManager v2.0
3. StateIntelligence v2.0
4. StateDetectionPipeline v2.0
5. ComplexComplexityHandler v2.0
6. PredictiveQueryHandler v2.0

---

## Need More Details?

- **Full Usage Examples**: See [STARTUP_EXAMPLES.md](STARTUP_EXAMPLES.md)
- **Goal Inference Guide**: See [GOAL_INFERENCE_GUIDE.md](../guides/GOAL_INFERENCE_GUIDE.md)
- **Configuration**: Run `python configure_goal_inference.py --interactive`

---

## First Time Setup

1. **Run JARVIS**:
   ```bash
   python start_system.py
   ```

2. **Select "learning" preset** (Option 4)
   - This helps JARVIS learn your patterns quickly

3. **Keep automation OFF** for the first week
   - Review suggestions to build trust

4. **After 1-2 weeks**, switch to "balanced":
   ```bash
   python start_system.py --goal-preset balanced
   ```

5. **Enable automation** when ready:
   ```bash
   python start_system.py --goal-preset aggressive --enable-automation
   ```

---

## All Methods in One Place

### Method 1: Automatic (Recommended) 🤖
```bash
python start_system.py
```
✅ **Auto-detects best preset** → Smart automation → Zero configuration

### Method 2: Command-Line Override
```bash
python start_system.py --goal-preset learning --enable-automation
```
✅ **Manual override** → Uses specified preset → Skips auto-detection

### Method 3: Environment Variables (Permanent Override)
```bash
export JARVIS_GOAL_PRESET=balanced
python start_system.py
```
✅ **Permanent setting** → Skips auto-detection every time

---

## Everything is Intelligent & Automatic! 🎉

No configuration needed - JARVIS adapts to YOU!

- 🤖 **Fully automatic** - Detects best preset from your usage
- 🧠 **Self-learning** - Adapts as you use it more
- ⚙️ **Smart automation** - Enables when you're ready (80%+ success)
- 🔧 **Manual override** - Use `--goal-preset` if you want control
- 📊 **Experience-based** - Different presets as you progress
- ✅ **Zero configuration** - Works perfectly out of the box

**Intelligent. Adaptive. Autonomous.** 🚀
