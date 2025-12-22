# Intelligence System API Documentation

**Version:** 5.0.0
**Last Updated:** 2024-12-22

## Overview

The Intelligence System provides REST API endpoints for monitoring component health, viewing status, and managing the intelligence infrastructure.

---

## Base URL

```
http://localhost:8010/api/intelligence
```

---

## Endpoints

### GET `/health`

**Description:** Quick health check for the intelligence system

**Response:**
```json
{
  "status": "healthy",
  "initialized": true,
  "enabled": true,
  "total_components": 5,
  "ready": 5,
  "degraded": 0,
  "failed": 0,
  "health_monitoring": true
}
```

**Status Codes:**
- `200` - System healthy
- `503` - System degraded or failed

---

### GET `/status`

**Description:** Detailed status of all intelligence components

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
      "initialized_at": "2024-12-22T10:15:30.123456",
      "last_check": "2024-12-22T10:20:30.456789",
      "error_message": null,
      "metadata": {
        "type": "NetworkContextProvider"
      }
    },
    "pattern_tracker": {
      "name": "pattern_tracker",
      "status": "ready",
      "initialized_at": "2024-12-22T10:15:31.234567",
      "last_check": "2024-12-22T10:20:31.567890",
      "error_message": null,
      "metadata": {
        "type": "UnlockPatternTracker"
      }
    },
    "device_monitor": {
      "name": "device_monitor",
      "status": "ready",
      "initialized_at": "2024-12-22T10:15:31.345678",
      "last_check": "2024-12-22T10:20:31.678901",
      "error_message": null,
      "metadata": {
        "type": "DeviceStateMonitor"
      }
    },
    "fusion_engine": {
      "name": "fusion_engine",
      "status": "ready",
      "initialized_at": "2024-12-22T10:15:32.456789",
      "last_check": "2024-12-22T10:20:32.789012",
      "error_message": null,
      "metadata": {
        "type": "MultiFactorAuthFusion",
        "method": "bayesian"
      }
    },
    "learning_coordinator": {
      "name": "learning_coordinator",
      "status": "ready",
      "initialized_at": "2024-12-22T10:15:33.567890",
      "last_check": "2024-12-22T10:20:33.890123",
      "error_message": null,
      "metadata": {
        "type": "IntelligenceLearningCoordinator",
        "rag_enabled": true,
        "rlhf_enabled": true
      }
    }
  }
}
```

**Status Codes:**
- `200` - Success

---

### GET `/components`

**Description:** List all available intelligence components

**Response:**
```json
{
  "components": [
    {
      "name": "network_context",
      "display_name": "Network Context Intelligence",
      "type": "NetworkContextProvider",
      "enabled": true,
      "status": "ready"
    },
    {
      "name": "pattern_tracker",
      "display_name": "Unlock Pattern Intelligence",
      "type": "UnlockPatternTracker",
      "enabled": true,
      "status": "ready"
    },
    {
      "name": "device_monitor",
      "display_name": "Device State Intelligence",
      "type": "DeviceStateMonitor",
      "enabled": true,
      "status": "ready"
    },
    {
      "name": "fusion_engine",
      "display_name": "Multi-Factor Fusion Engine",
      "type": "MultiFactorAuthFusion",
      "enabled": true,
      "status": "ready"
    },
    {
      "name": "learning_coordinator",
      "display_name": "RAG + RLHF Learning System",
      "type": "IntelligenceLearningCoordinator",
      "enabled": true,
      "status": "ready"
    }
  ]
}
```

**Status Codes:**
- `200` - Success

---

### GET `/components/{component_name}`

**Description:** Get detailed information about a specific component

**Path Parameters:**
- `component_name` - One of: `network_context`, `pattern_tracker`, `device_monitor`, `fusion_engine`, `learning_coordinator`

**Example Request:**
```bash
curl http://localhost:8010/api/intelligence/components/fusion_engine
```

**Response:**
```json
{
  "name": "fusion_engine",
  "display_name": "Multi-Factor Fusion Engine",
  "type": "MultiFactorAuthFusion",
  "enabled": true,
  "status": "ready",
  "initialized_at": "2024-12-22T10:15:32.456789",
  "last_check": "2024-12-22T10:20:32.789012",
  "error_message": null,
  "metadata": {
    "type": "MultiFactorAuthFusion",
    "method": "bayesian",
    "auth_threshold": 0.85,
    "challenge_threshold": 0.70,
    "deny_threshold": 0.70,
    "weights": {
      "voice": 0.50,
      "network": 0.15,
      "temporal": 0.15,
      "device": 0.12,
      "drift": 0.08
    }
  }
}
```

**Status Codes:**
- `200` - Success
- `404` - Component not found

---

### GET `/config`

**Description:** Get current intelligence system configuration

**Response:**
```json
{
  "enabled": true,
  "parallel_init": true,
  "init_timeout_seconds": 30,
  "health_check_interval": 300,
  "fail_fast": false,
  "required_components": ["fusion_engine"],
  "components": {
    "network_context_enabled": true,
    "pattern_tracker_enabled": true,
    "device_monitor_enabled": true,
    "fusion_engine_enabled": true,
    "learning_coordinator_enabled": true
  },
  "data_dir": "/Users/derek/.jarvis"
}
```

**Status Codes:**
- `200` - Success

---

### POST `/restart`

**Description:** Restart the intelligence system (shutdown and re-initialize all components)

**⚠️ Warning:** This will temporarily disable intelligence during restart (~2-5 seconds)

**Request Body:** None

**Response:**
```json
{
  "message": "Intelligence system restart initiated",
  "previous_status": {
    "ready": 5,
    "degraded": 0,
    "failed": 0
  },
  "restart_id": "restart_2024-12-22_10-25-30"
}
```

**Status Codes:**
- `202` - Restart accepted
- `503` - System already restarting

---

### POST `/components/{component_name}/restart`

**Description:** Restart a specific component

**Path Parameters:**
- `component_name` - Component to restart

**Example Request:**
```bash
curl -X POST http://localhost:8010/api/intelligence/components/network_context/restart
```

**Response:**
```json
{
  "message": "Component restart initiated",
  "component": "network_context",
  "previous_status": "ready",
  "restart_id": "restart_network_context_2024-12-22_10-25-30"
}
```

**Status Codes:**
- `202` - Restart accepted
- `404` - Component not found

---

### GET `/metrics`

**Description:** Get performance metrics for the intelligence system

**Response:**
```json
{
  "uptime_seconds": 3600,
  "total_authentications": 147,
  "successful_authentications": 143,
  "failed_authentications": 4,
  "average_auth_time_ms": 185,
  "p50_auth_time_ms": 170,
  "p95_auth_time_ms": 245,
  "p99_auth_time_ms": 310,
  "component_performance": {
    "network_context": {
      "avg_query_time_ms": 12,
      "cache_hit_rate": 0.87
    },
    "pattern_tracker": {
      "avg_query_time_ms": 15,
      "cache_hit_rate": 0.82
    },
    "device_monitor": {
      "avg_query_time_ms": 8,
      "cache_hit_rate": 0.95
    },
    "fusion_engine": {
      "avg_fusion_time_ms": 25,
      "method": "bayesian"
    },
    "learning_coordinator": {
      "avg_rag_retrieval_ms": 45,
      "total_learning_records": 1847,
      "rlhf_feedback_count": 23
    }
  }
}
```

**Status Codes:**
- `200` - Success

---

### GET `/learning/stats`

**Description:** Get learning statistics (RAG + RLHF)

**Response:**
```json
{
  "total_authentications": 1847,
  "successful_authentications": 1803,
  "failed_authentications": 44,
  "rlhf_feedback_count": 23,
  "positive_feedback": 19,
  "negative_feedback": 4,
  "learning_enabled": true,
  "rag_enabled": true,
  "rlhf_enabled": true,
  "learning_phase": "active",
  "learning_days": 45,
  "average_confidence_trend": {
    "week_1": 0.78,
    "week_2": 0.82,
    "week_3": 0.85,
    "week_4": 0.87,
    "current": 0.89
  }
}
```

**Status Codes:**
- `200` - Success
- `404` - Learning coordinator not enabled

---

### POST `/learning/feedback`

**Description:** Provide RLHF feedback on an authentication attempt

**Request Body:**
```json
{
  "record_id": 1847,
  "was_correct": true,
  "feedback_score": 1.0,
  "feedback_notes": "Voice was slightly different but correctly identified"
}
```

**Response:**
```json
{
  "message": "Feedback recorded successfully",
  "record_id": 1847,
  "updated_confidence": 0.91,
  "learning_impact": "positive"
}
```

**Status Codes:**
- `200` - Success
- `404` - Record not found or learning coordinator not enabled

---

## Error Responses

All endpoints may return error responses:

### 500 Internal Server Error
```json
{
  "error": "Internal server error",
  "message": "Detailed error message",
  "timestamp": "2024-12-22T10:25:30.123456"
}
```

### 503 Service Unavailable
```json
{
  "error": "Service unavailable",
  "message": "Intelligence system not initialized",
  "timestamp": "2024-12-22T10:25:30.123456"
}
```

---

## WebSocket API (Real-Time Updates)

### `ws://localhost:8010/api/intelligence/ws`

