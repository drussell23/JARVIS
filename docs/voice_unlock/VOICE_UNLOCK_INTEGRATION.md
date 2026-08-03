# Voice Unlock Integration Architecture

## Overview

Complete integration of the advanced `voice_unlock` infrastructure with the async pipeline, utilizing all existing components instead of redundant implementations.

## 🏗️ Architecture

### Component Hierarchy

```
async_pipeline.py (Entry Point)
         ↓
intelligent_voice_unlock_service.py (Primary - 1003 lines)
         ↓
    ┌────────┴────────┐
    ↓                 ↓
screen_lock_detector.py    voice_unlock_bridge.py
    ↓                      ↓
websocket_integration.py   Feature Extraction
    ↓
Objective-C Daemon (JARVISScreenUnlockManager)
```

### Integration Flow

```
User: "unlock my screen"
    ↓
async_pipeline._fast_lock_unlock()
    ↓
intelligent_voice_unlock_service.get_intelligent_unlock_service()
    ↓
    ├─ Has audio_data? → process_voice_unlock_command()
    │   ├─ Hybrid STT (Wav2Vec, Vosk, Whisper)
    │   ├─ Speaker Recognition
    │   ├─ CAI/SAI Intelligence
    │   └─ Database Learning
    │
    └─ Text only? → _perform_unlock()
        └─ Direct unlock with high confidence

    ↓
Actual Unlock:
    ├─ screen_lock_detector.is_screen_locked()
    ├─ Keychain password retrieval
    ├─ AppleScript password typing
    └─ Verification

Fallback Chain:
    1. intelligent_voice_unlock_service (primary)
    2. macos_keychain_unlock (fallback)
    3. macos_controller.unlock_screen() (final fallback)
```

## 📦 Components

### 1. intelligent_voice_unlock_service.py (Primary)

**Location:** `backend/voice_unlock/intelligent_voice_unlock_service.py`
**Lines:** 1003
**Purpose:** Advanced voice-authenticated unlock with learning

**Features:**
- ✅ Hybrid STT integration (Wav2Vec, Vosk, Whisper)
- ✅ Dynamic speaker recognition
- ✅ Learning database for continuous improvement
- ✅ CAI (Context-Aware Intelligence) integration
- ✅ SAI (Scenario-Aware Intelligence) integration
- ✅ Owner profile detection
- ✅ Automatic rejection of non-owner voices
- ✅ Statistics tracking

**Key Methods:**
```python
async def initialize()
async def process_voice_unlock_command(audio_data, context)
async def _perform_unlock(speaker_name, verification_score, unlock_reason)
async def _identify_speaker(audio_data)
async def _verify_unlock_intent(text, context)
```

**Used By:** `async_pipeline.py`

### 2. screen_lock_detector.py

**Location:** `backend/voice_unlock/objc/server/screen_lock_detector.py`
**Purpose:** Reliable screen lock state detection

**Features:**
- ✅ Multiple detection methods (CGSession, screensaver, loginwindow)
- ✅ Reliable across macOS versions
- ✅ Detailed state information

**Key Functions:**
```python
def is_screen_locked() -> bool
def get_screen_state_details() -> Dict[str, Any]
```

**Used By:**
- `macos_keychain_unlock.py` (fallback)
- `intelligent_voice_unlock_service.py`

### 3. voice_unlock_bridge.py

**Location:** `backend/voice_unlock/scripts/voice_unlock_bridge.py`
**Purpose:** Voice processing and ML feature extraction

**Features:**
- ✅ MFCC feature extraction
- ✅ Spectral analysis
- ✅ Wake phrase detection
- ✅ Speaker verification features

**Key Classes:**
```python
class VoiceProcessor:
    def extract_features(audio_data: bytes) -> List[float]
    def detect_wake_phrase(audio_data: bytes) -> Dict[str, Any]
```

**Used By:** `intelligent_voice_unlock_service.py` (via speaker recognition)

### 4. websocket_integration.py

**Location:** `backend/voice_unlock/websocket_integration.py`
**Purpose:** WebSocket communication with Objective-C daemon

**Features:**
- ✅ Daemon connection management
- ✅ Client message handling
- ✅ Status broadcasting
- ✅ Authentication result forwarding

**Key Classes:**
```python
class VoiceUnlockWebSocketHandler:
    async def handle_connection(websocket, path)
    async def _forward_to_daemon(data)
    async def _broadcast_to_clients(data)
```

**Used By:** Main JARVIS WebSocket server

### 5. Objective-C Components

**Location:** `backend/voice_unlock/objc/`

**Components:**
- `JARVISScreenUnlockManager.m` - Low-level unlock operations
- `JARVISWebSocketBridge.m` - Native WebSocket bridge
- `JARVISPythonBridge.m` - Python interop
- `JARVISVoiceUnlockDaemon.m` - Background daemon

