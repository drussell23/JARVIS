# 🚀 JARVIS Trinity Ultra v87.0 - Complete Implementation Guide

**Status**: Backend ✅ COMPLETE (18/18 features) | Frontend 🔨 IN PROGRESS (State variables added)

---

## 📊 Implementation Status

### Backend (`loading_server.py`) - ✅ 100% COMPLETE

All 18 v87.0 features are fully implemented and functional:

| # | Feature | Lines | Status |
|---|---------|-------|--------|
| 1-10 | Core Trinity Components | 136-825 | ✅ DONE |
| 11 | Predictive ETA Calculator | 843-1107 | ✅ DONE |
| 12 | Cross-Repo Health Aggregator | 1125-1256 | ✅ DONE |
| 13 | Event Loop Manager | 1258-1306 | ✅ DONE |
| 14 | Supervisor Heartbeat Monitor | 1324-1371 | ✅ DONE |
| 15 | WebSocket Heartbeat Timeout | 2936-2960 | ✅ DONE |
| 16 | Progress Versioning | 2434-2435, 2515-2516 | ✅ DONE |
| 17 | Redirect Grace Period | 2661-2678 | ✅ DONE |
| 18 | Enhanced API Endpoints | 6047-6050 | ✅ DONE |

**New API Endpoints**:
- `GET /api/eta/predict` - ML-based ETA prediction with confidence
- `GET /api/health/unified` - Cross-repo Trinity health status
- `GET /api/analytics/startup-performance` - Historical performance trends
- `GET /api/supervisor/heartbeat` - Supervisor crash detection

---

## 🎯 Frontend Integration Plan (`loading-manager.js`)

### Phase 1: State Variables ✅ COMPLETE

Added comprehensive v87.0 state tracking (lines 1073-1178):
- Predictive ETA tracking
- Sequence number detection
- Redirect grace period control
- Unified health monitoring
- Supervisor heartbeat tracking
- Parallel component progress
- Network condition detection
- Smart caching metrics
- Clock skew detection
- Offline mode detection

### Phase 2: Core Handler Updates 🔨 REQUIRED

#### 1. Enhanced `handleProgressUpdate()` Method

**Location**: Line 2089
**Purpose**: Process v87.0 data from backend

