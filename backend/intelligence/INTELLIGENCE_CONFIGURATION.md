# Intelligence System Configuration Guide

**Version:** 5.0.0
**Component:** Intelligence Component Manager
**Last Updated:** 2024-12-22

## Overview

The Intelligence System orchestrates all intelligence providers for voice authentication, providing multi-factor authentication fusion, RAG (Retrieval-Augmented Generation), and RLHF (Reinforcement Learning from Human Feedback).

This guide documents all configuration options via environment variables - **NO hardcoding required**.

---

## Architecture

The Intelligence Component Manager orchestrates 5 core components:

1. **Network Context Provider** - Learns trusted networks and provides confidence boosts
2. **Unlock Pattern Tracker** - Learns temporal patterns (time-of-day, day-of-week)
3. **Device State Monitor** - Tracks device state (idle time, battery, location)
4. **Multi-Factor Auth Fusion Engine** - Bayesian probability fusion of all signals
5. **Intelligence Learning Coordinator** - RAG + RLHF continuous learning system

---

## Global Intelligence Settings

### `INTELLIGENCE_ENABLED`
**Type:** Boolean
**Default:** `true`
**Description:** Master switch for entire intelligence system

```bash
# Disable all intelligence (voice-only authentication)
export INTELLIGENCE_ENABLED=false

# Enable intelligence (recommended)
export INTELLIGENCE_ENABLED=true
```

---

### `INTELLIGENCE_PARALLEL_INIT`
**Type:** Boolean
**Default:** `true`
**Description:** Initialize components in parallel for faster startup

```bash
# Parallel initialization (faster, recommended)
export INTELLIGENCE_PARALLEL_INIT=true

# Sequential initialization (slower, more reliable)
export INTELLIGENCE_PARALLEL_INIT=false
```

**Performance Impact:**
- Parallel: ~2-3 seconds startup
- Sequential: ~5-7 seconds startup

---

### `INTELLIGENCE_INIT_TIMEOUT`
**Type:** Integer (seconds)
**Default:** `30`
**Description:** Maximum time to wait for component initialization

```bash
# Standard timeout (30 seconds)
export INTELLIGENCE_INIT_TIMEOUT=30

# Shorter timeout for fast-fail (10 seconds)
export INTELLIGENCE_INIT_TIMEOUT=10

# Longer timeout for slow systems (60 seconds)
export INTELLIGENCE_INIT_TIMEOUT=60
```

---

### `INTELLIGENCE_HEALTH_INTERVAL`
**Type:** Integer (seconds)
**Default:** `300` (5 minutes)
**Description:** Interval for component health checks

```bash
# Frequent health checks (every minute)
export INTELLIGENCE_HEALTH_INTERVAL=60

# Standard health checks (every 5 minutes)
export INTELLIGENCE_HEALTH_INTERVAL=300

# Disable health monitoring (set to 0)
export INTELLIGENCE_HEALTH_INTERVAL=0
```

---

### `INTELLIGENCE_FAIL_FAST`
**Type:** Boolean
**Default:** `false`
**Description:** Fail startup if required components don't initialize

```bash
# Graceful degradation (continue without failed components)
export INTELLIGENCE_FAIL_FAST=false

# Fail fast (abort if required components fail)
export INTELLIGENCE_FAIL_FAST=true
```

---

### `INTELLIGENCE_REQUIRED_COMPONENTS`
**Type:** Comma-separated list
**Default:** `fusion_engine`
**Description:** Components that must initialize successfully (only used if `INTELLIGENCE_FAIL_FAST=true`)

```bash
# Only fusion engine required
export INTELLIGENCE_REQUIRED_COMPONENTS=fusion_engine

# Multiple required components
export INTELLIGENCE_REQUIRED_COMPONENTS=fusion_engine,learning_coordinator

# All components required
export INTELLIGENCE_REQUIRED_COMPONENTS=network_context,pattern_tracker,device_monitor,fusion_engine,learning_coordinator
```

---

## Component Enable/Disable Flags

