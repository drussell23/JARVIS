# Multi-Factor Authentication Configuration Guide

## Overview

JARVIS v5.0 introduces advanced Multi-Factor Authentication Intelligence that combines multiple contextual signals for enhanced security and reliability:

1. **Voice Biometric Intelligence** - ECAPA-TDNN speaker verification
2. **Network Context Provider** - WiFi/location awareness
3. **Unlock Pattern Tracker** - Temporal behavioral patterns
4. **Device State Monitor** - Physical device state tracking
5. **Voice Drift Detector** - Voice evolution and anomaly detection
6. **Multi-Factor Fusion Engine** - Bayesian probability fusion

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Voice Authentication Request                │
└────────────────────┬────────────────────────────────────────┘
                     │
        ┌────────────┴─────────────┐
        │                          │
        v                          v
┌───────────────┐         ┌──────────────┐
│ Voice         │         │ Contextual   │
│ Biometric     │         │ Intelligence │
│ (ECAPA-TDNN)  │         │ Gathering    │
│               │         │              │
│ • Embedding   │         │ • Network    │
│ • Similarity  │         │ • Temporal   │
│ • Confidence  │         │ • Device     │
│               │         │ • Drift      │
└───────┬───────┘         └──────┬───────┘
        │                        │
        └────────────┬───────────┘
                     │
                     v
        ┌────────────────────────┐
        │  Multi-Factor Fusion   │
        │  Engine                │
        │                        │
        │  • Bayesian Fusion     │
        │  • Risk Assessment     │
        │  • Decision Making     │
        │  • Learning           │
        └────────┬───────────────┘
                 │
                 v
        ┌────────────────┐
        │ Authentication │
        │ Decision       │
        │                │
        │ • Authenticate │
        │ • Challenge    │
        │ • Deny         │
        │ • Escalate     │
        └────────────────┘
```

## Configuration

### Environment Variables

#### Multi-Factor Fusion Engine

```bash
# Decision Thresholds
AUTH_FUSION_AUTH_THRESHOLD=0.85          # Grant access above this confidence
AUTH_FUSION_CHALLENGE_THRESHOLD=0.70     # Challenge between this and auth threshold
AUTH_FUSION_DENY_THRESHOLD=0.70          # Deny below this threshold

# Factor Weights (must sum to 1.0)
AUTH_FUSION_VOICE_WEIGHT=0.50            # Voice biometric weight (primary factor)
AUTH_FUSION_NETWORK_WEIGHT=0.15          # Network context weight
AUTH_FUSION_TEMPORAL_WEIGHT=0.15         # Temporal pattern weight
AUTH_FUSION_DEVICE_WEIGHT=0.12           # Device state weight
AUTH_FUSION_DRIFT_WEIGHT=0.08            # Drift adjustment weight

# Risk Assessment
AUTH_FUSION_RISK_ASSESSMENT=true         # Enable risk scoring
AUTH_FUSION_HIGH_RISK_THRESHOLD=0.70     # High risk threshold

# Continuous Learning
AUTH_FUSION_CONTINUOUS_LEARNING=true     # Enable learning from successful auth
AUTH_FUSION_MIN_LEARN_CONF=0.90          # Minimum confidence to learn

# Fusion Methods
AUTH_FUSION_METHOD=bayesian              # "bayesian", "weighted", "unanimous"
AUTH_FUSION_UNANIMOUS_VETO=true          # Allow any factor to veto
AUTH_FUSION_VETO_THRESHOLD=0.30          # Veto if factor below this
```

#### Network Context Provider

```bash
# Network Trust Configuration
NETWORK_TRUSTED_THRESHOLD=0.90           # Threshold for trusted network
NETWORK_KNOWN_THRESHOLD=0.75             # Threshold for known network
NETWORK_UNKNOWN_PENALTY=0.50             # Trust score for unknown networks

# Pattern Learning
NETWORK_MIN_SUCCESSFUL_UNLOCKS=3         # Min unlocks to trust network
NETWORK_PATTERN_TRACKING=true            # Track network unlock patterns

# Connection Stability
NETWORK_STABILITY_WINDOW_SECONDS=300     # Window for stability check
NETWORK_CHECK_INTERVAL_SECONDS=30        # Interval for stability monitoring