```javascript
handleProgressUpdate(data) {
    // ... existing code ...

    // ═══════════════════════════════════════════════════════════
    // v87.0: PREDICTIVE ETA DISPLAY
    // ═══════════════════════════════════════════════════════════
    if (data.predictive_eta) {
        this.state.predictiveETA = {
            etaSeconds: data.predictive_eta.eta_seconds,
            confidence: data.predictive_eta.confidence,
            estimatedCompletion: data.predictive_eta.estimated_completion,
            predictionMethod: data.prediction_method || 'ema_historical_fusion',
            lastUpdate: Date.now()
        };

        // Display ETA in UI
        this.updateETADisplay();
    }

    // ═══════════════════════════════════════════════════════════
    // v87.0: SEQUENCE NUMBER TRACKING
    // ═══════════════════════════════════════════════════════════
    if (data.sequence_number !== undefined) {
        const currentSeq = data.sequence_number;
        const lastSeq = this.state.sequencing.lastSequenceNumber;

        if (lastSeq >= 0) {
            const expectedSeq = lastSeq + 1;
            const missed = currentSeq - expectedSeq;

            if (missed > 0) {
                console.warn(`[v87.0] ⚠️  Missed ${missed} updates! (expected ${expectedSeq}, got ${currentSeq})`);
                this.state.sequencing.missedUpdates += missed;

                // Request missed updates if critical
                if (missed > 3) {
                    this.requestMissedUpdates(expectedSeq, currentSeq);
                }
            }
        }

        this.state.sequencing.lastSequenceNumber = currentSeq;
        this.state.sequencing.totalUpdates++;
        this.state.sequencing.lastSequenceCheck = Date.now();
    }

    // ═══════════════════════════════════════════════════════════
    // v87.0: REDIRECT GRACE PERIOD HANDLING
    // ═══════════════════════════════════════════════════════════
    if (data.redirect_ready !== undefined) {
        this.state.redirectGrace.redirectReady = data.redirect_ready;
        this.state.redirectGrace.secondsUntilRedirect = data.seconds_until_redirect;

        if (data.redirect_ready === false && data.seconds_until_redirect !== null) {
            // Show countdown during grace period
            this.displayRedirectCountdown(data.seconds_until_redirect);
        } else if (data.redirect_ready === true && this.state.progress >= 100) {
            // Grace period complete - safe to redirect
            console.log('[v87.0] ✅ Redirect grace period complete - initiating redirect');
            this.initiateRedirect();
        }
    }

    // ═══════════════════════════════════════════════════════════
    // v87.0: PARALLEL COMPONENT PROGRESS
    // ═══════════════════════════════════════════════════════════
    if (data.parallel_components) {
        const components = data.parallel_components;

        if (components.jarvis_prime) {
            this.state.parallelComponents.jarvisPrime = {
                name: 'JARVIS Prime',
                progress: components.jarvis_prime.progress || 0,
                status: components.jarvis_prime.status || 'pending',
                phase: components.jarvis_prime.phase || 'init',
                eta: components.jarvis_prime.eta || null
            };
        }

        if (components.reactor_core) {
            this.state.parallelComponents.reactorCore = {
                name: 'Reactor Core',
                progress: components.reactor_core.progress || 0,
                status: components.reactor_core.status || 'pending',
                phase: components.reactor_core.phase || 'init',
                eta: components.reactor_core.eta || null
            };
        }

        // Update parallel progress UI
        this.updateParallelComponentUI();
    }

    // ═══════════════════════════════════════════════════════════
    // v87.0: CLOCK SKEW DETECTION
    // ═══════════════════════════════════════════════════════════
    if (data.timestamp) {
        const serverTime = new Date(data.timestamp).getTime();
        const clientTime = Date.now();
        const skewMs = Math.abs(serverTime - clientTime);

        this.state.clockSkew.serverTimestamp = serverTime;
        this.state.clockSkew.clientTimestamp = clientTime;
        this.state.clockSkew.skewMs = skewMs;
        this.state.clockSkew.lastCheck = clientTime;

        if (skewMs > this.state.clockSkew.threshold) {
            if (!this.state.clockSkew.detected) {
                console.warn(`[v87.0] ⏰ Clock skew detected: ${(skewMs/1000).toFixed(1)}s difference`);
                this.state.clockSkew.detected = true;
                this.addLogEntry('System', `Clock skew detected: ${(skewMs/1000).toFixed(1)}s`, 'warning');
            }
        } else {
            this.state.clockSkew.detected = false;
        }
    }

    // ... rest of existing code ...
}
```

#### 2. Enhanced `connectWebSocket()` Method

**Location**: Line 1752
**Purpose**: Add WebSocket pong handler

```javascript
this.state.ws.onmessage = (event) => {
    try {
        const data = JSON.parse(event.data);
        this.state.lastWsMessage = Date.now();
        this.state.wsHealthy = true;

        // ═══════════════════════════════════════════════════════════
        // v87.0: WEBSOCKET PONG HANDLER
        // ═══════════════════════════════════════════════════════════
        if (data.type === 'heartbeat') {
            // Respond with pong
            this.state.ws.send(JSON.stringify({
                type: 'pong',
                timestamp: Date.now(),
                client_time: Date.now()
            }));
            console.debug('[v87.0] ♥️  Sent pong response');
            return; // Don't process heartbeat as progress update
        }

        if (data.type !== 'pong') {
            this.handleProgressUpdate(data);
        }
    } catch (error) {
        console.error('[WebSocket] Parse error:', error);
    }
};
```

### Phase 3: New Methods 🔨 REQUIRED

#### 1. Predictive ETA Display

