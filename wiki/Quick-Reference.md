# JARVIS Quick Reference Guide

> **Fast Lookup** | **Common Commands** | **Quick Troubleshooting** | **Cheat Sheet**

This is your go-to reference for common JARVIS operations, commands, and quick troubleshooting.

---

## 🚀 Quick Start Commands

```bash
# Start JARVIS
./start_system.py

# Start backend only
python backend/main.py

# Start with GCP integration
ENABLE_GCP=true ./start_system.py

# Check system status
curl http://localhost:8000/health

# View logs
tail -f jarvis_startup.log
```

---

## 🎙️ Common Voice Commands

### Screen Control
```
"Hey JARVIS, unlock my screen"
"Lock my screen"
"Wake my displays"
```

### Application Control
```
"Open Safari"
"Close Chrome"
"Switch to VS Code"
"Show me all running apps"
```

### Display Management
```
"Show me my displays"
"Move this window to the TV"
"Switch to desktop 2"
```

### System Information
```
"What's my RAM usage?"
"How much memory am I using?"
"Check system status"
"What's the temperature?"
```

### Intelligence Queries
```
"What was I working on yesterday?"
"Show me my recent activities"
"What apps do I use most?"
"Predict what I'll do next"
```

---

## 🔧 Common Troubleshooting

### Voice Not Working

**Problem:** Wake word not detected
```bash
# Check microphone permissions
python -c "import sounddevice as sd; print(sd.query_devices())"

# Test wake word
python backend/voice/test_wake_word.py

# Re-enroll voice
python backend/voice/enroll_speaker.py
```

**Problem:** Speaker not recognized
```bash
# Check Cloud SQL connection
./cloud_sql_proxy -instances=PROJECT:REGION:INSTANCE=tcp:5432

# Verify voice profile
python -c "from backend.voice.speaker_recognition import *; verify_speaker()"

# Re-train voice model
python backend/voice/enroll_speaker.py --retrain
```

### Database Issues

**Problem:** Cloud SQL connection failed
```bash
# Start Cloud SQL proxy
./cloud_sql_proxy -instances=YOUR-INSTANCE=tcp:5432

# Test connection
psql -h 127.0.0.1 -U postgres -d jarvis_voice_db

# Check credentials
echo $DATABASE_URL
```

**Problem:** Sync conflicts
```bash
# Force sync from local to cloud
python scripts/sync_databases.py --force-upload

# Force sync from cloud to local
python scripts/sync_databases.py --force-download

# Reset local database
rm backend/jarvis.db
python backend/core/database.py --init
```

### GCP Issues

**Problem:** VM not auto-creating
```bash
# Check GCP credentials
gcloud auth list

# Test VM creation manually
python scripts/create_spot_vm.py --test

# Check quotas
gcloud compute project-info describe --project=YOUR-PROJECT

# View logs
gcloud compute operations list --limit=5
```

**Problem:** High costs
```bash
# Check current VMs
gcloud compute instances list

# Stop all VMs
gcloud compute instances stop --all

# Review billing
gcloud billing accounts list
gcloud billing accounts get-activity ACCOUNT-ID
```

### Performance Issues

**Problem:** Slow responses
```bash
# Check RAM usage
python -c "import psutil; print(f'RAM: {psutil.virtual_memory().percent}%')"

# Check CPU
top -l 1 | grep "CPU usage"

# Force GCP routing
export FORCE_GCP_ROUTING=true

# Clear caches
rm -rf backend/.jarvis_cache/*
```

**Problem:** Memory pressure
```bash
# Check what's using RAM
ps aux | sort -nrk 4 | head -10

# Clear Python cache
find . -type d -name "__pycache__" -exec rm -rf {} +

# Restart with minimal components
MINIMAL_MODE=true ./start_system.py
```

---

## 📡 API Quick Reference

### Health Check
```bash
curl http://localhost:8000/health
```

### Voice Command
```bash
curl -X POST http://localhost:8000/api/voice/command \
  -H "Content-Type: application/json" \
  -d '{"command": "unlock my screen"}'
```

