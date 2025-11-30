# JARVIS Postman Flows

## Voice Unlock Authentication Flow

The primary authentication pipeline for JARVIS voice biometric unlock system.

### Flow Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                         JARVIS VOICE UNLOCK AUTHENTICATION FLOW                      │
└─────────────────────────────────────────────────────────────────────────────────────┘

                                    ┌──────────────────┐
                                    │   Voice Unlock   │
                                    │     Request      │
                                    │   (audio_base64) │
                                    └────────┬─────────┘
                                             │
                                             ▼
                                    ┌──────────────────┐
                                    │  Check System    │
                                    │     Health       │
                                    └────────┬─────────┘
                                             │
                              ┌──────────────┴──────────────┐
                              │                             │
                         [Healthy]                    [Unhealthy]
                              │                             │
                              ▼                             ▼
                    ┌──────────────────┐          ┌──────────────────┐
                    │  Start Langfuse  │          │     Return       │
                    │  Audit Session   │          │   Unavailable    │
                    └────────┬─────────┘          └──────────────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │   Anti-Spoofing  │
                    │  Replay Attack   │
                    │    Detection     │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
       [No Attack]                   [Replay Detected]
              │                             │
              ▼                             ▼
    ┌──────────────────┐          ┌──────────────────┐
    │  Enhanced Voice  │          │  Log Security    │
    │  Authentication  │          │  Alert & Block   │
    │   (ECAPA-TDNN)   │          └──────────────────┘
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │    Evaluate      │
    │   Confidence     │
    └────────┬─────────┘
             │
    ┌────────┴────────────────────────────────────┐
    │                    │                         │
    │              ┌─────┴─────┐            ┌──────┴──────┐
    │              │           │            │             │
[≥90%]         [85-90%]    [75-85%]       [<75%]
High Conf       Pass       Borderline     Low Conf
    │              │            │             │
    ▼              ▼            ▼             ▼
┌────────┐   ┌──────────┐  ┌──────────┐  ┌──────────────┐
│ Quick  │   │ Standard │  │ Enhanced │  │   Generate   │
│ Unlock │   │ Fusion   │  │  Fusion  │  │    Retry     │
│        │   │ (70/20/10)│ │(50/35/15)│  │   Guidance   │
└───┬────┘   └────┬─────┘  └────┬─────┘  └──────────────┘
    │             │             │
    │        [≥85%?]       [≥80%?]
    │         │   │         │   │
    │        Yes  No       Yes  No
    │         │   │         │   │
    │         │   ▼         │   ▼
    │         │ ┌─────────┐ │ ┌───────────┐
    │         │ │ Request │ │ │ Challenge │
    │         │ │ Clarity │ │ │ Question  │
    │         │ └─────────┘ │ └───────────┘
    │         │             │
    └────┬────┴─────────────┘
         │
         ▼