### `NETWORK_CONTEXT_ENABLED`
**Type:** Boolean
**Default:** `true`
**Description:** Enable/disable Network Context Provider

```bash
export NETWORK_CONTEXT_ENABLED=true   # Enable (recommended)
export NETWORK_CONTEXT_ENABLED=false  # Disable
```

---

### `PATTERN_TRACKER_ENABLED`
**Type:** Boolean
**Default:** `true`
**Description:** Enable/disable Unlock Pattern Tracker

```bash
export PATTERN_TRACKER_ENABLED=true   # Enable (recommended)
export PATTERN_TRACKER_ENABLED=false  # Disable
```

---

### `DEVICE_MONITOR_ENABLED`
**Type:** Boolean
**Default:** `true`
**Description:** Enable/disable Device State Monitor

```bash
export DEVICE_MONITOR_ENABLED=true    # Enable (recommended)
export DEVICE_MONITOR_ENABLED=false   # Disable
```

---

### `FUSION_ENGINE_ENABLED`
**Type:** Boolean
**Default:** `true`
**Description:** Enable/disable Multi-Factor Auth Fusion Engine

```bash
export FUSION_ENGINE_ENABLED=true     # Enable (required for multi-factor)
export FUSION_ENGINE_ENABLED=false    # Disable (voice-only auth)
```

**⚠️ Warning:** Disabling fusion engine reverts to voice-only authentication.

---

### `LEARNING_COORDINATOR_ENABLED`
**Type:** Boolean
**Default:** `true`
**Description:** Enable/disable Intelligence Learning Coordinator (RAG + RLHF)

```bash
export LEARNING_COORDINATOR_ENABLED=true   # Enable RAG + RLHF
export LEARNING_COORDINATOR_ENABLED=false  # Disable learning
```

---

## Network Context Configuration

### `NETWORK_TRUSTED_THRESHOLD`
**Type:** Integer
**Default:** `5`
**Description:** Successful unlocks required to consider a network "trusted"

```bash
export NETWORK_TRUSTED_THRESHOLD=5    # Standard
export NETWORK_TRUSTED_THRESHOLD=10   # More conservative
export NETWORK_TRUSTED_THRESHOLD=2    # More aggressive
```

---

### `NETWORK_KNOWN_THRESHOLD`
**Type:** Integer
**Default:** `2`
**Description:** Successful unlocks required to consider a network "known"

```bash
export NETWORK_KNOWN_THRESHOLD=2      # Standard
export NETWORK_KNOWN_THRESHOLD=3      # More conservative
```

---

### `NETWORK_TRUSTED_CONFIDENCE`
**Type:** Float (0.0 - 1.0)
**Default:** `0.95`
**Description:** Confidence boost for trusted networks

```bash
export NETWORK_TRUSTED_CONFIDENCE=0.95   # Very high confidence
export NETWORK_TRUSTED_CONFIDENCE=0.90   # High confidence
export NETWORK_TRUSTED_CONFIDENCE=0.85   # Moderate confidence
```

---

### `NETWORK_KNOWN_CONFIDENCE`
**Type:** Float (0.0 - 1.0)
**Default:** `0.85`
**Description:** Confidence for known (not yet trusted) networks

```bash
export NETWORK_KNOWN_CONFIDENCE=0.85     # Standard
export NETWORK_KNOWN_CONFIDENCE=0.75     # Lower confidence
```

---

### `NETWORK_UNKNOWN_CONFIDENCE`
**Type:** Float (0.0 - 1.0)
**Default:** `0.50`
**Description:** Confidence for completely unknown networks

```bash
export NETWORK_UNKNOWN_CONFIDENCE=0.50   # Neutral (no boost)
export NETWORK_UNKNOWN_CONFIDENCE=0.40   # Penalty for unknown
export NETWORK_UNKNOWN_CONFIDENCE=0.60   # Slight boost even for unknown
```

---

### `NETWORK_MAX_HISTORY`
**Type:** Integer
**Default:** `100`
**Description:** Maximum network entries to store