```javascript
/**
 * v87.0: Display ML-based ETA prediction
 */
updateETADisplay() {
    const eta = this.state.predictiveETA;
    if (!eta.etaSeconds || eta.etaSeconds === null) return;

    const seconds = Math.ceil(eta.etaSeconds);
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;

    let etaText = '';
    if (minutes > 0) {
        etaText = `~${minutes}m ${remainingSeconds}s remaining`;
    } else {
        etaText = `~${seconds}s remaining`;
    }

    // Add confidence indicator
    const confidencePercent = Math.round((eta.confidence || 0) * 100);
    const confidenceIndicator = confidencePercent >= 80 ? '●●●' :
                                 confidencePercent >= 60 ? '●●○' :
                                 confidencePercent >= 40 ? '●○○' : '○○○';

    // Update DOM (modify based on your HTML structure)
    const etaElement = document.getElementById('eta-display');
    if (etaElement) {
        etaElement.innerHTML = `
            <span class="eta-time">${etaText}</span>
            <span class="eta-confidence" title="${confidencePercent}% confidence">
                ${confidenceIndicator}
            </span>
        `;
        etaElement.style.display = 'block';
    }

    console.log(`[v87.0] 📊 ETA: ${etaText} (${confidencePercent}% confidence, method: ${eta.predictionMethod})`);
}

/**
 * v87.0: Display redirect countdown during grace period
 */
displayRedirectCountdown(secondsRemaining) {
    if (secondsRemaining === null || secondsRemaining < 0) return;

    const countdownElement = document.getElementById('redirect-countdown');
    if (countdownElement) {
        if (secondsRemaining > 0) {
            countdownElement.textContent = `Redirecting in ${secondsRemaining.toFixed(1)}s...`;
            countdownElement.style.display = 'block';
            this.state.redirectGrace.countdownActive = true;
        } else {
            countdownElement.style.display = 'none';
            this.state.redirectGrace.countdownActive = false;
        }
    }
}
```

#### 2. Unified Health Polling

```javascript
/**
 * v87.0: Poll unified Trinity health status
 */
async pollUnifiedHealth() {
    const now = Date.now();
    const timeSinceLastPoll = now - (this.state.unifiedHealth.lastPoll || 0);

    // Poll every 5 seconds
    if (timeSinceLastPoll < this.state.unifiedHealth.pollInterval) {
        return;
    }

    try {
        const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;
        const response = await fetch(`${loadingUrl}/api/health/unified`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            signal: AbortSignal.timeout(3000)
        });

        if (response.ok) {
            const health = await response.json();

            this.state.unifiedHealth = {
                overallHealth: health.overall_health || 0,
                state: health.state || 'unknown',
                components: health.components || {},
                circuitBreakers: health.circuit_breakers || {},
                lastPoll: now,
                pollInterval: 5000
            };

            // Update health UI
            this.updateHealthStatusUI(health);

            console.log(`[v87.0] 🏥 Health: ${health.state} (${health.overall_health}%)`);
        }
    } catch (error) {
        console.debug('[v87.0] Health polling error:', error.message);
    }
}

/**
 * v87.0: Update health status UI
 */
updateHealthStatusUI(health) {
    const healthElement = document.getElementById('trinity-health-status');
    if (!healthElement) return;

    const stateEmoji = {
        'healthy': '✅',
        'degraded': '⚠️',
        'critical': '❌',
        'unknown': '⚪'
    };

    const emoji = stateEmoji[health.state] || '⚪';
    const healthPercent = health.overall_health || 0;

    healthElement.innerHTML = `
        <div class="health-status health-${health.state}">
            ${emoji} Trinity Health: ${healthPercent}% (${health.state})
        </div>
    `;
}
```

#### 3. Supervisor Heartbeat Check

