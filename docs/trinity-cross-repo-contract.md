# Trinity Cross-Repo Coordination Contract

> This document defines the coordination contract between JARVIS, JARVIS-Prime, and Reactor-Core.
> Version: 1.0.0 | Last Updated: 2026-02-02

## Overview

The JARVIS Trinity system consists of three coordinating components:
- **JARVIS (Body)** - Main supervisor, UI server, and orchestrator
- **JARVIS-Prime (Mind)** - AI inference orchestrator and ML model host
- **Reactor-Core (Nerves)** - Background processing, training, and learning

```
    +------------------+
    |    JARVIS        |
    |  (Supervisor)    |
    |   Port: 8010     |
    +--------+---------+
             |
     +-------+-------+
     |               |
+----v-----+   +-----v------+
| J-Prime  |   | Reactor    |
|  (Mind)  |   |   (Nerves) |
| Port:8001|   | Port: 8090 |
+----------+   +------------+
```

## Health Endpoints

### JARVIS (Body)
- **URL**: `http://localhost:${JARVIS_PORT:-8010}/health`
- **Ping URL**: `http://localhost:${JARVIS_PORT:-8010}/health/ping`
- **Expected Response**: HTTP 200 with JSON `{"status": "healthy"}` or `{"ready": true}`

### JARVIS-Prime
- **URL**: `http://localhost:${JARVIS_PRIME_PORT:-8001}/health`
- **Expected Response**: HTTP 200 with JSON containing status and optional PID
- **Note**: Port changed from 8000 to 8001 in v192.2 to avoid conflicts with unified_supervisor

### Reactor-Core
- **URL**: `http://localhost:${REACTOR_CORE_PORT:-8090}/health`
- **Expected Response**: HTTP 200 with JSON `{"status": "healthy"}`

### Unified Health Aggregation
- **URL**: `http://localhost:${LOADING_PORT}/api/health/unified`
- **Purpose**: Cross-repo health status aggregation for all Trinity components

## Environment Variables

### Global Timeout Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_MAX_TIMEOUT` | 900.0 | Maximum allowed timeout for any operation (safety cap) |

### Signal Timeouts (Shutdown)

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_CLEANUP_TIMEOUT_SIGINT` | 10.0 | Wait time after SIGINT before SIGTERM |
| `JARVIS_CLEANUP_TIMEOUT_SIGTERM` | 5.0 | Wait time after SIGTERM before SIGKILL |
| `JARVIS_CLEANUP_TIMEOUT_SIGKILL` | 2.0 | Wait time after SIGKILL before giving up |

### Port and Network Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_PORT_CHECK_TIMEOUT` | 1.0 | Timeout for TCP port availability check |
| `JARVIS_PORT_RELEASE_WAIT` | 2.0 | Time to wait for port release after process exit |
| `JARVIS_IPC_SOCKET_TIMEOUT` | 8.0 | Timeout for Unix socket connections |

### Health Check Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_BACKEND_HEALTH_TIMEOUT` | 30.0 | Timeout for backend HTTP health check |
| `JARVIS_FRONTEND_HEALTH_TIMEOUT` | 60.0 | Timeout for frontend health check |
| `JARVIS_LOADING_SERVER_HEALTH_TIMEOUT` | 5.0 | Timeout for loading server health |
| `JARVIS_REACTOR_HEALTH_TIMEOUT` | 10.0 | Timeout for Reactor-Core health check |
| `HEALTH_CHECK_TIMEOUT` | 3.0 | General health check timeout |
| `HEALTH_CHECK_INTERVAL` | 5.0 | Interval between health checks |

### Trinity Component Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_PRIME_STARTUP_TIMEOUT` | 600.0 | Timeout for JARVIS-Prime startup (includes model loading) |
| `JARVIS_REACTOR_STARTUP_TIMEOUT` | 120.0 | Timeout for Reactor-Core startup |
| `TRINITY_SIGTERM_TIMEOUT` | 5.0 | Graceful shutdown SIGTERM wait |
| `TRINITY_SIGKILL_TIMEOUT` | 2.0 | Forced shutdown SIGKILL wait |