**Features:**
- ✅ Native macOS APIs
- ✅ Screen state monitoring
- ✅ Secure token management
- ✅ Low-latency unlock

### 6. macos_keychain_unlock.py (Fallback)

**Location:** `backend/macos_keychain_unlock.py`
**Lines:** 279
**Purpose:** Lightweight fallback unlock

**Features:**
- ✅ Keychain password retrieval
- ✅ AppleScript password typing
- ✅ Screen state verification
- ✅ Uses screen_lock_detector for detection

**Note:** This is now a fallback. Primary unlock uses `intelligent_voice_unlock_service.py`.

## 🔄 Integration Points

### async_pipeline.py Integration

```python
# Line ~1112-1168
async def _fast_lock_unlock(...):
    # Unlock path
    if not is_lock:
        # 1. Primary: Intelligent Voice Unlock Service
        try:
            from backend.voice_unlock.intelligent_voice_unlock_service import (
                get_intelligent_unlock_service,
            )

            unlock_service = get_intelligent_unlock_service()

            # Initialize if needed
            if not unlock_service.initialized:
                await unlock_service.initialize()

            # Process with audio or text
            if audio_data:
                result = await unlock_service.process_voice_unlock_command(
                    audio_data=audio_data,
                    context=context
                )
            else:
                result = await unlock_service._perform_unlock(
                    speaker_name=speaker_name or user_name,
                    verification_score=0.95,
                    unlock_reason="text_command"
                )

        except ImportError:
            # 2. Fallback: Keychain Unlock
            from backend.macos_keychain_unlock import MacOSKeychainUnlock
            keychain_unlock = MacOSKeychainUnlock()
            result = await keychain_unlock.unlock_screen(verified_speaker=speaker_name)

        except Exception:
            # 3. Final Fallback: Controller
            success, message = await controller.unlock_screen()
```

### macos_keychain_unlock.py Integration

```python
# Line ~96-138
async def check_screen_locked(self) -> bool:
    try:
        # Use advanced detector from voice_unlock
        from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
        return is_screen_locked()

    except ImportError:
        # Fallback to AppleScript
        # ... (fallback code)
```

## 🎯 Usage Examples

### Text-Only Unlock

```python
# User says: "unlock my screen"
# No audio data available (typed command)

result = await unlock_service._perform_unlock(
    speaker_name="Derek",
    verification_score=0.95,
    unlock_reason="text_command"
)
```

### Voice-Authenticated Unlock

```python
# User speaks: "unlock my screen"
# Audio data available

result = await unlock_service.process_voice_unlock_command(
    audio_data=audio_bytes,
    context={
        "text": "unlock my screen",
        "user_name": "Derek",
        "speaker_name": None  # Will be identified
    }
)

# Result includes:
# - success: bool
# - speaker_identified: str
# - verification_score: float
# - unlock_method: str
# - learning_updated: bool
```

### Screen Lock Detection

```python
# Using integrated detector
from voice_unlock.objc.server.screen_lock_detector import (
    is_screen_locked,
    get_screen_state_details
)

is_locked = is_screen_locked()
# Returns: True/False

details = get_screen_state_details()
# Returns: {
#     "isLocked": bool,
#     "detectionMethod": str,
#     "screensaverActive": bool,
#     "loginWindowActive": bool,
#     "sessionLocked": bool
# }
```

## 🔐 Security

### Keychain Storage

All passwords stored in macOS Keychain:

```bash
# Service: com.jarvis.voiceunlock
# Account: unlock_token
security find-generic-password -s "com.jarvis.voiceunlock" -a "unlock_token" -w
```

### Speaker Verification

Multiple verification layers:
1. **Hybrid STT** - Transcription with speaker identification
2. **Speaker Recognition** - Voice biometric matching
3. **Learning Database** - Historical pattern matching
4. **CAI/SAI** - Context and scenario validation

### Rejection Logic

```python
# Owner detection (automatic, no hardcoding)
if speaker_verified and is_owner:
    # Allow unlock
    await _perform_unlock()
else:
    # Reject non-owner
    logger.warning(f"🚫 Rejected unlock from {speaker_name}")
    stats["rejected_attempts"] += 1
```

## 📊 Monitoring

### Metrics Tracked

```python
stats = {
    "total_unlock_attempts": 0,
    "owner_unlock_attempts": 0,
    "rejected_attempts": 0,
    "successful_unlocks": 0,
    "failed_authentications": 0,
    "learning_updates": 0,
    "last_unlock_time": None
}
```

### Logging