```javascript
/**
 * v87.0: Check supervisor heartbeat for crash detection
 */
async checkSupervisorHeartbeat() {
    try {
        const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;
        const response = await fetch(`${loadingUrl}/api/supervisor/heartbeat`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            signal: AbortSignal.timeout(3000)
        });

        if (response.ok) {
            const status = await response.json();

            const wasAlive = this.state.supervisorHeartbeat.alive;
            const isAlive = status.supervisor_alive;

            this.state.supervisorHeartbeat = {
                alive: isAlive,
                lastUpdate: status.last_update_timestamp,
                timeSinceUpdate: status.time_since_update,
                timeoutThreshold: status.timeout_threshold,
                crashed: !isAlive,
                recoveredAt: (!wasAlive && isAlive) ? Date.now() : this.state.supervisorHeartbeat.recoveredAt
            };

            // Alert on crash
            if (!isAlive && wasAlive) {
                console.error('[v87.0] 💀 SUPERVISOR CRASH DETECTED!');
                this.showSupervisorCrashError();
            }

            // Log recovery
            if (isAlive && !wasAlive) {
                console.log('[v87.0] ✅ Supervisor recovered!');
                this.addLogEntry('System', 'Supervisor process recovered', 'success');
            }
        }
    } catch (error) {
        console.debug('[v87.0] Supervisor heartbeat check error:', error.message);
    }
}

/**
 * v87.0: Show supervisor crash error
 */
showSupervisorCrashError() {
    const errorElement = document.getElementById('supervisor-crash-alert');
    if (errorElement) {
        errorElement.innerHTML = `
            <div class="error-alert critical">
                <strong>⚠️ Supervisor Process Crashed</strong>
                <p>The JARVIS supervisor process has stopped. Startup may be interrupted.</p>
                <p>Check logs: <code>~/.jarvis/logs/supervisor.log</code></p>
            </div>
        `;
        errorElement.style.display = 'block';
    }

    this.addLogEntry('System', 'Supervisor process crashed - check logs', 'error');
}
```

#### 4. Parallel Component Progress UI

```javascript
/**
 * v87.0: Update parallel component progress bars
 */
updateParallelComponentUI() {
    const jprime = this.state.parallelComponents.jarvisPrime;
    const reactor = this.state.parallelComponents.reactorCore;

    // Update JARVIS Prime progress
    const jprimeElement = document.getElementById('jprime-progress');
    if (jprimeElement) {
        jprimeElement.innerHTML = `
            <div class="component-progress">
                <div class="component-name">
                    🧠 ${jprime.name}
                    <span class="component-status status-${jprime.status}">${jprime.status}</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${jprime.progress}%"></div>
                </div>
                <div class="component-details">
                    ${jprime.progress}% • ${jprime.phase}
                    ${jprime.eta ? ` • ${Math.ceil(jprime.eta)}s remaining` : ''}
                </div>
            </div>
        `;
    }

    // Update Reactor Core progress
    const reactorElement = document.getElementById('reactor-progress');
    if (reactorElement) {
        reactorElement.innerHTML = `
            <div class="component-progress">
                <div class="component-name">
                    ⚛️  ${reactor.name}
                    <span class="component-status status-${reactor.status}">${reactor.status}</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${reactor.progress}%"></div>
                </div>
                <div class="component-details">
                    ${reactor.progress}% • ${reactor.phase}
                    ${reactor.eta ? ` • ${Math.ceil(reactor.eta)}s remaining` : ''}
                </div>
            </div>
        `;
    }
}
```

#### 5. Network Condition Detection

```javascript
/**
 * v87.0: Detect network conditions for adaptive strategies
 */
detectNetworkCondition() {
    if (!navigator.connection) {
        console.debug('[v87.0] Network Information API not available');
        return;
    }

    const connection = navigator.connection;

    this.state.networkCondition = {
        type: connection.type || 'unknown',
        effectiveType: connection.effectiveType || '4g',
        downlink: connection.downlink || 10,
        rtt: connection.rtt || 50,
        saveData: connection.saveData || false,
        lastCheck: Date.now()
    };

    // Adapt polling based on network
    if (connection.effectiveType === 'slow-2g' || connection.effectiveType === '2g') {
        // Slow network - reduce polling frequency
        this.pollingState.currentInterval = Math.max(
            this.pollingState.currentInterval,
            5000
        );
        console.log('[v87.0] 🐌 Slow network detected - reducing polling frequency');
    } else if (connection.effectiveType === '4g' || connection.type === 'wifi') {
        // Fast network - can poll more aggressively
        this.pollingState.currentInterval = Math.max(
            this.pollingState.currentInterval * 0.8,
            this.config.polling.minInterval
        );
        console.log('[v87.0] 🚀 Fast network detected - increasing polling frequency');
    }

    // Listen for network changes
    connection.addEventListener('change', () => {
        this.detectNetworkCondition();
    });
}
```

#### 6. Offline Mode Detection