```bash
export NETWORK_MAX_HISTORY=100       # Standard
export NETWORK_MAX_HISTORY=50        # Smaller storage
export NETWORK_MAX_HISTORY=200       # Larger history
```

---

### `NETWORK_DECAY_DAYS`
**Type:** Integer
**Default:** `90`
**Description:** Days before forgetting an unused network

```bash
export NETWORK_DECAY_DAYS=90         # 3 months
export NETWORK_DECAY_DAYS=180        # 6 months
export NETWORK_DECAY_DAYS=30         # 1 month
```

---

### `NETWORK_CACHE_DURATION`
**Type:** Integer (seconds)
**Default:** `30`
**Description:** Cache duration for network information

```bash
export NETWORK_CACHE_DURATION=30     # 30 seconds
export NETWORK_CACHE_DURATION=60     # 1 minute
export NETWORK_CACHE_DURATION=10     # 10 seconds
```

---

## Multi-Factor Fusion Configuration

### `AUTH_FUSION_AUTH_THRESHOLD`
**Type:** Float (0.0 - 1.0)
**Default:** `0.85`
**Description:** Fused confidence threshold for instant authentication

```bash
export AUTH_FUSION_AUTH_THRESHOLD=0.85   # Standard (85%)
export AUTH_FUSION_AUTH_THRESHOLD=0.90   # More secure (90%)
export AUTH_FUSION_AUTH_THRESHOLD=0.80   # More lenient (80%)
```

---

### `AUTH_FUSION_CHALLENGE_THRESHOLD`
**Type:** Float (0.0 - 1.0)
**Default:** `0.70`
**Description:** Fused confidence threshold for challenge question

```bash
export AUTH_FUSION_CHALLENGE_THRESHOLD=0.70   # Standard
export AUTH_FUSION_CHALLENGE_THRESHOLD=0.75   # Higher bar for challenge
export AUTH_FUSION_CHALLENGE_THRESHOLD=0.65   # Lower bar
```

---

### `AUTH_FUSION_DENY_THRESHOLD`
**Type:** Float (0.0 - 1.0)
**Default:** `0.70`
**Description:** Below this threshold = instant deny

```bash
export AUTH_FUSION_DENY_THRESHOLD=0.70    # Standard
export AUTH_FUSION_DENY_THRESHOLD=0.60    # More lenient
export AUTH_FUSION_DENY_THRESHOLD=0.75    # More secure
```

---

### Fusion Weights

Control how much each signal contributes to the final decision:

```bash
# Voice biometric (default: 50%)
export AUTH_FUSION_VOICE_WEIGHT=0.50

# Network context (default: 15%)
export AUTH_FUSION_NETWORK_WEIGHT=0.15

# Temporal patterns (default: 15%)
export AUTH_FUSION_TEMPORAL_WEIGHT=0.15

# Device state (default: 12%)
export AUTH_FUSION_DEVICE_WEIGHT=0.12

# Voice drift adjustment (default: 8%)
export AUTH_FUSION_DRIFT_WEIGHT=0.08
```

**Note:** Weights should sum to 1.0 for proper Bayesian fusion.

---

### `AUTH_FUSION_RISK_ASSESSMENT`
**Type:** Boolean
**Default:** `true`
**Description:** Enable risk-based authentication adjustments

```bash
export AUTH_FUSION_RISK_ASSESSMENT=true    # Enable risk scoring
export AUTH_FUSION_RISK_ASSESSMENT=false   # Disable risk scoring
```

---

### `AUTH_FUSION_HIGH_RISK_THRESHOLD`
**Type:** Float (0.0 - 1.0)
**Default:** `0.70`
**Description:** Risk score above this is considered "high risk"

```bash
export AUTH_FUSION_HIGH_RISK_THRESHOLD=0.70    # Standard
export AUTH_FUSION_HIGH_RISK_THRESHOLD=0.60    # More sensitive
export AUTH_FUSION_HIGH_RISK_THRESHOLD=0.80    # Less sensitive
```

