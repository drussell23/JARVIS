# Reactor Core API Specification v2.0

**Status:** Production-Grade Contract
**Last Updated:** January 14, 2026
**Version:** 2.0.0

This document defines the complete API contract between JARVIS and Reactor Core for training coordination, experience ingestion, and model deployment.

---

## 🎯 Overview

Reactor Core is a separate repository that handles:
1. **Experience ingestion** from JARVIS via file system
2. **Training execution** triggered by JARVIS API calls
3. **Model deployment** with version management
4. **Status streaming** via Server-Sent Events (SSE)

---

## 📡 API Endpoints

### Base URL
```
http://localhost:8090  (configurable via REACTOR_CORE_PORT)
```

###

 Required Environment Variables
```bash
# In Reactor Core
REACTOR_CORE_PORT=8090
REACTOR_CORE_HOST=0.0.0.0
EXPERIENCE_DIR=~/.jarvis/trinity/events
CHECKPOINT_DIR=~/.jarvis/training_checkpoints
MODEL_REGISTRY_DIR=~/.jarvis/models
```

---

## 1️⃣ Training API

### `POST /api/training/start`

Start a new training job.

**Request:**
```json
{
  "job_id": "uuid-string",
  "model_type": "voice" | "nlu" | "vision" | "embedding",
  "experiences": [
    {
      "type": "voice_auth" | "command" | "vision" | "interaction",
      "input": "any",
      "expected_output": "any",
      "actual_output": "any",
      "success": true,
      "confidence": 0.95,
      "metadata": {},
      "timestamp": 1736895345.123
    }
  ],
  "config": {
    "learning_rate": 0.001,
    "batch_size": 32,
    "model_architecture": "custom"
  },
  "epochs": 10,
  "checkpoint_enabled": true,
  "checkpoint_interval": 10
}
```

**Response (200 OK):**
```json
{
  "job_id": "uuid-string",
  "status": "started",
  "message": "Training job started successfully",
  "estimated_duration_seconds": 600
}
```

**Response (503 Service Unavailable):**
```json
{
  "error": "Another training job is running",
  "active_job_id": "other-uuid",
  "retry_after_seconds": 300
}
```

**Response (400 Bad Request):**
```json
{
  "error": "Invalid request",
  "details": "Missing required field: experiences"
}
```

---

### `GET /api/training/stream/{job_id}`

Stream real-time training status updates via Server-Sent Events (SSE).

**Response (SSE Stream):**
```
event: status
data: {"job_id": "uuid", "status": "training", "epoch": 1, "total_epochs": 10, "loss": 0.5, "accuracy": 0.85}

event: status
data: {"job_id": "uuid", "status": "training", "epoch": 2, "total_epochs": 10, "loss": 0.3, "accuracy": 0.90}

event: checkpoint
data: {"job_id": "uuid", "epoch": 10, "checkpoint_path": "/path/to/checkpoint", "metrics": {"loss": 0.1}}

event: completed
data: {"job_id": "uuid", "status": "completed", "model_version": "v1.2.4", "metrics": {"loss": 0.05, "accuracy": 0.98}}
```

**Event Types:**
- `status` - Training progress update
- `checkpoint` - Checkpoint saved
- `completed` - Training completed successfully
- `failed` - Training failed
- `cancelled` - Training cancelled

---

### `GET /api/training/status/{job_id}`

Get current status of a training job (non-streaming).

**Response (200 OK):**
```json
{
  "job_id": "uuid-string",
  "status": "training" | "completed" | "failed" | "cancelled",
  "epoch": 5,
  "total_epochs": 10,
  "loss": 0.2,
  "accuracy": 0.92,
  "metrics": {
    "loss": 0.2,
    "accuracy": 0.92,
    "val_loss": 0.25,
    "val_accuracy": 0.90
  },
  "model_version": null,  // Set when completed
  "error": null,  // Set when failed
  "started_at": 1736895345.123,
  "completed_at": null
}
```

---

### `POST /api/training/cancel/{job_id}`

Cancel a running training job.

**Response (200 OK):**
```json
{
  "job_id": "uuid-string",
  "status": "cancelled",
  "message": "Training job cancelled successfully"
}
```

**Response (404 Not Found):**
```json
{
  "error": "Training job not found"
}
```

---

## 2️⃣ Model Deployment API

### `POST /api/models/deploy`

Deploy a trained model.