### Lock Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_LOCK_DIR` | ~/.jarvis/cross_repo/locks | Directory for lock files |
| `JARVIS_MAX_LOCK_TIMEOUT` | 300.0 | Maximum lock acquisition timeout |
| `JARVIS_MIN_LOCK_TIMEOUT` | 0.1 | Minimum lock acquisition timeout (prevents spin-lock) |
| `JARVIS_DEFAULT_LOCK_TIMEOUT` | 5.0 | Default lock timeout if not specified |
| `JARVIS_STALE_LOCK_RETRY_TIMEOUT` | 1.0 | Timeout for retry after stale lock removal |
| `JARVIS_STARTUP_LOCK_TIMEOUT` | 30.0 | Timeout for acquiring startup lock |
| `JARVIS_TAKEOVER_HANDOVER_TIMEOUT` | 15.0 | Timeout for instance takeover handover |
| `LOCK_ACQUIRE_TIMEOUT_S` | 5.0 | Lock acquisition timeout for RobustFileLock |
| `LOCK_POLL_INTERVAL_S` | 0.05 | Lock polling interval |
| `LOCK_STALE_WARNING_S` | 30.0 | Threshold for stale lock warning |

### Service Port Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_HOST` | localhost | JARVIS Body API host |
| `JARVIS_PORT` | 8010 | JARVIS Body API port |
| `JARVIS_PRIME_HOST` | localhost | JARVIS Prime API host |
| `JARVIS_PRIME_PORT` | 8001 | JARVIS Prime API port |
| `REACTOR_CORE_HOST` | localhost | Reactor Core API host |
| `REACTOR_CORE_PORT` | 8090 | Reactor Core API port |

### Trinity Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TRINITY_DIR` | ~/.jarvis/trinity | Base directory for Trinity files |
| `TRINITY_ENABLED` | true | Enable Trinity coordination |
| `TRINITY_DEBUG` | false | Enable debug logging |
| `TRINITY_HEARTBEAT_INTERVAL` | 5.0 | Heartbeat interval in seconds |
| `TRINITY_HEARTBEAT_TIMEOUT` | 15.0 | Heartbeat timeout in seconds |
| `TRINITY_HEALTH_CHECK_INTERVAL` | 5.0 | Health check interval |
| `TRINITY_HEALTH_CHECK_TIMEOUT` | 5.0 | Individual health check timeout |
| `TRINITY_STALE_THRESHOLD` | 120.0 | Seconds before state is stale |
| `TRINITY_MAX_RETRIES` | 3 | Maximum retry attempts |
| `TRINITY_RETRY_DELAY` | 0.1 | Initial retry delay |
| `TRINITY_RETRY_MAX_DELAY` | 30.0 | Maximum retry delay (with backoff) |
| `TRINITY_JITTER_FACTOR` | 0.1 | Jitter factor for polling (0.0-1.0) |
| `TRINITY_GRACEFUL_SHUTDOWN_TIMEOUT` | 30.0 | Graceful shutdown timeout |

### Broadcast Timeout

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_BROADCAST_TIMEOUT` | 2.0 | Timeout for progress/status broadcasts |

### Async Utility Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_PROCESS_WAIT_TIMEOUT` | 10.0 | Default timeout for async_process_wait |
| `JARVIS_SUBPROCESS_TIMEOUT` | 30.0 | Default timeout for async_subprocess_run |
| `JARVIS_LSOF_TIMEOUT` | 5.0 | Timeout for lsof subprocess calls |

## Lock Files

### Locations
- **Startup Lock**: `${JARVIS_LOCK_DIR}/startup_lock.lock`
- **VBIA State Lock**: `${JARVIS_LOCK_DIR}/vbia_state.lock`
- **Voice Client Lock**: `${JARVIS_LOCK_DIR}/voice_client.lock`

### Format
Lock files contain JSON metadata written atomically:

```json
{
  "owner_pid": 12345,
  "owner_host": "hostname",
  "acquired_at": 1707000000.0,
  "source": "jarvis"
}
```

### Lock Behavior
- **Atomic Acquisition**: Uses `fcntl.flock()` for kernel-level atomic locking (POSIX)
- **Ephemeral**: Lock automatically released on process death
- **Non-Blocking Event Loop**: All blocking I/O runs in executor threads
- **Not Reentrant**: Same process must not acquire same lock twice
- **Stale Detection**: Warns if lock held > 30 seconds (configurable)