---

### `AUTH_FUSION_CONTINUOUS_LEARNING`
**Type:** Boolean
**Default:** `true`
**Description:** Continuously learn and adapt weights

```bash
export AUTH_FUSION_CONTINUOUS_LEARNING=true    # Adaptive learning
export AUTH_FUSION_CONTINUOUS_LEARNING=false   # Fixed weights
```

---

### `AUTH_FUSION_MIN_LEARN_CONF`
**Type:** Float (0.0 - 1.0)
**Default:** `0.90`
**Description:** Minimum confidence required to learn from authentication

```bash
export AUTH_FUSION_MIN_LEARN_CONF=0.90    # Very confident only
export AUTH_FUSION_MIN_LEARN_CONF=0.85    # Moderately confident
export AUTH_FUSION_MIN_LEARN_CONF=0.95    # Extremely confident only
```

---

### `AUTH_FUSION_METHOD`
**Type:** String
**Default:** `bayesian`
**Options:** `bayesian`, `weighted_average`, `unanimous`
**Description:** Primary fusion algorithm

```bash
export AUTH_FUSION_METHOD=bayesian           # Bayesian probability fusion (recommended)
export AUTH_FUSION_METHOD=weighted_average   # Simple weighted average
export AUTH_FUSION_METHOD=unanimous          # All signals must agree
```

---

### `AUTH_FUSION_UNANIMOUS_VETO`
**Type:** Boolean
**Default:** `true`
**Description:** Any signal can veto if extremely low

```bash
export AUTH_FUSION_UNANIMOUS_VETO=true    # Enable veto power
export AUTH_FUSION_UNANIMOUS_VETO=false   # Disable veto
```

---

### `AUTH_FUSION_VETO_THRESHOLD`
**Type:** Float (0.0 - 1.0)
**Default:** `0.30`
**Description:** If any signal below this, instant deny (veto)

```bash
export AUTH_FUSION_VETO_THRESHOLD=0.30    # Standard (30%)
export AUTH_FUSION_VETO_THRESHOLD=0.20    # Lower (more lenient)
export AUTH_FUSION_VETO_THRESHOLD=0.40    # Higher (more secure)
```

---

## Data Storage Configuration

### `JARVIS_DATA_DIR`
**Type:** Path
**Default:** `~/.jarvis`
**Description:** Root directory for all JARVIS data

```bash
export JARVIS_DATA_DIR=~/.jarvis              # Default
export JARVIS_DATA_DIR=/var/lib/jarvis        # System-wide
export JARVIS_DATA_DIR=/Volumes/Secure/jarvis # Encrypted volume
```

**Directory Structure:**
```
$JARVIS_DATA_DIR/
├── intelligence/
│   ├── network_context.db
│   ├── pattern_tracker.db
│   ├── device_monitor.db
│   ├── fusion_engine.db
│   └── learning_coordinator.db
├── voice/
│   └── embeddings/
└── logs/
```

---

## Example Configurations

### Development Configuration (Lenient)
```bash
# Lenient thresholds for testing
export INTELLIGENCE_ENABLED=true
export INTELLIGENCE_PARALLEL_INIT=true
export INTELLIGENCE_FAIL_FAST=false

export AUTH_FUSION_AUTH_THRESHOLD=0.75
export AUTH_FUSION_CHALLENGE_THRESHOLD=0.60
export AUTH_FUSION_DENY_THRESHOLD=0.60

export NETWORK_TRUSTED_THRESHOLD=2
export NETWORK_TRUSTED_CONFIDENCE=0.90
```

---

