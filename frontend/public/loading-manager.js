/**
 * JARVIS Advanced Loading Manager v5.1 - Zero-Touch Edition
 *
 * ARCHITECTURE: Display-only client that trusts start_system.py as authority
 *
 * Flow: start_system.py → loading_server.py → loading-manager.js (here)
 *
 * This component does NOT independently verify system readiness.
 * It displays progress received from the loading server and redirects
 * when told to. start_system.py is responsible for verifying frontend
 * is ready before sending "complete".
 *
 * Features:
 * - Smooth 1-100% progress with real-time updates
 * - Detailed stage tracking matching backend broadcast stages
 * - Voice biometric system initialization feedback
 * - Cloud ECAPA pre-warming status
 * - Memory-aware mode selection display
 * - Recent speaker cache initialization
 * - WebSocket PRIMARY + Adaptive HTTP polling fallback
 * - Exponential backoff with jitter for rate limiting
 * - Circuit breaker pattern for resilience
 * - Dynamic interval adjustment based on server response
 * - Request deduplication and throttling
 * - Epic cinematic completion animation
 * - Quick sanity check before redirect (safety net only)
 * 
 * v5.1 Zero-Touch Features:
 * - Zero-Touch autonomous update stage tracking
 * - Dead Man's Switch monitoring display
 * - Update validation progress
 * - DMS rollback stage handling
 */

class JARVISLoadingManager {
    constructor() {
        this.config = {
            // Loading server runs on port 3001 during restart
            loadingServerPort: 3001,
            backendPort: 8010,
            mainAppPort: 3000,
            wsProtocol: window.location.protocol === 'https:' ? 'wss:' : 'ws:',
            httpProtocol: window.location.protocol,
            hostname: window.location.hostname || 'localhost',
            reconnect: {
                enabled: true,
                initialDelay: 500,
                maxDelay: 10000,
                maxAttempts: 30,
                backoffMultiplier: 1.5
            },
            polling: {
                enabled: true,
                // Adaptive polling configuration
                baseInterval: 2000,      // Base: 2 seconds (conservative)
                minInterval: 1000,       // Min: 1 second (when server is responsive)
                maxInterval: 30000,      // Max: 30 seconds (when rate limited)
                timeout: 5000,
                // Backoff settings
                backoffMultiplier: 2.0,  // Double interval on rate limit
                recoveryMultiplier: 0.8, // Reduce by 20% on success
                jitterFactor: 0.2,       // Add 0-20% random jitter
                // Circuit breaker
                circuitBreaker: {
                    threshold: 5,        // Open after 5 consecutive failures
                    resetTimeout: 30000, // Reset circuit after 30 seconds
                    halfOpenRequests: 2  // Test with 2 requests in half-open
                }
            },
            smoothProgress: {
                enabled: true,
                incrementDelay: 50,  // Update every 50ms for smooth animation
                maxAutoProgress: 98   // Don't auto-progress past 98%
            }
        };

        // Adaptive polling state
        this.pollingState = {
            currentInterval: this.config.polling.baseInterval,
            consecutiveFailures: 0,
            consecutiveSuccesses: 0,
            lastRequestTime: 0,
            pendingRequest: null,
            circuitState: 'closed', // 'closed', 'open', 'half-open'
            circuitOpenTime: 0,
            halfOpenAttempts: 0,
            rateLimitedUntil: 0,
            totalRequests: 0,
            totalRateLimits: 0,
            avgResponseTime: 0
        };

        // Stage definitions matching start_system.py broadcast stages
        // Organized by phase: Cleanup (0-40%), Starting (40-50%), Initialization (50-95%), Ready (95-100%)
        this.stageDefinitions = {
            // === PHASE 1: CLEANUP (0-40%) ===
            'initializing': {
                name: 'Initializing',
                icon: '⚡',
                phase: 'cleanup',
                expectedProgress: [0, 5],
                substeps: ['System check', 'Preparing environment']
            },
            'path_setup': {
                name: 'Path Configuration',
                icon: '📁',
                phase: 'cleanup',
                expectedProgress: [2, 3],
                substeps: ['Configuring Python paths', 'Module imports']
            },
            'detecting': {
                name: 'Process Detection',
                icon: '🔍',
                phase: 'cleanup',
                expectedProgress: [3, 5],
                substeps: ['Scanning PID table', 'Identifying JARVIS processes']
            },
            'scanning_ports': {
                name: 'Port Scanning',
                icon: '🔌',
                phase: 'cleanup',
                expectedProgress: [5, 7],
                substeps: ['Checking port 3000', 'Checking port 8010']
            },
            'enumerating': {
                name: 'Process Enumeration',
                icon: '📊',
                phase: 'cleanup',
                expectedProgress: [7, 8],
                substeps: ['Counting instances', 'Building process list']
            },
            'detected': {
                name: 'Detection Complete',
                icon: '✓',
                phase: 'cleanup',
                expectedProgress: [8, 10],
                substeps: ['Processes identified', 'Ready for cleanup']
            },
            'preparing_kill': {
                name: 'Preparing Shutdown',
                icon: '🛑',
                phase: 'cleanup',
                expectedProgress: [10, 12],
                substeps: ['Saving state', 'Preparing graceful shutdown']
            },
            'terminating': {
                name: 'Terminating Processes',
                icon: '⚔️',
                phase: 'cleanup',
                expectedProgress: [12, 16],
                substeps: ['Sending SIGTERM', 'Waiting for shutdown']
            },
            'verifying_kill': {
                name: 'Verifying Termination',
                icon: '🔍',
                phase: 'cleanup',
                expectedProgress: [16, 20],
                substeps: ['Checking PIDs', 'Confirming shutdown']
            },
            'killed': {
                name: 'Processes Terminated',
                icon: '✓',
                phase: 'cleanup',
                expectedProgress: [20, 23],
                substeps: ['All processes stopped', 'Resources released']
            },
            'port_cleanup': {
                name: 'Port Cleanup',
                icon: '🔌',
                phase: 'cleanup',
                expectedProgress: [23, 25],
                substeps: ['Releasing port 3000', 'Releasing port 8010']
            },
            'cleanup': {
                name: 'Resource Cleanup',
                icon: '🧹',
                phase: 'cleanup',
                expectedProgress: [25, 28],
                substeps: ['Shared memory', 'File locks', 'Temp files']
            },
            'checking_proxies': {
                name: 'Database Proxy Check',
                icon: '🔐',
                phase: 'cleanup',
                expectedProgress: [28, 30],
                substeps: ['Scanning cloud-sql-proxy', 'Checking connections']
            },
            'terminating_proxies': {
                name: 'Terminating Proxies',
                icon: '🔐',
                phase: 'cleanup',
                expectedProgress: [30, 32],
                substeps: ['Closing database tunnels']
            },
            'scanning_vms': {
                name: 'Cloud VM Scan',
                icon: '☁️',
                phase: 'cleanup',
                expectedProgress: [32, 33],
                substeps: ['Querying GCP', 'Finding orphaned VMs']
            },
            'deleting_vms': {
                name: 'Deleting VMs',
                icon: '☁️',
                phase: 'cleanup',
                expectedProgress: [33, 35],
                substeps: ['Terminating instances', 'Stopping cloud costs']
            },
            'vm_cleanup': {
                name: 'Cloud Cleanup Complete',
                icon: '☁️',
                phase: 'cleanup',
                expectedProgress: [35, 38],
                substeps: ['VMs terminated', 'Costs stopped']
            },
            'ready_to_start': {
                name: 'Environment Ready',
                icon: '✓',
                phase: 'cleanup',
                expectedProgress: [38, 40],
                substeps: ['Ports free', 'Resources available']
            },

            // === PHASE 2: STARTING BACKEND (40-50%) ===
            'starting': {
                name: 'Starting Backend',
                icon: '🚀',
                phase: 'starting',
                expectedProgress: [40, 50],
                substeps: ['Spawning FastAPI', 'Initializing uvicorn']
            },
            'backend_spawned': {
                name: 'Backend Process Spawned',
                icon: '🚀',
                phase: 'starting',
                expectedProgress: [45, 50],
                substeps: ['Process created', 'Waiting for init']
            },

            // === PHASE 3: INITIALIZATION (50-95%) ===
            'cloud_sql_proxy': {
                name: 'Cloud SQL Proxy',
                icon: '🔐',
                phase: 'initialization',
                expectedProgress: [50, 55],
                substeps: ['Starting proxy', 'Connecting to database']
            },
            'database': {
                name: 'Database Connection',
                icon: '🗄️',
                phase: 'initialization',
                expectedProgress: [55, 60],
                substeps: ['Connecting to PostgreSQL', 'Initializing pool']
            },
            'hybrid_coordinator': {
                name: 'Hybrid Cloud Intelligence',
                icon: '🌐',
                phase: 'initialization',
                expectedProgress: [60, 63],
                substeps: ['RAM monitor', 'Workload router']
            },
            'metrics_monitor': {
                name: 'Metrics Monitor',
                icon: '📊',
                phase: 'initialization',
                expectedProgress: [63, 65],
                substeps: ['Voice unlock metrics', 'DB Browser']
            },
            'cost_optimization': {
                name: 'Cost Optimization',
                icon: '💰',
                phase: 'initialization',
                expectedProgress: [65, 68],
                substeps: ['Semantic cache', 'Physics auth']
            },
            'voice_biometrics': {
                name: 'Voice Biometric Intelligence',
                icon: '🎤',
                phase: 'initialization',
                expectedProgress: [68, 75],
                substeps: ['Loading ECAPA-TDNN', 'Speaker cache', 'VBI initialization']
            },
            'cloud_ecapa': {
                name: 'Cloud ECAPA Service',
                icon: '☁️',
                phase: 'initialization',
                expectedProgress: [75, 78],
                substeps: ['Connecting to Cloud Run', 'Warming ML endpoint']
            },
            'speaker_cache': {
                name: 'Speaker Recognition Cache',
                icon: '🔐',
                phase: 'initialization',
                expectedProgress: [78, 80],
                substeps: ['Fast-path cache', 'Loading recent speakers']
            },
            'autonomous_systems': {
                name: 'Autonomous Systems',
                icon: '🤖',
                phase: 'initialization',
                expectedProgress: [80, 83],
                substeps: ['Orchestrator', 'Service mesh']
            },
            'module_prewarming': {
                name: 'Module Pre-Warming',
                icon: '🔥',
                phase: 'initialization',
                expectedProgress: [83, 85],
                substeps: ['Background imports', 'JIT compilation']
            },
            'websocket': {
                name: 'WebSocket System',
                icon: '🔌',
                phase: 'initialization',
                expectedProgress: [85, 88],
                substeps: ['Unified WS manager', 'Voice streaming']
            },
            'api_routes': {
                name: 'API Routes',
                icon: '🌐',
                phase: 'initialization',
                expectedProgress: [88, 92],
                substeps: ['Health routes', 'Voice routes', 'Command routes']
            },
            'final_checks': {
                name: 'Final System Checks',
                icon: '✅',
                phase: 'initialization',
                expectedProgress: [92, 95],
                substeps: ['Service verification', 'Health check']
            },

            // === PHASE 4: READY (95-100%) ===
            'health_checks': {
                name: 'Health Checks',
                icon: '🔍',
                phase: 'ready',
                expectedProgress: [86, 89],
                substeps: ['Verifying services', 'Running health probes']
            },
            'health_checks_complete': {
                name: 'Health Verified',
                icon: '✓',
                phase: 'ready',
                expectedProgress: [89, 92],
                substeps: ['All services responding', 'Verifying availability']
            },
            'services_verified': {
                name: 'Services Ready',
                icon: '✅',
                phase: 'ready',
                expectedProgress: [92, 94],
                substeps: ['Backend online', 'APIs ready']
            },
            'frontend_verification': {
                name: 'Frontend Check',
                icon: '🌐',
                phase: 'ready',
                expectedProgress: [94, 96],
                substeps: ['Verifying UI', 'Checking interface']
            },
            'waiting_for_frontend': {
                name: 'Frontend Starting',
                icon: '⏳',
                phase: 'ready',
                expectedProgress: [95, 97],
                substeps: ['Waiting for frontend', 'UI initializing']
            },
            'frontend_ready': {
                name: 'Frontend Ready',
                icon: '✅',
                phase: 'ready',
                expectedProgress: [97, 98],
                substeps: ['UI verified', 'Interface ready']
            },
            'system_ready': {
                name: 'System Ready',
                icon: '🚀',
                phase: 'ready',
                expectedProgress: [97, 99],
                substeps: ['Final checks', 'Almost ready']
            },
            'finalizing': {
                name: 'Finalizing',
                icon: '⏳',
                phase: 'ready',
                expectedProgress: [98, 99],
                substeps: ['Final initialization', 'Preparing launch']
            },
            'ready': {
                name: 'System Ready',
                icon: '✅',
                phase: 'ready',
                expectedProgress: [95, 98],
                substeps: ['All systems operational']
            },
            'complete': {
                name: 'JARVIS Online',
                icon: '🚀',
                phase: 'complete',
                expectedProgress: [98, 100],
                substeps: ['Ready for commands']
            },

            // === v5.0: PARTIAL COMPLETION & SLOW STARTUP STAGES ===
            // These handle cases where startup takes longer than expected
            'partial_complete': {
                name: 'Partially Ready',
                icon: '⚠️',
                phase: 'ready',
                expectedProgress: [50, 95],
                substeps: ['Some services available', 'Some features may be limited']
            },
            'startup_slow': {
                name: 'Startup Slow',
                icon: '⏳',
                phase: 'initialization',
                expectedProgress: [30, 80],
                substeps: ['Services initializing slowly', 'Please wait...']
            },
            'startup_timeout': {
                name: 'Startup Timeout',
                icon: '⏰',
                phase: 'ready',
                expectedProgress: [50, 95],
                substeps: ['Extended startup time', 'Some services may be unavailable']
            },
            'services_warming': {
                name: 'Services Warming Up',
                icon: '🔥',
                phase: 'initialization',
                expectedProgress: [70, 95],
                substeps: ['ML models loading', 'This may take a moment...']
            },

            // === ERROR STATE ===
            'failed': {
                name: 'Startup Failed',
                icon: '❌',
                phase: 'error',
                expectedProgress: [0, 0],
                substeps: ['Error occurred']
            },
            
            // === v3.0: ZERO-TOUCH AUTONOMOUS UPDATE STAGES ===
            'zero_touch_initiated': {
                name: 'Zero-Touch Update',
                icon: '🤖',
                phase: 'zero_touch',
                expectedProgress: [0, 10],
                substeps: ['Autonomous update initiated', 'Preparing update']
            },
            'zero_touch_staging': {
                name: 'Staging Update',
                icon: '📦',
                phase: 'zero_touch',
                expectedProgress: [10, 30],
                substeps: ['Downloading changes', 'Creating staging area']
            },
            'zero_touch_validating': {
                name: 'Validating Code',
                icon: '🔍',
                phase: 'zero_touch',
                expectedProgress: [30, 50],
                substeps: ['Syntax check', 'Import validation', 'Dependency check']
            },
            'zero_touch_validation_passed': {
                name: 'Validation Passed',
                icon: '✓',
                phase: 'zero_touch',
                expectedProgress: [50, 55],
                substeps: ['All checks passed', 'Ready to apply']
            },
            'zero_touch_applying': {
                name: 'Applying Update',
                icon: '⚡',
                phase: 'zero_touch',
                expectedProgress: [55, 70],
                substeps: ['Merging changes', 'Updating dependencies']
            },
            'zero_touch_restarting': {
                name: 'Restarting JARVIS',
                icon: '🔄',
                phase: 'zero_touch',
                expectedProgress: [70, 85],
                substeps: ['Shutting down', 'Starting new version']
            },
            'dms_monitoring': {
                name: 'Stability Monitor',
                icon: '🎯',
                phase: 'zero_touch',
                expectedProgress: [85, 95],
                substeps: ['Dead Man\'s Switch active', 'Verifying stability']
            },
            'dms_passed': {
                name: 'Update Stable',
                icon: '✅',
                phase: 'complete',
                expectedProgress: [95, 100],
                substeps: ['Version committed as stable']
            },
            'dms_rollback': {
                name: 'Rolling Back',
                icon: '⏪',
                phase: 'zero_touch',
                expectedProgress: [85, 90],
                substeps: ['Stability check failed', 'Reverting to previous version']
            },
            // v5.0: Hot Reload (Dev Mode) stages
            'hot_reload_detected': {
                name: 'Changes Detected',
                icon: '👀',
                phase: 'hot_reload',
                expectedProgress: [0, 20],
                substeps: ['Scanning changes', 'Identifying file types']
            },
            'hot_reload_restarting': {
                name: 'Hot Reloading',
                icon: '🔥',
                phase: 'hot_reload',
                expectedProgress: [20, 80],
                substeps: ['Clearing cache', 'Restarting backend']
            },
            'hot_reload_rebuilding': {
                name: 'Rebuilding Frontend',
                icon: '🔨',
                phase: 'hot_reload',
                expectedProgress: [30, 70],
                substeps: ['Compiling assets', 'Building bundle']
            },
            'hot_reload_complete': {
                name: 'Reload Complete',
                icon: '✅',
                phase: 'hot_reload',
                expectedProgress: [100, 100],
                substeps: ['Changes applied', 'Ready']
            }
        };

        this.state = {
            ws: null,
            connected: false,
            wsHealthy: false,           // Track WebSocket health for polling decisions
            progress: 0,
            targetProgress: 0,
            stage: 'initializing',
            substage: null,
            message: 'Initializing JARVIS...',
            reconnectAttempts: 0,
            pollingInterval: null,
            pollingTimeout: null,       // For adaptive polling setTimeout
            smoothProgressInterval: null,
            startTime: Date.now(),
            lastUpdate: Date.now(),
            lastWsMessage: 0,           // Track last WS message for health
            redirecting: false,         // Prevent polling during redirect
            // Detailed tracking
            completedStages: [],
            currentSubstep: 0,
            memoryMode: null,
            memoryInfo: null,
            voiceBiometricsReady: false,
            speakerCacheReady: false,
            phase: 'cleanup',
            // Live operations log
            operationsLog: [],
            maxLogEntries: 50
        };

        this.elements = this.cacheElements();
        this.init();
    }

