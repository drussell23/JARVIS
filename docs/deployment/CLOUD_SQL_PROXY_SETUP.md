# Cloud SQL Proxy Setup Guide

Advanced, dynamic, robust Cloud SQL proxy management for JARVIS voice biometric authentication.

## Features

✅ **Zero Hardcoding** - All configuration from `~/.jarvis/gcp/database_config.json`
✅ **Auto-Discovery** - Finds proxy binary and config automatically
✅ **System Service** - Persists across reboots (launchd/systemd)
✅ **Runtime Management** - Ensures proxy runs before JARVIS starts
✅ **Health Monitoring** - Auto-recovers if proxy crashes
✅ **Port Conflict Resolution** - Handles multiple proxy instances gracefully
✅ **Multi-Platform** - macOS, Linux, Windows support

---

## Quick Start (Automatic)

The proxy manager is **automatically integrated** into `start_system.py`:

```bash
# Just start JARVIS normally - proxy auto-starts if needed
python start_system.py
python start_system.py --restart
```

JARVIS will:
1. Check if proxy is running
2. Start it if needed (with health monitoring)
3. Load voice profiles from Cloud SQL
4. Enable voice biometric authentication

---

## Manual Management (Optional)

### Check Status
```bash
cd backend
python intelligence/cloud_sql_proxy_manager.py status
```

### Start Proxy
```bash
python intelligence/cloud_sql_proxy_manager.py start
```

### Stop Proxy
```bash
python intelligence/cloud_sql_proxy_manager.py stop
```

### Restart Proxy
```bash
python intelligence/cloud_sql_proxy_manager.py restart
```

---

## System Service Installation (Recommended)

Install proxy as system service to auto-start on boot:

### macOS (launchd)
```bash
cd backend
python intelligence/cloud_sql_proxy_manager.py install
```

This creates `~/Library/LaunchAgents/com.jarvis.cloudsql-proxy.plist`

**Verify:**
```bash
launchctl list | grep jarvis
```

**Uninstall:**
```bash
python intelligence/cloud_sql_proxy_manager.py uninstall
```

### Linux (systemd)
```bash
cd backend
python intelligence/cloud_sql_proxy_manager.py install
```

This creates `~/.config/systemd/user/com.jarvis.cloudsql-proxy.service`

**Verify:**
```bash
systemctl --user status com.jarvis.cloudsql-proxy
```

**Uninstall:**
```bash
python intelligence/cloud_sql_proxy_manager.py uninstall
```

---

## Configuration

All settings loaded from `~/.jarvis/gcp/database_config.json`:

```json
{
  "cloud_sql": {
    "instance_name": "jarvis-learning-db",
    "connection_name": "jarvis-473803:us-central1:jarvis-learning-db",
    "private_ip": "34.46.152.27",
    "database": "jarvis_learning",
    "user": "jarvis",
    "password": "YOUR_DB_PASSWORD_HERE",
    "port": 5432
  },
  "project_id": "jarvis-473803",
  "region": "us-central1"
}
```

**No hardcoded values!** All proxy settings derived from this config.

---

## How It Works

### Startup Flow

1. **`start_system.py` runs**
2. **Proxy Manager checks:**
   - Is proxy already running?
   - Is port 5432 available?
   - Is config file present?
3. **Auto-start if needed:**
   - Discovers proxy binary location
   - Reads config for connection details
   - Starts proxy with health monitoring
4. **JARVIS backend starts:**
   - Connects to Cloud SQL via proxy
   - Loads speaker profiles (voice biometrics)
   - Enables voice-authenticated unlock

### Runtime Monitoring

Proxy manager runs health checks every 60 seconds:
- If proxy dies → auto-restart
- If port blocked → resolve conflict
- If unrecoverable → fallback to SQLite

### Graceful Degradation

If Cloud SQL proxy fails, JARVIS automatically falls back to local SQLite:
- No crashes or errors
- Voice features still work (limited profiles)
- Warning logged for troubleshooting

---

## Troubleshooting

### Proxy Not Starting

**Check binary location:**
```bash
which cloud-sql-proxy
```

**Install if missing:**
```bash
gcloud components install cloud-sql-proxy
# OR download from: https://cloud.google.com/sql/docs/mysql/connect-admin-proxy
```

**Check config:**
```bash
cat ~/.jarvis/gcp/database_config.json
```

### Port Already in Use

Proxy manager automatically kills conflicting processes:
```bash
python intelligence/cloud_sql_proxy_manager.py restart --force
```

### Connection Timeout

**Verify GCP authentication:**
```bash
gcloud auth application-default login
```

**Test direct connection:**
```bash
cloud-sql-proxy jarvis-473803:us-central1:jarvis-learning-db --port 5432
```