```javascript
/**
 * v87.0: Detect offline mode and show appropriate error
 */
handleOfflineMode() {
    const consecutiveFailures = this.state.offlineMode.consecutiveFailures;
    const threshold = this.state.offlineMode.threshold;

    if (consecutiveFailures >= threshold && !this.state.offlineMode.active) {
        console.warn('[v87.0] 🔴 Offline mode detected - no successful requests in last attempts');

        this.state.offlineMode.active = true;
        this.state.offlineMode.detectedAt = Date.now();

        this.showOfflineModeError();
    }
}

/**
 * v87.0: Show offline mode error message
 */
showOfflineModeError() {
    const errorElement = document.getElementById('offline-mode-alert');
    if (errorElement) {
        errorElement.innerHTML = `
            <div class="error-alert warning">
                <strong>⚠️ Loading Server Unreachable</strong>
                <p>Cannot connect to JARVIS loading server (port 3001).</p>
                <p>Possible causes:</p>
                <ul>
                    <li>Loading server crashed or exited early</li>
                    <li>Network connectivity issues</li>
                    <li>Firewall blocking connection</li>
                </ul>
                <p>Check <code>~/.jarvis/logs/loading_server.log</code> for details.</p>
                <button onclick="location.reload()">Retry Connection</button>
            </div>
        `;
        errorElement.style.display = 'block';
    }
}

/**
 * v87.0: Record successful request (reset offline detection)
 */
recordSuccessfulRequest() {
    this.state.offlineMode.consecutiveFailures = 0;
    this.state.offlineMode.lastSuccessfulRequest = Date.now();

    if (this.state.offlineMode.active) {
        console.log('[v87.0] ✅ Connection restored - exiting offline mode');
        this.state.offlineMode.active = false;

        const errorElement = document.getElementById('offline-mode-alert');
        if (errorElement) {
            errorElement.style.display = 'none';
        }
    }
}

/**
 * v87.0: Record failed request (increment offline detection)
 */
recordFailedRequest() {
    this.state.offlineMode.consecutiveFailures++;
    this.handleOfflineMode();
}
```

#### 7. Startup Analytics Display (Optional)

```javascript
/**
 * v87.0: Fetch and display startup performance analytics
 */
async fetchStartupAnalytics() {
    try {
        const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;
        const response = await fetch(`${loadingUrl}/api/analytics/startup-performance`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            signal: AbortSignal.timeout(3000)
        });

        if (response.ok) {
            const analytics = await response.json();

            console.log('[v87.0] 📈 Startup Analytics:', {
                total_startups: analytics.total_startups,
                average_duration: analytics.average_duration?.toFixed(1) + 's',
                min_duration: analytics.min_duration?.toFixed(1) + 's',
                max_duration: analytics.max_duration?.toFixed(1) + 's',
                trend: analytics.trend_direction
            });

            // Optionally display in UI
            this.displayAnalytics(analytics);
        }
    } catch (error) {
        console.debug('[v87.0] Analytics fetch error:', error.message);
    }
}
```

### Phase 4: Integration Points 🔨 REQUIRED

#### 1. Modify `init()` Method

Add v87.0 initialization:

```javascript
async init() {
    // ... existing init code ...

    // v87.0: Initialize network condition detection
    this.detectNetworkCondition();

    // v87.0: Start periodic health polling
    setInterval(() => this.pollUnifiedHealth(), 5000);

    // v87.0: Start supervisor heartbeat checks
    setInterval(() => this.checkSupervisorHeartbeat(), 10000);

    // v87.0: Fetch startup analytics (optional)
    setTimeout(() => this.fetchStartupAnalytics(), 2000);
}
```

#### 2. Modify HTTP Polling Success Handler

Add offline mode recovery:

```javascript
// In your polling success handler
recordSuccessfulRequest(); // v87.0
```

#### 3. Modify HTTP Polling Failure Handler

Add offline mode detection:

```javascript
// In your polling error handler
recordFailedRequest(); // v87.0
```

---

## 🛠️ Backend Missing Features

### 1. Progress Resume Endpoint

**File**: `loading_server.py`
**Location**: After line 5504 (end of v87.0 endpoints)