**Request:**
```json
{
  "model_version": "v1.2.4",
  "model_type": "voice" | "nlu" | "vision" | "embedding",
  "strategy": "immediate" | "ab_test" | "gradual_rollout" | "canary" | "blue_green",
  "config": {
    "initial_percentage": 10,
    "rollout_steps": [10, 25, 50, 75, 100],
    "rollback_on_error_rate": 0.05,
    "monitor_duration_seconds": 300,
    "auto_rollback": true
  }
}
```

**Response (200 OK):**
```json
{
  "deployment_id": "deploy-uuid",
  "model_version": "v1.2.4",
  "status": "deploying",
  "current_percentage": 10,
  "message": "Model deployment started"
}
```

---

### `POST /api/models/rollback`

Rollback to previous model version.

**Request:**
```json
{
  "model_type": "voice",
  "previous_version": "v1.2.3"
}
```

**Response (200 OK):**
```json
{
  "model_type": "voice",
  "rolled_back_to": "v1.2.3",
  "message": "Model rolled back successfully"
}
```

---

## 3️⃣ Experience Ingestion API

### File-Based Ingestion (Primary Method)

Reactor Core watches `~/.jarvis/trinity/events/` for experience files.

**File Format:**
```bash
~/.jarvis/trinity/events/
├── experiences_voice_1736895345.json
├── experiences_nlu_1736895346.json
└── experiences_vision_1736895347.json
```

**File Content:**
```json
{
  "batch_id": "uuid-string",
  "model_type": "voice",
  "experiences": [
    {
      "type": "voice_auth",
      "input": {"audio_embedding": [0.1, 0.2, ...]},
      "expected_output": {"user": "Derek J. Russell"},
      "actual_output": {"user": "Derek J. Russell"},
      "success": true,
      "confidence": 0.95,
      "metadata": {"duration_ms": 250, "snr_db": 16.2},
      "timestamp": 1736895345.123
    }
  ],
  "created_at": 1736895345.123
}
```

**Reactor Core Implementation:**
```python
# reactor_core/integration/trinity_experience_receiver.py

import asyncio
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class TrinityExperienceReceiver(FileSystemEventHandler):
    """Watches ~/.jarvis/trinity/events/ for experience files."""

    def __init__(self, experience_dir: Path):
        self.experience_dir = experience_dir
        self.observer = Observer()

    async def start(self):
        """Start watching directory."""
        self.observer.schedule(self, str(self.experience_dir), recursive=False)
        self.observer.start()

    def on_created(self, event):
        """Handle new experience file."""
        if event.is_directory or not event.src_path.endswith('.json'):
            return

        asyncio.create_task(self._process_experience_file(event.src_path))

    async def _process_experience_file(self, file_path: str):
        """Process experience file and add to buffer."""
        try:
            async with aiofiles.open(file_path, 'r') as f:
                data = json.loads(await f.read())

            # Add experiences to training buffer
            from reactor_core.training.experience_buffer import ExperienceBuffer
            buffer = ExperienceBuffer.get_instance()

            for exp in data["experiences"]:
                await buffer.add_experience(exp)

            # Delete processed file
            Path(file_path).unlink()

        except Exception as e:
            logger.error(f"Error processing experience file: {e}")
```

---

## 4️⃣ Health & Status API

### `GET /api/health`

Check Reactor Core health.

**Response (200 OK):**
```json
{
  "status": "healthy",
  "uptime_seconds": 3600,
  "active_training_jobs": 0,
  "experience_buffer_size": 150,
  "models_deployed": {
    "voice": "v1.2.3",
    "nlu": "v2.1.0",
    "vision": "v1.0.5"
  }
}
```

---

### `GET /api/resources`

Get resource usage.

**Response (200 OK):**
```json
{
  "memory_usage_gb": 12.5,
  "memory_total_gb": 64.0,
  "cpu_percent": 25.0,
  "gpu_memory_gb": 0.0,
  "gpu_utilization_percent": 0.0,
  "active_training_jobs": 0
}
```

---

## 5️⃣ Trinity Integration (Cross-Repo)

### State File Updates

Reactor Core must update `~/.jarvis/cross_repo/reactor_state.json`:

