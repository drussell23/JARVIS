# Voice Unlock Quick Start Guide

## 🚀 One Command to Rule Them All

```bash
python start_system.py --restart
```

That's it! This will:
1. ✅ Auto-start CloudSQL proxy
2. ✅ Bootstrap your voice profiles to SQLite cache
3. ✅ Verify offline authentication readiness
4. ✅ Make voice unlock work immediately

## 🎯 What You'll See

```
============================================================
🔐 Voice Biometric System Initialization
============================================================
✅ CloudSQL Proxy started (PID: 12345)
✅ Listening on port 5432
✅ Voice biometric data access ready
============================================================

============================================================
🎤 Voice Profile Cache Bootstrap
============================================================
   Initializing voice cache system...
   📥 Bootstrapping voice profiles from CloudSQL...
   ✅ Bootstrap complete!
      • Cached profiles: 1
      • FAISS cache size: 1 embeddings
      • Ready for offline authentication

   🔍 Verifying voice authentication readiness...
   ✅ SQLite cache ready: 1 profile(s)
      • Derek J. Russell: 59 samples
   ✅ Voice cache system ready
============================================================
```

## 🗣️ Test Voice Unlock

After JARVIS starts:

```
You: "Hey JARVIS, unlock my screen"

JARVIS: "Of course, Derek. Unlocking your screen now."
[Screen unlocks] ✅
```

**Expected Confidence:** 95%+ (not 0%!)

## 🔍 Troubleshooting

### Still Getting 0% Confidence?

**1. Check Cache Status:**
```bash
sqlite3 ~/.jarvis/jarvis_learning.db "SELECT speaker_name, total_samples FROM speaker_profiles"
```

**Expected Output:**
```
Derek J. Russell|59
```

**2. Check Logs:**
```bash
grep "Bootstrap\|Voice cache" ~/Documents/repos/JARVIS-AI-Agent/jarvis_startup.log
```

**3. Force Re-Bootstrap:**
```bash
# Delete cache and restart
rm ~/.jarvis/jarvis_learning.db
python start_system.py --restart
```

### CloudSQL Proxy Won't Start?

**Check if already running:**
```bash
pgrep -fl cloud-sql-proxy
```

**Kill and restart:**
```bash
pkill cloud-sql-proxy
python start_system.py --restart
```

### No Voice Samples in CloudSQL?

**Enroll your voice:**
```bash
# Use the voice enrollment script
python backend/voice/enroll_voice.py --name "Derek J. Russell"
```

Then restart JARVIS to pull the new profile.

## 📊 Architecture Summary

```
┌──────────────────────────────────────────────────────┐
│  start_system.py --restart                           │
├──────────────────────────────────────────────────────┤
│                                                       │
│  1. Start CloudSQL Proxy                            │
│     └─> Auto-detects if not running                 │
│     └─> Starts on 127.0.0.1:5432                   │
│                                                       │
│  2. Bootstrap Voice Profiles                        │
│     ├─> Check cache staleness                       │
│     ├─> Pull from CloudSQL (one-time)              │
│     ├─> Insert into SQLite                          │
│     └─> Load into FAISS cache                       │
│                                                       │
│  3. Verify Readiness                                │
│     ├─> List cached profiles                        │
│     ├─> Show sample counts                          │
│     └─> Confirm offline capability                  │
│                                                       │
│  4. Start JARVIS                                    │
│     └─> Voice unlock ready! ✅                      │
│                                                       │
└──────────────────────────────────────────────────────┘
```

## 🎉 Benefits

| Feature | Before | After |
|---------|--------|-------|
| **Voice Unlock** | ❌ 0% confidence | ✅ 95%+ confidence |
| **Setup** | ❌ Manual steps | ✅ Automatic |
| **CloudSQL** | ❌ Required always | ✅ One-time only |
| **Offline Auth** | ❌ Not possible | ✅ Fully supported |
| **Cache Refresh** | ❌ Manual | ✅ Auto every 5min |

## 📝 Next Steps

After successful bootstrap:
1. **Test voice unlock** - Say "Hey JARVIS, unlock my screen"
2. **Test offline mode** - Stop CloudSQL proxy, try unlock again (should still work!)
3. **Enroll more samples** - Improve accuracy by enrolling more voice samples
4. **Check metrics** - Monitor cache performance via logs

---

**Quick Command:**
```bash
python start_system.py --restart
```

**That's it! Your voice unlock is now fully automatic and offline-capable.** 🚀