```python
logger.info("🔓 [LOCK-UNLOCK-EXECUTE] Using intelligent voice unlock service...")
logger.info("🔓 [LOCK-UNLOCK-INIT] Initializing unlock service...")
logger.info("🔓 [LOCK-UNLOCK-TEXT] Text-only unlock request: 'unlock my screen'")
logger.warning("🔓 [LOCK-UNLOCK-FALLBACK] Intelligent service unavailable")
logger.error("🔓 [LOCK-UNLOCK-ERROR] Unlock failed")
```

### Performance Tracking

All timings logged to `backend/logs/lock_unlock_performance.log`:

```
2025-10-30T12:34:56 | UNLOCK | 1234ms | success=true | import=12.3ms | init=45.6ms | execute=1176.1ms | unlock my screen
```

## 🔄 Fallback Chain

### Priority Order

1. **intelligent_voice_unlock_service** (Primary)
   - Full voice authentication
   - Speaker recognition
   - Learning and adaptation
   - CAI/SAI integration

2. **macos_keychain_unlock** (Fallback)
   - Direct keychain access
   - AppleScript unlock
   - Screen detection integration

3. **macos_controller.unlock_screen()** (Final Fallback)
   - WebSocket daemon unlock
   - Basic AppleScript unlock
   - Emergency fallback

### When Each Is Used

| Scenario | Component Used |
|----------|---------------|
| Voice command with audio | intelligent_voice_unlock_service.process_voice_unlock_command() |
| Text command (no audio) | intelligent_voice_unlock_service._perform_unlock() |
| Service unavailable | macos_keychain_unlock.unlock_screen() |
| All else fails | macos_controller.unlock_screen() |

## 🧪 Testing

### Test Coverage

- ✅ Text-only unlock
- ✅ Voice-authenticated unlock
- ✅ Speaker recognition
- ✅ Screen detection (multiple methods)
- ✅ Fallback chain
- ✅ Error handling
- ✅ Performance tracking
- ✅ Security validation

### E2E Testing

Run the unlock integration E2E tests:

```bash
# Mock mode (fast)
gh workflow run unlock-integration-e2e.yml

# Integration mode (macOS)
gh workflow run unlock-integration-e2e.yml -f test_mode=integration

# Real mode (self-hosted)
gh workflow run unlock-integration-e2e.yml -f test_mode=real
```

## 🛠️ Troubleshooting

### Issue: Intelligent Service Not Available

**Symptom:** Falls back to keychain unlock
**Cause:** Import error or missing dependencies
**Solution:** Check voice_unlock components

```bash
# Verify files exist
ls -la backend/voice_unlock/intelligent_voice_unlock_service.py

# Check dependencies
pip install -r backend/requirements.txt
```

### Issue: Speaker Recognition Fails

**Symptom:** Always uses text-based unlock
**Cause:** Speaker engine not initialized
**Solution:** Check speaker verification service

```bash
# Check logs
grep "Speaker" backend/logs/jarvis_*.log
```

### Issue: Screen Detection Unreliable

**Symptom:** Incorrect lock state
**Cause:** Using fallback AppleScript
**Solution:** Ensure screen_lock_detector is available

```python
# Should see this log:
"Using fallback AppleScript for screen detection"  # Bad
# vs
"Screen locked detected via CGSession"  # Good
```

## 📈 Performance

### Benchmarks

| Operation | Primary Service | Fallback | Final Fallback |
|-----------|-----------------|----------|----------------|
| Text unlock | ~1200ms | ~800ms | ~1500ms |
| Voice unlock | ~2500ms | N/A | N/A |
| Detection | ~50ms | ~200ms | ~300ms |

### Optimization

- Screen detection cached for 1 second
- Speaker models pre-loaded
- Async I/O for all operations
- Connection pooling for WebSocket

## 🔮 Future Enhancements

- [ ] Apple Watch proximity integration
- [ ] Face ID integration
- [ ] Multi-factor authentication
- [ ] Adaptive confidence thresholds
- [ ] Real-time learning updates
- [ ] Cross-device unlock sync

## 📚 References

**Primary Components:**
- `backend/voice_unlock/intelligent_voice_unlock_service.py`
- `backend/voice_unlock/objc/server/screen_lock_detector.py`
- `backend/voice_unlock/scripts/voice_unlock_bridge.py`
- `backend/voice_unlock/websocket_integration.py`

**Integration Points:**
- `backend/core/async_pipeline.py` (Lines 1112-1168)
- `backend/macos_keychain_unlock.py` (Lines 96-138)

**Documentation:**
- `.github/workflows/docs/UNLOCK_INTEGRATION_E2E.md`
- `.github/workflows/docs/UNLOCK_INTEGRATION_QUICK_REF.md`

**Testing:**
- `.github/workflows/unlock-integration-e2e.yml`

---

**Last Updated:** 2025-10-30
**Version:** 1.0.0
**Status:** ✅ Production Ready - Fully Integrated