# Storage
JARVIS_DATA_DIR=/path/to/jarvis/data     # Base data directory
NETWORK_HISTORY_FILE=network_history.json
NETWORK_MAX_HISTORY=1000                 # Max history events
```

#### Unlock Pattern Tracker

```bash
# Temporal Analysis
PATTERN_TYPICAL_TIME_THRESHOLD=0.80      # Threshold for typical time
PATTERN_ANOMALY_THRESHOLD=0.70           # Threshold for anomaly detection
PATTERN_CONFIDENCE_BOOST=0.10            # Boost for typical patterns

# Learning Configuration
PATTERN_MIN_SAMPLES=10                   # Min samples for pattern analysis
PATTERN_LEARNING_WINDOW_DAYS=30          # Days of history for learning
PATTERN_RECOMPUTE_INTERVAL=10            # Recompute every N events

# Storage
PATTERN_HISTORY_FILE=unlock_patterns.json
PATTERN_MAX_HISTORY=2000
```

#### Device State Monitor

```bash
# Movement Detection
DEVICE_MOVEMENT_CHECK_INTERVAL=30        # Seconds between movement checks
DEVICE_STATIONARY_THRESHOLD=300          # Seconds to consider stationary

# Wake Detection
DEVICE_WAKE_THRESHOLD=300                # Seconds after wake = "just woke"

# Trust Scoring
DEVICE_STATIONARY_BOOST=0.15             # Trust boost for stationary device
DEVICE_DOCKED_BOOST=0.12                 # Trust boost for docked device
DEVICE_WAKE_PENALTY=-0.10                # Penalty if just woke (groggier voice)
DEVICE_MOVING_PENALTY=-0.20              # Penalty if device moving

# Storage
DEVICE_STATE_HISTORY_FILE=device_state_history.json
DEVICE_MAX_HISTORY=500
```

#### Voice Drift Detector

```bash
# Drift Thresholds
VOICE_DRIFT_THRESHOLD=0.05               # Significant drift threshold
VOICE_DRIFT_SEVERE_THRESHOLD=0.15        # Severe drift threshold

# Adaptation
VOICE_DRIFT_ADAPTATION_RATE=0.10         # Baseline adaptation rate
VOICE_DRIFT_AUTO_ADAPTATION=true         # Auto-adapt baseline

# Analysis Windows
VOICE_DRIFT_MIN_SAMPLES=5                # Min samples for analysis
VOICE_DRIFT_ANALYSIS_WINDOW_DAYS=30      # Days for drift analysis
VOICE_DRIFT_SHORT_TERM_HOURS=24          # Short-term drift window
VOICE_DRIFT_SEASONAL_PERIOD_DAYS=90      # Seasonal drift period

# Cause Detection
VOICE_DRIFT_ILLNESS_THRESHOLD=0.08       # Drift suggesting illness
VOICE_DRIFT_EQUIPMENT_THRESHOLD=0.12     # Drift suggesting equipment change
```

## Usage Examples

### Basic Authentication

```python
from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence

# Get VBI instance
vbi = await get_voice_biometric_intelligence()

# Perform authentication (multi-factor automatically enabled)
result = await vbi.verify_and_announce(
    audio_data=audio_bytes,
    context={'user_id': 'derek'},
    speak=True
)

# Check result
if result.verified:
    print(f"Authenticated: {result.speaker_name}")
    print(f"Confidence: {result.confidence:.1%}")
    print(f"Multi-Factor Decision: {result.multi_factor_decision}")
    print(f"Risk Score: {result.risk_score:.1%}")
else:
    print(f"Authentication failed: {result.level.value}")