┌──────────────────┐
│  Generate JARVIS │
│    Feedback      │
│   (Personalized) │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   Unlock Screen  │
│   (via Keychain) │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   End Audit      │
│   Session        │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Return Success  │
│  + JARVIS Speaks │
└──────────────────┘
```

### Confidence Levels & Routing

| Confidence | Route | Multi-Factor Weights | Action |
|------------|-------|---------------------|--------|
| ≥ 90% | High Confidence | N/A (instant) | Direct unlock |
| 85-90% | Standard Fusion | Voice 70%, Behavioral 20%, Context 10% | Fusion → Unlock if ≥85% |
| 75-85% | Enhanced Fusion | Voice 50%, Behavioral 35%, Context 15% | Fusion → Unlock if ≥80%, else Challenge |
| < 75% | Low Confidence | N/A | Retry guidance with suggestions |

### Multi-Factor Fusion Components

**Voice Factors:**
- ECAPA-TDNN embedding similarity (cosine distance)
- Voice quality (SNR in dB)
- Embedding quality score
- Anomaly detection (illness, stress indicators)

**Behavioral Factors:**
- Time of day (typical unlock time)
- Time since last unlock
- Same WiFi network
- Device movement since lock

**Context Factors:**
- Microphone type (learned profiles)
- Location (home vs office)
- Ambient noise level

### Anti-Spoofing Checks

1. **Exact Audio Match** - Detects replayed recordings
2. **Spectral Fingerprint** - Catches modified recordings
3. **Environmental Signature** - Detects static background (recording artifact)
4. **Liveness Detection** - Micro-variations in live speech

### JARVIS Response Examples

**High Confidence (≥90%):**
```
"Of course, Derek. Unlocking for you."
```

**Pass with Fusion (85-90%):**
```
"Good morning, Derek. Unlocking now."
```

**Borderline Success (75-85% + fusion):**
```
"Your voice sounds a bit different today - are you feeling alright?
Your behavioral patterns match perfectly though. Unlocking for you."
```

**Retry Needed:**
```
"I'm having trouble hearing you clearly. Could you try again,
maybe speak a bit louder and closer to the microphone?"
```

**Challenge Required:**
```
"I need a quick verification. What was the last project
you worked on yesterday?"
```

**Security Alert:**
```
"Security alert: I detected characteristics consistent with
a voice recording. Access denied. This attempt has been logged."
```

### API Endpoints Used

| Block | Endpoint | Method |
|-------|----------|--------|
| Health Check | `/api/voice-auth-intelligence/health` | GET |
| Start Audit | `/api/voice-auth-intelligence/audit/session/start` | POST |
| Replay Detection | `/api/voice-auth-intelligence/patterns/detect-replay` | POST |
| Authentication | `/api/voice-auth-intelligence/authenticate/enhanced` | POST |
| Multi-Factor Fusion | `/api/voice-auth-intelligence/fusion/calculate` | POST |
| Generate Feedback | `/api/voice-auth-intelligence/feedback/generate` | POST |
| Cache Pattern | `/api/voice-auth-intelligence/patterns/store` | POST |
| Security Alert | `/api/voice-auth-intelligence/feedback/security-alert` | POST |
| Unlock Screen | `/api/screen/unlock` | POST |
| End Audit | `/api/voice-auth-intelligence/audit/session/end` | POST |

### Environment Variables

Set these in your Postman environment:

```json
{
  "base_url": "http://localhost:8010",
  "confidence_threshold_high": 0.90,
  "confidence_threshold_pass": 0.85,
  "confidence_threshold_borderline": 0.75,
  "max_retry_attempts": 3,
  "enable_anti_spoofing": true,
  "enable_behavioral_fusion": true,
  "enable_audit_trail": true
}
```

### Monitoring & Alerts

**Metrics Tracked:**
- Latency per block
- Overall success rate
- Confidence score distribution
- Retry rate
- Security events (replay attacks, unknown speakers)

**Alert Conditions:**
- `security_events > 3 in 5m` → Critical
- `success_rate < 80%` → Warning
- `avg_latency > 5000ms` → Warning

### Import Instructions

1. Open Postman
2. Go to **Flows** tab
3. Click **Import**
4. Select `voice_unlock_authentication_flow.json`
5. Configure environment variables
6. Deploy as Action (optional) for external triggering

### Testing the Flow

**Simulate Success:**
```bash
curl -X POST http://localhost:8010/api/voice-auth-intelligence/authenticate/simulate \
  -H "Content-Type: application/json" \
  -d '{"scenario": "success", "speaker_name": "Derek"}'
```

**Simulate Replay Attack:**
```bash
curl -X POST http://localhost:8010/api/voice-auth-intelligence/authenticate/simulate \
  -H "Content-Type: application/json" \
  -d '{"scenario": "replay_attack", "speaker_name": "Derek"}'
```

**Simulate Borderline:**
```bash
curl -X POST http://localhost:8010/api/voice-auth-intelligence/authenticate/simulate \
  -H "Content-Type: application/json" \
  -d '{"scenario": "borderline", "speaker_name": "Derek"}'
```

### Deployment as Cloud Action

When deployed to Postman Cloud, this flow becomes a public API endpoint:

```
POST https://api.getpostman.com/flows/<flow-id>/execute
Content-Type: application/json

{
  "audio_base64": "<base64_encoded_audio>",
  "speaker_name": "Derek",
  "device_context": {
    "microphone": "MacBook Pro Microphone",
    "location": "home",
    "ambient_noise_db": 35
  },
  "behavioral_context": {
    "last_unlock_time": "2024-12-01T06:00:00Z",
    "typical_unlock_hour": 7,
    "same_wifi_network": true,
    "device_moved_since_lock": false
  }
}
```

This enables:
- Mobile app voice unlock
- Home automation integration
- Remote unlock via webhooks
- Third-party AI agent integration