### Production Configuration (Secure)
```bash
# Secure thresholds for production
export INTELLIGENCE_ENABLED=true
export INTELLIGENCE_PARALLEL_INIT=true
export INTELLIGENCE_FAIL_FAST=true
export INTELLIGENCE_REQUIRED_COMPONENTS=fusion_engine,learning_coordinator

export AUTH_FUSION_AUTH_THRESHOLD=0.90
export AUTH_FUSION_CHALLENGE_THRESHOLD=0.75
export AUTH_FUSION_DENY_THRESHOLD=0.75

export NETWORK_TRUSTED_THRESHOLD=10
export NETWORK_TRUSTED_CONFIDENCE=0.95

export AUTH_FUSION_HIGH_RISK_THRESHOLD=0.60
export AUTH_FUSION_UNANIMOUS_VETO=true
export AUTH_FUSION_VETO_THRESHOLD=0.35
```

---

### High-Performance Configuration
```bash
# Optimized for speed
export INTELLIGENCE_ENABLED=true
export INTELLIGENCE_PARALLEL_INIT=true
export INTELLIGENCE_INIT_TIMEOUT=10
export INTELLIGENCE_HEALTH_INTERVAL=600  # Check every 10 minutes

export NETWORK_CACHE_DURATION=60  # Longer cache
export NETWORK_MAX_HISTORY=50     # Smaller database

# Disable components not needed
export PATTERN_TRACKER_ENABLED=false
export DEVICE_MONITOR_ENABLED=false
```

---

### Minimal Configuration (Voice-Only)
```bash
# Disable all intelligence - pure voice authentication
export INTELLIGENCE_ENABLED=false
```

---

## Monitoring and Debugging

### Health Check Endpoint

```bash
curl http://localhost:8010/api/intelligence/health
```

**Response:**
```json
{
  "initialized": true,
  "enabled": true,
  "total_components": 5,
  "ready": 5,
  "degraded": 0,
  "failed": 0,
  "health_monitoring": true,
  "components": {
    "network_context": {
      "name": "network_context",
      "status": "ready",
      "initialized_at": "2024-12-22T10:15:30",
      "last_check": "2024-12-22T10:20:30",
      "error_message": null,
      "metadata": {"type": "NetworkContextProvider"}
    },
    ...
  }
}
```

---

### Component Status Endpoint

```bash
curl http://localhost:8010/api/intelligence/components/<component_name>
```

**Available Components:**
- `network_context`
- `pattern_tracker`
- `device_monitor`
- `fusion_engine`
- `learning_coordinator`

---

### Logging

Set log level for intelligence components:

```bash
export JARVIS_LOG_LEVEL=DEBUG  # Verbose intelligence logs
export JARVIS_LOG_LEVEL=INFO   # Standard logs
export JARVIS_LOG_LEVEL=WARNING # Minimal logs
```

Intelligence component logs include:
- `🧠` - Intelligence system messages
- `✅` - Component initialization success
- `❌` - Component failures
- `⚠️` - Degraded component warnings
- `🩺` - Health check messages

---

## Performance Tuning

### Startup Performance

**Goal:** Minimize startup time

```bash
# Enable parallel initialization
export INTELLIGENCE_PARALLEL_INIT=true

# Reduce timeout for faster fail
export INTELLIGENCE_INIT_TIMEOUT=10

# Disable health monitoring during startup
export INTELLIGENCE_HEALTH_INTERVAL=0
```

**Expected startup time:** 2-3 seconds (parallel), 5-7 seconds (sequential)

---

### Memory Optimization

**Goal:** Reduce memory footprint

```bash
# Reduce history sizes
export NETWORK_MAX_HISTORY=50

# Disable learning coordinator (large ChromaDB)
export LEARNING_COORDINATOR_ENABLED=false

# Reduce cache durations
export NETWORK_CACHE_DURATION=10
```

**Memory savings:** ~50-100 MB without learning coordinator

---

### Authentication Speed

**Goal:** Fastest possible authentication

```bash
# Longer caches to reduce database queries
export NETWORK_CACHE_DURATION=60

# Reduce fusion complexity
export AUTH_FUSION_RISK_ASSESSMENT=false
export AUTH_FUSION_CONTINUOUS_LEARNING=false

# Disable pattern tracker and device monitor if not needed
export PATTERN_TRACKER_ENABLED=false
export DEVICE_MONITOR_ENABLED=false
```