```

### Accessing Multi-Factor Details

```python
# Get detailed multi-factor analysis
if hasattr(result, 'multi_factor_fusion'):
    fusion_data = result.multi_factor_fusion

    print(f"\nVoice Confidence: {fusion_data['voice_confidence']:.1%}")
    print(f"Network Trust: {fusion_data['network_trust']:.1%}")
    print(f"Temporal Confidence: {fusion_data['temporal_confidence']:.1%}")
    print(f"Device Trust: {fusion_data['device_trust']:.1%}")
    print(f"Drift Adjustment: {fusion_data['drift_adjustment']:+.1%}")

    print(f"\nFinal Decision: {fusion_data['decision']}")
    print(f"Final Confidence: {fusion_data['final_confidence']:.1%}")
    print(f"Risk Score: {fusion_data['risk_score']:.1%}")

    print(f"\nReasoning:")
    for reason in fusion_data['reasoning']:
        print(f"  • {reason}")

    if fusion_data['anomalies']:
        print(f"\nAnomalies Detected:")
        for anomaly in fusion_data['anomalies']:
            print(f"  ⚠️  {anomaly}")
```

### Manual Context Gathering

```python
from intelligence.network_context_provider import get_network_provider
from intelligence.unlock_pattern_tracker import get_pattern_tracker
from intelligence.device_state_monitor import get_device_monitor

# Gather individual contexts
network_provider = await get_network_provider()
network_ctx = await network_provider.get_network_context()

pattern_tracker = await get_pattern_tracker()
temporal_ctx = await pattern_tracker.get_unlock_context()

device_monitor = await get_device_monitor()
device_ctx = await device_monitor.get_device_context()

print(f"Network: {network_ctx.ssid_trust_level.value} ({network_ctx.trust_score:.0%})")
print(f"Timing: {'typical' if temporal_ctx.is_typical_time else 'unusual'}")
print(f"Device: {device_ctx.state.value} ({device_ctx.trust_score:.0%})")
```

### Direct Fusion Engine Usage

```python
from intelligence.multi_factor_auth_fusion import get_fusion_engine

# Get fusion engine
fusion = await get_fusion_engine()

# Run fusion with explicit contexts
result = await fusion.fuse_and_decide(
    user_id="derek",
    voice_confidence=0.92,
    voice_reasoning="Strong voice match",
    network_context={
        'trust_score': 0.95,
        'confidence': 0.90,
        'ssid_trust_level': 'trusted',
        'reasoning': 'Home network'
    },
    temporal_context={
        'confidence': 0.88,
        'is_typical_time': True,
        'anomaly_score': 0.0,
        'reasoning': 'Typical morning unlock'
    },
    device_context={
        'trust_score': 0.92,
        'confidence': 0.90,
        'state': 'stationary',
        'is_stationary': True,
        'is_docked': True,
        'reasoning': 'Docked workstation'
    }
)

print(f"Decision: {result.decision.value}")
print(f"Confidence: {result.final_confidence:.1%}")
print(f"Risk: {result.risk_score:.1%}")
```

## Security Model

### Confidence Levels

| Level | Confidence Range | Action | Description |
|-------|-----------------|--------|-------------|
| **Instant** | >92% | ✅ Authenticate | Immediate unlock - very high confidence |
| **Confident** | 85-92% | ✅ Authenticate | Clear match - unlock granted |
| **Good** | 75-85% | ✅ Authenticate | Solid match - unlock granted |
| **Borderline** | 70-75% | ⚠️ Challenge | Requires security question |
| **Uncertain** | 60-70% | ❌ Deny | Not confident - deny access |
| **Failed** | <60% | ❌ Deny | Poor match - deny access |

### Risk Scoring

Risk scores combine:
- Low voice confidence (+40% risk)
- Unknown network (+15% risk)
- Unusual timing (up to +20% risk based on anomaly score)
- Device movement (+10% risk)
- Factor disagreement (up to +15% risk based on variance)

**High Risk Threshold**: 70% - Triggers escalation and additional verification

### Authentication Decisions

1. **AUTHENTICATE** (confidence ≥ 85%, low risk)
   - Grant immediate access
   - Record for learning if confidence ≥ 90%
   - All factors aligned positively

2. **CHALLENGE** (70% ≤ confidence < 85%)
   - Ask security question
   - Moderate confidence but manageable risk
   - One or more factors borderline

3. **DENY** (confidence < 70%)
   - Refuse access
   - Require password authentication
   - Low confidence or significant anomalies

4. **ESCALATE** (risk ≥ 70%)
   - Immediate denial
   - Log security event
   - Alert user of suspicious activity
   - Multiple anomalies detected

## Tuning for Your Environment

### High Security Mode

For maximum security (reduce false positives):

```bash
# Stricter thresholds
AUTH_FUSION_AUTH_THRESHOLD=0.90          # Require 90% confidence
AUTH_FUSION_CHALLENGE_THRESHOLD=0.80     # Challenge at 80%
VOICE_UNLOCK_BASE_THRESHOLD=0.85         # Higher voice threshold
VOICE_DRIFT_AUTO_ADAPTATION=false        # Disable auto-adaptation
AUTH_FUSION_HIGH_RISK_THRESHOLD=0.60     # Lower risk tolerance

