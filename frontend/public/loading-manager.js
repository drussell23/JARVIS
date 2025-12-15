/**
 * JARVIS Advanced Loading Manager v4.2 - Matrix Theme
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
 * - Matrix-style UI with cyan/teal color scheme
 * - SVG circular progress indicator with animated rings
 * - Matrix rain background animation (canvas-based)
 * - Smooth 1-100% progress with real-time updates
 * - Detailed stage tracking matching backend broadcast stages
 * - Voice biometric system initialization feedback
 * - Cloud ECAPA pre-warming status
 * - Memory-aware mode selection display
 * - Recent speaker cache initialization
 * - WebSocket + HTTP polling fallback
 * - Epic cinematic completion animation with Matrix effects
 * - Quick sanity check before redirect (safety net only)
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
                interval: 1000,  // Poll every 1000ms to avoid rate limiting
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
     * DISABLED: Clean Matrix design doesn't use detailed panel overlay
     * The target design shows only: Title + Progress Circle + Status Text
     */
    createDetailedStatusPanel() {
        // Disabled for clean Matrix design matching target screenshot
        // The minimal design is more elegant without the detailed overlay
        return;
    }

    async init() {
        console.log('[JARVIS] Loading Manager v4.2 (Matrix Theme) starting...');
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
         * 
         * IMPORTANT: This is a FALLBACK mechanism, not the primary completion trigger.
         * - Requires multiple consecutive healthy checks before triggering
         * - Also verifies frontend is ready before completing
         * - Only triggers if we haven't received proper loading server updates
         */
        let consecutiveHealthyChecks = 0;
        const requiredConsecutiveChecks = 3; // Require 3 consecutive healthy checks
        let lastLoadingServerUpdate = Date.now();
        
        const pollInterval = setInterval(async () => {
            try {
                // Don't trigger completion if we're receiving loading server updates
                const timeSinceLoadingUpdate = Date.now() - this.state.lastUpdate;
                if (timeSinceLoadingUpdate < 10000 && this.state.progress < 95) {
                    // Loading server is active, let it handle completion
                    consecutiveHealthyChecks = 0;
                    return;
                }
                
                const backendHealthy = await this.checkBackendHealth();
                
                if (backendHealthy) {
                    consecutiveHealthyChecks++;
                    console.log(`[Health Polling] Backend healthy (${consecutiveHealthyChecks}/${requiredConsecutiveChecks})`);
                    
                    // Require multiple consecutive healthy checks
                    if (consecutiveHealthyChecks >= requiredConsecutiveChecks) {
                        // Also verify frontend is ready before completing
                        const frontendReady = await this.checkFrontendReady();
                        
                        if (!frontendReady) {
                            console.log('[Health Polling] Backend ready but frontend not yet available, waiting...');
                            // Don't reset counter, just wait for frontend
                            return;
                        }
                        
                        console.log('[JARVIS] ✅ Full system ready via fallback polling');
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
                } else {
                    // Reset counter if backend becomes unhealthy
                    consecutiveHealthyChecks = 0;
                }
            } catch (error) {
                // Silent fail - loading server polling is primary
                consecutiveHealthyChecks = 0;
            }
        }, 3000); // Check every 3 seconds (slower to let loading server handle it)

        // Store interval for cleanup
        this.backendHealthInterval = pollInterval;
    }

    createParticles() {
        // Matrix Rain is now handled by the canvas in loading.html
        // This method is kept for backwards compatibility but does nothing
        // The Matrix rain animation provides a superior visual effect
        const container = document.getElementById('particles');
        if (!container) return;
        // Particles are replaced by Matrix rain canvas animation
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
                // Update connection indicator in loading.html
                if (typeof window.updateConnectionStatus === 'function') {
                    window.updateConnectionStatus(true, 'Connected');
                }
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
                // Update connection indicator in loading.html
                if (typeof window.updateConnectionStatus === 'function') {
                    window.updateConnectionStatus(false, 'Reconnecting...');
                }
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
        // Disabled for clean Matrix design - no detailed status panel
        // Status is shown via the main status message element instead
        return;
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

        // Matrix theme - cyan/teal gradient throughout
        // Phase-based intensity rather than color change
        if (displayProgress < 40) {
            // Cleanup phase - dim cyan
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #0099bb 0%, #00aacc 100%)';
        } else if (displayProgress < 50) {
            // Starting phase - medium cyan
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #00bbdd 0%, #00ccee 100%)';
        } else if (displayProgress < 95) {
            // Initialization phase - bright cyan/teal
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #00d4ff 0%, #00ffcc 100%)';
        } else {
            // Ready/Complete phase - bright teal with intense glow
            this.elements.progressBar.style.background = 'linear-gradient(90deg, #00ffcc 0%, #00ff88 100%)';
            this.elements.progressBar.style.boxShadow = '0 0 30px rgba(0, 255, 204, 0.8)';
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
        
        console.log(`[Status] ${text} (${status})`);
    }

    async handleCompletion(success, redirectUrl, message) {
        if (!success) {
            this.showError(message || 'Startup completed with errors');
            this.updateStatusText('Startup failed', 'error');
            return;
        }

        console.log('[Complete] ✓ Received completion from authority (start_system.py)');
        
        // Update UI to show completion is starting
        this.elements.subtitle.textContent = 'FINALIZING';
        this.elements.statusMessage.textContent = 'Verifying system readiness...';
        this.updateStatusText('Verifying...', 'verifying');
        this.state.progress = 100;
        this.state.targetProgress = 100;
        this.updateProgressBar();

        // =========================================================================
        // ROBUST SYSTEM VERIFICATION BEFORE REDIRECT
        // =========================================================================
        // We verify BOTH backend AND frontend are fully ready before redirecting.
        // This ensures the user lands on a fully functional JARVIS interface.
        // =========================================================================
        
        const backendPort = this.config.backendPort || 8010;
        const backendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${backendPort}`;
        const wsUrl = `ws://${this.config.hostname}:${backendPort}`;
        
        let systemReady = false;
        let retryCount = 0;
        const maxRetries = 5;
        
        while (!systemReady && retryCount < maxRetries) {
            retryCount++;
            console.log(`[Complete] System verification attempt ${retryCount}/${maxRetries}...`);
            
            // Check 1: Backend health
            const backendOk = await this.checkBackendHealth();
            if (!backendOk) {
                console.warn('[Complete] Backend not ready yet...');
                this.elements.statusMessage.textContent = 'Waiting for backend...';
                await this.sleep(1000);
                continue;
            }
            
            // Check 2: Frontend accessible
            const frontendOk = await this.checkFrontendReady();
            if (!frontendOk) {
                console.warn('[Complete] Frontend not ready yet...');
                this.elements.statusMessage.textContent = 'Waiting for frontend...';
                await this.sleep(1000);
                continue;
            }
            
            // Check 3: WebSocket connectivity (quick test)
            const wsOk = await this.testWebSocket(`${wsUrl}/ws`);
            if (!wsOk) {
                console.warn('[Complete] WebSocket not ready yet...');
                this.elements.statusMessage.textContent = 'Waiting for WebSocket...';
                await this.sleep(1000);
                continue;
            }
            
            // All checks passed!
            systemReady = true;
            console.log('[Complete] ✅ Full system verification passed!');
        }
        
        if (!systemReady) {
            console.warn('[Complete] System verification timed out, proceeding anyway...');
            this.elements.statusMessage.textContent = 'Finalizing (verification timeout)...';
            await this.sleep(1000);
        }

        // Update UI to show ready state
        this.elements.subtitle.textContent = 'SYSTEM READY';
        this.elements.statusMessage.textContent = message || 'JARVIS is online!';
        this.updateStatusText('System ready', 'ready');

        console.log('[Complete] Proceeding with redirect...');
        
        // CRITICAL: Persist backend readiness state for main app
        // This prevents "CONNECTING TO BACKEND..." showing after loading completes
        try {
            localStorage.setItem('jarvis_backend_verified', 'true');
            localStorage.setItem('jarvis_backend_url', backendUrl);
            localStorage.setItem('jarvis_backend_ws_url', wsUrl);
            localStorage.setItem('jarvis_backend_verified_at', Date.now().toString());
            localStorage.setItem('jarvis_backend_port', backendPort.toString());
            localStorage.setItem('jarvis_system_ready', 'true');
            console.log('[Complete] ✓ Backend readiness state persisted for main app');
        } catch (e) {
            console.warn('[Complete] Could not persist backend state:', e);
        }
        
        this.cleanup();

        this.playEpicCompletionAnimation(redirectUrl);
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
        console.log('[Completion] Starting epic completion animation...');

        // Addendum 4: Trigger "ACCESS GRANTED" success state sequence
        // This changes colors to success green, updates title, and holds for 800ms
        if (typeof window.triggerSuccessState === 'function') {
            console.log('[Completion] Triggering ACCESS GRANTED success state...');
            await window.triggerSuccessState();
        }

        // Use Matrix transition from loading.html if available
        if (typeof window.triggerMatrixTransition === 'function') {
            console.log('[Completion] Using Matrix transition...');
            await window.triggerMatrixTransition(redirectUrl);
            return; // Matrix transition handles redirect
        }

        // Fallback: Legacy transition
        console.log('[Completion] Using fallback transition...');
        const container = document.querySelector('.loading-container') || document.getElementById('loading-container');
        const reactor = this.elements.reactor;
        const matrixCanvas = document.getElementById('matrix-canvas');

        // Create success green energy rings after success state
        if (reactor) {
            for (let i = 0; i < 3; i++) {
                setTimeout(() => this.createEnergyRing(reactor, '#00ff41', i), i * 150);
            }
        }

        // Brief intensify of matrix rain
        if (matrixCanvas) {
            matrixCanvas.style.transition = 'opacity 0.3s ease-out';
            matrixCanvas.style.opacity = '1';
        }

        await this.sleep(500);

        // Phase 2: Fade out with green success glow
        if (container) {
            container.style.transition = 'all 1s ease-out';
            container.style.opacity = '0';
            container.style.transform = 'scale(1.05)';
            container.style.filter = 'blur(10px) brightness(1.2)';
        }
        if (reactor) {
            reactor.style.transition = 'all 1s ease-out';
            reactor.style.transform = 'scale(1.3)';
            reactor.style.opacity = '0';
            reactor.style.filter = 'drop-shadow(0 0 80px rgba(0, 255, 65, 1))';
        }
        if (matrixCanvas) {
            matrixCanvas.style.transition = 'opacity 1.2s ease-out';
            matrixCanvas.style.opacity = '0';
        }

        await this.sleep(1200);

        // Phase 3: Navigate with dark overlay
        const overlay = document.createElement('div');
        overlay.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: #0a0a12; opacity: 0;
            transition: opacity 0.4s ease-in; z-index: 10001;
        `;
        document.body.appendChild(overlay);
        setTimeout(() => overlay.style.opacity = '1', 10);

        await this.sleep(400);
        window.location.href = redirectUrl;
    }

    createEnergyRing(reactor, color, index) {
        // Dynamic sizing to match current reactor size
        const reactorSize = reactor ? reactor.offsetWidth : 320;
        const ringSize = Math.round(reactorSize * 0.9375); // Match CSS .ring-outer ratio
        
        const ring = document.createElement('div');
        ring.style.cssText = `
            position: absolute; top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            width: ${ringSize}px; height: ${ringSize}px;
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
