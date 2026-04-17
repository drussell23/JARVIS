# Complete VIBA/PAVA Integration Guide: Docker, Cloud Run & VM Spot Instances

## Executive Summary

This guide provides a **complete, production-ready integration** of:
- **VIBA** (Voice Identity Biometric Authentication) - Orchestration layer
- **PAVA** (Physics-Aware Voice Authentication) - Physics-based anti-spoofing
- **Docker** - Containerized deployment
- **GCP Cloud Run** - Serverless ML service
- **GCP VM Spot Instances** - Cost-effective compute for heavy workloads
- **Screen Unlock** - macOS screen unlock functionality

**Goal:** Enable JARVIS to unlock your screen using voice biometrics with a fully integrated, scalable, cost-optimized architecture.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Component Integration](#2-component-integration)
3. [Docker Deployment](#3-docker-deployment)
4. [GCP Cloud Run Setup](#4-gcp-cloud-run-setup)
5. [GCP VM Spot Instance Integration](#5-gcp-vm-spot-instance-integration)
6. [Screen Unlock Integration](#6-screen-unlock-integration)
7. [Diagnostic & Health Checks](#7-diagnostic--health-checks)
8. [Troubleshooting](#8-troubleshooting)
9. [Cost Optimization](#9-cost-optimization)

---

## 1. Architecture Overview

### 1.1 System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    JARVIS Voice Unlock System                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐                                              │
│  │   macOS      │  Voice Command: "Unlock my screen"          │
│  │  Frontend    │  ────────────────────────────────────────►   │
│  └──────┬───────┘                                              │
│         │                                                       │
│         │ WebSocket/HTTP                                       │
│         ▼                                                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │         JARVIS Backend (Local or GCP VM)                  │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │  IntelligentVoiceUnlockService                      │  │  │
│  │  │  ┌───────────────────────────────────────────────┐ │  │  │
│  │  │  │  VIBA: Voice Biometric Intelligence            │ │  │  │
│  │  │  │  - Orchestrates verification                   │ │  │  │
│  │  │  │  - Provides upfront transparency               │ │  │  │
│  │  │  └───────────────────────────────────────────────┘ │  │  │
│  │  │                                                     │  │  │
│  │  │  ┌───────────────────────────────────────────────┐ │  │  │
│  │  │  │  Parallel Verification Engines                │ │  │  │
│  │  │  │  ┌────────┐  ┌────────┐  ┌────────┐         │ │  │  │
│  │  │  │  │  ML    │  │ Physics│  │Behavior│         │ │  │  │
│  │  │  │  │ ECAPA  │  │  PAVA  │  │Context │         │ │  │  │
│  │  │  │  └───┬────┘  └───┬────┘  └───┬────┘         │ │  │  │
│  │  │  │      │           │           │               │ │  │  │
│  │  │  │      └───────────┴───────────┘               │ │  │  │
│  │  │  │              │                               │ │  │  │
│  │  │  │      ┌───────▼────────┐                      │ │  │  │
│  │  │  │      │ Bayesian Fusion│                      │ │  │  │
│  │  │  │      │ (Adaptive)      │                      │ │  │  │
│  │  │  │      └───────┬────────┘                      │ │  │  │
│  │  │  └──────────────┼──────────────────────────────┘ │  │  │
│  │  └─────────────────┼──────────────────────────────┘  │  │
│  │                     │                                  │  │
│  │                     ▼                                  │  │
│  │            ┌─────────────────┐                        │  │
│  │            │ Screen Unlock    │                        │  │
│  │            │ (macOS Keychain) │                        │  │
│  │            └─────────────────┘                        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                     │                                          │
│         ┌───────────┴───────────┐                            │
│         │                       │                            │
│         ▼                       ▼                            │
│  ┌──────────────┐      ┌──────────────────┐                 │
│  │  Cloud Run   │      │  GCP VM Spot     │                 │
│  │  (ECAPA ML)  │      │  (Heavy Compute) │                 │
│  │              │      │                  │                 │
│  │  - JIT Model │      │  - 32GB RAM      │                 │
│  │  - <5s start │      │  - Auto-scaling  │                 │
│  │  - Scale-2-0 │      │  - Cost-optimized│                 │
│  └──────────────┘      └──────────────────┘                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Data Flow

```
1. User: "Unlock my screen"
   ↓
2. macOS Frontend → Audio Capture (16kHz PCM)
   ↓
3. Backend: IntelligentVoiceUnlockService
   ↓
4. VIBA: verify_and_announce()
   ├─► Parallel Tasks (asyncio.gather):
   │   ├─► ML Verification (ECAPA)
   │   │   ├─► Local (if RAM > 8GB)
   │   │   └─► Cloud Run (if local unavailable)
   │   │
   │   ├─► Physics Analysis (PAVA)
   │   │   ├─► VTL Verification
   │   │   ├─► Reverb Analysis
   │   │   └─► Doppler Detection
   │   │
   │   ├─► Behavioral Context
   │   └─► Contextual Factors
   │
   ↓
5. Bayesian Fusion (Adaptive)
   ├─► Excludes unavailable sources
   ├─► Renormalizes weights
   └─► Calculates P(authentic|evidence)
   ↓
6. Decision
   ├─► P(auth) >= 85% → AUTHENTICATE
   ├─► 40% <= P(auth) < 85% → CHALLENGE
   └─► P(auth) < 40% → REJECT
   ↓
7. Screen Unlock (if authenticated)
   ├─► Retrieve password from Keychain
   ├─► AppleScript/CGEvent typing
   └─► Verify unlock success
```

---

## 2. Component Integration

### 2.1 VIBA Integration

**Location:** `backend/voice_unlock/voice_biometric_intelligence.py`

**Key Methods:**
- `verify_and_announce()` - Main orchestration method
- `_verify_speaker()` - Speaker verification with ECAPA
- `_apply_bayesian_fusion()` - Confidence fusion

**Integration Points:**
```python
# In intelligent_voice_unlock_service.py
from backend.voice_unlock.voice_biometric_intelligence import VoiceBiometricIntelligence

viba = VoiceBiometricIntelligence()
result = await viba.verify_and_announce(
    audio_data=audio_data,
    command_text="unlock my screen"
)

if result.verified:
    await execute_unlock()
```

### 2.2 PAVA Integration

**Location:** `backend/voice_unlock/core/anti_spoofing.py`

**Key Components:**
- `AntiSpoofingDetector` - 7-layer detection
- `VoiceFeatureExtractor` - Physics feature extraction
- `BayesianConfidenceFusion` - Adaptive fusion engine

**Integration:**
```python
# PAVA is automatically loaded by VIBA
# No manual integration needed - VIBA orchestrates everything

# Check PAVA availability:
from backend.voice_unlock.core.anti_spoofing import get_anti_spoofing_detector
detector = get_anti_spoofing_detector()
if detector:
    print("✅ PAVA available")
else:
    print("⚠️ PAVA unavailable (optional)")
```

### 2.3 Bayesian Fusion (Adaptive)

**Location:** `backend/voice_unlock/core/bayesian_fusion.py`

**Key Features:**
- ✅ **Adaptive Exclusion** - Automatically excludes unavailable sources
- ✅ **Weight Renormalization** - Adjusts weights when components missing
- ✅ **Degradation Mode** - Lower thresholds when ML unavailable
- ✅ **Diagnostic Feedback** - Explains why confidence is low

**Configuration:**
```python
# Environment variables (or defaults)
BAYESIAN_ML_WEIGHT=0.40
BAYESIAN_PHYSICS_WEIGHT=0.30
BAYESIAN_BEHAVIORAL_WEIGHT=0.20
BAYESIAN_CONTEXT_WEIGHT=0.10
BAYESIAN_AUTH_THRESHOLD=0.85
BAYESIAN_REJECT_THRESHOLD=0.40
BAYESIAN_ADAPTIVE_EXCLUSION=true  # Enable adaptive exclusion
BAYESIAN_MIN_VALID_CONFIDENCE=0.02  # Exclude sources below this
```

---

## 3. Docker Deployment

### 3.1 Local Docker Development

**File:** `backend/cloud_services/docker-compose.yml`

**Start:**
```bash
cd backend/cloud_services
docker compose up -d
```

**Test:**
```bash
curl http://localhost:8010/health
curl http://localhost:8010/status
```

**Stop:**
```bash
docker compose down
```

### 3.2 Docker Build for Cloud Run

**File:** `backend/cloud_services/Dockerfile`

**Build:**
```bash
cd backend/cloud_services
docker build -t ecapa-cloud-service:latest .
```

**Run Locally:**
```bash
docker run -p 8010:8010 \
  -e ECAPA_DEVICE=cpu \
  -e ECAPA_WARMUP_ON_START=true \
  ecapa-cloud-service:latest
```

**Key Features:**
- ✅ Pre-baked model cache (no runtime downloads)
- ✅ JIT/ONNX optimization (ultra-fast cold starts)
- ✅ Multi-stage build (minimal image size)
- ✅ Non-root user (security)

---

## 4. GCP Cloud Run Setup

### 4.1 Prerequisites

```bash
# Install gcloud CLI
# https://cloud.google.com/sdk/docs/install

# Authenticate
gcloud auth login
gcloud auth application-default login

# Set project
gcloud config set project jarvis-473803
```

### 4.2 Deploy to Cloud Run

**File:** `backend/cloud_services/deploy_cloud_run.sh`

**Deploy:**
```bash
cd backend/cloud_services
./deploy_cloud_run.sh
```

**Or with options:**
```bash
./deploy_cloud_run.sh \
  --region us-central1 \
  --service-name jarvis-ml \
  --local-build
```

### 4.3 Configuration

**Environment Variables (set in Cloud Run):**
```bash
ECAPA_DEVICE=cpu
ECAPA_WARMUP_ON_START=true
ECAPA_USE_OPTIMIZED=true
ECAPA_CACHE_TTL=3600
PORT=8010
```

**Resource Limits:**
- Memory: 4Gi
- CPU: 2
- Min Instances: 0 (scale to zero)
- Max Instances: 3
- Timeout: 300s

### 4.4 Update Backend Configuration

After deployment, update `.env`:

```bash
# Get service URL
SERVICE_URL=$(gcloud run services describe jarvis-ml \
  --region us-central1 \
  --format 'value(status.url)')

# Update .env
echo "JARVIS_CLOUD_ML_ENDPOINT=${SERVICE_URL}/api/ml" >> .env
```

### 4.5 Test Cloud Run Service

```bash
# Health check
curl ${SERVICE_URL}/health

# Status
curl ${SERVICE_URL}/status

# Test embedding extraction
curl -X POST ${SERVICE_URL}/api/ml/speaker_embedding \
  -H "Content-Type: application/json" \
  -d '{"audio_data": "<base64_audio>"}'
```

---

## 5. GCP VM Spot Instance Integration

### 5.1 Automatic VM Creation

**Trigger:** Memory pressure >85% RAM usage

**Configuration:**
```bash
export GCP_VM_ENABLED=true
export GCP_PROJECT_ID=jarvis-473803
export GCP_REGION=us-central1
export GCP_ZONE=us-central1-a
export GCP_VM_MACHINE_TYPE=e2-highmem-4  # 4 vCPU, 32GB RAM
export GCP_VM_USE_SPOT=true
export GCP_VM_DAILY_BUDGET=5.0
export GCP_VM_MAX_CONCURRENT=2
```

### 5.2 VM Startup Script

**File:** `backend/core/gcp_vm_startup.sh`

**What it does:**
1. Installs system dependencies
2. Clones JARVIS repository
3. Installs Python dependencies
4. Starts Cloud SQL Proxy
5. Launches JARVIS backend on port 8010

**Manual VM Creation:**
```python
from backend.core.gcp_vm_manager import get_gcp_vm_manager

vm_manager = await get_gcp_vm_manager()
vm = await vm_manager.create_vm(
    components=['VISION', 'CHATBOTS', 'ML_MODELS'],
    trigger_reason="Manual testing"
)
print(f"VM created: {vm.name}")
print(f"IP: {vm.ip_address}")
```

### 5.3 Connect to VM Backend

**Update frontend to use VM IP:**
```javascript
// In frontend configuration
const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || 
  'http://<VM_IP>:8010';

// Voice unlock request
fetch(`${BACKEND_URL}/api/voice/unlock`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    audio_data: base64Audio,
    command: "unlock my screen"
  })
})
```

### 5.4 VM Monitoring

**Check VM status:**
```bash
cd backend
python3 core/gcp_vm_status.py
```

**Terminate VMs:**
```bash
python3 core/gcp_vm_status.py --terminate
```

---

## 6. Screen Unlock Integration

### 6.1 macOS Integration

**File:** `backend/voice_unlock/services/mac_unlock_service.py`

**Requirements:**
- macOS Keychain access
- Accessibility permissions
- Screen lock detection

**Setup:**
```bash
# Grant accessibility permissions
# System Preferences → Security & Privacy → Accessibility
# Add Terminal/Python to allowed apps

# Store password in Keychain
security add-generic-password \
  -a "jarvis" \
  -s "screen_unlock" \
  -w "your_password" \
  -U
```

### 6.2 Unlock Flow

```python
# In intelligent_voice_unlock_service.py
async def _perform_unlock(self, speaker_name, context, scenario, attempt_id):
    """Execute screen unlock with password typing."""
    
    # 1. Retrieve password from Keychain
    password = await self._get_password_from_keychain()
    
    # 2. Detect screen lock state
    is_locked = await self._check_screen_locked()
    
    if not is_locked:
        return {"success": True, "message": "Screen already unlocked"}
    
    # 3. Type password using secure method
    success = await self._type_password_securely(password)
    
    # 4. Verify unlock
    await asyncio.sleep(0.5)  # Wait for unlock
    verified = await self._check_screen_locked()
    
    return {
        "success": not verified,
        "message": "Screen unlocked" if not verified else "Unlock failed"
    }
```

### 6.3 Testing Screen Unlock

```bash
# Lock screen manually
# Cmd+Ctrl+Q (or System Preferences)

# Test voice unlock
python3 -c "
import asyncio
from backend.voice_unlock.intelligent_voice_unlock_service import IntelligentVoiceUnlockService

async def test():
    service = IntelligentVoiceUnlockService()
    await service.initialize()
    
    # Load test audio
    with open('test_audio.wav', 'rb') as f:
        audio = f.read()
    
    result = await service.process_unlock_command_async(audio)
    print(f'Result: {result}')

asyncio.run(test())
"
```

---

## 7. Diagnostic & Health Checks

### 7.1 Diagnostic System

**File:** `backend/voice_unlock/intelligent_diagnostic_system.py`

**Run Full Diagnostic:**
```bash
python backend/voice_unlock/intelligent_diagnostic_system.py
```

**Auto-Remediate:**
```bash
python backend/voice_unlock/intelligent_diagnostic_system.py --auto-remediate
```

**JSON Output:**
```bash
python backend/voice_unlock/intelligent_diagnostic_system.py --json > diagnostics.json
```

### 7.2 Health Check Endpoints

**Backend Health:**
```bash
curl http://localhost:8000/health
```

**Voice Unlock Health:**
```bash
curl http://localhost:8000/api/voice-unlock/health
```

**Cloud Run Health:**
```bash
curl ${SERVICE_URL}/health
```

**VM Backend Health:**
```bash
curl http://<VM_IP>:8010/health
```

### 7.3 Diagnostic API

**Endpoint:** `GET /api/voice-unlock/diagnostics`

**Response:**
```json
{
  "overall_status": "healthy",
  "overall_confidence": 0.95,
  "components": {
    "dependencies": {
      "status": "healthy",
      "message": "All dependencies installed"
    },
    "ecapa_encoder": {
      "status": "healthy",
      "source": "cloud",
      "endpoint": "https://jarvis-ml-xxx.run.app"
    },
    "voice_profiles": {
      "status": "healthy",
      "count": 1,
      "samples": 59
    },
    "pava_components": {
      "status": "healthy",
      "anti_spoofing": true,
      "bayesian_fusion": true
    }
  },
  "root_causes": [],
  "recommended_actions": []
}
```

---

## 8. Troubleshooting

### 8.1 0% Confidence Issue

**Symptoms:**
- Voice unlock returns 0% confidence
- Logs show "ECAPA encoder unavailable"

**Diagnosis:**
```bash
# Check ECAPA status
python3 -c "
from backend.voice_unlock.ml_engine_registry import get_ml_registry_sync
registry = get_ml_registry_sync()
status = registry.get_ecapa_status()
print(status)
"
```

**Fixes:**
1. **Install dependencies:**
   ```bash
   pip install numpy torch speechbrain
   ```

2. **Check Cloud Run:**
   ```bash
   curl ${SERVICE_URL}/health
   ```

3. **Verify voice profile:**
   ```bash
   sqlite3 ~/.jarvis/jarvis_learning.db \
     "SELECT speaker_name, total_samples FROM speaker_profiles"
   ```

4. **Run diagnostic:**
   ```bash
   python backend/voice_unlock/intelligent_diagnostic_system.py --auto-remediate
   ```

### 8.2 PAVA Not Working

**Symptoms:**
- ML confidence works, but physics analysis missing
- No PAVA logs

**Diagnosis:**
```python
from backend.voice_unlock.core.anti_spoofing import get_anti_spoofing_detector
detector = get_anti_spoofing_detector()
if detector is None:
    print("PAVA unavailable - check dependencies")
```

**Fixes:**
```bash
# Install PAVA dependencies
pip install scipy librosa soundfile

# Check imports
python3 -c "
from backend.voice_unlock.core.anti_spoofing import get_anti_spoofing_detector
detector = get_anti_spoofing_detector()
print('PAVA available:', detector is not None)
"
```

### 8.3 Cloud Run Cold Start

**Symptoms:**
- First request takes 3-5 seconds
- Subsequent requests are fast

**Solutions:**
1. **Keep warm (costs ~$15/month):**
   ```bash
   gcloud run services update jarvis-ml \
     --min-instances=1 \
     --region=us-central1
   ```

2. **Pre-warm endpoint:**
   ```bash
   curl ${SERVICE_URL}/api/ml/prewarm
   ```

3. **Use local fallback:**
   ```bash
   export JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED=true
   ```

### 8.4 VM Not Creating

**Symptoms:**
- Memory pressure detected but no VM created

**Diagnosis:**
```bash
# Check VM manager status
python3 backend/core/gcp_vm_status.py

# Check logs
grep "GCP VM" backend.log
```

**Fixes:**
1. **Enable VM creation:**
   ```bash
   export GCP_VM_ENABLED=true
   ```

2. **Check budget:**
   ```bash
   export GCP_VM_DAILY_BUDGET=5.0
   ```

3. **Check authentication:**
   ```bash
   gcloud auth application-default login
   ```

### 8.5 Screen Unlock Fails

**Symptoms:**
- Voice verification succeeds but screen doesn't unlock

**Diagnosis:**
```bash
# Check Keychain access
security find-generic-password -a "jarvis" -s "screen_unlock"

# Check permissions
# System Preferences → Security & Privacy → Accessibility
```

**Fixes:**
1. **Grant accessibility permissions**
2. **Store password in Keychain:**
   ```bash
   security add-generic-password \
     -a "jarvis" \
     -s "screen_unlock" \
     -w "your_password" \
     -U
   ```

3. **Test unlock manually:**
   ```python
   from backend.voice_unlock.services.mac_unlock_service import MacUnlockService
   service = MacUnlockService()
   await service.unlock_screen("your_password")
   ```

---

## 9. Cost Optimization

### 9.1 Cloud Run Costs

**Pricing:**
- CPU: $0.000024 per vCPU-second
- Memory: $0.0000025 per GB-second
- Requests: $0.40 per million

**Monthly Estimate (50 unlocks/day):**
- Base: ~$0.02/month
- With min-instances=1: ~$15/month

**Optimization:**
- ✅ Scale to zero (min-instances=0)
- ✅ Pre-baked models (no download costs)
- ✅ JIT optimization (faster = cheaper)

### 9.2 VM Spot Instance Costs

**Pricing:**
- e2-highmem-4 (Spot): $0.029/hour
- Regular: $0.312/hour (91% savings)

**Daily Budget Example:**
- Budget: $5.00/day
- Max runtime: 172 hours/day
- With 3-hour limit: 1-2 VMs/day

**Optimization:**
- ✅ Auto-termination after 3 hours
- ✅ Idle timeout (30 minutes)
- ✅ Daily budget limits

### 9.3 Hybrid Strategy

**Decision Flow:**
```
Memory Pressure?
  ├─► Low (<85%) → Use Local ECAPA ($0.00)
  ├─► Medium (85-95%) → Use Cloud Run (~$0.01/1000 req)
  └─► High (>95%) → Create VM Spot ($0.029/hour)
```

**Cost Breakdown:**
- Local: 80% of requests → $0.00
- Cloud Run: 15% of requests → ~$0.01/1000
- VM Spot: 5% of requests → ~$0.15/day

**Total Monthly:** ~$5-10/month (very affordable!)

---

## 10. Quick Start Checklist

### ✅ Setup Checklist

- [ ] Install dependencies: `pip install numpy torch speechbrain scipy librosa`
- [ ] Complete voice enrollment: `python backend/voice/enroll_voice.py`
- [ ] Deploy Cloud Run: `cd backend/cloud_services && ./deploy_cloud_run.sh`
- [ ] Update `.env` with Cloud Run URL
- [ ] Test local unlock: Say "Hey JARVIS, unlock my screen"
- [ ] Configure VM (optional): Set `GCP_VM_ENABLED=true`
- [ ] Grant macOS permissions: Accessibility + Keychain
- [ ] Run diagnostic: `python backend/voice_unlock/intelligent_diagnostic_system.py`

### ✅ Verification

```bash
# 1. Check ECAPA
curl http://localhost:8000/api/voice-unlock/diagnostics | jq '.components.ecapa_encoder'

# 2. Check Voice Profiles
sqlite3 ~/.jarvis/jarvis_learning.db "SELECT * FROM speaker_profiles"

# 3. Test Voice Unlock
# Say: "Hey JARVIS, unlock my screen"
# Expected: 95%+ confidence, screen unlocks

# 4. Check Cloud Run
curl ${SERVICE_URL}/health

# 5. Check VM (if enabled)
python3 backend/core/gcp_vm_status.py
```

---

## 11. Next Steps

### Immediate
1. ✅ Follow setup checklist
2. ✅ Test voice unlock locally
3. ✅ Deploy Cloud Run service
4. ✅ Test end-to-end unlock flow

### Short-Term
1. Monitor costs and optimize
2. Fine-tune Bayesian fusion weights
3. Add more voice samples for better accuracy
4. Set up monitoring/alerts

### Long-Term
1. Multi-region Cloud Run deployment
2. Pre-baked VM images for faster startup
3. Advanced behavioral pattern learning
4. Multi-modal authentication (face + voice)

---

## 12. Support & Resources

### Documentation
- `INTELLIGENT_PAVA_VIBA_INTEGRATION_SOLUTION.md` - Detailed integration guide
- `docs/voice_unlock/NEURAL_PARALLEL_ARCHITECTURE.md` - Architecture deep dive
- `PAVA_VIBA_INTEGRATION_ANALYSIS.md` - Integration analysis
- `VOICE_UNLOCK_QUICK_START.md` - Quick start guide

### Diagnostic Tools
- `backend/voice_unlock/intelligent_diagnostic_system.py` - Full diagnostic
- `backend/core/gcp_vm_status.py` - VM status checker
- `/api/voice-unlock/diagnostics` - Health check API

### Logs
- Backend: `backend.log` or `~/Documents/repos/JARVIS-AI-Agent/jarvis_startup.log`
- Cloud Run: `gcloud run logs read jarvis-ml --region us-central1`
- VM: `/var/log/jarvis/backend.log` (on VM)

---

**Last Updated:** December 2024  
**Version:** 1.0.0  
**Status:** Production-Ready ✅