# Increase voice weight
AUTH_FUSION_VOICE_WEIGHT=0.60            # 60% voice
AUTH_FUSION_NETWORK_WEIGHT=0.15          # 15% network
AUTH_FUSION_TEMPORAL_WEIGHT=0.12         # 12% temporal
AUTH_FUSION_DEVICE_WEIGHT=0.10           # 10% device
AUTH_FUSION_DRIFT_WEIGHT=0.03            # 3% drift
```

### Convenience Mode

For better user experience (reduce false negatives):

```bash
# Relaxed thresholds
AUTH_FUSION_AUTH_THRESHOLD=0.80          # Allow 80% confidence
AUTH_FUSION_CHALLENGE_THRESHOLD=0.65     # Challenge at 65%
VOICE_UNLOCK_BASE_THRESHOLD=0.75         # Lower voice threshold
VOICE_DRIFT_AUTO_ADAPTATION=true         # Enable auto-adaptation
AUTH_FUSION_HIGH_RISK_THRESHOLD=0.80     # Higher risk tolerance

# Increase contextual weights
AUTH_FUSION_VOICE_WEIGHT=0.45            # 45% voice
AUTH_FUSION_NETWORK_WEIGHT=0.18          # 18% network
AUTH_FUSION_TEMPORAL_WEIGHT=0.18         # 18% temporal
AUTH_FUSION_DEVICE_WEIGHT=0.12           # 12% device
AUTH_FUSION_DRIFT_WEIGHT=0.07            # 7% drift
```

### Balanced Mode (Default)

Current default configuration provides excellent security and reliability:

```bash
AUTH_FUSION_AUTH_THRESHOLD=0.85
AUTH_FUSION_VOICE_WEIGHT=0.50
AUTH_FUSION_NETWORK_WEIGHT=0.15
AUTH_FUSION_TEMPORAL_WEIGHT=0.15
AUTH_FUSION_DEVICE_WEIGHT=0.12
AUTH_FUSION_DRIFT_WEIGHT=0.08
```

## Monitoring and Debugging

### Statistics

Get fusion engine statistics:

```python
from intelligence.multi_factor_auth_fusion import get_fusion_engine

fusion = await get_fusion_engine()
stats = fusion.get_stats()

print(f"Total Authentications: {stats['total_authentications']}")
print(f"Authenticated: {stats['authenticated']} ({stats['authenticate_rate']:.1%})")
print(f"Challenged: {stats['challenged']} ({stats['challenge_rate']:.1%})")
print(f"Denied: {stats['denied']} ({stats['deny_rate']:.1%})")
print(f"Escalated: {stats['escalated']} ({stats['escalate_rate']:.1%})")
print(f"Avg Confidence: {stats['avg_confidence']:.1%}")
print(f"Avg Risk: {stats['avg_risk_score']:.1%}")
```

### Component Statistics

```python
# Network Provider
from intelligence.network_context_provider import get_network_provider
network_provider = await get_network_provider()
network_stats = await network_provider.get_statistics()

# Pattern Tracker
from intelligence.unlock_pattern_tracker import get_pattern_tracker
pattern_tracker = await get_pattern_tracker()
pattern_stats = await pattern_tracker.get_statistics()

# Device Monitor
from intelligence.device_state_monitor import get_device_monitor
device_monitor = await get_device_monitor()
device_stats = await device_monitor.get_statistics()
```

### Logging

Enable debug logging:

```python
import logging

# Enable debug logs for all intelligence components
logging.getLogger('intelligence').setLevel(logging.DEBUG)
logging.getLogger('voice_unlock').setLevel(logging.DEBUG)