### Vision Analysis
```bash
curl -X POST http://localhost:8000/api/vision/analyze \
  -H "Content-Type: application/json" \
  -d '{"action": "find", "target": "Safari icon"}'
```

### Intelligence Query
```bash
curl -X POST http://localhost:8000/api/intelligence/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What was I working on?", "system": "uae"}'
```

### WebSocket Connection
```javascript
const ws = new WebSocket('ws://localhost:8000/ws');
ws.onmessage = (event) => console.log(JSON.parse(event.data));
ws.send(JSON.stringify({type: 'command', data: 'status'}));
```

---

## 🗄️ Database Quick Commands

### SQLite (Local)
```bash
# Open database
sqlite3 backend/jarvis.db

# Common queries
SELECT * FROM voice_interactions ORDER BY timestamp DESC LIMIT 10;
SELECT * FROM learning_patterns WHERE success_rate > 0.8;
SELECT COUNT(*) FROM speaker_profiles;
```

### PostgreSQL (Cloud SQL)
```bash
# Connect
psql -h 127.0.0.1 -U postgres -d jarvis_voice_db

# Common queries
SELECT * FROM voice_biometric_data ORDER BY recorded_at DESC LIMIT 10;
SELECT speaker_name, sample_count FROM speaker_profiles;
SELECT COUNT(*) FROM voice_interactions WHERE confidence > 0.75;
```

---

## ⚙️ Configuration Quick Reference

### Environment Variables
```bash
# Core settings
export JARVIS_ENV=production
export LOG_LEVEL=INFO

# GCP settings
export GCP_PROJECT_ID=your-project
export GCP_REGION=us-central1
export ENABLE_GCP=true

# Database
export DATABASE_URL=postgresql://user:pass@host:5432/db
export USE_CLOUD_SQL=true

# Voice settings
export WAKE_WORD=jarvis
export SPEAKER_THRESHOLD=0.75
```

### Config File Locations
```
.env                              # Main environment config
.env.gcp                          # GCP-specific settings
backend/core/hybrid_config.yaml   # Hybrid routing config
.github/workflows/config/         # CI/CD configs
```

---

## 🔍 Diagnostic Commands

### System Status
```bash
# Overall health
curl http://localhost:8000/health | jq

# Component status
curl http://localhost:8000/api/components/status | jq

# RAM usage
curl http://localhost:8000/api/system/ram | jq

# Active agents
curl http://localhost:8000/api/agents/list | jq
```

### Logs
```bash
# Main log
tail -f jarvis_startup.log

# Backend log
tail -f backend.log

# Errors only
tail -f jarvis_startup.log | grep -i error

# Specific component
tail -f backend.log | grep "voice_system"
```

### Performance Metrics
```bash
# Response times
curl http://localhost:8000/api/metrics/response-times | jq

# Success rates
curl http://localhost:8000/api/metrics/success-rates | jq

# Component load times
curl http://localhost:8000/api/metrics/component-load | jq
```

---

## 🧪 Testing Commands

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/unit/backend/test_voice_system.py

# Run with coverage
pytest --cov=backend --cov-report=html

# Run integration tests
pytest tests/integration/

# Run E2E tests
pytest tests/e2e/

# Run specific test
pytest -k "test_voice_unlock"
```

---

## 📦 Installation Quick Commands

```bash
# Clone repository
git clone https://github.com/yourusername/JARVIS-AI-Agent.git
cd JARVIS-AI-Agent

# Install Python dependencies
pip install -r backend/requirements.txt

# Install Node dependencies (frontend)
cd frontend && npm install && cd ..

# Setup GCP
gcloud auth login
gcloud config set project YOUR-PROJECT-ID

# Download Cloud SQL Proxy
curl -o cloud_sql_proxy https://dl.google.com/cloudsql/cloud_sql_proxy.darwin.amd64
chmod +x cloud_sql_proxy

# Initialize databases
python backend/core/database.py --init
./cloud_sql_proxy -instances=INSTANCE=tcp:5432 &
psql -h 127.0.0.1 -U postgres -d jarvis_voice_db < backend/schema.sql

# Enroll voice
python backend/voice/enroll_speaker.py