```json
{
  "status": "healthy" | "busy" | "degraded" | "failed",
  "last_update": "2026-01-14T15:30:45Z",
  "active_training_jobs": 0,
  "memory_usage_gb": 12.5,
  "cpu_percent": 25.0,
  "models_deployed": {
    "voice": "v1.2.3",
    "nlu": "v2.1.0"
  },
  "last_heartbeat": 1736895345.123
}
```

---

## 📦 Implementation Checklist

### JARVIS (This Repo) - ✅ Complete
- [x] Advanced Training Coordinator
- [x] Reactor Core API Client
- [x] Resource Manager (OOM prevention)
- [x] Distributed lock coordination
- [x] Experience forwarding to file system
- [x] Streaming status consumption

### Reactor Core (External Repo) - ⚠️ Required
- [ ] `POST /api/training/start` endpoint
- [ ] `GET /api/training/stream/{job_id}` SSE endpoint
- [ ] `GET /api/training/status/{job_id}` endpoint
- [ ] `POST /api/training/cancel/{job_id}` endpoint
- [ ] `POST /api/models/deploy` endpoint
- [ ] `POST /api/models/rollback` endpoint
- [ ] `GET /api/health` endpoint
- [ ] `GET /api/resources` endpoint
- [ ] File watcher for `~/.jarvis/trinity/events/`
- [ ] Experience buffer management
- [ ] Training pipeline with checkpointing
- [ ] Model versioning system
- [ ] State file updates to `~/.jarvis/cross_repo/reactor_state.json`

---

## 🔒 Security Considerations

1. **Authentication:** Use token-based auth for API calls (optional for localhost)
2. **Rate Limiting:** Limit training job submissions to prevent abuse
3. **Resource Limits:** Enforce max concurrent training jobs (default: 1)
4. **File Permissions:** Secure experience files with 0600 permissions
5. **State File Locking:** Use distributed locks when updating state files

---

## 🚀 Example End-to-End Flow

```
1. User interacts with JARVIS
   ↓
2. JARVIS collects experience
   ↓
3. JARVIS forwards experience → ~/.jarvis/trinity/events/experiences_voice_123.json
   ↓
4. Reactor Core FileWatcher detects new file
   ↓
5. Reactor Core reads file, adds experiences to buffer
   ↓
6. JARVIS auto-trigger checks buffer (every 5 min)
   ↓
7. Buffer >= 100 experiences → Create TrainingJob
   ↓
8. JARVIS calls Advanced Training Coordinator
   ↓
9. Coordinator negotiates resources (waits for J-Prime idle)
   ↓
10. Coordinator acquires distributed training lock
   ↓
11. Coordinator calls Reactor Core: POST /api/training/start
   ↓
12. Reactor Core starts training, streams status via SSE
   ↓
13. JARVIS streams status updates (epoch progress, loss)
   ↓
14. Training completes → Reactor Core publishes MODEL_READY event
   ↓
15. Trinity Bridge forwards MODEL_READY to JARVIS
   ↓
16. JARVIS deploys new model (hot-swap)
   ↓
17. JARVIS updates UnifiedModelServing with new version
```

---

## 📝 Testing

### Manual Test (JARVIS → Reactor Core)

```bash
# Terminal 1: Start Reactor Core
cd ~/Documents/repos/reactor-core
python3 main.py

# Terminal 2: Start JARVIS
cd ~/Documents/repos/JARVIS-AI-Agent
python3 run_supervisor.py

# Terminal 3: Trigger training
curl -X POST http://localhost:8090/api/training/start \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "test-123",
    "model_type": "voice",
    "experiences": [...],
    "epochs": 10
  }'

# Terminal 4: Stream status
curl -N http://localhost:8090/api/training/stream/test-123
```

---

## 🐛 Troubleshooting

### Training doesn't start
- **Check:** Reactor Core health: `curl http://localhost:8090/api/health`
- **Check:** JARVIS can reach Reactor Core: `curl http://localhost:8090/api/health`
- **Check:** Experience files in `~/.jarvis/trinity/events/`
- **Check:** Reactor Core logs for errors

### OOM during training
- **Check:** J-Prime memory usage in `~/.jarvis/cross_repo/prime_state.json`
- **Check:** Resource Manager waited for J-Prime idle
- **Check:** `MAX_TOTAL_MEMORY_GB` environment variable

### Model not deployed after training
- **Check:** Reactor Core published MODEL_READY event
- **Check:** Trinity Bridge is running
- **Check:** JARVIS subscribed to model deployment events

---

**End of Specification**