**Description:** Real-time component status updates

**Messages:**

#### Component Status Update
```json
{
  "type": "component_status",
  "component": "network_context",
  "status": "ready",
  "timestamp": "2024-12-22T10:25:30.123456"
}
```

#### Authentication Event
```json
{
  "type": "authentication",
  "user": "Derek",
  "outcome": "success",
  "voice_confidence": 0.87,
  "fused_confidence": 0.91,
  "timestamp": "2024-12-22T10:25:30.123456"
}
```

#### Health Check
```json
{
  "type": "health_check",
  "total_components": 5,
  "ready": 5,
  "degraded": 0,
  "failed": 0,
  "timestamp": "2024-12-22T10:25:30.123456"
}
```

---

## Authentication

All API endpoints currently require the JARVIS API key:

```bash
curl -H "X-API-Key: $JARVIS_API_KEY" \
     http://localhost:8010/api/intelligence/health
```

**Environment Variable:**
```bash
export JARVIS_API_KEY=your_api_key_here
```

---

## Rate Limiting

- **Default:** 100 requests per minute per client
- **Health endpoint:** 300 requests per minute (higher limit for monitoring)
- **WebSocket:** 1 connection per client

**Rate Limit Headers:**
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1640174130
```

---

## Examples

### Check System Health
```bash
curl http://localhost:8010/api/intelligence/health
```

### Get Full Status
```bash
curl http://localhost:8010/api/intelligence/status | jq
```

### Get Specific Component
```bash
curl http://localhost:8010/api/intelligence/components/fusion_engine | jq
```

### Restart Intelligence System
```bash
curl -X POST http://localhost:8010/api/intelligence/restart
```

### Provide RLHF Feedback
```bash
curl -X POST http://localhost:8010/api/intelligence/learning/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "record_id": 1847,
    "was_correct": true,
    "feedback_score": 1.0,
    "feedback_notes": "Correctly identified despite background noise"
  }'