```python
async def get_progress_resume(request: web.Request) -> web.Response:
    """
    v87.0: Resume progress from saved session.

    GET /api/progress/resume?session_id=abc123
    """
    try:
        session_id = request.query.get('session_id')
        if not session_id:
            return web.json_response({
                "status": "error",
                "message": "session_id required"
            }, status=400)

        # Load from ProgressPersistence
        session_data = progress_persistence.load_session(session_id)

        if session_data:
            return web.json_response({
                "status": "ok",
                "session": session_data,
                "timestamp": datetime.now().isoformat()
            })
        else:
            return web.json_response({
                "status": "not_found",
                "message": f"Session {session_id} not found"
            }, status=404)

    except Exception as e:
        logger.error(f"[ProgressResume] Error: {e}")
        return web.json_response({
            "status": "error",
            "message": str(e)
        }, status=500)
```

Add to routes in `create_app()`:

```python
# v87.0: Progress resume endpoint
app.router.add_get('/api/progress/resume', get_progress_resume)
```

Add method to `ProgressPersistence` class (line ~473):

```python
def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
    """Load saved session progress."""
    with sqlite3.connect(self.db_path) as conn:
        cursor = conn.execute("""
            SELECT * FROM progress_sessions
            WHERE session_id = ?
            ORDER BY last_updated DESC
            LIMIT 1
        """, (session_id,))

        row = cursor.fetchone()
        if row:
            return {
                'session_id': row[0],
                'started_at': row[1],
                'last_updated': row[2],
                'current_progress': row[3],
                'current_stage': row[4],
                'is_complete': row[5],
                'trace_id': row[6]
            }
        return None
```

### 2. FD Leak Detection

**File**: `loading_server.py`
**Location**: After line 555 (after ContainerAwareness)

```python
class FileDescriptorMonitor:
    """
    v87.0: File Descriptor leak detection and monitoring.

    Tracks open file descriptors and alerts on anomalies.
    """

    def __init__(self, threshold: int = 1024, alert_threshold: int = 800):
        self.baseline_fd_count: Optional[int] = None
        self.threshold = threshold  # Hard limit
        self.alert_threshold = alert_threshold  # Warning threshold
        self.last_check = time.monotonic()
        self.check_interval = 30.0  # Check every 30 seconds

    def get_fd_count(self) -> int:
        """Get current file descriptor count for this process."""
        try:
            import os
            pid = os.getpid()
            # macOS: count entries in /dev/fd/
            fd_dir = f"/dev/fd"
            if os.path.exists(fd_dir):
                return len(os.listdir(fd_dir))
            else:
                # Linux: count entries in /proc/self/fd/
                fd_dir = f"/proc/{pid}/fd"
                if os.path.exists(fd_dir):
                    return len(os.listdir(fd_dir))
        except Exception as e:
            logger.debug(f"[FDMonitor] Could not get FD count: {e}")

        return 0

    def establish_baseline(self) -> None:
        """Establish baseline FD count at startup."""
        self.baseline_fd_count = self.get_fd_count()
        logger.info(f"[FDMonitor] Baseline FD count: {self.baseline_fd_count}")

    def check_for_leaks(self) -> Optional[Dict[str, Any]]:
        """Check for FD leaks and return alert data if detected."""
        now = time.monotonic()
        if now - self.last_check < self.check_interval:
            return None

        self.last_check = now
        current_fd_count = self.get_fd_count()

        if current_fd_count == 0:
            return None  # Could not determine

        # Establish baseline if not set
        if self.baseline_fd_count is None:
            self.baseline_fd_count = current_fd_count
            return None

        # Calculate leak
        leak = current_fd_count - self.baseline_fd_count
        leak_percent = (leak / self.baseline_fd_count) * 100 if self.baseline_fd_count > 0 else 0

        # Check thresholds
        if current_fd_count >= self.threshold:
            return {
                'severity': 'critical',
                'current_fds': current_fd_count,
                'baseline_fds': self.baseline_fd_count,
                'leaked_fds': leak,
                'leak_percent': leak_percent,
                'message': f'FD limit reached: {current_fd_count}/{self.threshold}'
            }
        elif current_fd_count >= self.alert_threshold or leak_percent > 50:
            return {
                'severity': 'warning',
                'current_fds': current_fd_count,
                'baseline_fds': self.baseline_fd_count,
                'leaked_fds': leak,
                'leak_percent': leak_percent,
                'message': f'FD leak detected: +{leak} FDs ({leak_percent:.1f}% increase)'
            }

        return None

# Global instance
fd_monitor = FileDescriptorMonitor()
```