## Heartbeat Files

### Supervisor Heartbeat Location
`~/.jarvis/supervisor_heartbeat.json` (primary)
`~/.jarvis/trinity/heartbeats/supervisor.json` (legacy)
`~/.jarvis/trinity/heartbeats/{component}.json` (per-component)

### Format
```json
{
  "pid": 12345,
  "timestamp": 1707000000.0,
  "status": "running",
  "component": "supervisor"
}
```

### Staleness Detection
A heartbeat file is considered stale if:
- `timestamp` > 30 seconds old (configurable via `TRINITY_HEARTBEAT_TIMEOUT`)
- PID no longer running (dead process verified via `os.kill(pid, 0)`)
- Process running but doesn't match JARVIS pattern (PID reused by OS)

### Multi-Directory Search
The system checks multiple heartbeat directories for backwards compatibility:
1. `~/.jarvis/trinity/heartbeats/` (PRIMARY)
2. `~/.jarvis/trinity/components/` (LEGACY)
3. `~/.jarvis/cross_repo/` (Cross-repo heartbeats)

## State Files

### Locations

| File | Purpose |
|------|---------|
| `~/.jarvis/trinity/orchestrator_state.json` | Main orchestrator state |
| `~/.jarvis/trinity/readiness_state.json` | Component readiness tracking |
| `~/.jarvis/trinity/cloud_lock.json` | GCP/cloud offload lock |
| `~/.jarvis/trinity/shutdown_state.json` | Shutdown state persistence |
| `~/.jarvis/cross_repo/orchestrator_state.json` | Cross-repo orchestrator state |
| `~/.jarvis/cross_repo/prime_state.json` | J-Prime specific state |
| `~/.jarvis/state/` | General state persistence directory |

### Trinity Directory Structure
```
~/.jarvis/trinity/
    commands/          # IPC command queue
    heartbeats/        # Component heartbeat files
    components/        # Component state (legacy)
    responses/         # IPC response storage
    dlq/               # Dead letter queue
    pids/              # PID files
```

## Must Succeed vs Best Effort Operations

### Must Succeed (Startup Fails if These Fail)

These operations MUST complete successfully or startup is aborted:

| Operation | Timeout | Description |
|-----------|---------|-------------|
| Startup lock acquisition | 30s | Single-instance coordination lock |
| Instance takeover handover | 15s | Graceful handover during instance takeover |
| Core service health checks | 30s | Backend and WebSocket server health |
| jarvis-core initialization | 30s | Core API must be available |

**Criticality Levels from ComponentRegistry:**
- `REQUIRED`: Failure causes system abort (e.g., jarvis-core)

### Best Effort (Should Not Block Startup)

These operations should not prevent startup if they fail:

| Operation | Behavior on Failure |
|-----------|---------------------|
| Progress broadcasts to loading server | Log warning, continue |
| Lock cleanup for stale locks | Log warning, continue |
| JARVIS-Prime startup | Fallback to Claude API |
| Reactor-Core startup | Continue without training |
| Redis connection | Continue without caching |
| GCP pre-warm | Continue without cloud acceleration |
| Heartbeat broadcasts | Log warning, continue |
| Voice unlock initialization | Continue without biometrics |

**Criticality Levels from ComponentRegistry:**
- `DEGRADED_OK`: System continues with reduced functionality (e.g., jarvis-prime, voice-unlock, cloud-sql)
- `OPTIONAL`: System continues normally without this component (e.g., reactor-core, redis, gcp-prewarm)

### Fallback Strategies

| Strategy | Behavior |
|----------|----------|
| `RETRY_THEN_CONTINUE` | Retry with backoff, then continue without component |
| `CONTINUE` | Immediately continue without component |
| `FAIL` | Abort startup if component fails |

## Shutdown Signals

| Signal | Exit Code | Behavior |
|--------|-----------|----------|
| `SIGINT` (Ctrl+C) | 130 (128+2) | Graceful shutdown - complete current operations |
| `SIGTERM` | 143 (128+15) | Graceful shutdown - same as SIGINT |
| `SIGHUP` | N/A | Reserved for `os.execv()` restart (supervisor_singleton) |
| `SIGKILL` | 137 (128+9) | Immediate termination (not catchable) |

