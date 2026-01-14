# Quick Start: Advanced Training System

## 🚀 One Command to Rule Them All

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
python3 run_supervisor.py
```

This single command:
- ✅ Starts JARVIS Core
- ✅ Launches JARVIS Prime (if not running)
- ✅ Launches Reactor Core (if not running)
- ✅ Connects all 3 repos
- ✅ Enables automatic training

---

## 📋 What Works Right Now

### ✅ In JARVIS (100% Complete)
- Experience collection from all interactions
- Experience forwarding to Reactor Core
- Auto-trigger when buffer >= 100 experiences
- Advanced Training Coordinator with resource negotiation
- Distributed locking (prevents OOM)
- Training priority queue (voice > NLU > vision > embeddings)
- Streaming status monitoring
- Model deployment

### ⚠️ In Reactor Core (Needs Implementation)
Reactor Core must implement the API endpoints. See:
- `REACTOR_CORE_API_SPECIFICATION.md` for complete details
- `ADVANCED_TRAINING_SYSTEM_SUMMARY.md` for architecture

---

## 🔧 Environment Configuration

```bash
# In ~/.bashrc or ~/.zshrc

# Repo paths (auto-detected if in standard locations)
export JARVIS_PRIME_PATH=~/Documents/repos/jarvis-prime
export REACTOR_CORE_PATH=~/Documents/repos/reactor-core

# Ports
export JARVIS_PRIME_PORT=8002
export REACTOR_CORE_PORT=8003

# Enable/disable repos
export JARVIS_PRIME_ENABLED=true
export REACTOR_CORE_ENABLED=true

# Resource management (prevents OOM)
export MAX_TOTAL_MEMORY_GB=64
export TRAINING_MEMORY_RESERVE_GB=40
export JPRIME_MEMORY_THRESHOLD_GB=20

# Training configuration
export MAX_CONCURRENT_TRAINING_JOBS=1
export TRAINING_LOCK_TTL=7200  # 2 hours
export CHECKPOINT_INTERVAL_EPOCHS=10

# A/B testing
export AB_TEST_ENABLED=true
export AB_TEST_INITIAL_PERCENTAGE=10
export ROLLOUT_STEPS=10,25,50,75,100
```

---

## 📊 Monitor Training

```bash
# View JARVIS logs
tail -f logs/jarvis*.log | grep -E "Training|Coordinator"

# Check all repos health
curl http://localhost:5001/health      # JARVIS
curl http://localhost:8002/health      # J-Prime
curl http://localhost:8003/api/health  # Reactor Core

# Stream training status (once Reactor Core implements it)
curl -N http://localhost:8003/api/training/stream/{job_id}
```

---

## 🐛 Troubleshooting

### Training doesn't start
1. **Check Reactor Core running:**
   ```bash
   curl http://localhost:8003/api/health
   ```

2. **Check experience files:**
   ```bash
   ls -la ~/.jarvis/trinity/events/
   ```

3. **Check training buffer:**
   ```bash
   # View JARVIS logs for "buffer_size"
   grep "buffer_size" logs/jarvis*.log
   ```

### OOM during training
1. **Check resource configuration:**
   ```bash
   echo $MAX_TOTAL_MEMORY_GB
   echo $TRAINING_MEMORY_RESERVE_GB
   ```

2. **Check J-Prime memory usage:**
   ```bash
   cat ~/.jarvis/cross_repo/prime_state.json | jq '.memory_usage_gb'
   ```

3. **Verify resource manager waiting:**
   ```bash
   grep "Waiting for J-Prime" logs/jarvis*.log
   ```

---

## 📚 Documentation

- `ADVANCED_TRAINING_SYSTEM_SUMMARY.md` - Complete architecture & features
- `REACTOR_CORE_API_SPECIFICATION.md` - API contract for Reactor Core
- `README.md` - 4-repo ecosystem overview
- `README_v2.md` - Technical reference

---

## ✅ Success Criteria

When everything is working:

```
# Terminal 1: Start supervisor
python3 run_supervisor.py

# Output:
======================================================================
Cross-Repo Startup Orchestration v1.0
======================================================================

📍 PHASE 1: JARVIS Core (starting via supervisor)
✅ JARVIS Core initialization in progress...

📍 PHASE 2: External repos startup (parallel)
  → Probing J-Prime...
✅ J-Prime healthy

  → Probing Reactor-Core...
✅ Reactor-Core healthy

📍 PHASE 3: Integration verification
✅ Cross-repo orchestration complete: 3/3 repos operational
✅ All repos operational - FULL MODE

======================================================================
🎯 Startup Summary:
  JARVIS Core:   ✅ Running
  J-Prime:       ✅ Running
  Reactor-Core:  ✅ Running
======================================================================

# Auto-trigger checks buffer every 5 minutes
[2026-01-14 15:30:00] Buffer size: 150 experiences
[2026-01-14 15:30:00] Creating training job: voice (priority: CRITICAL)
[2026-01-14 15:30:01] Acquiring distributed lock...
[2026-01-14 15:30:01] Reserving training slot (40GB required)...
[2026-01-14 15:30:02] J-Prime idle, resources available
[2026-01-14 15:30:02] Calling Reactor Core: POST /api/training/start
[2026-01-14 15:30:03] Training started: job_id=abc123
[2026-01-14 15:30:10] Epoch 1/50: Loss=0.5, Accuracy=0.85
[2026-01-14 15:30:20] Epoch 2/50: Loss=0.3, Accuracy=0.90
...
[2026-01-14 15:40:00] Training completed: v1.2.4, Loss=0.05, Accuracy=0.98
[2026-01-14 15:40:01] Deploying model with gradual rollout (10% → 100%)
[2026-01-14 15:40:02] ✅ Model deployed successfully
```

---

**All documentation**: See `ADVANCED_TRAINING_SYSTEM_SUMMARY.md`