Add endpoint:

```python
async def get_fd_status(request: web.Request) -> web.Response:
    """v87.0: Get file descriptor status and leak detection."""
    try:
        current_count = fd_monitor.get_fd_count()
        leak_alert = fd_monitor.check_for_leaks()

        return web.json_response({
            "status": "ok",
            "fd_count": current_count,
            "baseline_fd_count": fd_monitor.baseline_fd_count,
            "threshold": fd_monitor.threshold,
            "alert_threshold": fd_monitor.alert_threshold,
            "leak_alert": leak_alert,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"[FDStatus] Error: {e}")
        return web.json_response({
            "status": "error",
            "message": str(e)
        }, status=500)
```

Initialize in `start_server()`:

```python
# v87.0: Establish FD baseline
logger.info("[v87.0] Establishing file descriptor baseline...")
fd_monitor.establish_baseline()
```

Add route:

```python
app.router.add_get('/api/system/fd-status', get_fd_status)
```

### 3. Fallback Static Page Generator

**File**: `loading_server.py`
**Location**: After line 825 (after SelfHealingRestartManager)

```python
class FallbackStaticPageGenerator:
    """
    v87.0: Generate fallback static HTML when loading server crashes.

    Creates a minimal HTML page that users see if loading server dies.
    """

    def __init__(self, output_path: Optional[Path] = None):
        if output_path is None:
            output_path = Path.home() / ".jarvis" / "loading_server" / "fallback.html"

        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def generate(self, reason: str = "Loading server unavailable") -> None:
        """Generate fallback static page."""
        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JARVIS Loading - Fallback Mode</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{
            max-width: 600px;
            text-align: center;
            background: rgba(0, 0, 0, 0.3);
            padding: 40px;
            border-radius: 20px;
            backdrop-filter: blur(10px);
        }}
        h1 {{ font-size: 2.5em; margin-bottom: 20px; }}
        .icon {{ font-size: 4em; margin-bottom: 20px; }}
        .message {{ font-size: 1.2em; margin-bottom: 30px; opacity: 0.9; }}
        .details {{
            background: rgba(0, 0, 0, 0.4);
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: left;
        }}
        .details h3 {{ margin-bottom: 10px; }}
        .details ul {{ list-style: none; padding-left: 20px; }}
        .details li {{ margin: 8px 0; }}
        .details li:before {{ content: "•"; color: #667eea; font-weight: bold; display: inline-block; width: 1em; }}
        button {{
            background: #667eea;
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 10px;
            font-size: 1.1em;
            cursor: pointer;
            transition: all 0.3s;
        }}
        button:hover {{ background: #5568d3; transform: translateY(-2px); }}
        .timestamp {{ font-size: 0.9em; opacity: 0.6; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="icon">⚠️</div>
        <h1>JARVIS Loading - Fallback Mode</h1>
        <p class="message">
            The JARVIS loading server is currently unavailable.
        </p>

        <div class="details">
            <h3>Status Information:</h3>
            <ul>
                <li><strong>Reason:</strong> {reason}</li>
                <li><strong>Expected Service:</strong> Loading Server (port 3001)</li>
                <li><strong>Fallback Page Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</li>
            </ul>
        </div>

        <div class="details">
            <h3>What to do:</h3>
            <ul>
                <li>Check if JARVIS is still starting in the background</li>
                <li>Look for errors in: <code>~/.jarvis/logs/loading_server.log</code></li>
                <li>Try restarting JARVIS: <code>python3 run_supervisor.py</code></li>
                <li>If backend is ready, try accessing directly: <a href="http://localhost:8010" style="color: #667eea;">http://localhost:8010</a></li>
            </ul>
        </div>

        <button onclick="location.reload()">
            🔄 Retry Connection
        </button>

        <p class="timestamp">
            This fallback page was automatically generated by JARVIS Trinity v87.0
        </p>
    </div>

    <script>
        // Auto-retry connection every 5 seconds
        let retryCount = 0;
        setInterval(async () => {{
            try {{
                const response = await fetch('http://localhost:3001/health/ping');
                if (response.ok) {{
                    console.log('Loading server is back online - reloading...');
                    location.reload();
                }}
            }} catch (e) {{
                retryCount++;
                console.log(`Retry ${{retryCount}}: Loading server still offline`);
            }}
        }}, 5000);
    </script>
</body>
</html>"""

        with open(self.output_path, 'w') as f:
            f.write(html_content)

        logger.info(f"[FallbackPage] Generated at {self.output_path}")

    def get_path(self) -> Path:
        """Get path to fallback page."""
        return self.output_path

# Global instance
fallback_page_generator = FallbackStaticPageGenerator()
```