### Shutdown Signal Escalation

The shutdown sequence uses escalating signals:

```
SIGINT (graceful)
    |
    v (wait CLEANUP_TIMEOUT_SIGINT seconds)
SIGTERM
    |
    v (wait CLEANUP_TIMEOUT_SIGTERM seconds)
SIGKILL (force)
    |
    v (wait CLEANUP_TIMEOUT_SIGKILL seconds)
Give up / Report zombie
```

### Shutdown Phases

1. **ANNOUNCE** (5s) - Notify all components shutdown is starting
2. **DRAIN** (30s) - Complete in-flight requests (skipped on force shutdown)
3. **SAVE** (15s) - Persist critical state to disk
4. **CLEANUP** (10s) - Close connections, release resources, IPC cleanup
5. **TERMINATE** (20s) - Send signals to processes
6. **VERIFY** (5s) - Confirm all processes terminated

### Termination Order

Components are terminated in this order to respect dependencies:
1. `reactor_core` (Nerves)
2. `jarvis_prime` (Mind)
3. `jarvis_body` (Body)

## Platform Notes

### Windows
- **fcntl not available**: `RobustFileLock` raises `RuntimeError` on Windows
- **Alternative**: Use `msvcrt.locking()` for file locks (not currently implemented)
- **Unix domain sockets**: Not supported - use localhost TCP instead
- **Signal handling**: SIGTERM not available in all contexts; use `signal.signal()` fallback

```python
# Windows limitation in robust_file_lock.py
if sys.platform == "win32":
    raise RuntimeError(
        "RobustFileLock is POSIX-only (Linux, macOS). "
        "Windows requires a different implementation using msvcrt.locking."
    )
```

### macOS
- **Unix socket paths**: Limited to ~104 characters
- **Recommendation**: Use shorter paths or TCP for IPC
- **Apple Silicon**: Hardware detection available for adaptive startup profiles

### Linux
- **Full support**: All features including `fcntl.flock()`
- **NFS caveat**: Lock directory must be on local filesystem (not NFS)

## Service Registry

### Registry Location
`~/.jarvis/registry/services.json`

### Format
```json
{
  "jarvis": {"pid": 12345, "port": 8010, "status": "running"},
  "jarvis-prime": {"pid": 12346, "port": 8001, "status": "running"},
  "reactor-core": {"pid": 12347, "port": 8090, "status": "running"}
}
```

## IPC Communication

### Command Queue
Components communicate via file-based IPC through `~/.jarvis/trinity/commands/`:
- Commands are JSON files with UUID-based names
- Responses stored in `~/.jarvis/trinity/responses/`
- Dead letter queue in `~/.jarvis/trinity/dlq/` for failed commands

### Command Format
```json
{
  "command_id": "uuid",
  "source": "jarvis_body",
  "target": "jarvis_prime",
  "action": "shutdown_announce",
  "payload": {},
  "priority": "critical",
  "timeout_seconds": 5.0,
  "correlation_id": "shutdown_abc123"
}
```

## Orphan Process Detection

### Detection Methods
1. **PID file staleness**: Check if PID files point to dead processes
2. **Process pattern matching**: Find processes matching JARVIS patterns
3. **Heartbeat staleness**: Find processes with old/missing heartbeats
4. **HTTP health check**: Verify via health endpoint (most reliable)

### Component Patterns
```python
COMPONENT_PATTERNS = {
    "jarvis_body": ["backend.main", "jarvis_supervisor", "run_supervisor"],
    "jarvis_prime": ["jarvis_prime", "jarvis-prime", "jprime"],
    "reactor_core": ["reactor_core", "reactor-core", "training_pipeline"],
}
```

### HTTP Health Ports
```python
COMPONENT_HTTP_PORTS = {
    "jarvis_body": [8080, 8000, 5000],
    "jarvis_prime": [8091, 8001, 5001],
    "reactor_core": [8090, 8002, 5002],
}
```

### Startup Grace Period
Newly started processes (< 120 seconds old) are not marked as orphans to allow time for heartbeat establishment.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2026-02-02 | Initial documentation |