# First run
./start_system.py
```

---

## 🔐 Security Quick Reference

### Keychain Access
```bash
# Store password
security add-generic-password \
  -s "com.jarvis.voiceunlock" \
  -a "$(whoami)" \
  -w "YOUR_PASSWORD"

# Retrieve password
security find-generic-password \
  -s "com.jarvis.voiceunlock" \
  -a "$(whoami)" \
  -w
```

### GCP Service Account
```bash
# Create service account
gcloud iam service-accounts create jarvis-sa

# Grant permissions
gcloud projects add-iam-policy-binding PROJECT-ID \
  --member="serviceAccount:jarvis-sa@PROJECT-ID.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

# Download key
gcloud iam service-accounts keys create gcp-key.json \
  --iam-account=jarvis-sa@PROJECT-ID.iam.gserviceaccount.com
```

---

## 🛠️ Maintenance Commands

```bash
# Update dependencies
pip install --upgrade -r backend/requirements.txt
cd frontend && npm update && cd ..

# Clear caches
rm -rf backend/.jarvis_cache/*
rm -rf backend/__pycache__
find . -type d -name "__pycache__" -exec rm -rf {} +

# Vacuum databases
sqlite3 backend/jarvis.db "VACUUM;"
psql -h 127.0.0.1 -U postgres -d jarvis_voice_db -c "VACUUM FULL;"

# Backup databases
sqlite3 backend/jarvis.db ".backup 'backup.db'"
pg_dump -h 127.0.0.1 -U postgres jarvis_voice_db > backup.sql

# Restart system cleanly
pkill -f "python.*jarvis"
pkill -f "cloud_sql_proxy"
./start_system.py
```

---

## 📊 Monitoring Quick Commands

```bash
# Watch RAM usage (live)
watch -n 1 'curl -s http://localhost:8000/api/system/ram | jq'

# Monitor logs (live)
tail -f jarvis_startup.log | grep -E "ERROR|WARNING|INFO"

# Check GCP costs (daily)
gcloud billing accounts get-activity ACCOUNT-ID \
  --start-date=$(date -v-1d +%Y-%m-%d) \
  --end-date=$(date +%Y-%m-%d)

# Component status (live)
watch -n 5 'curl -s http://localhost:8000/api/components/status | jq'
```

---

## 🆘 Emergency Commands

### Kill Everything
```bash
# Stop all JARVIS processes
pkill -f "jarvis"
pkill -f "cloud_sql_proxy"

# Stop GCP VMs
gcloud compute instances stop --all

# Reset to clean state
rm -rf backend/.jarvis_cache/*
rm backend/jarvis.db
git reset --hard HEAD
```

### Factory Reset
```bash
# WARNING: This deletes all data!

# Stop processes
pkill -f "jarvis"

# Remove databases
rm backend/jarvis.db
psql -h 127.0.0.1 -U postgres -d postgres -c "DROP DATABASE jarvis_voice_db;"
psql -h 127.0.0.1 -U postgres -d postgres -c "CREATE DATABASE jarvis_voice_db;"

# Remove voice enrollments
rm -rf backend/voice/enrollments/*

# Remove caches
rm -rf backend/.jarvis_cache/*
rm -rf backend/__pycache__

# Reinitialize
python backend/core/database.py --init
python backend/voice/enroll_speaker.py
./start_system.py
```

---

## 📱 Mobile App Quick Commands (Future)

```bash
# Coming in Phase 4-5 (Q2-Q4 2025)

# iOS app build
cd mobile/ios && xcodebuild

# Android app build
cd mobile/android && ./gradlew build

# React Native hot reload
npm start
```

---

## 🔗 Quick Links

- [Full Documentation](./Home.md)
- [Architecture](./Architecture-&-Design.md)
- [Setup Guide](./Setup-&-Installation.md)
- [API Reference](./API-Documentation.md)
- [Troubleshooting](./Troubleshooting-Guide.md)
- [Contributing](./Contributing-Guidelines.md)

---

**Bookmark this page for quick reference!** 🔖

Last Updated: October 30, 2025
