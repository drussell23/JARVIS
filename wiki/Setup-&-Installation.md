# Setup & Installation

Complete step-by-step guide to installing and configuring JARVIS AI Agent, from prerequisites to your first voice command.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Prerequisites](#prerequisites)
3. [Local Environment Setup](#local-environment-setup)
4. [GCP Configuration](#gcp-configuration)
5. [Database Setup](#database-setup)
6. [Voice System Setup](#voice-system-setup)
7. [Dependencies Installation](#dependencies-installation)
8. [Configuration](#configuration)
9. [First Run](#first-run)
10. [Verification](#verification)
11. [Troubleshooting](#troubleshooting)

---

## Quick Start

Get JARVIS running in 10 minutes (macOS M1/M2):

```bash
# 1. Clone repository
git clone https://github.com/derekjrussell/JARVIS-AI-Agent.git
cd JARVIS-AI-Agent

# 2. Run setup script
./scripts/quick_start.sh

# 3. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 4. Start JARVIS
python start_system.py
```

For detailed installation, continue reading...

---

## Prerequisites

### System Requirements

**Minimum (Local Only):**
- macOS 12+ (Monterey or later) with M1/M2 chip
- 16GB RAM
- 50GB free disk space
- Python 3.10 or 3.11
- Active internet connection

**Recommended (Hybrid Cloud):**
- macOS 13+ (Ventura) with M1 Pro/M2 Pro
- 16GB+ RAM
- 100GB free disk space
- Python 3.10 or 3.11
- Google Cloud Platform account
- Stable internet (10+ Mbps)

### Required Software

1. **Python 3.10 or 3.11** (miniforge recommended for M1/M2)
   ```bash
   # Install miniforge (M1/M2 optimized)
   brew install --cask miniforge
   conda create -n jarvis python=3.10
   conda activate jarvis
   ```

2. **Homebrew** (macOS package manager)
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

3. **Git**
   ```bash
   brew install git
   ```

4. **Node.js & npm** (for frontend)
   ```bash
   brew install node
   ```

5. **Yabai** (window management, optional but recommended)
   ```bash
   brew install koekeishiya/formulae/yabai
   ```

### Optional Software

- **Google Cloud SDK** (for GCP hybrid architecture)
  ```bash
  brew install --cask google-cloud-sdk
  ```

- **PostgreSQL Client** (for Cloud SQL)
  ```bash
  brew install postgresql@14
  ```

- **Docker** (for containerized deployment)
  ```bash
  brew install --cask docker
  ```

---

## Local Environment Setup

### 1. Clone Repository

```bash
git clone https://github.com/derekjrussell/JARVIS-AI-Agent.git
cd JARVIS-AI-Agent
```

### 2. Create Python Virtual Environment

```bash
# Using venv
python3.10 -m venv venv
source venv/bin/activate

# OR using conda (recommended for M1/M2)
conda create -n jarvis python=3.10
conda activate jarvis
```

### 3. Install System Dependencies

**macOS-specific dependencies:**
```bash
# Audio processing
brew install portaudio ffmpeg sox

# Voice recognition
brew install espeak

# Image processing
brew install tesseract

# Development tools
brew install wget curl
```

### 4. Install Python Dependencies

```bash
# Navigate to backend
cd backend

# Install core dependencies
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# Install optional dependencies (for full features)
pip install -r requirements-optional.txt

# Install cloud dependencies (if using GCP)
pip install -r requirements-cloud.txt

# Install voice improvements
pip install -r voice/requirements_voice_improvements.txt
```

### 5. Install Frontend Dependencies

```bash
# Navigate to frontend
cd ../frontend

# Install Node packages
npm install

# Build frontend
npm run build

# Return to project root
cd ..
```

---

## GCP Configuration

### Prerequisites

- Google Cloud Platform account
- Billing enabled
- Project created (e.g., `jarvis-473803`)

### 1. Install Google Cloud SDK

```bash
# Install SDK
brew install --cask google-cloud-sdk

# Initialize gcloud
gcloud init

# Authenticate
gcloud auth login
gcloud auth application-default login

# Set project
gcloud config set project jarvis-473803
```

### 2. Enable Required APIs

```bash
# Enable Compute Engine API
gcloud services enable compute.googleapis.com

# Enable Cloud SQL Admin API
gcloud services enable sqladmin.googleapis.com

# Enable Cloud Resource Manager API
gcloud services enable cloudresourcemanager.googleapis.com
```

### 3. Create Service Account

```bash
# Create service account
gcloud iam service-accounts create jarvis-deployer \
  --display-name="JARVIS Deployer" \
  --description="Service account for JARVIS automation"

# Grant necessary permissions
gcloud projects add-iam-policy-binding jarvis-473803 \
  --member="serviceAccount:jarvis-deployer@jarvis-473803.iam.gserviceaccount.com" \
  --role="roles/compute.instanceAdmin.v1"

gcloud projects add-iam-policy-binding jarvis-473803 \
  --member="serviceAccount:jarvis-deployer@jarvis-473803.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"

# Create key (save securely!)
gcloud iam service-accounts keys create jarvis-sa-key.json \
  --iam-account=jarvis-deployer@jarvis-473803.iam.gserviceaccount.com
```

### 4. Configure GCP Environment Variables

Add to `.env.gcp`:
```bash
GCP_PROJECT_ID=jarvis-473803
GCP_REGION=us-central1
GCP_ZONE=us-central1-a
GOOGLE_APPLICATION_CREDENTIALS=/path/to/jarvis-sa-key.json
GCP_VM_ENABLED=true
```

---

## Database Setup

JARVIS uses a dual-database system:
- **SQLite** (local, fast, embedded)
- **PostgreSQL** (Cloud SQL, scalable, production)

### 1. SQLite Setup (Automatic)

SQLite database is created automatically on first run:
```bash
# Database will be created at:
backend/database/jarvis_local.db
```

### 2. Cloud SQL Setup (Optional but Recommended)

**Create Cloud SQL Instance:**
```bash
# Create PostgreSQL 14 instance
gcloud sql instances create jarvis-learning-db \
  --database-version=POSTGRES_14 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --enable-bin-log \
  --backup-start-time=03:00

# Set root password
gcloud sql users set-password postgres \
  --instance=jarvis-learning-db \
  --password=YOUR_SECURE_PASSWORD

# Create database
gcloud sql databases create jarvis_learning \
  --instance=jarvis-learning-db

# Create JARVIS user
gcloud sql users create jarvis \
  --instance=jarvis-learning-db \
  --password=JarvisSecure2025!
```

**Install Cloud SQL Proxy:**
```bash
# Download proxy binary
curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.7.0/cloud-sql-proxy.darwin.arm64
chmod +x cloud-sql-proxy
sudo mv cloud-sql-proxy /usr/local/bin/

# Verify installation
cloud-sql-proxy --version
```

**Configure Database Connection:**

Create `~/.jarvis/gcp/database_config.json`:
```json
{
  "cloud_sql": {
    "instance_name": "jarvis-learning-db",
    "connection_name": "jarvis-473803:us-central1:jarvis-learning-db",
    "private_ip": "YOUR_INSTANCE_IP",
    "database": "jarvis_learning",
    "user": "jarvis",
    "password": "JarvisSecure2025!",
    "port": 5432
  },
  "project_id": "jarvis-473803",
  "region": "us-central1"
}
```

**Start Cloud SQL Proxy:**
```bash
# Automatic (recommended)
python start_system.py  # Proxy auto-starts

# Manual
cd backend
python intelligence/cloud_sql_proxy_manager.py start

# Install as system service (auto-start on boot)
python intelligence/cloud_sql_proxy_manager.py install
```

**Verify Connection:**
```bash
# Test database connection
PGPASSWORD=JarvisSecure2025! psql -h 127.0.0.1 -U jarvis -d jarvis_learning -c "SELECT version();"
```

---

## Voice System Setup

### 1. Create Voice Profile Directory

```bash
mkdir -p ~/.jarvis/voice
mkdir -p ~/.jarvis/gcp
```

### 2. Install Voice Dependencies

```bash
cd backend

# Install SpeechBrain (STT)
pip install speechbrain==0.5.16 torchaudio==2.1.2

# Install speaker recognition
pip install -r voice/requirements_voice_improvements.txt

# Install TTS providers
pip install gtts pyttsx3 pygame
```

### 3. Configure Picovoice Wake Word (Optional)

**Get Picovoice Access Key:**
1. Sign up at https://console.picovoice.ai/
2. Create new project
3. Copy access key

**Add to `.env`:**
```bash
PICOVOICE_ACCESS_KEY=your_access_key_here
```

### 4. Set Up macOS Permissions

**Microphone Access:**
1. System Preferences → Security & Privacy → Privacy
2. Microphone → Add Terminal/Python
3. Enable access

**Accessibility Access:**
1. System Preferences → Security & Privacy → Privacy
2. Accessibility → Add Terminal/Python
3. Enable access (required for screen unlock)

**Keychain Access (for voice unlock):**
```bash
# Store your Mac password securely
security add-generic-password \
  -a "$USER" \
  -s "com.jarvis.voiceunlock" \
  -w "YOUR_MAC_PASSWORD"
```

### 5. Enroll Your Voice (First Time)

```bash
# Start JARVIS
python start_system.py

# Follow voice enrollment prompts
# You'll be asked to say phrases like:
# - "Hey JARVIS, unlock my screen"
# - "JARVIS, what's on my screen?"
# - "Hey JARVIS, open Safari"
# (Repeat 3-5 times for best accuracy)
```

Voice enrollment creates:
- **Local:** `~/.jarvis/voice/speaker_profiles.json`
- **Cloud:** PostgreSQL `speaker_profiles` table (59+ samples)

---

## Dependencies Installation

### Core Dependencies

**Backend (Python):**
```bash
cd backend
pip install -r requirements.txt
```

Key packages:
- `fastapi==0.120.2` - Web framework
- `uvicorn==0.27.0` - ASGI server
- `transformers==4.36.2` - NLP models
- `torch==2.1.2` - PyTorch
- `speechbrain==0.5.16` - Speech recognition
- `anthropic==0.72.0` - Claude API
- `spacy==3.7.2` - NLP processing

**Frontend (Node.js):**
```bash
cd frontend
npm install
```

Key packages:
- `react@18.x` - UI framework
- `typescript@5.x` - Type safety
- `websocket` - Real-time communication
- `recharts` - Data visualization

### Optional Dependencies

**Voice Improvements:**
```bash
pip install -r backend/voice/requirements_voice_improvements.txt
```

**Cloud Features:**
```bash
pip install -r backend/requirements-cloud.txt
```

**ML Models:**
```bash
pip install -r backend/voice/requirements_ml.txt
```

### Verify Installation

```bash
# Check Python packages
pip list | grep -E "fastapi|speechbrain|anthropic|torch"

# Check Node packages
cd frontend && npm list | grep -E "react|typescript|websocket"

# Test imports
python -c "import fastapi, speechbrain, anthropic, torch; print('✅ All core packages installed')"
```

---

## Configuration

### 1. Environment Variables

Create `.env` from template:
```bash
cp .env.example .env
```

**Edit `.env` with your credentials:**

```bash
# ============== CLAUDE AI ==============
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxx

# ============== PICOVOICE (WAKE WORD) ==============
PICOVOICE_ACCESS_KEY=your_picovoice_access_key

# ============== GCP (OPTIONAL) ==============
GCP_PROJECT_ID=jarvis-473803
GCP_REGION=us-central1
GCP_ZONE=us-central1-a
GCP_VM_ENABLED=true

# ============== VOICE SYSTEM ==============
VOICE_ENABLED=true
WAKE_WORD_ENABLED=true
SPEAKER_RECOGNITION_ENABLED=true
TTS_PROVIDER=gtts  # Options: gtts, macos, pyttsx3

# ============== DATABASE ==============
USE_CLOUD_SQL=true  # Set to false for SQLite-only
LOCAL_DB_PATH=backend/database/jarvis_local.db

# ============== SYSTEM ==============
DEBUG=false
LOG_LEVEL=INFO
PORT=8010
```

### 2. Hybrid Configuration

Create `backend/core/hybrid_config.yaml`:

```yaml
intelligence:
  uae:
    enabled: true
    local_mode: light
    cloud_mode: full
  sai:
    enabled: true
    self_healing: true
    pattern_learning: true
  cai:
    enabled: true
    intent_prediction: true
    proactive_assistance: true

memory:
  local_threshold: 70  # % RAM
  cloud_shift_threshold: 85  # % RAM
  reclaim_threshold: 60  # % RAM
  monitoring_interval: 30  # seconds

gcp:
  spot_vm:
    enabled: true
    machine_type: e2-highmem-4
    disk_size_gb: 50
    max_hourly_cost: 0.10
    max_vms: 2
    auto_shutdown_idle_minutes: 15
```

### 3. Database Configuration

Create `~/.jarvis/gcp/database_config.json` (if using Cloud SQL):

```json
{
  "cloud_sql": {
    "instance_name": "jarvis-learning-db",
    "connection_name": "jarvis-473803:us-central1:jarvis-learning-db",
    "private_ip": "34.46.152.27",
    "database": "jarvis_learning",
    "user": "jarvis",
    "password": "JarvisSecure2025!",
    "port": 5432
  },
  "project_id": "jarvis-473803",
  "region": "us-central1"
}
```

### 4. Voice Configuration

Create `~/.jarvis/voice/config.json`:

```json
{
  "wake_word": {
    "enabled": true,
    "sensitivity": 0.7,
    "model": "jarvis",
    "fallback_energy_threshold": -40
  },
  "stt": {
    "engine": "speechbrain",
    "model": "asr-crdnn-rnnlm-librispeech",
    "language": "en-US",
    "cache_ttl": 30
  },
  "speaker_recognition": {
    "enabled": true,
    "model": "ecapa-tdnn",
    "confidence_threshold": 0.75,
    "embedding_dim": 192
  },
  "tts": {
    "primary": "gtts",
    "fallback": ["macos", "pyttsx3"],
    "cache_enabled": true,
    "voice_settings": {
      "gtts": {"lang": "en", "tld": "com"},
      "macos": {"voice": "Samantha", "rate": 180},
      "pyttsx3": {"rate": 180, "volume": 0.9}
    }
  }
}
```

---

## First Run

### 1. Start JARVIS

**Option A: Standard Start**
```bash
python start_system.py
```

**Option B: Restart (clean slate)**
```bash
python start_system.py --restart
```

**Option C: Development Mode**
```bash
python start_system.py --dev
```

### 2. Monitor Startup

Watch logs for successful initialization:
```bash
tail -f jarvis_startup.log
```

**Expected output:**
```
[2025-10-30 12:00:00] ✅ Starting JARVIS AI Agent v17.4.0
[2025-10-30 12:00:01] ✅ Loading environment variables
[2025-10-30 12:00:02] ✅ Initializing hybrid orchestrator
[2025-10-30 12:00:03] ✅ Cloud SQL proxy started successfully
[2025-10-30 12:00:04] ✅ Speaker profiles loaded: 2 profiles
[2025-10-30 12:00:05] ✅ UAE initialized (local mode)
[2025-10-30 12:00:06] ✅ SAI initialized (self-healing enabled)
[2025-10-30 12:00:07] ✅ CAI initialized (intent prediction enabled)
[2025-10-30 12:00:08] ✅ Wake word detector ready (Picovoice)
[2025-10-30 12:00:09] ✅ Backend server started on http://localhost:8010
[2025-10-30 12:00:10] ✅ JARVIS is ready! Say "Hey JARVIS"
```

### 3. Access Web Interface

Open browser:
```
http://localhost:8010
```

### 4. Test Voice Commands

Say wake word:
```
"Hey JARVIS"
```

Test commands:
```
"What time is it?"
"What's on my screen?"
"Unlock my screen" (requires voice enrollment)
```

---

## Verification

### System Health Check

```bash
# Check backend health
curl http://localhost:8010/health

# Expected response:
# {
#   "status": "healthy",
#   "version": "17.4.0",
#   "components": {
#     "uae": "running",
#     "sai": "running",
#     "cai": "running",
#     "database": "connected",
#     "voice": "ready",
#     "cloud_sql_proxy": "running"
#   }
# }
```

### Component Verification

**1. Voice System:**
```bash
# Check speaker profiles
cd backend
python -c "
from intelligence.speaker_verification import get_speaker_verifier
verifier = get_speaker_verifier()
print(f'✅ {len(verifier.profiles)} speaker profiles loaded')
"
```

**2. Database:**
```bash
# Check SQLite
sqlite3 backend/database/jarvis_local.db "SELECT COUNT(*) FROM command_history;"

# Check Cloud SQL (if configured)
PGPASSWORD=JarvisSecure2025! psql -h 127.0.0.1 -U jarvis -d jarvis_learning \
  -c "SELECT COUNT(*) FROM speaker_profiles;"
```

**3. Intelligence Systems:**
```bash
# Test UAE
python -c "
from intelligence.unified_awareness_engine import get_uae
uae = get_uae()
print(f'✅ UAE: {uae.status}')
"

# Test SAI
python -c "
from intelligence.self_aware_intelligence import get_sai
sai = get_sai()
print(f'✅ SAI: {sai.status}')
"

# Test CAI
python -c "
from intelligence.context_awareness_intelligence import get_cai
cai = get_cai()
print(f'✅ CAI: {cai.status}')
"
```

**4. GCP Integration (if enabled):**
```bash
# Check GCP authentication
gcloud auth application-default print-access-token

# Check VM manager
python -c "
from core.gcp_vm_manager import GCPVMManager
manager = GCPVMManager()
print(f'✅ GCP VM Manager ready (max VMs: {manager.config.max_concurrent_vms})')
"
```

### Performance Test

```bash
# Run performance benchmarks
cd backend
pytest tests/performance/ -v

# Check memory usage
python -c "
import psutil
import os
process = psutil.Process(os.getpid())
print(f'✅ Memory: {process.memory_info().rss / 1024 / 1024:.2f} MB')
"
```

---

## Troubleshooting

See [Troubleshooting Guide](Troubleshooting-Guide.md) for comprehensive solutions.

### Common Issues

**Issue: "ModuleNotFoundError: No module named 'speechbrain'"**
```bash
# Solution:
pip install speechbrain==0.5.16 torchaudio==2.1.2
```

**Issue: "Cloud SQL proxy connection timeout"**
```bash
# Solution 1: Check proxy is running
lsof -i :5432

# Solution 2: Restart proxy
python backend/intelligence/cloud_sql_proxy_manager.py restart

# Solution 3: Check GCP authentication
gcloud auth application-default login
```

**Issue: "Wake word not detected"**
```bash
# Solution 1: Check microphone permissions
# System Preferences → Security & Privacy → Privacy → Microphone

# Solution 2: Test microphone
python -c "
import sounddevice as sd
print(sd.query_devices())
"

# Solution 3: Adjust sensitivity
# Edit ~/.jarvis/voice/config.json
# Increase "sensitivity" value (0.5-0.9)
```

**Issue: "Voice unlock not working"**
```bash
# Solution 1: Enroll your voice
python backend/voice_unlock/enroll_voice.py

# Solution 2: Check keychain password
security find-generic-password -s "com.jarvis.voiceunlock"

# Solution 3: Grant accessibility access
# System Preferences → Security & Privacy → Privacy → Accessibility
```

**Issue: "High memory usage"**
```bash
# Solution 1: Enable GCP auto-scaling
# Edit .env: GCP_VM_ENABLED=true

# Solution 2: Reduce component footprint
# Edit backend/core/hybrid_config.yaml
# Set local_threshold: 60 (shift to cloud earlier)

# Solution 3: Monitor memory
python -c "
from core.platform_memory_monitor import PlatformMemoryMonitor
monitor = PlatformMemoryMonitor()
print(monitor.get_memory_status())
"
```

---

## Next Steps

After successful installation:

1. **Voice Enrollment** - Complete full voice profile (10+ samples)
2. **Configure Automation** - Set up custom voice commands
3. **Explore Features** - Try vision analysis, chat mode, goal inference
4. **Enable Cloud** - Configure GCP for hybrid architecture
5. **Read Docs** - Explore [Architecture](Architecture-&-Design.md) and [API](API-Documentation.md)

---

**Related Documentation:**
- [Architecture & Design](Architecture-&-Design.md) - System architecture
- [API Documentation](API-Documentation.md) - API reference
- [Troubleshooting Guide](Troubleshooting-Guide.md) - Detailed solutions
- [Contributing Guidelines](Contributing-Guidelines.md) - How to contribute

**External Resources:**
- [HYBRID_ARCHITECTURE.md](../docs/architecture/HYBRID_ARCHITECTURE.md) - Detailed architecture guide
- [CLOUD_SQL_PROXY_SETUP.md](../docs/deployment/CLOUD_SQL_PROXY_SETUP.md) - Database setup
- [VOICE_UNLOCK_INTEGRATION.md](../docs/voice_unlock/VOICE_UNLOCK_INTEGRATION.md) - Voice unlock guide

---

**Last Updated:** 2025-10-30
**Version:** 17.4.0