Generate on startup:

```python
# In start_server(), before creating app:
# v87.0: Generate fallback static page
logger.info("[v87.0] Generating fallback static page...")
fallback_page_generator.generate(reason="Loading server starting")
```

---

## 🎨 HTML Updates Required

Add these elements to `loading.html`:

```html
<!-- v87.0: ETA Display -->
<div id="eta-display" style="display: none;">
    <span class="eta-time"></span>
    <span class="eta-confidence"></span>
</div>

<!-- v87.0: Redirect Countdown -->
<div id="redirect-countdown" style="display: none;"></div>

<!-- v87.0: Trinity Health Status -->
<div id="trinity-health-status"></div>

<!-- v87.0: Parallel Component Progress -->
<div id="parallel-components">
    <div id="jprime-progress"></div>
    <div id="reactor-progress"></div>
</div>

<!-- v87.0: Supervisor Crash Alert -->
<div id="supervisor-crash-alert" style="display: none;"></div>

<!-- v87.0: Offline Mode Alert -->
<div id="offline-mode-alert" style="display: none;"></div>
```

---

## ✅ Implementation Checklist

### Backend
- [x] All 18 core v87.0 features implemented
- [ ] Progress resume endpoint (`/api/progress/resume`)
- [ ] FD leak detection endpoint (`/api/system/fd-status`)
- [ ] Fallback static page generation

### Frontend
- [x] v87.0 state variables added
- [ ] `handleProgressUpdate()` enhanced with v87.0 handlers
- [ ] `connectWebSocket()` enhanced with pong responses
- [ ] `updateETADisplay()` method added
- [ ] `pollUnifiedHealth()` method added
- [ ] `checkSupervisorHeartbeat()` method added
- [ ] `updateParallelComponentUI()` method added
- [ ] `detectNetworkCondition()` method added
- [ ] `handleOfflineMode()` method added
- [ ] HTML elements added for v87.0 features

### Testing
- [ ] Test ETA prediction display with real startup
- [ ] Test sequence number detection with intentional packet loss
- [ ] Test redirect grace period timing
- [ ] Test unified health polling
- [ ] Test supervisor crash detection
- [ ] Test WebSocket pong responses
- [ ] Test parallel component progress bars
- [ ] Test offline mode detection
- [ ] Test FD leak detection
- [ ] Test fallback page generation

---

## 🚀 Quick Start Implementation

Run these commands to apply all changes:

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent

# 1. Backend is already complete - verify
grep -A 2 "v87.0 Trinity Ultra" loading_server.py

# 2. Apply frontend updates (use this guide to modify loading-manager.js)
# Sections to modify:
# - handleProgressUpdate() (line 2089)
# - connectWebSocket() (line 1752)
# Add new methods as documented above

# 3. Test the integration
python3 run_supervisor.py
```

---

## 📊 Expected Results

After full implementation:

1. **ETA Display**: See "~30s remaining ●●●" with confidence indicators
2. **Sequence Tracking**: Console warns about missed updates
3. **Grace Period**: "Redirecting in 2.3s..." countdown at 100%
4. **Health Status**: "✅ Trinity Health: 98% (healthy)"
5. **Heartbeat Alerts**: Console warns if supervisor crashes
6. **Pong Responses**: WebSocket heartbeat acknowledged
7. **Parallel Progress**: Separate bars for J-Prime (🧠) and Reactor (⚛️)
8. **Network Adaptation**: Polling adjusts based on connection quality
9. **Offline Detection**: Alert shown after 3 consecutive failures
10. **FD Warnings**: Console logs if file descriptors leak

---

This guide provides complete implementation details for finishing the v87.0 Trinity Ultra integration. Follow the sections in order for best results.