**Expected authentication time:**
- With all components: 150-250ms
- Optimized: 80-120ms
- Voice-only (intelligence disabled): 60-80ms

---

## Troubleshooting

### Issue: Intelligence components fail to initialize

**Check:**
1. `JARVIS_DATA_DIR` exists and is writable
2. No other JARVIS processes holding database locks
3. Sufficient disk space

**Solution:**
```bash
# Check data directory
ls -la $JARVIS_DATA_DIR/intelligence/

# Increase timeout
export INTELLIGENCE_INIT_TIMEOUT=60

# Enable detailed logging
export JARVIS_LOG_LEVEL=DEBUG
```

---

### Issue: Components stuck in "degraded" status

**Check:**
1. Review health check logs
2. Check database integrity
3. Verify network connectivity (for Cloud SQL)

**Solution:**
```bash
# Restart intelligence system
curl -X POST http://localhost:8010/api/intelligence/restart

# Or disable health monitoring if false positives
export INTELLIGENCE_HEALTH_INTERVAL=0
```

---

### Issue: Slow authentication with intelligence enabled

**Check:**
1. Database query performance
2. Network latency (Cloud SQL)
3. Cache hit rates

**Solution:**
```bash
# Increase cache durations
export NETWORK_CACHE_DURATION=60

# Reduce fusion complexity
export AUTH_FUSION_RISK_ASSESSMENT=false

# Profile with debug logs
export JARVIS_LOG_LEVEL=DEBUG
```

---

## Security Considerations

### Secure Storage

**Recommendation:** Store intelligence databases on encrypted volumes

```bash
# Use encrypted volume
export JARVIS_DATA_DIR=/Volumes/Secure/jarvis

# Or use system keychain
export JARVIS_DATA_DIR=/var/lib/jarvis
chmod 700 /var/lib/jarvis
```

---

### Network Trust

**Recommendation:** Conservative network trust settings for shared environments

```bash
# Require many successful unlocks before trusting
export NETWORK_TRUSTED_THRESHOLD=20

# Lower confidence boost for networks
export NETWORK_TRUSTED_CONFIDENCE=0.85

# Shorter decay period (forget networks faster)
export NETWORK_DECAY_DAYS=30
```

---

### High-Security Mode

For maximum security (e.g., enterprise environments):

```bash
export INTELLIGENCE_FAIL_FAST=true
export INTELLIGENCE_REQUIRED_COMPONENTS=fusion_engine,learning_coordinator

export AUTH_FUSION_AUTH_THRESHOLD=0.95
export AUTH_FUSION_CHALLENGE_THRESHOLD=0.85
export AUTH_FUSION_DENY_THRESHOLD=0.85

export AUTH_FUSION_UNANIMOUS_VETO=true
export AUTH_FUSION_VETO_THRESHOLD=0.40

export NETWORK_TRUSTED_THRESHOLD=50
export NETWORK_UNKNOWN_CONFIDENCE=0.30  # Penalty for unknown networks
```

---

## Migration Guide

### From Voice-Only to Intelligence-Enabled

1. **Enable intelligence with defaults:**
   ```bash
   export INTELLIGENCE_ENABLED=true
   ```

2. **Let system learn for 7 days** (initial learning period)

3. **Review authentication logs** to tune thresholds

4. **Gradually increase security** as confidence grows

---

### From v4.x to v5.0

**Breaking Changes:**
- Intelligence components now managed by Intelligence Component Manager
- Environment variables prefixed with `INTELLIGENCE_` for global settings
- Component-specific settings use component prefixes

**Migration Steps:**
1. Update environment variables (see table above)
2. Remove old component initialization code
3. Clear old database schemas: `rm -rf ~/.jarvis/intelligence/*.db`
4. Restart JARVIS

---

## Reference