    cacheElements() {
        return {
            statusText: document.getElementById('status-text'),
            subtitle: document.getElementById('subtitle'),
            progressBar: document.getElementById('progress-bar'),
            progressPercentage: document.getElementById('progress-percentage'),
            statusMessage: document.getElementById('status-message'),
            errorContainer: document.getElementById('error-container'),
            errorMessage: document.getElementById('error-message'),
            reactor: document.querySelector('.arc-reactor'),
            // Detailed status elements
            detailedStatus: document.getElementById('detailed-status'),
            stageIcon: document.getElementById('stage-icon'),
            stageName: document.getElementById('stage-name'),
            substepList: document.getElementById('substep-list'),
            memoryStatus: document.getElementById('memory-status'),
            modeIndicator: document.getElementById('mode-indicator'),
            phaseIndicator: document.getElementById('phase-indicator'),
            // Live operations log elements
            operationsLog: document.getElementById('operations-log'),
            logEntries: document.getElementById('log-entries'),
            logCount: document.getElementById('log-count'),
            detailsPanel: document.getElementById('details-panel')
        };
    }

    /**
     * Add an entry to the live operations log.
     * 
     * @param {string} source - Source of the operation (supervisor, backend, system)
     * @param {string} message - Log message
     * @param {string} type - Type of log (info, success, error, warning)
     */
    addLogEntry(source, message, type = 'info') {
        if (!this.elements.logEntries) return;
        
        const now = new Date();
        const timeStr = now.toLocaleTimeString('en-US', { 
            hour12: false, 
            hour: '2-digit', 
            minute: '2-digit',
            second: '2-digit'
        });
        
        // Map source to CSS class
        const sourceClass = {
            'supervisor': 'supervisor',
            'backend': 'backend',
            'system': 'system',
            'start_system': 'backend',
            'jarvis': 'backend',
            'error': 'error',
            'success': 'success'
        }[source.toLowerCase()] || 'system';
        
        // Create log entry
        const entry = document.createElement('div');
        entry.className = `log-entry ${sourceClass}`;
        entry.innerHTML = `
            <span class="log-time">${timeStr}</span>
            <span class="log-source">${source.substring(0, 12)}</span>
            <span class="log-message">${this._escapeHtml(message)}</span>
        `;
        
        // Add to DOM
        this.elements.logEntries.appendChild(entry);
        
        // Keep only last N entries
        while (this.elements.logEntries.children.length > this.state.maxLogEntries) {
            this.elements.logEntries.removeChild(this.elements.logEntries.firstChild);
        }
        
        // Auto-scroll to bottom
        this.elements.logEntries.scrollTop = this.elements.logEntries.scrollHeight;
        
        // Update count
        if (this.elements.logCount) {
            const count = this.elements.logEntries.children.length;
            this.elements.logCount.textContent = count;
        }
        
        // Store in state
        this.state.operationsLog.push({
            time: timeStr,
            source,
            message,
            type
        });
        
        // Trim state array too
        if (this.state.operationsLog.length > this.state.maxLogEntries) {
            this.state.operationsLog.shift();
        }
        
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Create the detailed status panel dynamically
     * v4.0: DISABLED - Using HTML-defined details-panel instead
     * The HTML version has minimize/hide functionality built-in
     */
    createDetailedStatusPanel() {
        // v4.0: Use existing HTML panel instead of creating a duplicate
        // The HTML details-panel has minimize/hide functionality
        if (document.getElementById('details-panel')) {
            console.log('[LoadingManager] Using existing details-panel from HTML');
            return;
        }
        
        // Fallback: Only create if HTML panel doesn't exist (shouldn't happen)
        if (document.getElementById('detailed-status')) return;

        const panel = document.createElement('div');
        panel.id = 'detailed-status';
        panel.className = 'detailed-status-panel';
        panel.innerHTML = `
            <div class="phase-indicator" id="phase-indicator">
                <span class="phase-label">Phase:</span>
                <span class="phase-value">Cleanup</span>
            </div>
            <div class="stage-header">
                <span id="stage-icon" class="stage-icon">⚡</span>
                <span id="stage-name" class="stage-name">System Initialization</span>
            </div>
            <div id="substep-list" class="substep-list"></div>
            <div class="system-info">
                <div id="memory-status" class="memory-status">
                    <span class="info-label">Memory:</span>
                    <span class="info-value">Analyzing...</span>
                </div>
                <div id="mode-indicator" class="mode-indicator">
                    <span class="info-label">Mode:</span>
                    <span class="info-value">Detecting...</span>
                </div>
            </div>
        `;

        // Create responsive styles using CSS custom properties and viewport units
        const style = document.createElement('style');
        style.id = 'detailed-status-styles';
        style.textContent = `
            /* Detailed Status Panel - Responsive & Flexbox-friendly */
            .detailed-status-panel {
                /* Use relative positioning within flex container */
                position: relative;
                width: 100%;
                max-width: min(520px, 90vw);
                margin: 0 auto;
                
                /* Responsive padding and sizing */
                padding: clamp(12px, 2vw, 16px) clamp(16px, 3vw, 24px);
                
                /* Visual styling */
                background: rgba(0, 20, 0, 0.85);
                border: 1px solid rgba(0, 255, 65, 0.3);
                border-radius: clamp(8px, 1.5vw, 12px);
                
                /* Typography */
                font-family: 'SF Mono', Monaco, 'Courier New', monospace;
                font-size: clamp(10px, 1.5vw, 12px);
                color: #00ff41;
                
                /* Effects */
                box-shadow: 0 4px 20px rgba(0, 255, 65, 0.2);
                backdrop-filter: blur(10px);
                -webkit-backdrop-filter: blur(10px);
                
                /* Flex item behavior */
                flex-shrink: 0;
                order: 7; /* Position after status-message (order: 6) in flex layout */
                
                /* Animation */
                opacity: 0;
                transform: translateY(10px);
                animation: panelFadeIn 0.4s ease-out forwards;
                animation-delay: 0.3s;
            }
            
            @keyframes panelFadeIn {
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            
            .detailed-status-panel .phase-indicator {
                display: flex;
                align-items: center;
                gap: clamp(6px, 1vw, 8px);
                margin-bottom: clamp(8px, 1.5vh, 12px);
                padding-bottom: clamp(6px, 1vh, 8px);
                border-bottom: 1px solid rgba(0, 255, 65, 0.2);
                font-size: clamp(8px, 1.2vw, 10px);
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            
            .detailed-status-panel .phase-label {
                opacity: 0.6;
            }
            
            .detailed-status-panel .phase-value {
                font-weight: 600;
                padding: 2px clamp(6px, 1vw, 8px);
                background: rgba(0, 255, 65, 0.1);
                border-radius: 4px;
                transition: color 0.3s ease, background 0.3s ease;
            }
            
            .detailed-status-panel .phase-value.cleanup { color: #ffaa00; background: rgba(255, 170, 0, 0.1); }
            .detailed-status-panel .phase-value.starting { color: #00aaff; background: rgba(0, 170, 255, 0.1); }
            .detailed-status-panel .phase-value.initialization { color: #00ff88; background: rgba(0, 255, 136, 0.1); }
            .detailed-status-panel .phase-value.ready { color: #00ffff; background: rgba(0, 255, 255, 0.1); }
            .detailed-status-panel .phase-value.complete { color: #00ff41; background: rgba(0, 255, 65, 0.15); }
            .detailed-status-panel .phase-value.error { color: #ff4444; background: rgba(255, 68, 68, 0.1); }
            .detailed-status-panel .phase-value.zero_touch { color: #00aaff; background: rgba(0, 170, 255, 0.15); animation: ztPulse 1.5s ease-in-out infinite; }
            
            @keyframes ztPulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.7; }
            }
            
            .detailed-status-panel .stage-header {
                display: flex;
                align-items: center;
                gap: clamp(8px, 1.5vw, 10px);
                margin-bottom: clamp(8px, 1.5vh, 12px);
                font-size: clamp(12px, 2vw, 14px);
                font-weight: 600;
            }
            
            .detailed-status-panel .stage-icon {
                font-size: clamp(14px, 2.5vw, 18px);
            }
            
            .detailed-status-panel .substep-list {
                display: flex;
                flex-direction: column;
                gap: clamp(4px, 0.8vh, 6px);
                margin-bottom: clamp(8px, 1.5vh, 12px);
                padding-left: clamp(20px, 4vw, 28px);
            }
            
            .detailed-status-panel .substep {
                display: flex;
                align-items: center;
                gap: clamp(6px, 1vw, 8px);
                font-size: clamp(9px, 1.4vw, 11px);
                opacity: 0.6;
                transition: opacity 0.3s ease, color 0.3s ease;
            }
            
            .detailed-status-panel .substep.active {
                opacity: 1;
                color: #00ff88;
            }
            
            .detailed-status-panel .substep.completed {
                opacity: 0.8;
                color: #00aa44;
            }
            
            .detailed-status-panel .substep-indicator {
                width: clamp(4px, 0.8vw, 6px);
                height: clamp(4px, 0.8vw, 6px);
                border-radius: 50%;
                background: rgba(0, 255, 65, 0.3);
                transition: all 0.3s ease;
                flex-shrink: 0;
            }
            
            .detailed-status-panel .substep.active .substep-indicator {
                background: #00ff88;
                box-shadow: 0 0 8px #00ff88;
                animation: indicatorPulse 1s ease-in-out infinite;
            }
            
            .detailed-status-panel .substep.completed .substep-indicator {
                background: #00aa44;
            }
            
            @keyframes indicatorPulse {
                0%, 100% { transform: scale(1); }
                50% { transform: scale(1.3); }
            }
            
            .detailed-status-panel .system-info {
                display: flex;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: clamp(8px, 1.5vw, 12px);
                padding-top: clamp(8px, 1.5vh, 12px);
                border-top: 1px solid rgba(0, 255, 65, 0.2);
                font-size: clamp(8px, 1.3vw, 10px);
            }
            
            .detailed-status-panel .info-label {
                opacity: 0.6;
                margin-right: clamp(4px, 0.8vw, 6px);
            }
            
            .detailed-status-panel .info-value {
                font-weight: 500;
                transition: color 0.3s ease;
            }
            
            .detailed-status-panel .memory-status .info-value.low { color: #ff4444; }
            .detailed-status-panel .memory-status .info-value.medium { color: #ffaa00; }
            .detailed-status-panel .memory-status .info-value.good { color: #00ff88; }
            
            .detailed-status-panel .mode-indicator .info-value.minimal { color: #ffaa00; }
            .detailed-status-panel .mode-indicator .info-value.standard { color: #00ff88; }
            .detailed-status-panel .mode-indicator .info-value.full { color: #00ffff; }
            
            /* Responsive adjustments for very small screens */
            @media (max-width: 480px) {
                .detailed-status-panel {
                    padding: clamp(10px, 2.5vw, 14px);
                }
                
                .detailed-status-panel .system-info {
                    flex-direction: column;
                    gap: 6px;
                }
            }
            
            /* Landscape mobile optimization */
            @media (max-height: 500px) and (orientation: landscape) {
                .detailed-status-panel {
                    padding: 8px 12px;
                    max-width: min(400px, 60vw);
                }
                
                .detailed-status-panel .substep-list {
                    max-height: 60px;
                    overflow-y: auto;
                }
            }
        `;
        
        // Only add styles once
        if (!document.getElementById('detailed-status-styles')) {
            document.head.appendChild(style);
        }

        // Insert panel AFTER status-message (which is after progress-container)
        // This places it below the progress bar in the flex layout
        const statusMessage = document.getElementById('status-message');
        const stagesContainer = document.getElementById('stages-container');
        
        if (statusMessage && statusMessage.parentNode) {
            // Insert after status message
            statusMessage.parentNode.insertBefore(panel, statusMessage.nextSibling);
        } else if (stagesContainer && stagesContainer.parentNode) {
            // Fallback: insert before stages container
            stagesContainer.parentNode.insertBefore(panel, stagesContainer);
        } else {
            // Last resort: append to loading container
            document.querySelector('.loading-container')?.appendChild(panel);
        }

        // Cache element references
        this.elements.detailedStatus = panel;
        this.elements.stageIcon = document.getElementById('stage-icon');
        this.elements.stageName = document.getElementById('stage-name');
        this.elements.substepList = document.getElementById('substep-list');
        this.elements.memoryStatus = document.getElementById('memory-status');
        this.elements.modeIndicator = document.getElementById('mode-indicator');
        this.elements.phaseIndicator = document.getElementById('phase-indicator');
    }

    async init() {
        console.log('[JARVIS] Loading Manager v4.1 starting...');
        console.log(`[Config] Loading server: ${this.config.hostname}:${this.config.loadingServerPort}`);
        console.log('[Mode] DISPLAY - trusts start_system.py as authority');

        // Quick check: Skip loading if system already fully ready
        // This handles page refresh when JARVIS is already running
        const fullyReady = await this.checkFullSystemReady();
        if (fullyReady) {
            console.log('[JARVIS] ✅ Full system already ready - skipping loading screen');
            this.quickRedirectToApp();
            return;
        }

        this.createParticles();
        this.createDetailedStatusPanel();
        this.startSmoothProgress();
        
        // Make details panel visible immediately and add initial log
        if (this.elements.detailsPanel) {
            this.elements.detailsPanel.classList.add('visible');
        }
        this.addLogEntry('System', 'Loading manager initialized', 'info');
        this.addLogEntry('System', 'Connecting to supervisor...', 'info');

        // Connect to loading server (port 3001) - this is our PRIMARY source
        // start_system.py → loading_server.py → here
        await this.connectWebSocket();
        this.startPolling();
        this.startHealthMonitoring();
        
        // NOTE: No independent health polling - we trust the loading server
        // The loading server has a watchdog for edge cases
    }

    async checkBackendHealth() {
        /**
         * Check if backend is already running and healthy.
         * This allows skipping the loading screen if JARVIS is already up.
         */
        try {
            const url = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.backendPort}/health`;
            const response = await fetch(url, {
                method: 'GET',
                cache: 'no-cache',
                signal: AbortSignal.timeout(3000)
            });

            if (response.ok) {
                const data = await response.json();
                // Backend is healthy if status is healthy/ok and ECAPA is ready
                const isHealthy = data.status === 'healthy' || data.status === 'ok';
                const ecapaReady = data.ecapa_ready === true;
                
                console.log(`[Health] Backend: ${data.status}, ECAPA: ${data.ecapa_ready}`);
                
                // Consider ready if healthy (ECAPA can initialize later)
                return isHealthy;
            }
        } catch (error) {
            console.log('[Health] Backend not yet available:', error.message);
        }
        return false;
    }

    async checkFrontendReady() {
        /**
         * Check if the frontend (main app on port 3000) is ready.
         * This prevents redirecting to a non-existent page.
         */
        try {
            const url = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
            const response = await fetch(url, {
                method: 'HEAD',
                cache: 'no-cache',
                signal: AbortSignal.timeout(2000)
            });
            
            const isReady = response.ok || response.status === 304;
            console.log(`[Health] Frontend (${this.config.mainAppPort}): ${isReady ? 'ready' : 'not ready'}`);
            return isReady;
        } catch (error) {
            console.log('[Health] Frontend not yet available:', error.message);
        }
        return false;
    }

    async checkFullSystemReady() {
        /**
         * Check if BOTH backend AND frontend are ready.
         * Only returns true if both services are available.
         * This prevents the loading screen from skipping when only backend is up.
         */
        const [backendReady, frontendReady] = await Promise.all([
            this.checkBackendHealth(),
            this.checkFrontendReady()
        ]);
        
        console.log(`[Health] Full system check - Backend: ${backendReady}, Frontend: ${frontendReady}`);
        return backendReady && frontendReady;
    }

    async quickBackendCheck() {
        /**
         * Quick backend health check for recovery scenarios.
         * Returns true if backend is responsive.
         * Uses a short timeout to avoid blocking.
         */
        const backendPort = this.config.backendPort || 8010;
        const backendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${backendPort}`;
        try {
            const response = await fetch(`${backendUrl}/health`, {
                method: 'GET',
                cache: 'no-cache',
                signal: AbortSignal.timeout(2000)
            });
            const isReady = response.ok;
            console.log(`[Recovery] Quick backend check: ${isReady ? 'UP' : 'DOWN'}`);
            return isReady;
        } catch (e) {
            console.debug('[Recovery] Quick backend check failed:', e.message);
            return false;
        }
    }

    quickRedirectToApp() {
        /**
         * Quick redirect to main app when backend is already ready.
         * Shows brief success animation before redirect.
         */
        // Stop polling
        this.state.redirecting = true;
        
        // Update UI to show ready state
        if (this.elements.statusMessage) {
            this.elements.statusMessage.textContent = 'JARVIS is already online!';
        }
        if (this.elements.progressBar) {
            this.elements.progressBar.style.width = '100%';
        }
        if (this.elements.progressPercentage) {
            this.elements.progressPercentage.textContent = '100%';
        }
        if (this.elements.subtitle) {
            this.elements.subtitle.textContent = 'SYSTEM READY';
        }

        // Brief delay for visual feedback, then redirect
        setTimeout(() => {
            const redirectUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
            console.log(`[JARVIS] Redirecting to ${redirectUrl}`);
            window.location.href = redirectUrl;
        }, 1000);
    }

    startBackendHealthPolling() {
        /**
         * v4.0: Intelligent backend health polling with stuck detection
         * 
         * Poll backend health directly as fallback when loading server isn't available.
         * Also detects when we're STUCK at 95%+ and forces completion.
         * 
         * Features:
         * - Multiple consecutive healthy checks before triggering
         * - Frontend verification before completing
         * - STUCK DETECTION: If at 95%+ for > 30s, force complete
         */
        let consecutiveHealthyChecks = 0;
        const requiredConsecutiveChecks = 3;
        let stuckAt95Timer = null;
        const STUCK_TIMEOUT_MS = 30000; // 30 seconds stuck = force complete
        
        const pollInterval = setInterval(async () => {
            try {
                // ═══════════════════════════════════════════════════════════════════════════
                // v4.0 STUCK DETECTION: If at 95%+ for too long, force completion
                // ═══════════════════════════════════════════════════════════════════════════
                if (this.state.progress >= 95 && this.state.progress < 100 && !this.state.redirecting) {
                    if (!stuckAt95Timer) {
                        stuckAt95Timer = Date.now();
                        console.log('[Stuck Detection] Progress at 95%+, starting timer...');
                    } else {
                        const stuckDuration = Date.now() - stuckAt95Timer;
                        console.log(`[Stuck Detection] At ${Math.round(this.state.progress)}% for ${Math.round(stuckDuration/1000)}s`);
                        
                        if (stuckDuration >= STUCK_TIMEOUT_MS) {
                            // We're stuck - check if services are actually ready
                            const backendHealthy = await this.checkBackendHealth();
                            const frontendReady = await this.checkFrontendReady();
                            
                            if (backendHealthy && frontendReady) {
                                console.log('[JARVIS] ✅ Stuck detection: Services ready, forcing completion');
                                clearInterval(pollInterval);
                                
                                this.handleProgressUpdate({
                                    stage: 'complete',
                                    message: 'JARVIS is online!',
                                    progress: 100,
                                    metadata: {
                                        success: true,
                                        forced_completion: true,
                                        redirect_url: `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`
                                    }
                                });
                                return;
                            } else if (backendHealthy) {
                                console.log('[Stuck Detection] Backend ready, waiting for frontend...');
                            }
                        }
                    }
                } else {
                    // Reset timer if progress changes
                    stuckAt95Timer = null;
                }

                // ═══════════════════════════════════════════════════════════════════════════
                // FALLBACK: Standard health polling
                // ═══════════════════════════════════════════════════════════════════════════
                const timeSinceLoadingUpdate = Date.now() - this.state.lastUpdate;
                if (timeSinceLoadingUpdate < 10000 && this.state.progress < 95) {
                    // Loading server is active and we're not stuck, let it handle completion
                    consecutiveHealthyChecks = 0;
                    return;
                }
                
                const backendHealthy = await this.checkBackendHealth();
                
                if (backendHealthy) {
                    consecutiveHealthyChecks++;
                    console.log(`[Health Polling] Backend healthy (${consecutiveHealthyChecks}/${requiredConsecutiveChecks})`);
                    
                    if (consecutiveHealthyChecks >= requiredConsecutiveChecks) {
                        const frontendReady = await this.checkFrontendReady();
                        
                        if (!frontendReady) {
                            console.log('[Health Polling] Backend ready but frontend not yet available, waiting...');
                            return;
                        }
                        
                        console.log('[JARVIS] ✅ Full system ready via fallback polling');
                        clearInterval(pollInterval);
                        
                        this.handleProgressUpdate({
                            stage: 'complete',
                            message: 'JARVIS is online - All systems operational',
                            progress: 100,
                            metadata: {
                                success: true,
                                redirect_url: `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`
                            }
                        });
                    }
                } else {
                    consecutiveHealthyChecks = 0;
                }
            } catch (error) {
                consecutiveHealthyChecks = 0;
            }
        }, 3000);

        this.backendHealthInterval = pollInterval;
    }

    createParticles() {
        const container = document.getElementById('particles');
        if (!container) return;

        const particleCount = 50;
        for (let i = 0; i < particleCount; i++) {
            const particle = document.createElement('div');
            particle.className = 'particle';

            const size = Math.random() * 3 + 1;
            particle.style.width = `${size}px`;
            particle.style.height = `${size}px`;
            particle.style.left = `${Math.random() * 100}%`;

            const duration = Math.random() * 10 + 10;
            const delay = Math.random() * 5;
            particle.style.animationDuration = `${duration}s`;
            particle.style.animationDelay = `${delay}s`;

            container.appendChild(particle);
        }
    }

    async connectWebSocket() {
        if (this.state.reconnectAttempts >= this.config.reconnect.maxAttempts) {
            console.warn('[WebSocket] Max reconnection attempts reached');
            return;
        }

        try {
            // Connect to loading server on port 3001
            const wsUrl = `${this.config.wsProtocol}//${this.config.hostname}:${this.config.loadingServerPort}/ws/startup-progress`;
            console.log(`[WebSocket] Connecting to ${wsUrl}...`);

            this.state.ws = new WebSocket(wsUrl);

            this.state.ws.onopen = () => {
                console.log('[WebSocket] ✓ Connected to loading server');
                this.state.connected = true;
                this.state.wsHealthy = true;
                this.state.reconnectAttempts = 0;
                this.state.lastWsMessage = Date.now();
                this.updateStatusText('Connected', 'connected');
                
                // WebSocket is primary - reduce polling aggression
                console.log('[WebSocket] 🔗 Primary connection established - polling will back off');
            };

            this.state.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    this.state.lastWsMessage = Date.now();
                    this.state.wsHealthy = true;
                    
                    if (data.type !== 'pong') {
                        this.handleProgressUpdate(data);
                    }
                } catch (error) {
                    console.error('[WebSocket] Parse error:', error);
                }
            };

            this.state.ws.onerror = (error) => {
                console.error('[WebSocket] Error:', error);
                this.state.wsHealthy = false;
            };

            this.state.ws.onclose = () => {
                console.log('[WebSocket] Disconnected');
                this.state.connected = false;
                this.state.wsHealthy = false;
                this.scheduleReconnect();
            };

        } catch (error) {
            console.error('[WebSocket] Connection failed:', error);
            this.scheduleReconnect();
        }
    }

    scheduleReconnect() {
        if (!this.config.reconnect.enabled) return;
        if (this.state.reconnectAttempts >= this.config.reconnect.maxAttempts) return;

        this.state.reconnectAttempts++;
        const delay = Math.min(
            this.config.reconnect.initialDelay * Math.pow(
                this.config.reconnect.backoffMultiplier,
                this.state.reconnectAttempts - 1
            ),
            this.config.reconnect.maxDelay
        );

        console.log(`[WebSocket] Reconnecting in ${delay}ms (attempt ${this.state.reconnectAttempts})...`);
        setTimeout(() => this.connectWebSocket(), delay);
    }

    /**
     * Adaptive HTTP Polling with Exponential Backoff & Circuit Breaker
     * 
     * Features:
     * - Exponential backoff on rate limits (429)
     * - Circuit breaker to prevent hammering failed servers
     * - Jitter to prevent thundering herd
     * - Dynamic interval adjustment based on server health
     * - Request deduplication (no parallel requests)
     * - Graceful degradation when WebSocket is primary
     */
    startPolling() {
        if (!this.config.polling.enabled) return;

        console.log('[Polling] 🚀 Starting adaptive HTTP polling (WebSocket primary, HTTP fallback)');
        console.log(`[Polling] Base interval: ${this.config.polling.baseInterval}ms, Max: ${this.config.polling.maxInterval}ms`);
        
        // Use recursive setTimeout instead of setInterval for adaptive timing
        this.scheduleNextPoll();
    }

    scheduleNextPoll() {
        // Don't poll if we're done or WebSocket is healthy
        if (this.state.progress >= 100 || this.state.redirecting) {
            console.log('[Polling] ✅ Stopping - loading complete');
            return;
        }

        // Check circuit breaker state
        const circuitAction = this.checkCircuitBreaker();
        if (circuitAction === 'block') {
            // Circuit is open - schedule retry after reset timeout
            const retryIn = this.config.polling.circuitBreaker.resetTimeout;
            console.log(`[Polling] 🔴 Circuit OPEN - retrying in ${retryIn}ms`);
            this.state.pollingTimeout = setTimeout(() => this.scheduleNextPoll(), retryIn);
            return;
        }

        // Check if we're rate limited
        const now = Date.now();
        if (now < this.pollingState.rateLimitedUntil) {
            const waitTime = this.pollingState.rateLimitedUntil - now;
            console.log(`[Polling] ⏳ Rate limited - waiting ${waitTime}ms`);
            this.state.pollingTimeout = setTimeout(() => this.scheduleNextPoll(), waitTime);
            return;
        }

        // Calculate next interval with jitter
        const interval = this.calculatePollingInterval();
        
        this.state.pollingTimeout = setTimeout(() => this.executePollingRequest(), interval);
    }

    calculatePollingInterval() {
        const config = this.config.polling;
        let interval = this.pollingState.currentInterval;

        // Add jitter to prevent thundering herd (±20%)
        const jitter = interval * config.jitterFactor * (Math.random() - 0.5) * 2;
        interval = Math.max(config.minInterval, Math.min(config.maxInterval, interval + jitter));

        // If WebSocket is connected and healthy, poll much less frequently
        if (this.state.connected && this.state.wsHealthy) {
            interval = Math.max(interval, 5000); // At least 5 seconds when WS is healthy
        }

        return Math.round(interval);
    }

    async executePollingRequest() {
        // Deduplicate - skip if request already in flight
        if (this.pollingState.pendingRequest) {
            console.debug('[Polling] Skipping - request already pending');
            this.scheduleNextPoll();
            return;
        }

        const startTime = Date.now();
        this.pollingState.totalRequests++;
        this.pollingState.lastRequestTime = startTime;

        try {
                const url = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}/api/startup-progress`;
            
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), this.config.polling.timeout);
            
            this.pollingState.pendingRequest = fetch(url, {
                    method: 'GET',
                    cache: 'no-cache',
                headers: {
                    'Accept': 'application/json',
                    'X-Client-Version': '5.0',
                    'X-Request-ID': `poll-${this.pollingState.totalRequests}`
                },
                signal: controller.signal
            });

            const response = await this.pollingState.pendingRequest;
            clearTimeout(timeoutId);
            
            const responseTime = Date.now() - startTime;
            this.updateResponseTimeAverage(responseTime);

                if (response.ok) {
                    const data = await response.json();
                this.handlePollingSuccess(data, responseTime);
            } else if (response.status === 429) {
                this.handleRateLimit(response);
            } else {
                this.handlePollingError(new Error(`HTTP ${response.status}`));
                }
            } catch (error) {
            if (error.name === 'AbortError') {
                console.warn('[Polling] ⏱️ Request timed out');
            }
            this.handlePollingError(error);
        } finally {
            this.pollingState.pendingRequest = null;
            this.scheduleNextPoll();
        }
    }

    handlePollingSuccess(data, responseTime) {
        this.pollingState.consecutiveSuccesses++;
        this.pollingState.consecutiveFailures = 0;

        // Gradually reduce interval on success (recover from backoff)
        if (this.pollingState.currentInterval > this.config.polling.baseInterval) {
            this.pollingState.currentInterval = Math.max(
                this.config.polling.baseInterval,
                this.pollingState.currentInterval * this.config.polling.recoveryMultiplier
            );
            console.debug(`[Polling] 📉 Interval reduced to ${this.pollingState.currentInterval}ms`);
        }

        // Close circuit breaker if in half-open state
        if (this.pollingState.circuitState === 'half-open') {
            this.pollingState.halfOpenAttempts++;
            if (this.pollingState.halfOpenAttempts >= this.config.polling.circuitBreaker.halfOpenRequests) {
                this.pollingState.circuitState = 'closed';
                console.log('[Polling] 🟢 Circuit CLOSED - server recovered');
            }
        }

        // Process the data
        this.handleProgressUpdate(data);

        // Log stats periodically
        if (this.pollingState.totalRequests % 10 === 0) {
            console.log(`[Polling] 📊 Stats: ${this.pollingState.totalRequests} requests, ${this.pollingState.totalRateLimits} rate limits, avg ${Math.round(this.pollingState.avgResponseTime)}ms`);
        }
    }

    handleRateLimit(response) {
        this.pollingState.totalRateLimits++;
        this.pollingState.consecutiveFailures++;
        this.pollingState.consecutiveSuccesses = 0;

        // Check for Retry-After header
        let retryAfter = 5000; // Default 5 seconds
        const retryHeader = response.headers.get('Retry-After');
        if (retryHeader) {
            // Retry-After can be seconds or HTTP date
            const parsed = parseInt(retryHeader, 10);
            if (!isNaN(parsed)) {
                retryAfter = parsed * 1000; // Convert to ms
            } else {
                const date = new Date(retryHeader);
                if (!isNaN(date.getTime())) {
                    retryAfter = Math.max(1000, date.getTime() - Date.now());
                }
            }
        }

        // Exponential backoff on rate limit
        this.pollingState.currentInterval = Math.min(
            this.config.polling.maxInterval,
            this.pollingState.currentInterval * this.config.polling.backoffMultiplier
        );

        // Set rate limit cooldown
        this.pollingState.rateLimitedUntil = Date.now() + retryAfter;

        console.warn(`[Polling] ⚠️ Rate limited (429) - backing off to ${this.pollingState.currentInterval}ms, retry after ${retryAfter}ms`);
    }

    handlePollingError(error) {
        this.pollingState.consecutiveFailures++;
        this.pollingState.consecutiveSuccesses = 0;

        // Exponential backoff on error
        this.pollingState.currentInterval = Math.min(
            this.config.polling.maxInterval,
            this.pollingState.currentInterval * this.config.polling.backoffMultiplier
        );

        // Check circuit breaker threshold
        if (this.pollingState.consecutiveFailures >= this.config.polling.circuitBreaker.threshold) {
            if (this.pollingState.circuitState === 'closed') {
                this.pollingState.circuitState = 'open';
                this.pollingState.circuitOpenTime = Date.now();
                console.warn(`[Polling] 🔴 Circuit OPEN - ${this.pollingState.consecutiveFailures} consecutive failures`);
            }
        }

        console.debug(`[Polling] Error: ${error.message}, interval now ${this.pollingState.currentInterval}ms`);
    }

    checkCircuitBreaker() {
        const config = this.config.polling.circuitBreaker;
        const state = this.pollingState;

        if (state.circuitState === 'closed') {
            return 'allow';
        }

        if (state.circuitState === 'open') {
            // Check if reset timeout has elapsed
            if (Date.now() - state.circuitOpenTime >= config.resetTimeout) {
                state.circuitState = 'half-open';
                state.halfOpenAttempts = 0;
                console.log('[Polling] 🟡 Circuit HALF-OPEN - testing server');
                return 'allow';
            }
            return 'block';
        }

        // half-open state - allow limited requests
        return 'allow';
    }

    updateResponseTimeAverage(responseTime) {
        // Exponential moving average
        const alpha = 0.2;
        if (this.pollingState.avgResponseTime === 0) {
            this.pollingState.avgResponseTime = responseTime;
        } else {
            this.pollingState.avgResponseTime = alpha * responseTime + (1 - alpha) * this.pollingState.avgResponseTime;
        }
    }

    handleProgressUpdate(data) {
        this.state.lastUpdate = Date.now();

        const stage = data.stage;
        const message = data.message;
        const progress = data.progress;
        const metadata = data.metadata || {};

        // Skip keepalive messages
        if (stage === 'keepalive' || data.type === 'pong') return;

        // CRITICAL: Progress must be monotonically increasing (never decrease)
        // Backend stages complete in different orders, but UI should only show forward progress
        const effectiveProgress = typeof progress === 'number' ? progress : 0;
        const currentMax = Math.max(this.state.targetProgress || 0, this.state.progress || 0);
        
        // Only log and update if progress is increasing (or same stage update)
        if (effectiveProgress >= currentMax || stage === 'complete') {
            console.log(`[Progress] ${effectiveProgress}% - ${stage}: ${message}`);
        } else {
            // Log skipped updates at debug level
            console.debug(`[Progress] Skipped backward: ${effectiveProgress}% (current: ${currentMax}%) - ${stage}: ${message}`);
        }

        // Update target progress ONLY if it increases (monotonic progress)
        if (typeof progress === 'number' && progress >= 0 && progress <= 100) {
            // Allow progress to increase or stay the same, never decrease
            // Exception: 'complete' stage always sets to 100%
            if (stage === 'complete') {
                this.state.targetProgress = 100;
            } else if (progress > this.state.targetProgress) {
                this.state.targetProgress = progress;
            }
            // If progress is less than current, ignore it (out-of-order message)
        }

        // Update stage
        if (stage && stage !== this.state.stage) {
            if (this.state.stage) {
                this.state.completedStages.push(this.state.stage);
            }
            this.state.stage = stage;
            this.state.currentSubstep = 0;

            // Update phase based on stage
            const stageDef = this.stageDefinitions[stage];
            if (stageDef) {
                this.state.phase = stageDef.phase;
            }
        }
        if (message) this.state.message = message;

        // Update status text based on stage/progress
        if (stage === 'ready' || stage === 'complete') {
            this.updateStatusText('Backend ready', 'ready');
        } else if (stage === 'failed') {
            this.updateStatusText('Startup failed', 'error');
        } else if (stage === 'starting' || effectiveProgress < 30) {
            this.updateStatusText('Starting backend...', 'starting');
        } else if (effectiveProgress >= 30 && effectiveProgress < 70) {
            this.updateStatusText('Loading components...', 'loading');
        } else if (effectiveProgress >= 70 && effectiveProgress < 95) {
            this.updateStatusText('Initializing services...', 'initializing');
        } else if (effectiveProgress >= 95) {
            this.updateStatusText('Almost ready...', 'finalizing');
        }

        // Extract metadata
        if (metadata) {
            if (metadata.memory_available_gb !== undefined) {
                this.state.memoryInfo = {
                    availableGb: metadata.memory_available_gb,
                    pressure: metadata.memory_pressure || 0,
                    status: metadata.memory_status || 'unknown'
                };
            }
            if (metadata.startup_mode) {
                this.state.memoryMode = metadata.startup_mode;
            }
            if (metadata.voice_biometrics_ready !== undefined) {
                this.state.voiceBiometricsReady = metadata.voice_biometrics_ready;
            }
            if (metadata.speaker_cache_ready !== undefined) {
                this.state.speakerCacheReady = metadata.speaker_cache_ready;
            }
            if (metadata.substep !== undefined) {
                this.state.currentSubstep = metadata.substep;
            }
            if (metadata.label) {
                // Use metadata label for display if provided
                this.state.displayLabel = metadata.label;
            }
            if (metadata.sublabel) {
                this.state.displaySublabel = metadata.sublabel;
            }
            
            // Process operations log from metadata
            if (metadata.operations && Array.isArray(metadata.operations)) {
                for (const op of metadata.operations) {
                    this.addLogEntry(
                        op.source || 'system',
                        op.message || op.text || '',
                        op.type || 'info'
                    );
                }
            }
            
            // Single operation log entry
            if (metadata.log_entry) {
                this.addLogEntry(
                    metadata.log_source || 'system',
                    metadata.log_entry,
                    metadata.log_type || 'info'
                );
            }
            
            // Auto-log stage changes with source info
            if (metadata.source && message) {
                this.addLogEntry(metadata.source, message, 'info');
            }
        }

        // Handle special log-only stages
        if (stage === '_log' || stage === '_log_batch') {
            // Don't update visual progress for log-only updates
            return;
        }

        // Log stage transitions automatically
        if (stage && stage !== this.state.stage && message) {
            const source = stage.includes('supervisor') ? 'Supervisor' : 
                          stage.includes('backend') || stage === 'api' ? 'Backend' : 
                          'System';
            this.addLogEntry(source, message, stage === 'failed' ? 'error' : 'info');
        }

        // Update UI
        this.updateUI();
        this.updateDetailedStatus();

        // Handle completion
        if (stage === 'complete' || progress >= 100) {
            const success = metadata.success !== false;
            const redirectUrl = metadata.redirect_url || `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
            this.handleCompletion(success, redirectUrl, message);
        }

        // v5.0: Handle PARTIAL completion (supervisor FALLBACK 5)
        // System is usable but some services are unavailable
        if (stage === 'partial_complete') {
            const success = true; // Partial is still a success, just limited
            const redirectUrl = metadata.redirect_url || `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
            
            // Show warning about partial completion
            console.warn('[Progress] ⚠️ Partial completion - some services unavailable');
            console.warn('[Progress] Services ready:', metadata.services_ready || []);
            console.warn('[Progress] Services failed:', metadata.services_failed || []);
            
            // Update status to reflect partial state
            this.updateStatusText('Partially ready - some features limited', 'warning');
            
            // Show notification about partial completion
            this.showPartialCompletionNotice(metadata.services_ready, metadata.services_failed);
            
            // Still proceed to main app after brief delay
            setTimeout(() => {
                this.handleCompletion(success, redirectUrl, message || 'JARVIS partially ready');
            }, 2000);
        }

        // v5.0: Handle slow startup notification
        if (stage === 'startup_slow' || stage === 'startup_timeout' || stage === 'services_warming') {
            this.updateStatusText(message || 'Startup taking longer than usual...', 'warning');
            // Don't redirect - let the system continue trying
        }

        // Handle failure - but be smart about it
        if (stage === 'failed' || metadata.success === false) {
            // Don't show error if we've made significant progress - backend might be up
            const hasProgress = this.state.progress > 30 || effectiveProgress > 30;
            const backendReady = metadata.backend_ready === true || this.state.backendReady;
            
            if (hasProgress && backendReady) {
                // Backend is up - this might be a frontend-only issue
                console.warn('[Progress] Backend ready but startup marked failed - attempting graceful recovery');
                this.updateStatusText('Finishing up...', 'warning');
                // Give it more time before showing error
                setTimeout(() => {
                    if (this.state.progress < 100 && !this.state.redirecting) {
                        this.showError(message || 'Frontend startup incomplete');
                    }
                }, 10000); // 10 second grace period
            } else {
                this.showError(message || 'Startup failed');
            }
        }
    }

    updateDetailedStatus() {
        // Get stage definition or create dynamic one
        let stageDef = this.stageDefinitions[this.state.stage];
        if (!stageDef) {
            const componentName = this.state.stage || 'unknown';
            const formattedName = this.state.displayLabel || componentName
                .replace(/_/g, ' ')
                .replace(/\b\w/g, c => c.toUpperCase());
            stageDef = {
                name: formattedName,
                icon: this._getComponentIcon(componentName),
                phase: this._inferPhase(this.state.targetProgress),
                substeps: [this.state.displaySublabel || `Initializing ${formattedName}...`]
            };
        }

        // Update phase indicator
        if (this.elements.phaseIndicator) {
            const phaseValue = this.elements.phaseIndicator.querySelector('.phase-value');
            if (phaseValue) {
                const phase = stageDef.phase || this.state.phase;
                const phaseNames = {
                    'cleanup': 'Cleanup',
                    'starting': 'Starting',
                    'initialization': 'Initialization',
                    'ready': 'Ready',
                    'complete': 'Complete',
                    'partial': 'Partial Ready',
                    'warning': 'Warning',
                    'slow': 'Slow Startup',
                    'error': 'Error',
                    'zero_touch': 'Zero-Touch Update',
                    'hot_reload': 'Hot Reload'
                };
                phaseValue.textContent = phaseNames[phase] || phase;
                phaseValue.className = `phase-value ${phase}`;
            }
        }

        // Update stage header
        if (this.elements.stageIcon) {
            this.elements.stageIcon.textContent = stageDef.icon;
        }
        if (this.elements.stageName) {
            this.elements.stageName.textContent = stageDef.name;
        }

        // Update substeps
        if (this.elements.substepList && stageDef.substeps) {
            const substepsHtml = stageDef.substeps.map((substep, idx) => {
                let className = 'substep';
                if (idx < this.state.currentSubstep) {
                    className += ' completed';
                } else if (idx === this.state.currentSubstep) {
                    className += ' active';
                }
                return `
                    <div class="${className}">
                        <span class="substep-indicator"></span>
                        <span>${substep}</span>
                    </div>
                `;
            }).join('');
            this.elements.substepList.innerHTML = substepsHtml;
        }

        // Update memory status
        if (this.elements.memoryStatus && this.state.memoryInfo) {
            const mem = this.state.memoryInfo;
            let memClass = 'good';
            if (mem.pressure > 70) memClass = 'low';
            else if (mem.pressure > 40) memClass = 'medium';

            const memValue = this.elements.memoryStatus.querySelector('.info-value');
            if (memValue) {
                memValue.textContent = `${mem.availableGb?.toFixed(1) || '?'}GB (${Math.round(mem.pressure || 0)}% used)`;
                memValue.className = `info-value ${memClass}`;
            }
        }

        // Update mode indicator
        if (this.elements.modeIndicator && this.state.memoryMode) {
            const modeValue = this.elements.modeIndicator.querySelector('.info-value');
            if (modeValue) {
                const modeText = this.state.memoryMode.replace(/_/g, ' ').toUpperCase();
                let modeClass = 'standard';
                if (this.state.memoryMode.includes('minimal')) modeClass = 'minimal';
                else if (this.state.memoryMode.includes('full')) modeClass = 'full';

                modeValue.textContent = modeText;
                modeValue.className = `info-value ${modeClass}`;
            }
        }
    }

    _inferPhase(progress) {
        if (progress < 40) return 'cleanup';
        if (progress < 50) return 'starting';
        if (progress < 95) return 'initialization';
        if (progress < 100) return 'ready';
        return 'complete';
    }

    startSmoothProgress() {
        if (!this.config.smoothProgress.enabled) return;

        this.state.smoothProgressInterval = setInterval(() => {
            if (this.state.progress < this.state.targetProgress) {
                const diff = this.state.targetProgress - this.state.progress;
                const increment = Math.max(0.3, diff / 15);
                this.state.progress = Math.min(
                    this.state.progress + increment,
                    this.state.targetProgress
                );
                this.updateProgressBar();
            }
        }, this.config.smoothProgress.incrementDelay);
    }

    updateUI() {
        this.updateProgressBar();

        if (this.state.message) {
            this.elements.statusMessage.textContent = this.state.message;
        }
    }

    updateProgressBar() {
        const displayProgress = Math.round(this.state.progress);
        this.elements.progressBar.style.width = `${displayProgress}%`;
        this.elements.progressPercentage.textContent = `${displayProgress}%`;

        // Phase-based color
        if (displayProgress < 40) {
            // Cleanup phase - orange
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #ff8800 0%, #ffaa00 100%)';
        } else if (displayProgress < 50) {
            // Starting phase - blue
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #0088ff 0%, #00aaff 100%)';
        } else if (displayProgress < 95) {
            // Initialization phase - green
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #00aa44 0%, #00ff41 100%)';
        } else {
            // Ready/Complete phase - bright green with glow
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #00ff41 0%, #00ff88 100%)';
            this.elements.progressBar.style.boxShadow = '0 0 20px rgba(0, 255, 65, 0.8)';
        }
    }

    updateStatusText(text, status) {
        if (this.elements.statusText) {
            this.elements.statusText.textContent = text;
            this.elements.statusText.className = `status-text ${status}`;
        }
        
        // Also update the status indicator visual if it exists
        const statusIndicator = document.getElementById('status-indicator');
        if (statusIndicator) {
            statusIndicator.className = `status-indicator ${status}`;
        }
        
        // v4.0: Keep subtitle in sync with phase for consistency
        // The subtitle below JARVIS logo should match the connection status
        if (this.elements.subtitle) {
            const phaseMap = {
                'connected': 'CONNECTED',
                'starting': 'STARTING',
                'loading': 'LOADING',
                'initializing': 'INITIALIZING',
                'verifying': 'VERIFYING',
                'finalizing': 'FINALIZING',
                'ready': 'SYSTEM READY',
                'partial': 'PARTIALLY READY',
                'slow': 'STARTUP SLOW',
                'warming': 'SERVICES WARMING',
                'error': 'ERROR',
                'warning': 'WARNING'
            };
            const newSubtitle = phaseMap[status] || 'INITIALIZING';
            if (this.elements.subtitle.textContent !== newSubtitle) {
                this.elements.subtitle.textContent = newSubtitle;
            }
        }
        
        console.log(`[Status] ${text} (${status})`);
    }

    async handleCompletion(success, redirectUrl, message) {
        // ═══════════════════════════════════════════════════════════════════════════
        // v4.0: ROBUST COMPLETION HANDLING
        // Never redirect until BOTH backend AND frontend are VERIFIED operational
        // This prevents the "OFFLINE - SEARCHING FOR BACKEND" issue
        // ═══════════════════════════════════════════════════════════════════════════
        
        // Stop regular polling - we're in completion mode now
        this.state.redirecting = true;
        console.log('[Complete] Starting completion verification...');

        // Update UI to show we're finalizing
        this.elements.subtitle.textContent = 'FINALIZING';
        this.state.progress = 95;
        this.state.targetProgress = 95;
        this.updateProgressBar();

        // ═══════════════════════════════════════════════════════════════════════════
        // STEP 1: Wait for BACKEND to be OPERATIONALLY ready (not just HTTP responding)
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[Complete] Step 1: Waiting for backend to be operationally ready...');
        this.updateStatusText('Waiting for backend services...', 'loading');
        
        const backendOperational = await this.waitForBackendOperational();
        
        if (!backendOperational) {
            // Backend didn't become operational in time
            console.warn('[Complete] Backend not operational - showing error');
            this.showError(message || 'Backend services failed to initialize. Please check the console.');
            this.updateStatusText('Backend services unavailable', 'error');
            return;
        }
        
        console.log('[Complete] ✓ Backend operationally ready');
        this.state.progress = 97;
        this.state.targetProgress = 97;
        this.updateProgressBar();

        // ═══════════════════════════════════════════════════════════════════════════
        // STEP 2: Wait for FRONTEND to be responding
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[Complete] Step 2: Waiting for frontend...');
        this.updateStatusText('Waiting for user interface...', 'loading');
        
        const frontendReady = await this.waitForFrontendWithRetries();

        if (!frontendReady) {
            this.showError('Frontend failed to start. Please check the console.');
            this.updateStatusText('Frontend unavailable', 'error');
            return;
        }

        console.log('[Complete] ✓ Frontend ready');
        this.state.progress = 99;
        this.state.targetProgress = 99;
        this.updateProgressBar();

        // ═══════════════════════════════════════════════════════════════════════════
        // STEP 3: FINAL verification - make sure backend STILL responds after frontend wait
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[Complete] Step 3: Final system verification...');
        this.updateStatusText('Final system check...', 'verifying');
        
        const finalBackendCheck = await this.quickBackendCheck();
        if (!finalBackendCheck) {
            console.warn('[Complete] Final backend check failed - retrying...');
            await this.sleep(2000);
            const retryCheck = await this.quickBackendCheck();
            if (!retryCheck) {
                this.showError('Backend became unavailable. Please refresh.');
                this.updateStatusText('Backend connection lost', 'error');
                return;
            }
        }

        // ═══════════════════════════════════════════════════════════════════════════
        // ALL SYSTEMS VERIFIED - Safe to redirect
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[Complete] ✓ All systems verified - proceeding with redirect');

        // Update UI to show completion
        this.elements.subtitle.textContent = 'SYSTEM READY';
        this.elements.statusMessage.textContent = message || 'JARVIS is online!';
        this.updateStatusText('System ready', 'ready');
        this.state.progress = 100;
        this.state.targetProgress = 100;
        this.updateProgressBar();

        // CRITICAL: Pass backend readiness state to main app via URL parameters
        // localStorage doesn't work cross-origin (port 3001 -> port 3000 are different origins)
        // So we encode the state in the redirect URL instead
        const backendPort = this.config.backendPort || 8010;
        const timestamp = Date.now().toString();

        // Build redirect URL with backend readiness parameters
        // The main app's JarvisConnectionService will read these for instant connection
        const params = new URLSearchParams({
            jarvis_ready: '1',
            backend_port: backendPort.toString(),
            ts: timestamp
        });

        // Ensure we have a valid redirect URL
        const baseRedirectUrl = redirectUrl || `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;

        // Append params to redirect URL
        const finalRedirectUrl = baseRedirectUrl.includes('?')
            ? `${baseRedirectUrl}&${params.toString()}`
            : `${baseRedirectUrl}?${params.toString()}`;

        console.log('[Complete] ✓ Backend readiness encoded in redirect URL');
        console.log(`[Complete] Redirect URL: ${finalRedirectUrl}`);

        this.cleanup();
        this.playEpicCompletionAnimation(finalRedirectUrl);
    }

    async waitForBackendOperational() {
        /**
         * v4.0: Wait for backend to be OPERATIONALLY ready, not just HTTP responding.
         * 
         * This is the CRITICAL gate that prevents "OFFLINE - SEARCHING FOR BACKEND"
         * by ensuring the backend is fully initialized before redirecting.
         * 
         * Checks:
         * 1. /health endpoint responds
         * 2. /health/ready returns ready=true (services operational)
         * 3. WebSocket endpoint is accessible
         */
        const config = {
            maxWaitTime: 90000,        // Maximum 90 seconds for backend
            initialDelay: 1000,         // Start with 1s between checks
            maxDelay: 3000,             // Cap delay at 3 seconds
            backoffMultiplier: 1.3,     // Increase delay by 30% each attempt
        };

        const backendPort = this.config.backendPort || 8010;
        const backendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${backendPort}`;
        
        const startTime = Date.now();
        let attempt = 0;
        let delay = config.initialDelay;
        let lastStatus = null;

        console.log('[Backend Wait] Starting intelligent wait for backend operational state...');

        while (Date.now() - startTime < config.maxWaitTime) {
            attempt++;
            const elapsed = Math.round((Date.now() - startTime) / 1000);

            try {
                // Check 1: Basic health endpoint
                const healthResponse = await fetch(`${backendUrl}/health`, {
                    method: 'GET',
                    cache: 'no-cache',
                    signal: AbortSignal.timeout(5000)
                });

                if (!healthResponse.ok) {
                    console.log(`[Backend Wait] Attempt ${attempt}: Health check failed (${healthResponse.status})`);
                    this.elements.statusMessage.textContent = `Backend starting... (${elapsed}s)`;
                    await this.sleep(delay);
                    delay = Math.min(delay * config.backoffMultiplier, config.maxDelay);
                    continue;
                }

                // Check 2: Operational readiness endpoint
                try {
                    const readyResponse = await fetch(`${backendUrl}/health/ready`, {
                        method: 'GET',
                        cache: 'no-cache',
                        signal: AbortSignal.timeout(5000)
                    });

                    if (readyResponse.ok) {
                        const readyData = await readyResponse.json();
                        
                        // Log status changes
                        const status = readyData.status || 'unknown';
                        if (status !== lastStatus) {
                            console.log(`[Backend Wait] Status changed: ${lastStatus} -> ${status}`);
                            lastStatus = status;
                        }

                        // Check if operationally ready
                        // ready=true OR operational=true OR status includes "ready"
                        const isOperational = 
                            readyData.ready === true || 
                            readyData.operational === true ||
                            status === 'ready' ||
                            status === 'operational';

                        if (isOperational) {
                            // v5.0: Also verify WebSocket connectivity before declaring ready
                            console.log(`[Backend Wait] Backend operational, verifying WebSocket...`);
                            this.elements.statusMessage.textContent = 'Verifying WebSocket connection...';
                            
                            const wsReady = await this.verifyWebSocketConnectivity();
                            if (wsReady) {
                                console.log(`[Backend Wait] ✓ Backend + WebSocket operational after ${attempt} attempts (${elapsed}s)`);
                                console.log('[Backend Wait] Ready data:', readyData);
                                return true;
                            } else {
                                console.log('[Backend Wait] WebSocket not ready yet, continuing...');
                                this.elements.statusMessage.textContent = 'WebSocket initializing...';
                                // Continue waiting - WebSocket might need more time
                            }
                        }

                        // Show detailed status to user
                        const details = readyData.details || {};
                        const mlStatus = details.ml_models_status || 'initializing';
                        this.elements.statusMessage.textContent = `Initializing services... (${mlStatus})`;
                        
                        // If we have WebSocket ready indication, verify it actually works
                        if (elapsed > 30 && (details.websocket_ready === true || readyData.operational === true)) {
                            // Verify WebSocket actually connects
                            const wsVerified = await this.verifyWebSocketConnectivity();
                            if (wsVerified) {
                                console.log(`[Backend Wait] ✓ WebSocket verified, accepting after ${elapsed}s`);
                                return true;
                            } else {
                                console.log('[Backend Wait] WebSocket indicator true but verification failed');
                            }
                        }
                    }
                } catch (readyError) {
                    // /health/ready might not exist - fall back to basic check + WebSocket
                    console.debug('[Backend Wait] /health/ready failed:', readyError.message);
                    
                    // If basic health works and WebSocket connects, we're good
                    if (elapsed > 30) {
                        const wsVerified = await this.verifyWebSocketConnectivity();
                        if (wsVerified) {
                            console.log(`[Backend Wait] ✓ Basic health + WebSocket OK, accepting after ${elapsed}s`);
                            return true;
                        } else {
                            console.log('[Backend Wait] Basic health OK but WebSocket not ready');
                        }
                    }
                }

                // Backend is responding but not fully ready - keep waiting
                console.log(`[Backend Wait] Attempt ${attempt}: Backend responding but not operational (${elapsed}s)`);
                this.elements.statusMessage.textContent = `Backend initializing... (${elapsed}s)`;
                
            } catch (error) {
                console.debug(`[Backend Wait] Attempt ${attempt} failed: ${error.message}`);
                this.elements.statusMessage.textContent = `Waiting for backend... (${elapsed}s)`;
            }

            await this.sleep(delay);
            delay = Math.min(delay * config.backoffMultiplier, config.maxDelay);
        }

        // Timeout - but do a final check
        console.warn('[Backend Wait] Timeout reached, doing final check...');
        const finalCheck = await this.quickBackendCheck();
        if (finalCheck) {
            console.log('[Backend Wait] Final check passed - accepting');
            return true;
        }

        console.error(`[Backend Wait] ✗ Backend failed to become operational after ${config.maxWaitTime / 1000}s`);
        return false;
    }

    async verifyWebSocketConnectivity() {
        /**
         * v5.0: Verify WebSocket is accepting connections before redirect.
         * 
         * This prevents the "CONNECTING..." issue where HTTP is ready but
         * WebSocket isn't accepting connections yet.
         * 
         * Returns true if WebSocket can connect, false otherwise.
         */
        const backendPort = this.config.backendPort || 8010;
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProtocol}//${this.config.hostname}:${backendPort}/ws/chat`;
        
        return new Promise((resolve) => {
            const timeout = setTimeout(() => {
                console.log('[WebSocket Verify] Connection timeout');
                resolve(false);
            }, 5000);
            
            try {
                const ws = new WebSocket(wsUrl);
                
                ws.onopen = () => {
                    clearTimeout(timeout);
                    console.log('[WebSocket Verify] ✓ Connection successful');
                    ws.close(1000, 'Verification complete');
                    resolve(true);
                };
                
                ws.onerror = (error) => {
                    clearTimeout(timeout);
                    console.log('[WebSocket Verify] Connection failed:', error);
                    resolve(false);
                };
                
                ws.onclose = () => {
                    // This fires after successful open+close or on error
                };
                
            } catch (error) {
                clearTimeout(timeout);
                console.log('[WebSocket Verify] Exception:', error.message);
                resolve(false);
            }
        });
    }

    async waitForFrontendWithRetries() {
        /**
         * Intelligent frontend waiting with exponential backoff.
         * This is the CRITICAL gate that prevents redirecting to an unavailable frontend.
         *
         * Strategy:
         * - Quick initial checks (frontend might already be up)
         * - Exponential backoff to avoid hammering
         * - Maximum wait time before giving up
         * - Clear status updates to the user
         */
        const config = {
            maxWaitTime: 60000,        // Maximum 60 seconds total wait
            initialDelay: 500,          // Start with 500ms between checks
            maxDelay: 3000,             // Cap delay at 3 seconds
            backoffMultiplier: 1.5,     // Increase delay by 50% each attempt
        };

        const startTime = Date.now();
        let attempt = 0;
        let delay = config.initialDelay;

        console.log('[Frontend Wait] Starting intelligent wait for frontend...');
        this.updateStatusText('Waiting for frontend...', 'loading');
        this.elements.statusMessage.textContent = 'Starting user interface...';

        while (Date.now() - startTime < config.maxWaitTime) {
            attempt++;
            const elapsed = Math.round((Date.now() - startTime) / 1000);

            console.log(`[Frontend Wait] Attempt ${attempt} (${elapsed}s elapsed)...`);

            try {
                const isReady = await this.checkFrontendReady();

                if (isReady) {
                    console.log(`[Frontend Wait] ✓ Frontend ready after ${attempt} attempts (${elapsed}s)`);
                    return true;
                }
            } catch (error) {
                console.debug(`[Frontend Wait] Check failed: ${error.message}`);
            }

            // Update user-facing status
            if (elapsed < 10) {
                this.elements.statusMessage.textContent = 'Starting user interface...';
            } else if (elapsed < 30) {
                this.elements.statusMessage.textContent = 'Frontend is initializing...';
                this.updateStatusText('Starting frontend...', 'loading');
            } else {
                this.elements.statusMessage.textContent = `Still waiting for frontend (${elapsed}s)...`;
                this.updateStatusText('Frontend slow to start...', 'warning');
            }

            // Update progress to show we're making progress in waiting
            const waitProgress = 95 + Math.min(4, (elapsed / 60) * 4); // 95% -> 99% over 60s
            this.state.progress = waitProgress;
            this.state.targetProgress = waitProgress;
            this.updateProgressBar();

            // Wait with exponential backoff
            await this.sleep(delay);
            delay = Math.min(delay * config.backoffMultiplier, config.maxDelay);
        }

        console.error(`[Frontend Wait] ✗ Frontend failed to become ready after ${config.maxWaitTime / 1000}s`);
        return false;
    }

    async verifyBackendReady(redirectUrl) {
        /**
         * Verify the full system is ready before redirecting.
         * Checks:
         * 1. HTTP health endpoint responds
         * 2. WebSocket endpoint is accessible
         * 3. Frontend is accessible (CRITICAL - prevents redirect to offline page)
         */
        const backendPort = this.config.backendPort || 8010;
        const backendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${backendPort}`;
        
        this.updateStatusText('Checking health...', 'verifying');
        
        try {
            // Check 1: HTTP health
            console.log(`[Verify] Checking backend health at ${backendUrl}/health...`);
            const healthResponse = await fetch(`${backendUrl}/health`, {
                method: 'GET',
                headers: { 'Accept': 'application/json' },
                signal: AbortSignal.timeout(5000)
            });
            
            if (!healthResponse.ok) {
                console.warn(`[Verify] Health check failed: ${healthResponse.status}`);
                this.updateStatusText('Health check failed', 'warning');
                return false;
            }
            
            const healthData = await healthResponse.json();
            console.log('[Verify] Health check passed:', healthData);
            this.updateStatusText('Testing WebSocket...', 'verifying');
            
            // Check 2: Try WebSocket connection (quick test)
            // Use /ws which is the unified WebSocket endpoint
            console.log(`[Verify] Testing WebSocket at ws://${this.config.hostname}:${backendPort}/ws...`);
            const wsReady = await this.testWebSocket(`ws://${this.config.hostname}:${backendPort}/ws`);
            
            if (!wsReady) {
                console.warn('[Verify] WebSocket not ready yet');
                this.updateStatusText('WebSocket not ready', 'warning');
                return false;
            }
            
            // Check 3: Verify frontend is accessible (CRITICAL)
            this.updateStatusText('Checking frontend...', 'verifying');
            console.log(`[Verify] Checking frontend at ${this.config.hostname}:${this.config.mainAppPort}...`);
            const frontendReady = await this.checkFrontendReady();
            
            if (!frontendReady) {
                console.warn('[Verify] Frontend not ready yet');
                this.updateStatusText('Frontend starting...', 'warning');
                return false;
            }
            
            console.log('[Verify] ✓ All services verified (backend + frontend)!');
            this.updateStatusText('System ready', 'ready');
            return true;
            
        } catch (error) {
            console.warn('[Verify] System verification failed:', error.message);
            this.updateStatusText('Verification failed', 'warning');
            return false;
        }
    }

    testWebSocket(wsUrl) {
        /**
         * Quick WebSocket connectivity test.
         * Returns true if connection succeeds, false otherwise.
         */
        return new Promise((resolve) => {
            const timeout = setTimeout(() => {
                console.warn('[Verify] WebSocket test timeout');
                resolve(false);
            }, 3000);
            
            try {
                const ws = new WebSocket(wsUrl);
                
                ws.onopen = () => {
                    clearTimeout(timeout);
                    console.log('[Verify] WebSocket test succeeded');
                    ws.close();
                    resolve(true);
                };
                
                ws.onerror = () => {
                    clearTimeout(timeout);
                    resolve(false);
                };
                
                ws.onclose = () => {
                    // If closed before open, it failed
                };
                
            } catch (error) {
                clearTimeout(timeout);
                resolve(false);
            }
        });
    }

    async playEpicCompletionAnimation(redirectUrl) {
        const container = document.querySelector('.loading-container');
        const reactor = this.elements.reactor;

        const totalDuration = 3000;

        // Phase 1: Power surge
        if (reactor) {
            reactor.style.transition = 'all 0.3s ease-out';
            reactor.style.transform = 'translate(-50%, -50%) scale(1.5)';
            reactor.style.filter = 'drop-shadow(0 0 80px rgba(0, 255, 65, 1)) brightness(2)';

            for (let i = 0; i < 3; i++) {
                setTimeout(() => this.createEnergyRing(reactor, '#00ff41', i), i * 200);
            }
        }

        await this.sleep(600);

        // Phase 2: Fade out
        if (container) {
            container.style.transition = 'opacity 1s ease-out';
            container.style.opacity = '0';
        }
        if (reactor) {
            reactor.style.transition = 'all 1s ease-out';
            reactor.style.transform = 'translate(-50%, -50%) scale(2)';
            reactor.style.opacity = '0';
        }

        await this.sleep(1500);

        // Phase 3: Navigate
        const overlay = document.createElement('div');
        overlay.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: #000; opacity: 0;
            transition: opacity 0.5s ease-in; z-index: 10001;
        `;
        document.body.appendChild(overlay);
        setTimeout(() => overlay.style.opacity = '1', 10);

        await this.sleep(500);
        window.location.href = redirectUrl;
    }

    createEnergyRing(reactor, color, index) {
        const ring = document.createElement('div');
        ring.style.cssText = `
            position: absolute; top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            width: 300px; height: 300px;
            border: 3px solid ${color}; border-radius: 50%;
            opacity: 1; animation: expandRing 1s ease-out forwards;
            pointer-events: none;
        `;

        const style = document.createElement('style');
        style.textContent = `
            @keyframes expandRing {
                0% { transform: translate(-50%, -50%) scale(0); opacity: 1; }
                100% { transform: translate(-50%, -50%) scale(3); opacity: 0; }
            }
        `;
        document.head.appendChild(style);

        reactor.parentElement.appendChild(ring);
        setTimeout(() => { ring.remove(); style.remove(); }, 1000);
    }

    sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    showPartialCompletionNotice(servicesReady = [], servicesFailed = []) {
        /**
         * v5.0: Show notice about partial completion
         * This is displayed when the system times out but is partially usable.
         */
        const readyCount = servicesReady.length;
        const failedCount = servicesFailed.length;
        
        // Update the status message to reflect partial state
        if (this.elements.statusMessage) {
            if (failedCount > 0) {
                this.elements.statusMessage.textContent = 
                    `${readyCount} services ready, ${failedCount} unavailable`;
            } else {
                this.elements.statusMessage.textContent = 
                    'System partially ready - loading remaining services...';
            }
        }
        
        // Update subtitle to warning state
        if (this.elements.subtitle) {
            this.elements.subtitle.textContent = 'PARTIALLY READY';
        }
        
        // Change progress bar to warning color
        if (this.elements.progressBar) {
            this.elements.progressBar.style.background = 
                'linear-gradient(90deg, #ff9500 0%, #ffcc00 100%)';
        }
        
        // Log details
        console.warn('[Partial] Services ready:', servicesReady);
        console.warn('[Partial] Services failed:', servicesFailed);
        
        // Add to operation log
        this.addLogEntry('System', `Partial startup: ${readyCount} ready, ${failedCount} unavailable`, 'warning');
        if (failedCount > 0) {
            this.addLogEntry('System', `Unavailable: ${servicesFailed.join(', ')}`, 'warning');
        }
    }

    async showError(message) {
        console.error('[Error]', message);
        
        // Before showing error, do a quick sanity check
        // Maybe backend is actually fine and we can recover
        const backendUp = await this.quickBackendCheck();
        
        if (backendUp && this.state.progress >= 30) {
            // Backend is up! Try recovery instead of showing error
            console.warn('[Error] Backend is UP despite error - attempting recovery redirect');
            this.updateStatusText('Recovering...', 'warning');
            
            // Give frontend a moment to stabilize, then try redirect
            await this.sleep(3000);
            
            const frontendUp = await this.checkFrontendReady();
            if (frontendUp) {
                console.log('[Error] Recovery successful - redirecting');
                const redirectUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
                this.playEpicCompletionAnimation(redirectUrl);
                return;
            }
        }
        
        // Actually show the error
        this.cleanup();

        if (this.elements.errorContainer) {
            this.elements.errorContainer.classList.add('visible');
        }
        if (this.elements.errorMessage) {
            // Make error message more helpful
            const helpfulMessage = backendUp 
                ? `${message}\n\nNote: Backend is running. Try refreshing or accessing http://localhost:3000 directly.`
                : message;
            this.elements.errorMessage.textContent = helpfulMessage;
        }
        if (this.elements.subtitle) {
            // Use more accurate subtitle based on what's actually happening
            const subtitle = backendUp ? 'PARTIAL INITIALIZATION' : 'INITIALIZATION FAILED';
            this.elements.subtitle.textContent = subtitle;
        }
        if (this.elements.reactor) {
            this.elements.reactor.style.opacity = backendUp ? '0.6' : '0.3';
        }
    }

    startHealthMonitoring() {
        setInterval(() => {
            const timeSinceUpdate = Date.now() - this.state.lastUpdate;
            const totalTime = Date.now() - this.state.startTime;

            if (timeSinceUpdate > 30000 && this.state.progress < 100) {
                console.warn('[Health] No updates for 30 seconds');
                if (!this.state.connected) {
                    this.connectWebSocket();
                }
            }

            if (totalTime > 600000 && this.state.progress < 100) {
                this.showError('Startup timed out. Please check terminal logs.');
            }
        }, 5000);
    }

    _getComponentIcon(componentName) {
        const iconMap = {
            'config': '⚙️', 'cloud_sql': '🗄️', 'learning': '🧠',
            'memory': '💾', 'cloud_ml': '☁️', 'cloud_ecapa': '🎤',
            'vbi': '🔐', 'ml_engine': '🤖', 'speaker': '🔊',
            'voice': '🎙️', 'jarvis': '🤖', 'websocket': '🔌',
            'unified': '🔗', 'neural': '🧠', 'goal': '🎯',
            'uae': '🌐', 'hybrid': '⚡', 'orchestrator': '🎼',
            'vision': '👁️', 'display': '🖥️', 'dynamic': '⚡',
            'api': '🌐', 'database': '🗄️', 'ecapa': '🎤',
            'proxy': '🔐', 'kill': '⚔️', 'cleanup': '🧹',
            'port': '🔌', 'vm': '☁️', 'cost': '💰',
            'autonomous': '🤖', 'metrics': '📊'
        };

        for (const [key, icon] of Object.entries(iconMap)) {
            if (componentName.toLowerCase().includes(key)) {
                return icon;
            }
        }
        return '🔧';
    }

    cleanup() {
        console.log('[Cleanup] 🧹 Cleaning up resources...');
        
        if (this.state.ws) {
            this.state.ws.close();
            this.state.ws = null;
        }
        // Clear adaptive polling timeout
        if (this.state.pollingTimeout) {
            clearTimeout(this.state.pollingTimeout);
            this.state.pollingTimeout = null;
        }
        // Legacy interval cleanup
        if (this.state.pollingInterval) {
            clearInterval(this.state.pollingInterval);
            this.state.pollingInterval = null;
        }
        if (this.state.smoothProgressInterval) {
            clearInterval(this.state.smoothProgressInterval);
            this.state.smoothProgressInterval = null;
        }
        if (this.backendHealthInterval) {
            clearInterval(this.backendHealthInterval);
            this.backendHealthInterval = null;
        }

        // Log final polling stats
        if (this.pollingState.totalRequests > 0) {
            console.log(`[Cleanup] 📊 Final polling stats: ${this.pollingState.totalRequests} total requests, ${this.pollingState.totalRateLimits} rate limits (${((this.pollingState.totalRateLimits / this.pollingState.totalRequests) * 100).toFixed(1)}%)`);
        }
    }
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        window.jarvisLoader = new JARVISLoadingManager();
    });
} else {
    window.jarvisLoader = new JARVISLoadingManager();
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (window.jarvisLoader) {
        window.jarvisLoader.cleanup();
    }
});