# Or specific components
logging.getLogger('intelligence.network_context_provider').setLevel(logging.DEBUG)
logging.getLogger('intelligence.multi_factor_auth_fusion').setLevel(logging.DEBUG)
```

## Troubleshooting

### Issue: High false negative rate

**Symptoms**: Legitimate owner being denied access

**Solutions**:
1. Lower authentication threshold: `AUTH_FUSION_AUTH_THRESHOLD=0.80`
2. Enable voice drift auto-adaptation: `VOICE_DRIFT_AUTO_ADAPTATION=true`
3. Increase contextual factor weights
4. Check voice profile quality (need more samples?)

### Issue: Concerns about false positives

**Symptoms**: Worried about unauthorized access

**Solutions**:
1. Increase authentication threshold: `AUTH_FUSION_AUTH_THRESHOLD=0.90`
2. Increase voice weight: `AUTH_FUSION_VOICE_WEIGHT=0.60`
3. Enable unanimous veto: `AUTH_FUSION_UNANIMOUS_VETO=true`
4. Lower risk threshold: `AUTH_FUSION_HIGH_RISK_THRESHOLD=0.60`

### Issue: "Unknown network" always flagged

**Symptoms**: New locations always suspicious

**Solutions**:
1. Increase network learning rate by lowering min unlocks: `NETWORK_MIN_SUCCESSFUL_UNLOCKS=2`
2. Reduce unknown network penalty: `NETWORK_UNKNOWN_PENALTY=0.60`
3. Lower network context weight: `AUTH_FUSION_NETWORK_WEIGHT=0.10`

### Issue: Morning voice not recognized

**Symptoms**: Authentication fails after waking up

**Solutions**:
1. Enable drift detector: Automatically handles morning voice
2. Reduce wake penalty: `DEVICE_WAKE_PENALTY=-0.05`
3. Enable voice adaptation: `VOICE_DRIFT_AUTO_ADAPTATION=true`

## Migration Guide

### From Voice-Only Authentication

The multi-factor system is fully backward compatible. To migrate:

1. **No code changes required** - Existing code continues to work
2. **Gradual rollout** - Multi-factor enhances existing authentication
3. **Learning phase** - System learns patterns over 1-2 weeks
4. **Monitor stats** - Check confidence improvements
5. **Tune as needed** - Adjust weights based on your usage

### Disabling Multi-Factor (Not Recommended)

To disable multi-factor fusion:

```bash
# In VBI config or environment
VBI_ENABLE_MULTI_FACTOR_FUSION=false
```

This reverts to voice-only + behavioral context authentication.

## Best Practices

1. **Start with defaults** - Default configuration works well for most users
2. **Monitor for 1-2 weeks** - Let learning systems gather patterns
3. **Review statistics** - Check authentication rates and confidence scores
4. **Tune gradually** - Make small adjustments (±5-10%) when needed
5. **Test edge cases** - Different locations, times, device states
6. **Enable learning** - Continuous learning improves accuracy over time
7. **Regular profile updates** - Enroll new voice samples periodically
8. **Security first** - When in doubt, prefer higher thresholds

## Performance Impact

- **Latency**: +50-100ms for multi-factor gathering (parallel execution)
- **Memory**: +5-10MB for intelligence providers (lazy-loaded)
- **Storage**: ~100KB per user for pattern history
- **CPU**: Minimal (mostly I/O for context gathering)

Multi-factor fusion runs in parallel with existing verification, so total authentication time increases minimally.

## Privacy Considerations

- **Network SSIDs**: Hashed with SHA-256 before storage (privacy-preserving)
- **Location data**: Only network-based, no GPS tracking
- **Temporal patterns**: Only hour/day statistics, no detailed logs
- **Device state**: Physical state only, no content or personal data
- **Voice data**: Embeddings only, raw audio not stored

All intelligence data stored locally in `~/.jarvis/intelligence/`.

## Support

For issues, questions, or feature requests related to multi-factor authentication:

1. Check this configuration guide
2. Review logs for specific error messages
3. Test with debug logging enabled
4. Report issues with detailed context (thresholds, stats, logs)

---

**JARVIS Multi-Factor Authentication v5.0**
*Intelligent, adaptive, context-aware voice authentication*