### All Environment Variables (Alphabetical)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `AUTH_FUSION_AUTH_THRESHOLD` | Float | 0.85 | Threshold for instant authentication |
| `AUTH_FUSION_CHALLENGE_THRESHOLD` | Float | 0.70 | Threshold for challenge question |
| `AUTH_FUSION_CONTINUOUS_LEARNING` | Boolean | true | Enable adaptive learning |
| `AUTH_FUSION_DENY_THRESHOLD` | Float | 0.70 | Threshold for instant denial |
| `AUTH_FUSION_DEVICE_WEIGHT` | Float | 0.12 | Device signal weight |
| `AUTH_FUSION_DRIFT_WEIGHT` | Float | 0.08 | Drift signal weight |
| `AUTH_FUSION_HIGH_RISK_THRESHOLD` | Float | 0.70 | High risk threshold |
| `AUTH_FUSION_METHOD` | String | bayesian | Fusion algorithm |
| `AUTH_FUSION_MIN_LEARN_CONF` | Float | 0.90 | Min confidence for learning |
| `AUTH_FUSION_NETWORK_WEIGHT` | Float | 0.15 | Network signal weight |
| `AUTH_FUSION_RISK_ASSESSMENT` | Boolean | true | Enable risk assessment |
| `AUTH_FUSION_TEMPORAL_WEIGHT` | Float | 0.15 | Temporal signal weight |
| `AUTH_FUSION_UNANIMOUS_VETO` | Boolean | true | Enable veto power |
| `AUTH_FUSION_VETO_THRESHOLD` | Float | 0.30 | Veto threshold |
| `AUTH_FUSION_VOICE_WEIGHT` | Float | 0.50 | Voice signal weight |
| `DEVICE_MONITOR_ENABLED` | Boolean | true | Enable Device State Monitor |
| `FUSION_ENGINE_ENABLED` | Boolean | true | Enable Fusion Engine |
| `INTELLIGENCE_ENABLED` | Boolean | true | Master intelligence switch |
| `INTELLIGENCE_FAIL_FAST` | Boolean | false | Fail if required components fail |
| `INTELLIGENCE_HEALTH_INTERVAL` | Integer | 300 | Health check interval (seconds) |
| `INTELLIGENCE_INIT_TIMEOUT` | Integer | 30 | Init timeout (seconds) |
| `INTELLIGENCE_PARALLEL_INIT` | Boolean | true | Parallel initialization |
| `INTELLIGENCE_REQUIRED_COMPONENTS` | CSV | fusion_engine | Required components |
| `JARVIS_DATA_DIR` | Path | ~/.jarvis | Data directory |
| `JARVIS_LOG_LEVEL` | String | INFO | Log level |
| `LEARNING_COORDINATOR_ENABLED` | Boolean | true | Enable RAG + RLHF |
| `NETWORK_CACHE_DURATION` | Integer | 30 | Cache duration (seconds) |
| `NETWORK_CONTEXT_ENABLED` | Boolean | true | Enable Network Context |
| `NETWORK_DECAY_DAYS` | Integer | 90 | Network decay period (days) |
| `NETWORK_KNOWN_CONFIDENCE` | Float | 0.85 | Known network confidence |
| `NETWORK_KNOWN_THRESHOLD` | Integer | 2 | Known network threshold |
| `NETWORK_MAX_HISTORY` | Integer | 100 | Max network history entries |
| `NETWORK_TRUSTED_CONFIDENCE` | Float | 0.95 | Trusted network confidence |
| `NETWORK_TRUSTED_THRESHOLD` | Integer | 5 | Trusted network threshold |
| `NETWORK_UNKNOWN_CONFIDENCE` | Float | 0.50 | Unknown network confidence |
| `PATTERN_TRACKER_ENABLED` | Boolean | true | Enable Pattern Tracker |

---

## Support

For issues or questions:
- **GitHub Issues:** https://github.com/anthropics/jarvis-ai-agent/issues
- **Documentation:** See `INTEGRATION_SUMMARY_V5.md` and `RAG_RLHF_LEARNING_GUIDE.md`
- **Logs:** Check `$JARVIS_DATA_DIR/logs/intelligence.log`

---

**End of Configuration Guide**
