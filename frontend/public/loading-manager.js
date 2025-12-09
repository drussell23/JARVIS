/**
 * JARVIS Advanced Loading Manager v4.0
 * 
 * Synchronized with start_system.py --restart backend stages
 *
 * Features:
 * - Smooth 1-100% progress with real-time updates
 * - Detailed stage tracking matching backend broadcast stages
 * - Voice biometric system initialization feedback
 * - Cloud ECAPA pre-warming status
 * - Memory-aware mode selection display
 * - Recent speaker cache initialization
 * - Robust error handling with automatic retry
 * - WebSocket + HTTP polling fallback
 * - Epic cinematic completion animation
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
                maxDelay: 5000,
                maxAttempts: 30,
                backoffMultiplier: 1.3
            },
            polling: {
                enabled: true,
                interval: 300,  // Poll every 300ms for smooth updates
                timeout: 3000
            },
            smoothProgress: {
                enabled: true,
                incrementDelay: 50,  // Update every 50ms for smooth animation
                maxAutoProgress: 98   // Don't auto-progress past 98%
            }
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

            // === ERROR STATE ===
            'failed': {
                name: 'Startup Failed',
                icon: '❌',
                phase: 'error',
                expectedProgress: [0, 0],
                substeps: ['Error occurred']
            }
        };

        this.state = {
            ws: null,
            connected: false,
            progress: 0,
            targetProgress: 0,
            stage: 'initializing',
            substage: null,
            message: 'Initializing JARVIS...',
            reconnectAttempts: 0,
            pollingInterval: null,
            smoothProgressInterval: null,
            startTime: Date.now(),
            lastUpdate: Date.now(),
            // Detailed tracking
            completedStages: [],
            currentSubstep: 0,
            memoryMode: null,
            memoryInfo: null,
            voiceBiometricsReady: false,
            speakerCacheReady: false,
            phase: 'cleanup'
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
            phaseIndicator: document.getElementById('phase-indicator')
        };
    }

    /**
     * Create the detailed status panel dynamically
     */
    createDetailedStatusPanel() {
        if (document.getElementById('detailed-status')) return;

        const panel = document.createElement('div');
        panel.id = 'detailed-status';
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
        panel.style.cssText = `
            position: absolute;
            bottom: 120px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(0, 20, 0, 0.85);
            border: 1px solid rgba(0, 255, 65, 0.3);
            border-radius: 12px;
            padding: 16px 24px;
            min-width: 360px;
            max-width: 520px;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 12px;
            color: #00ff41;
            box-shadow: 0 4px 20px rgba(0, 255, 65, 0.2);
            z-index: 100;
        `;

        const style = document.createElement('style');
        style.textContent = `
            .phase-indicator {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-bottom: 12px;
                padding-bottom: 8px;
                border-bottom: 1px solid rgba(0, 255, 65, 0.2);
                font-size: 10px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            .phase-label {
                opacity: 0.6;
            }
            .phase-value {
                font-weight: 600;
                padding: 2px 8px;
                background: rgba(0, 255, 65, 0.1);
                border-radius: 4px;
            }
            .phase-value.cleanup { color: #ffaa00; }
            .phase-value.starting { color: #00aaff; }
            .phase-value.initialization { color: #00ff88; }
            .phase-value.ready { color: #00ffff; }
            .phase-value.complete { color: #00ff41; }
            .phase-value.error { color: #ff4444; }
            .stage-header {
                display: flex;
                align-items: center;
                gap: 10px;
                margin-bottom: 12px;
                font-size: 14px;
                font-weight: 600;
            }
            .stage-icon {
                font-size: 18px;
            }
            .substep-list {
                display: flex;
                flex-direction: column;
                gap: 6px;
                margin-bottom: 12px;
                padding-left: 28px;
            }
            .substep {
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 11px;
                opacity: 0.6;
                transition: opacity 0.3s ease;
            }
            .substep.active {
                opacity: 1;
                color: #00ff88;
            }
            .substep.completed {
                opacity: 0.8;
                color: #00aa44;
            }
            .substep-indicator {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: rgba(0, 255, 65, 0.3);
                transition: all 0.3s ease;
            }
            .substep.active .substep-indicator {
                background: #00ff88;
                box-shadow: 0 0 8px #00ff88;
                animation: pulse 1s ease-in-out infinite;
            }
            .substep.completed .substep-indicator {
                background: #00aa44;
            }
            @keyframes pulse {
                0%, 100% { transform: scale(1); }
                50% { transform: scale(1.3); }
            }
            .system-info {
                display: flex;
                justify-content: space-between;
                padding-top: 12px;
                border-top: 1px solid rgba(0, 255, 65, 0.2);
                font-size: 10px;
            }
            .info-label {
                opacity: 0.6;
                margin-right: 6px;
            }
            .info-value {
                font-weight: 500;
            }
            .memory-status .info-value.low { color: #ff4444; }
            .memory-status .info-value.medium { color: #ffaa00; }
            .memory-status .info-value.good { color: #00ff88; }
            .mode-indicator .info-value.minimal { color: #ffaa00; }
            .mode-indicator .info-value.standard { color: #00ff88; }
            .mode-indicator .info-value.full { color: #00ffff; }
        `;
        document.head.appendChild(style);

        const progressContainer = document.querySelector('.progress-container');
        if (progressContainer && progressContainer.parentNode) {
            progressContainer.parentNode.insertBefore(panel, progressContainer);
        } else {
            document.querySelector('.loading-container')?.appendChild(panel);
        }

        this.elements.detailedStatus = panel;
        this.elements.stageIcon = document.getElementById('stage-icon');
        this.elements.stageName = document.getElementById('stage-name');
        this.elements.substepList = document.getElementById('substep-list');
        this.elements.memoryStatus = document.getElementById('memory-status');
        this.elements.modeIndicator = document.getElementById('mode-indicator');
        this.elements.phaseIndicator = document.getElementById('phase-indicator');
    }

    async init() {
        console.log('[JARVIS] Loading Manager v4.0 starting...');
        console.log(`[Config] Loading server: ${this.config.hostname}:${this.config.loadingServerPort}`);

        // QUICK CHECK: If backend is already healthy, skip loading screen entirely
        const backendAlreadyReady = await this.checkBackendHealth();
        if (backendAlreadyReady) {
            console.log('[JARVIS] ✅ Backend already healthy - skipping loading screen');
            this.quickRedirectToApp();
            return;
        }

        this.createParticles();
        this.createDetailedStatusPanel();
        this.startSmoothProgress();

        // Connect to loading server (port 3001)
        await this.connectWebSocket();
        this.startPolling();
        this.startHealthMonitoring();
        
        // Also start polling backend directly as fallback
        this.startBackendHealthPolling();
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

    quickRedirectToApp() {
        /**
         * Quick redirect to main app when backend is already ready.
         * Shows brief success animation before redirect.
         */
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
         * Poll backend health directly as fallback when loading server isn't available.
         * This ensures we can still detect when backend is ready.
         */
        const pollInterval = setInterval(async () => {
            try {
                const isHealthy = await this.checkBackendHealth();
                
                if (isHealthy) {
                    console.log('[JARVIS] ✅ Backend became healthy via direct polling');
                    clearInterval(pollInterval);
                    
                    // Trigger completion
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
            } catch (error) {
                // Silent fail - loading server polling is primary
            }
        }, 2000); // Check every 2 seconds

        // Store interval for cleanup
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
                this.state.reconnectAttempts = 0;
                this.updateStatusText('Connected', 'connected');
            };

            this.state.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type !== 'pong') {
                        this.handleProgressUpdate(data);
                    }
                } catch (error) {
                    console.error('[WebSocket] Parse error:', error);
                }
            };

            this.state.ws.onerror = (error) => {
                console.error('[WebSocket] Error:', error);
            };

            this.state.ws.onclose = () => {
                console.log('[WebSocket] Disconnected');
                this.state.connected = false;
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

    startPolling() {
        if (!this.config.polling.enabled) return;

        console.log('[Polling] Starting HTTP polling on loading server...');
        this.state.pollingInterval = setInterval(async () => {
            try {
                // Poll loading server on port 3001
                const url = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}/api/startup-progress`;
                const response = await fetch(url, {
                    method: 'GET',
                    cache: 'no-cache',
                    signal: AbortSignal.timeout(this.config.polling.timeout)
                });

                if (response.ok) {
                    const data = await response.json();
                    this.handleProgressUpdate(data);
                }
            } catch (error) {
                // Silently fail - WebSocket is primary
            }
        }, this.config.polling.interval);
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

        // Handle failure
        if (stage === 'failed' || metadata.success === false) {
            this.showError(message || 'Startup failed');
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
                    'error': 'Error'
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
    }

    handleCompletion(success, redirectUrl, message) {
        if (!success) {
            this.showError(message || 'Startup completed with errors');
            return;
        }

        console.log('[Complete] ✓ Startup successful!');
        this.cleanup();

        this.elements.subtitle.textContent = 'SYSTEM READY';
        this.elements.statusMessage.textContent = message || 'JARVIS is online!';
        this.state.progress = 100;
        this.state.targetProgress = 100;
        this.updateProgressBar();

        this.playEpicCompletionAnimation(redirectUrl);
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

    showError(message) {
        console.error('[Error]', message);
        this.cleanup();

        if (this.elements.errorContainer) {
            this.elements.errorContainer.classList.add('visible');
        }
        if (this.elements.errorMessage) {
            this.elements.errorMessage.textContent = message;
        }
        if (this.elements.subtitle) {
            this.elements.subtitle.textContent = 'INITIALIZATION FAILED';
        }
        if (this.elements.reactor) {
            this.elements.reactor.style.opacity = '0.3';
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
        if (this.state.ws) {
            this.state.ws.close();
            this.state.ws = null;
        }
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