```

### Monitor Real-Time (WebSocket)
```javascript
const ws = new WebSocket('ws://localhost:8010/api/intelligence/ws');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Intelligence update:', data);
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};
```

---

## Status Codes Reference

| Code | Meaning | Description |
|------|---------|-------------|
| 200 | OK | Request successful |
| 202 | Accepted | Request accepted (async operation) |
| 400 | Bad Request | Invalid request parameters |
| 404 | Not Found | Resource not found |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server error |
| 503 | Service Unavailable | Intelligence system not available |

---

## Monitoring Best Practices

### 1. Health Checks
```bash
# Check every 30 seconds
watch -n 30 'curl -s http://localhost:8010/api/intelligence/health | jq'
```

### 2. Component Status
```bash
# Monitor specific component
watch -n 60 'curl -s http://localhost:8010/api/intelligence/components/fusion_engine | jq .status'
```

### 3. Performance Metrics
```bash
# Track authentication performance
curl -s http://localhost:8010/api/intelligence/metrics | \
  jq '.average_auth_time_ms, .p95_auth_time_ms'
```

### 4. Learning Progress
```bash
# Monitor learning improvement
curl -s http://localhost:8010/api/intelligence/learning/stats | \
  jq '.average_confidence_trend'
```

---

## Integration with Monitoring Tools

### Prometheus
```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'jarvis_intelligence'
    static_configs:
      - targets: ['localhost:8010']
    metrics_path: '/api/intelligence/metrics'
    scrape_interval: 30s
```

### Grafana
```json
{
  "dashboard": {
    "title": "JARVIS Intelligence System",
    "panels": [
      {
        "title": "Component Health",
        "targets": [
          {
            "expr": "jarvis_intelligence_component_status"
          }
        ]
      },
      {
        "title": "Authentication Time (p95)",
        "targets": [
          {
            "expr": "jarvis_intelligence_auth_time_p95"
          }
        ]
      }
    ]
  }
}
```

### Datadog
```python
from datadog import statsd

# Report component status
statsd.gauge('jarvis.intelligence.components.ready', 5)
statsd.gauge('jarvis.intelligence.auth_time.p95', 245)
```

---

## Troubleshooting

### No Response from API
**Check:**
1. JARVIS is running: `ps aux | grep jarvis`
2. Port 8010 is open: `lsof -i :8010`
3. Logs: `tail -f ~/.jarvis/logs/intelligence.log`

### 503 Service Unavailable
**Cause:** Intelligence system not initialized

**Solution:**
```bash
# Check initialization status
curl http://localhost:8010/api/intelligence/status

# Restart if needed
curl -X POST http://localhost:8010/api/intelligence/restart
```

### Degraded Components
**Check:**
```bash
curl http://localhost:8010/api/intelligence/status | \
  jq '.components | to_entries[] | select(.value.status == "degraded")'
```

**Solution:**
```bash
# Restart specific component
curl -X POST http://localhost:8010/api/intelligence/components/network_context/restart
```

---

## Changelog

### v5.0.0 (2024-12-22)
- Initial release
- Added all core intelligence endpoints
- WebSocket support for real-time updates
- Learning statistics and RLHF feedback
- Component-level management

---

**End of API Documentation**