### Voice Profiles Not Loading

**Check proxy is running:**
```bash
lsof -i :5432
```

**Check database connection:**
```bash
PGPASSWORD=YOUR_DB_PASSWORD_HERE psql -h 127.0.0.1 -U jarvis -d jarvis_learning -c "SELECT COUNT(*) FROM speaker_profiles;"
```

**Expected output:** `2` (Derek profiles)

### Logs

**Proxy logs:**
```bash
tail -f /tmp/cloud-sql-proxy.log
```

**JARVIS logs:**
```bash
tail -f backend/logs/jarvis_optimized_*.log | grep -E "Cloud SQL|Speaker|profiles"
```

---

## Advanced Usage

### Custom Config Path

```python
from intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager

manager = CloudSQLProxyManager(config_path="/custom/path/config.json")
manager.start()
```

### Programmatic Control

```python
from intelligence.cloud_sql_proxy_manager import get_proxy_manager
import asyncio

# Get singleton instance
manager = get_proxy_manager()

# Start with force restart
manager.start(force_restart=True)

# Start health monitor
await manager.monitor(check_interval=30)

# Stop gracefully
manager.stop()
```

### Health Check API

```python
if manager.is_running():
    print("✅ Proxy healthy")
else:
    print("❌ Proxy down, restarting...")
    manager.restart()
```

---

## Architecture

```
start_system.py
    ↓
CloudSQLProxyManager (intelligence/cloud_sql_proxy_manager.py)
    ↓
┌─────────────────────────────────────┐
│ 1. Auto-discover config & binary    │
│ 2. Check if running (port + PID)    │
│ 3. Start proxy if needed             │
│ 4. Monitor health (60s interval)    │
│ 5. Auto-recover on failure           │
└─────────────────────────────────────┘
    ↓
cloud-sql-proxy (GCP binary)
    ↓
127.0.0.1:5432 (local proxy)
    ↓
Cloud SQL (jarvis-473803:us-central1:jarvis-learning-db)
    ↓
JARVISLearningDatabase
    ↓
Speaker Profiles (voice biometrics)
```

---

## Benefits

### Before (Manual)
- ❌ Proxy must be started manually
- ❌ Dies on reboot
- ❌ No health monitoring
- ❌ Port conflicts unhandled
- ❌ Hardcoded connection strings

### After (Automated)
- ✅ Proxy auto-starts with JARVIS
- ✅ Persists across reboots (if installed as service)
- ✅ Self-healing with health monitor
- ✅ Automatic conflict resolution
- ✅ Zero hardcoding - all from config

---

## Security Notes

- ✅ Config file (`database_config.json`) contains sensitive credentials
- ✅ Stored in `~/.jarvis/gcp/` with restricted permissions
- ✅ Not checked into git
- ✅ Proxy uses GCP Application Default Credentials (ADC)
- ✅ Database password passed via environment variable (not command line)

---

## Testing

### Test Auto-Start
```bash
# Kill any existing proxy
pkill -f cloud-sql-proxy

# Start JARVIS - proxy should auto-start
python start_system.py

# Check logs for proxy startup
tail -f /tmp/cloud-sql-proxy.log
```

### Test Health Monitoring
```bash
# Start JARVIS
python start_system.py

# Kill proxy after 30 seconds
sleep 30 && pkill -f cloud-sql-proxy

# Monitor should detect and restart within 60 seconds
tail -f /tmp/cloud-sql-proxy.log
```

### Test Conflict Resolution
```bash
# Start two proxies manually (creates conflict)
cloud-sql-proxy jarvis-473803:us-central1:jarvis-learning-db --port 5432 &
cloud-sql-proxy jarvis-473803:us-central1:jarvis-learning-db --port 5432 &

# Start JARVIS - should kill conflicts and start clean
python start_system.py --restart
```

---

## FAQ

**Q: Do I need to install the proxy as a system service?**
A: No, runtime management works fine. System service is recommended for production (auto-start on boot).

**Q: What happens if proxy fails to start?**
A: JARVIS falls back to SQLite gracefully. Voice biometrics still work with local profiles.

**Q: Can I use a different port?**
A: Yes, change `port` in `database_config.json`. Proxy manager reads it dynamically.

**Q: Does this work on Windows?**
A: Yes! Proxy manager detects platform and adjusts accordingly (no systemd/launchd on Windows).

**Q: How do I check if voice profiles loaded?**
A: Look for `✅ Speaker Verification Service ready (2 profiles loaded)` in JARVIS logs.

---

## Credits

Created by Claude Code for the JARVIS AI Agent project.
Advanced, dynamic, robust - no hardcoding!
