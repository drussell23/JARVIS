/**
 * JARVIS Advanced Loading Manager v87.0 - Trinity Ultra Edition
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
 * ╔══════════════════════════════════════════════════════════════════════════════╗
 * ║  v87.0 TRINITY ULTRA FRONTEND ENHANCEMENTS                                   ║
 * ╠══════════════════════════════════════════════════════════════════════════════╣
 * ║  NEW in v87.0:                                                                ║
 * ║  1. ✅ ML-BASED ETA DISPLAY        - "30s remaining" with confidence         ║
 * ║  2. ✅ SEQUENCE NUMBER TRACKING    - Detect & request missed updates         ║
 * ║  3. ✅ REDIRECT GRACE PERIOD       - 2.5s animation delay at 100%           ║
 * ║  4. ✅ UNIFIED HEALTH POLLING      - Trinity component health status         ║
 * ║  5. ✅ SUPERVISOR HEARTBEAT CHECK  - Detect supervisor crashes               ║
 * ║  6. ✅ WEBSOCKET PONG HANDLER      - Respond to heartbeat messages           ║
 * ║  7. ✅ PARALLEL COMPONENT PROGRESS - Individual J-Prime/Reactor bars         ║
 * ║  8. ✅ OFFLINE MODE DETECTION      - Smart error handling for dead server    ║
 * ║  9. ✅ NETWORK CONDITION DETECTION - Adaptive retry based on network         ║
 * ║  10.✅ SMART CACHING STRATEGY      - Minimize redundant requests             ║
 * ║  11.✅ CLOCK SKEW DETECTION        - Warn on time discrepancies              ║
 * ║  12.✅ PROGRESSIVE ENHANCEMENT     - Works offline, enhances when online     ║
 * ╚══════════════════════════════════════════════════════════════════════════════╝
 *
 * Core Features (v5.0-v5.2):
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
 * - Zero-Touch autonomous update stage tracking
 * - Dead Man's Switch monitoring display
 * - Update validation progress
 * - DMS rollback stage handling
 * - Intelligent frontend unavailability handling
 * - "Continue to Backend" button when frontend is slow/unavailable
 * - frontend_optional URL parameter support
 * - Automatic fallback after 60s stuck at 95%+
 * - Clear user instructions when frontend not running
 * - Retry button for frontend reconnection
 */

// v225.0: Phase label mapping for v2 init_progress protocol timeline
const PHASE_LABELS = {
    'importing_ml_libraries': { short: 'ML', full: 'ML Libraries' },
    'initializing_bridge': { short: 'Bridge', full: 'Bridge Init' },
    'initializing_trinity': { short: 'Trinity', full: 'Trinity Sync' },
    'initializing_agi_hub': { short: 'AGI', full: 'AGI Hub' },
    'initializing_neural_orchestrator': { short: 'Neural', full: 'Neural Orch.' },
    'resolving_model': { short: 'Model', full: 'Model Resolve' },
    'configuring_hardware': { short: 'HW', full: 'Hardware Config' },
    'loading_model': { short: 'Load', full: 'Model Loading' },
    'marking_ready': { short: 'Ready', full: 'Finalizing' }
};

class JARVISLoadingManager {
    constructor() {
        // v210.0: Enhanced port detection with dynamic discovery
        // ROOT CAUSE FIX: Instead of relying on potentially stale values,
        // we now discover the actual running ports dynamically.
        
        // Priority 1: Server-injected ports (most reliable when page is freshly served)
        const injectedLoadingPort = window.JARVIS_LOADING_SERVER_PORT;
        const injectedFrontendPort = window.JARVIS_FRONTEND_PORT;
        const injectedBackendPort = window.JARVIS_BACKEND_PORT;
        
        // Priority 2: Window location (may be stale if page is cached)
        const currentPort = parseInt(window.location.port) || (window.location.protocol === 'https:' ? 443 : 80);
        const isUnifiedSupervisor = currentPort === 8080 || window.location.pathname.includes('unified');

        // v210.0: Define all possible loading server ports to try
        // This enables dynamic discovery when injected ports fail
        this._loadingServerCandidatePorts = [
            injectedLoadingPort,  // Server-injected (highest priority)
            currentPort,          // Current page port
            8080,                 // Default unified_supervisor loading port
            8081, 8082, 8083,     // Alternate loading ports
            3001,                 // Legacy loading port
        ].filter(p => p && p > 0); // Remove nulls and zeros
        
        // Remove duplicates while preserving order
        this._loadingServerCandidatePorts = [...new Set(this._loadingServerCandidatePorts)];

        // Start with best guess, will be updated by discovery
        const loadingServerPort = injectedLoadingPort || currentPort || 8080;
        
        if (injectedLoadingPort) {
            console.log(`[v210.0] Using server-injected loading port: ${injectedLoadingPort}`);
        } else {
            console.warn(`[v210.0] No server-injected port, will try discovery: [${this._loadingServerCandidatePorts.join(', ')}]`);
        }
        
        // v210.0: Track port discovery state
        this._portDiscoveryAttempted = false;
        this._discoveredLoadingPort = null;

        this.config = {
            // v186.0: Use server-injected port when available
            loadingServerPort: loadingServerPort,
            backendPort: injectedBackendPort || 8010,
            mainAppPort: injectedFrontendPort || 3000,
            wsProtocol: window.location.protocol === 'https:' ? 'wss:' : 'ws:',
            httpProtocol: window.location.protocol,
            hostname: window.location.hostname || 'localhost',
            // v181.0: Track if running under unified supervisor
            isUnifiedSupervisor: isUnifiedSupervisor,
            // v5.2: Intelligent frontend handling
            // When true, system can complete startup even if frontend is unavailable
            // This is set dynamically from supervisor or via URL parameter
            frontendOptional: this.getFrontendOptionalFromURL(),
            // Maximum time to wait for frontend before falling back to backend-only mode
            // v6.0: Increased from 90s to 120s - webpack compilation can take 30-60+ seconds
            frontendWaitTimeoutMs: 120000, // 120 seconds
            // After this many seconds of waiting for frontend, show helpful message
            frontendSlowThresholdSecs: 30,
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
                expectedProgress: [68, 72],
                substeps: ['Loading ECAPA-TDNN (192-dim)', 'Initializing speaker verification', 'Multi-tier thresholds (70%/85%/95%)']
            },
            'voice_bio_liveness': {
                name: 'Anti-Spoofing & Liveness Detection',
                icon: '🛡️',
                phase: 'initialization',
                expectedProgress: [72, 74],
                substeps: ['Replay attack detection', 'Deepfake detection', 'Liveness verification (99.8% accuracy)']
            },
            'voice_bio_cache': {
                name: 'Speaker Recognition Cache',
                icon: '🔐',
                phase: 'initialization',
                expectedProgress: [74, 75],
                substeps: ['Loading voiceprints (59 samples)', 'ChromaDB voice patterns', 'Behavioral biometrics ready']
            },
            'narrator_init': {
                name: 'Intelligent Voice Narrator',
                icon: '🗣️',
                phase: 'initialization',
                expectedProgress: [75, 76],
                substeps: ['Voice synthesis ready', 'Claude integration', 'Contextual announcements enabled']
            },
            'cost_optimization_init': {
                name: 'Cost Optimization & Helicone',
                icon: '💰',
                phase: 'initialization',
                expectedProgress: [76, 78],
                substeps: ['Helicone integration', 'Intelligent caching', 'API cost tracking']
            },
            'cross_repo_init': {
                name: 'Cross-Repository Intelligence',
                icon: '🔗',
                phase: 'initialization',
                expectedProgress: [78, 80],
                substeps: ['Connecting to JARVIS Prime (8002)', 'Reactor Core link', 'Neural Mesh coordination']
            },
            // === v80.0: PROJECT TRINITY CROSS-REPO LOADING ===
            'v80_cross_repo_init': {
                name: 'Trinity Cross-Repo System',
                icon: '🔺',
                phase: 'initialization',
                expectedProgress: [86, 88],
                substeps: ['Health monitor active', 'Circuit breakers online', 'Startup coordinator ready']
            },
            'trinity_infrastructure': {
                name: 'Trinity Infrastructure',
                icon: '🏗️',
                phase: 'initialization',
                expectedProgress: [80, 82],
                substeps: ['Creating Trinity directories', 'Cloud SQL check']
            },
            'trinity_jarvis_body': {
                name: 'Trinity: JARVIS Body',
                icon: '🦾',
                phase: 'initialization',
                expectedProgress: [82, 85],
                substeps: ['Execution layer online', 'Vision ready', 'Actions enabled']
            },
            'trinity_cross_repo_parallel': {
                name: 'Trinity: J-Prime + Reactor',
                icon: '🧠⚡',
                phase: 'initialization',
                expectedProgress: [85, 90],
                substeps: ['J-Prime Mind connecting', 'Reactor-Core Nerves connecting', 'Parallel startup']
            },
            'trinity_sync': {
                name: 'Trinity: Full Sync',
                icon: '🔄',
                phase: 'initialization',
                expectedProgress: [90, 92],
                substeps: ['Heartbeat synchronization', 'Event bus connected']
            },
            'cloud_ecapa': {
                name: 'Cloud ECAPA Service',
                icon: '☁️',
                phase: 'initialization',
                expectedProgress: [68, 70],
                substeps: ['Connecting to Cloud Run', 'Warming ML endpoint']
            },
            'speaker_cache': {
                name: 'Speaker Recognition Cache',
                icon: '🔐',
                phase: 'initialization',
                expectedProgress: [70, 72],
                substeps: ['Fast-path cache', 'Loading recent speakers']
            },
            'autonomous_systems': {
                name: 'Autonomous Systems',
                icon: '🤖',
                phase: 'initialization',
                expectedProgress: [80, 82],
                substeps: ['Orchestrator', 'Service mesh']
            },

            // === v5.0: TWO-TIER AGENTIC SECURITY ===
            'two_tier_init': {
                name: 'Two-Tier Security',
                icon: '🔐',
                phase: 'initialization',
                expectedProgress: [82, 84],
                substeps: ['Initializing security layers']
            },
            'two_tier_watchdog': {
                name: 'Agentic Watchdog',
                icon: '👁️',
                phase: 'initialization',
                expectedProgress: [82, 83],
                substeps: ['Heartbeat monitor', 'Kill switch', 'Activity tracker']
            },
            'two_tier_router': {
                name: 'Tiered Router',
                icon: '🛡️',
                phase: 'initialization',
                expectedProgress: [83, 84],
                substeps: ['Tier 1 (safe)', 'Tier 2 (agentic)', 'Intent classifier']
            },
            'two_tier_vbia': {
                name: 'VBIA Security',
                icon: '🎤',
                phase: 'initialization',
                expectedProgress: [84, 85],
                substeps: ['70% threshold (Tier 1)', '85% threshold (Tier 2)', 'Liveness check']
            },
            'two_tier_ready': {
                name: 'Security Ready',
                icon: '✅',
                phase: 'initialization',
                expectedProgress: [85, 86],
                substeps: ['All security layers active']
            },

            'module_prewarming': {
                name: 'Module Pre-Warming',
                icon: '🔥',
                phase: 'initialization',
                expectedProgress: [86, 88],
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
            },

            // ===================================================================
            // v9.4: ADVANCED INTELLIGENCE STAGES
            // Data Flywheel, JARVIS-Prime, Learning Goals, Reactor-Core, Neural Mesh
            // ===================================================================

            // === Data Flywheel (Experience Collection & Training Data) ===
            'flywheel_init': {
                name: 'Data Flywheel Init',
                icon: '🔄',
                phase: 'initialization',
                expectedProgress: [72, 74],
                substeps: ['Initializing experience collection', 'Connecting to training DB']
            },
            'flywheel_ready': {
                name: 'Data Flywheel Active',
                icon: '🌀',
                phase: 'initialization',
                expectedProgress: [74, 76],
                substeps: ['Experience capture active', 'Training pipeline connected'],
                detailFields: ['experiences', 'training_schedule', 'quality_threshold']
            },

            // === JARVIS-Prime (Tier-0 Local Brain) ===
            'jarvis_prime_init': {
                name: 'JARVIS-Prime Init',
                icon: '🧠',
                phase: 'initialization',
                expectedProgress: [76, 78],
                substeps: ['Loading local model', 'Initializing inference engine']
            },
            'jarvis_prime_loading_model': {
                name: 'Loading Model',
                icon: '📥',
                phase: 'initialization',
                expectedProgress: [78, 82],
                substeps: ['Downloading model weights', 'Loading into memory']
            },
            'jarvis_prime_ready': {
                name: 'JARVIS-Prime Active',
                icon: '🧠',
                phase: 'initialization',
                expectedProgress: [82, 84],
                substeps: ['Local inference ready', 'Tier-0 routing enabled'],
                detailFields: ['tier', 'model', 'memory_mb', 'cloud_fallback']
            },

            // === Learning Goals Discovery (Auto-learning Topics) ===
            'learning_goals_init': {
                name: 'Learning Goals Init',
                icon: '📚',
                phase: 'initialization',
                expectedProgress: [84, 85],
                substeps: ['Initializing topic discovery', 'Loading goal queue']
            },
            'learning_goals_discovering': {
                name: 'Discovering Topics',
                icon: '🔍',
                phase: 'initialization',
                expectedProgress: [85, 86],
                substeps: ['Analyzing conversations', 'Extracting learning topics']
            },
            'learning_goals_discovered': {
                name: 'Learning Topics Active',
                icon: '🎯',
                phase: 'initialization',
                expectedProgress: [86, 87],
                substeps: ['Topic discovery enabled', 'Auto-learning active'],
                detailFields: ['active_topics', 'queued_topics', 'sources']
            },

            // === Reactor-Core Training (Model Fine-tuning) ===
            'reactor_core_init': {
                name: 'Reactor-Core Init',
                icon: '⚛️',
                phase: 'initialization',
                expectedProgress: [87, 88],
                substeps: ['Initializing training orchestrator', 'Connecting to GCS']
            },
            'reactor_core_training': {
                name: 'Model Training',
                icon: '🔥',
                phase: 'training',
                expectedProgress: [0, 100],
                substeps: ['Fine-tuning in progress', 'Optimizing model'],
                detailFields: ['epoch', 'loss', 'progress', 'eta']
            },
            'reactor_core_ready': {
                name: 'Reactor-Core Ready',
                icon: '⚛️',
                phase: 'initialization',
                expectedProgress: [88, 89],
                substeps: ['Training pipeline active', 'Scheduled training enabled'],
                detailFields: ['next_training', 'experiences_queued', 'model_version']
            },

            // === Model Manager (Auto-download & Memory-aware Selection) ===
            'model_manager_init': {
                name: 'Model Manager Init',
                icon: '📦',
                phase: 'initialization',
                expectedProgress: [75, 76],
                substeps: ['Initializing model manager', 'Checking available models']
            },
            'model_manager_checking': {
                name: 'Checking Models',
                icon: '🔍',
                phase: 'initialization',
                expectedProgress: [76, 77],
                substeps: ['Scanning model directory', 'Checking reactor-core models']
            },
            'model_manager_downloading': {
                name: 'Downloading Model',
                icon: '📥',
                phase: 'initialization',
                expectedProgress: [77, 81],
                substeps: ['Downloading model weights', 'Please wait...'],
                detailFields: ['model', 'size_mb', 'progress', 'source']
            },
            'model_manager_ready': {
                name: 'Model Manager Ready',
                icon: '✅',
                phase: 'initialization',
                expectedProgress: [81, 82],
                substeps: ['Model loaded', 'Ready for inference'],
                detailFields: ['model', 'size_mb', 'source', 'memory_used']
            },

            // === Neural Mesh (Multi-Agent Coordination) ===
            'neural_mesh_init': {
                name: 'Neural Mesh Init',
                icon: '🕸️',
                phase: 'initialization',
                expectedProgress: [89, 90],
                substeps: ['Starting coordinator', 'Initializing communication bus']
            },
            'neural_mesh_agents_loading': {
                name: 'Loading Agents',
                icon: '🤖',
                phase: 'initialization',
                expectedProgress: [90, 92],
                substeps: ['Registering production agents', 'Starting agent pool']
            },
            'neural_mesh_bridge': {
                name: 'JARVIS Bridge',
                icon: '🌉',
                phase: 'initialization',
                expectedProgress: [92, 93],
                substeps: ['Connecting JARVIS systems', 'Cross-system integration']
            },
            'neural_mesh_ready': {
                name: 'Neural Mesh Active',
                icon: '🕸️',
                phase: 'initialization',
                expectedProgress: [93, 94],
                substeps: ['Multi-agent system online', 'Knowledge graph active'],
                detailFields: ['agents', 'messages', 'knowledge_entries', 'workflows']
            },

            // === UAE (Unified Awareness Engine) ===
            'uae_init': {
                name: 'UAE Initializing',
                icon: '👁️',
                phase: 'initialization',
                expectedProgress: [70, 71],
                substeps: ['Starting awareness engine', 'Loading vision models']
            },
            'uae_ready': {
                name: 'UAE Active',
                icon: '👁️',
                phase: 'initialization',
                expectedProgress: [71, 72],
                substeps: ['Screen awareness active', 'Chain-of-thought enabled'],
                detailFields: ['mode', 'vision_model', 'capture_interval']
            },

            // === SAI (Spatial Awareness Intelligence) ===
            'sai_init': {
                name: 'SAI Initializing',
                icon: '🗺️',
                phase: 'initialization',
                expectedProgress: [68, 69],
                substeps: ['Starting spatial awareness', 'Connecting to yabai']
            },
            'sai_ready': {
                name: 'SAI Active',
                icon: '🗺️',
                phase: 'initialization',
                expectedProgress: [69, 70],
                substeps: ['Window tracking active', 'Workspace awareness enabled'],
                detailFields: ['spaces', 'windows', 'focused_app']
            },

            // ===================================================================
            // v181.0: UNIFIED SUPERVISOR KERNEL STAGES
            // These stages are broadcast by unified_supervisor.py (Unified System Kernel)
            // When running via: python3 unified_supervisor.py
            // ===================================================================
            'loading': {
                name: 'Loading Server Ready',
                icon: '🚀',
                phase: 'cleanup',
                expectedProgress: [0, 5],
                substeps: ['Loading page initialized', 'Preparing system startup'],
                isUnifiedSupervisor: true
            },
            'preflight': {
                name: 'Preflight Checks',
                icon: '✈️',
                phase: 'cleanup',
                expectedProgress: [5, 15],
                substeps: ['Acquiring startup lock', 'Cleaning zombies', 'IPC server init', 'Health monitors'],
                isUnifiedSupervisor: true
            },
            'resources': {
                name: 'Resource Initialization',
                icon: '📦',
                phase: 'initialization',
                expectedProgress: [15, 30],
                substeps: ['Docker containers', 'GCP resources', 'Storage validation', 'Port allocation'],
                isUnifiedSupervisor: true
            },
            'backend': {
                name: 'Backend Server',
                icon: '⚡',
                phase: 'initialization',
                expectedProgress: [30, 50],
                substeps: ['Starting uvicorn', 'FastAPI initialization', 'Health endpoint ready'],
                isUnifiedSupervisor: true
            },
            'intelligence': {
                name: 'Intelligence Layer',
                icon: '🧠',
                phase: 'initialization',
                expectedProgress: [50, 65],
                substeps: ['ML model registry', 'Cost optimizer', 'Intent routing', 'Goal inference'],
                isUnifiedSupervisor: true
            },
            'parallel_init': {
                name: 'Parallel Initialization',
                icon: '⚡',
                phase: 'initialization',
                expectedProgress: [65, 70],
                substeps: ['Trinity + Enterprise running concurrently', 'Async parallel execution'],
                isUnifiedSupervisor: true
            },
            'trinity': {
                name: 'Trinity Integration',
                icon: '🔺',
                phase: 'initialization',
                expectedProgress: [70, 85],
                substeps: ['JARVIS Body (execution)', 'J-Prime Mind (reasoning)', 'Reactor-Core Nerves (learning)'],
                isUnifiedSupervisor: true,
                components: ['jarvis', 'prime', 'reactor']
            },
            'enterprise': {
                name: 'Enterprise Services',
                icon: '🏢',
                phase: 'initialization',
                expectedProgress: [85, 95],
                substeps: ['CloudSQL proxy', 'Voice biometrics (background)', 'Semantic cache', 'WebSocket hub'],
                isUnifiedSupervisor: true
            },
            'frontend': {
                name: 'Frontend Transition',
                icon: '🌐',
                phase: 'ready',
                expectedProgress: [95, 98],
                substeps: ['Starting React frontend', 'Transitioning from loading page'],
                isUnifiedSupervisor: true
            },

            // ===================================================================
            // TRINITY COMPONENT STATUS (shown in loading page)
            // ===================================================================
            'trinity_jarvis': {
                name: 'JARVIS Body',
                icon: '🦾',
                phase: 'initialization',
                expectedProgress: [70, 75],
                substeps: ['Execution layer', 'Vision system', 'Action handlers'],
                isUnifiedSupervisor: true
            },
            'trinity_prime': {
                name: 'J-Prime Mind',
                icon: '🧠',
                phase: 'initialization',
                expectedProgress: [75, 82],
                substeps: ['Local LLM inference', 'Reasoning engine', 'Context management'],
                isUnifiedSupervisor: true
            },
            'trinity_reactor': {
                name: 'Reactor-Core Nerves',
                icon: '⚡',
                phase: 'initialization',
                expectedProgress: [82, 88],
                substeps: ['Training pipeline', 'Model fine-tuning', 'Continuous learning'],
                isUnifiedSupervisor: true
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
            maxLogEntries: 50,
            // v5.0: Two-Tier Agentic Security state
            twoTierSecurity: {
                watchdogReady: false,
                routerReady: false,
                vbiaAdapterReady: false,
                tier1Operational: false,
                tier2Operational: false,
                watchdogStatus: 'initializing',
                watchdogMode: 'idle',
                overallStatus: 'initializing'
            },

            // ===================================================================
            // v9.4: Advanced Intelligence State
            // ===================================================================

            // Data Flywheel (Experience Collection)
            flywheel: {
                active: false,
                experiences: 0,
                trainingSchedule: 'none',
                qualityThreshold: 0.3,
                status: 'inactive'
            },

            // JARVIS-Prime (Tier-0 Local Brain)
            jarvisPrime: {
                active: false,
                tier: 'unknown',
                model: 'none',
                memoryMb: 0,
                cloudFallback: false,
                status: 'inactive'
            },

            // Learning Goals Discovery
            learningGoals: {
                active: false,
                activeTopics: 0,
                queuedTopics: 0,
                sources: [],
                lastDiscovery: null,
                status: 'inactive'
            },

            // Reactor-Core Training
            reactorCore: {
                active: false,
                training: false,
                epoch: 0,
                loss: 0,
                progress: 0,
                eta: null,
                nextTraining: null,
                experiencesQueued: 0,
                modelVersion: 'none',
                status: 'inactive'
            },

            // Model Manager
            modelManager: {
                active: false,
                model: 'none',
                sizeMb: 0,
                source: 'unknown',
                memoryUsed: 0,
                downloadProgress: 0,
                status: 'idle'
            },

            // Neural Mesh (Multi-Agent Coordination)
            neuralMesh: {
                active: false,
                production: false,
                agents: 0,
                agentsOnline: 0,
                messages: 0,
                knowledgeEntries: 0,
                workflows: 0,
                bridgeStatus: 'disconnected',
                status: 'inactive'
            },

            // UAE (Unified Awareness Engine)
            uae: {
                active: false,
                mode: 'standard',
                visionModel: 'default',
                captureInterval: 1000,
                status: 'inactive'
            },

            // SAI (Spatial Awareness Intelligence)
            sai: {
                active: false,
                spaces: 0,
                windows: 0,
                focusedApp: null,
                status: 'inactive'
            },

            // v6.2: Voice Biometric Authentication System
            voiceBiometrics: {
                active: false,
                status: 'idle',
                ecapaStatus: 'not_loaded',
                ecapaBackend: 'unknown',
                embeddingDimensions: 192,
                livenessEnabled: false,
                antiSpoofingReady: false,
                replayDetectionReady: false,
                deepfakeDetectionReady: false,
                livenessAccuracy: 99.8,
                speakerCacheStatus: 'empty',
                cachedSamples: 0,
                targetSamples: 59,
                cachePopulationPercent: 0,
                tier1Threshold: 70.0,
                tier2Threshold: 85.0,
                highSecurityThreshold: 95.0,
                chromadbVoicePatterns: false,
                behavioralBiometricsReady: false
            },

            // v6.2: Intelligent Voice Narrator
            narrator: {
                active: false,
                status: 'idle',
                enabled: true,
                voiceEnabled: false,
                contextualMessages: true,
                lastAnnouncement: null,
                announcementCount: 0,
                milestonesAnnounced: [],
                claudeIntegration: false,
                langfuseTracking: false
            },

            // v6.3: Cost Optimization & Helicone Integration
            costOptimization: {
                active: false,
                status: 'idle',
                heliconeEnabled: false,
                totalApiCalls: 0,
                cachedCalls: 0,
                cacheHitRate: 0.0,
                estimatedCostUsd: 0.0,
                estimatedSavingsUsd: 0.0,
                cachingEnabled: true,
                promptOptimization: false,
                modelRouting: false
            },

            // v6.3: Cross-Repository Intelligence Coordination
            crossRepo: {
                active: false,
                status: 'idle',
                jarvisPrimeConnected: false,
                jarvisPrimePort: 8002,
                jarvisPrimeHealth: 'unknown',
                jarvisPrimeTier: 'unknown',
                reactorCoreConnected: false,
                trainingPipelineActive: false,
                modelSyncEnabled: false,
                neuralMeshActive: false,
                neuralMeshCoordinator: 'offline',
                registeredAgents: 0,
                activeConversations: 0,
                stateSyncEnabled: false,
                lastSyncTimestamp: null,
                syncFailures: 0
            },

            // ===================================================================
            // v87.0: Trinity Ultra Advanced Intelligence State
            // ===================================================================

            // Predictive ETA (ML-based time estimation)
            predictiveETA: {
                etaSeconds: null,
                confidence: null,
                estimatedCompletion: null,
                predictionMethod: 'unknown',
                lastUpdate: null
            },

            // Sequence Number Tracking (detect missed updates)
            sequencing: {
                lastSequenceNumber: -1,
                missedUpdates: 0,
                totalUpdates: 0,
                lastSequenceCheck: Date.now()
            },

            // Redirect Grace Period (animation delay)
            redirectGrace: {
                completionTimestamp: null,
                gracePeriodSeconds: 2.5,
                redirectReady: false,
                secondsUntilRedirect: null,
                countdownActive: false
            },

            // Unified Health Monitoring (Trinity components)
            unifiedHealth: {
                overallHealth: 0,
                state: 'unknown',  // healthy, degraded, critical
                components: {},
                circuitBreakers: {},
                lastPoll: null,
                pollInterval: 5000  // 5 seconds
            },

            // Supervisor Heartbeat Monitoring
            supervisorHeartbeat: {
                alive: true,
                lastUpdate: null,
                timeSinceUpdate: 0,
                timeoutThreshold: 60000,  // 60 seconds
                crashed: false,
                recoveredAt: null
            },

            // v263.0: Dynamic Startup Timeout (negotiated with backend)
            // Replaces hardcoded 600s limit with progress-aware timeout
            startupTimeout: {
                // Backend-negotiated max startup time (default 10min = old behavior until backend tells us)
                negotiatedMs: 600000,
                // Whether we've received timeout from backend yet
                negotiated: false,
                // Stall detection: time of last progress advancement
                lastProgressAdvanceTime: Date.now(),
                // Last progress value seen (for detecting advancement)
                lastProgressValue: 0,
                // How long progress must be stalled before considering timeout (2 min)
                stallThresholdMs: 120000,
                // Whether timeout error has been shown (prevent repeated shows)
                timeoutShown: false,
            },

            // Parallel Component Progress (individual bars)
            parallelComponents: {
                jarvisPrime: {
                    name: 'JARVIS Prime',
                    progress: 0,
                    status: 'pending',
                    phase: 'init',
                    eta: null
                },
                reactorCore: {
                    name: 'Reactor Core',
                    progress: 0,
                    status: 'pending',
                    phase: 'init',
                    eta: null
                }
            },

            // Network Condition Detection
            networkCondition: {
                type: 'unknown',  // wifi, cellular, ethernet, offline
                effectiveType: '4g',  // slow-2g, 2g, 3g, 4g
                downlink: 10,  // Mbps
                rtt: 50,  // ms
                saveData: false,
                lastCheck: Date.now()
            },

            // Smart Caching Strategy
            cache: {
                lastProgressData: null,
                lastProgressTimestamp: 0,
                cacheHitRate: 0,
                cacheHits: 0,
                cacheMisses: 0
            },

            // Clock Skew Detection
            clockSkew: {
                detected: false,
                serverTimestamp: null,
                clientTimestamp: null,
                skewMs: 0,
                threshold: 5000,  // 5 seconds
                lastCheck: Date.now()
            },

            // Offline Mode Detection
            offlineMode: {
                active: false,
                detectedAt: null,
                lastSuccessfulRequest: Date.now(),
                consecutiveFailures: 0,
                threshold: 3  // Consider offline after 3 failures
            }
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
        console.log('[JARVIS] Loading Manager v210.0 starting...');
        console.log(`[Config] Initial loading server guess: ${this.config.hostname}:${this.config.loadingServerPort}`);
        console.log('[Mode] DISPLAY - trusts start_system.py as authority');

        // ═══════════════════════════════════════════════════════════════════════════
        // v210.0: PHASE 1 - INTELLIGENT PORT DISCOVERY
        // Run port discovery FIRST to determine actual server locations
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[v210.0] Phase 1: Discovering server ports...');
        const loadingServerPort = await this.discoverLoadingServerPort();
        const loadingServerFound = this._discoveredLoadingPort !== null;
        
        if (loadingServerFound) {
            console.log(`[v210.0] ✅ Loading server found on port ${loadingServerPort}`);
        } else {
            console.warn('[v210.0] ⚠️ No loading server responding - checking if system is already running...');
        }

        // ═══════════════════════════════════════════════════════════════════════════
        // v210.0: PHASE 2 - SYSTEM READINESS CHECK
        // If no loading server, check if system is already running
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[v210.0] Phase 2: Checking system readiness...');
        
        // Quick check: Skip loading if system already fully ready (backend + frontend + WebSocket)
        const fullyReady = await this.checkFullSystemReady();
        if (fullyReady) {
            console.log('[JARVIS] ✅ Full system already ready - skipping loading screen');
            await this.quickRedirectToApp();
            return;
        }

        // v210.0: If no loading server but backend is healthy, startup is complete
        if (!loadingServerFound) {
            const backendHealthy = await this.checkBackendHealth();
            if (backendHealthy) {
                console.log('[v210.0] ✅ Backend healthy, no loading server - startup is COMPLETE');
                
                // Check if frontend is ready
                const frontendReady = await this.checkFrontendReady();
                if (frontendReady) {
                    console.log('[v210.0] ✅ Frontend also ready - redirecting');
                    await this.quickRedirectToApp();
                    return;
                } else {
                    console.log('[v210.0] ℹ️ Frontend not ready yet - showing completion with fallback');
                    
                    // v210.0: CRITICAL FIX - Set progress to 100% since startup is complete
                    // The loading server being down means startup finished
                    this.state.progress = 95;
                    this.state.targetProgress = 100;
                    this.state.stage = 'complete';
                    this.state.message = 'JARVIS backend is ready! Waiting for frontend...';
                    
                    // Show backend fallback immediately
                    this._showBackendFallbackEarly = true;
                }
            } else {
                console.log('[v210.0] ℹ️ No loading server and backend not healthy - waiting for startup...');
                // Neither is ready - keep polling until something responds
                this._retryLoadingServerDiscovery = true;
            }
        }

        // ═══════════════════════════════════════════════════════════════════════════
        // v210.0: PHASE 3 - UI INITIALIZATION
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[v210.0] Phase 3: Initializing UI...');
        this.createParticles();
        this.createDetailedStatusPanel();
        
        // v210.0: Update progress bar immediately with current state
        // This ensures progress is shown even if set before UI initialization
        this.updateProgressBar();
        if (this.state.message) {
            if (this.elements.statusMessage) {
                this.elements.statusMessage.textContent = this.state.message;
            }
        }
        
        // Start smooth progress animation
        this.startSmoothProgress();

        // Make details panel visible immediately and add initial log
        if (this.elements.detailsPanel) {
            this.elements.detailsPanel.classList.add('visible');
        }
        this.addLogEntry('System', 'Loading manager initialized', 'info');
        
        if (loadingServerFound) {
            this.addLogEntry('System', `Connecting to loading server on port ${loadingServerPort}...`, 'info');
        } else {
            this.addLogEntry('System', 'Loading server not found - monitoring backend directly...', 'warning');
            // v210.0: If backend is healthy, add log entry
            if (this._showBackendFallbackEarly) {
                this.addLogEntry('System', 'Backend is ready! Waiting for frontend...', 'success');
            }
        }

        // ═══════════════════════════════════════════════════════════════════════════
        // v210.0: PHASE 4 - CONNECTION ESTABLISHMENT
        // Only try WebSocket if loading server was found
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[v210.0] Phase 4: Establishing connections...');
        
        if (loadingServerFound) {
            // Connect to loading server - this is our PRIMARY source
            await this.connectWebSocket();
            this.startPolling();
        } else {
            console.log('[v210.0] Skipping WebSocket - no loading server, using backend health polling only');
        }
        
        // v263.0: Fetch startup config for dynamic timeout negotiation
        this.fetchStartupConfig();

        this.startHealthMonitoring();

        // v210.0: Show backend fallback button early if loading server not found
        if (this._showBackendFallbackEarly) {
            this.showBackendFallbackButton();
        }

        // CRITICAL: Start backend health polling for stuck detection & fallback
        // This enables automatic redirect when system is already running but loading server is down
        this.startBackendHealthPolling();

        // ===================================================================
        // v87.0 ULTRA: TRINITY ADVANCED INTELLIGENCE INITIALIZATION
        // ===================================================================

        // Initialize network condition detection and adaptive polling
        this.detectNetworkCondition();

        // Initialize smart caching for performance optimization
        this.initSmartCache();

        // Initialize circuit breakers for resilience
        this.initCircuitBreakers();

        // Initialize cross-tab synchronization
        this.initCrossTabSync();

        // Start periodic unified health polling (Trinity components: J-Prime, Reactor-Core)
        setInterval(() => this.pollUnifiedHealth(), 5000);

        // Start supervisor heartbeat monitoring (detect supervisor crashes)
        setInterval(() => this.checkSupervisorHeartbeat(), 10000);

        console.log('[v87.0] ✅ Trinity Ultra intelligence systems initialized');
        console.log('[v87.0] 🚀 Ultra features: Smart Cache, Circuit Breakers, Cross-Tab Sync, Atomic Updates');

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
         * v211.0: Check if backend, WebSocket, AND frontend are all ready.
         * 
         * CRITICAL FIX: Previously only checked HTTP endpoints, causing redirect
         * before WebSocket was stable. This resulted in "OFFLINE - SEARCHING FOR BACKEND"
         * errors in the React frontend.
         * 
         * Now verifies:
         * 1. Backend HTTP health endpoint responds
         * 2. Backend WebSocket endpoint accepts connections (NEW!)
         * 3. Frontend HTTP is accessible
         * 
         * Only returns true if ALL services are verified operational.
         */
        const backendPort = this.config.backendPort || 8010;
        
        // Run all checks in parallel for speed
        const [backendReady, wsReady, frontendReady] = await Promise.all([
            this.checkBackendHealth(),
            this.testWebSocket(`ws://${this.config.hostname}:${backendPort}/ws`),
            this.checkFrontendReady()
        ]);

        console.log(`[Health] Full system check - Backend: ${backendReady}, WebSocket: ${wsReady}, Frontend: ${frontendReady}`);
        
        // ALL three must be ready
        return backendReady && wsReady && frontendReady;
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

    async quickRedirectToApp() {
        /**
         * v211.0: Quick redirect to main app with final verification.
         * 
         * CRITICAL FIX: Added final WebSocket verification before redirect.
         * This prevents "OFFLINE - SEARCHING FOR BACKEND" by ensuring
         * WebSocket connectivity is confirmed before sending user to React app.
         */
        // Prevent duplicate redirects
        if (this.state.redirecting) {
            console.log('[Redirect] Already redirecting, skipping duplicate call');
            return;
        }
        
        this.state.redirecting = true;

        // Update UI to show verifying state
        if (this.elements.statusMessage) {
            this.elements.statusMessage.textContent = 'Verifying connection...';
        }
        if (this.elements.progressBar) {
            this.elements.progressBar.style.width = '98%';
        }
        if (this.elements.subtitle) {
            this.elements.subtitle.textContent = 'VERIFYING';
        }

        // v211.0: Final WebSocket verification before redirect
        const backendPort = this.config.backendPort || 8010;
        const wsUrl = `ws://${this.config.hostname}:${backendPort}/ws`;
        
        console.log(`[Redirect] Final WebSocket verification at ${wsUrl}...`);
        const wsReady = await this.testWebSocket(wsUrl);
        
        if (!wsReady) {
            console.warn('[Redirect] WebSocket not ready - delaying redirect');
            this.state.redirecting = false;
            
            // Show warning and retry after delay
            if (this.elements.statusMessage) {
                this.elements.statusMessage.textContent = 'WebSocket connecting...';
            }
            if (this.elements.subtitle) {
                this.elements.subtitle.textContent = 'CONNECTING';
            }
            
            // Retry after 2 seconds
            setTimeout(() => this.quickRedirectToApp(), 2000);
            return;
        }

        console.log('[Redirect] ✓ WebSocket verified - proceeding with redirect');
        
        // Update UI to show ready state
        if (this.elements.statusMessage) {
            this.elements.statusMessage.textContent = 'JARVIS is online!';
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
        // v6.0: Increased from 30s to 60s - webpack can take 30-60+ seconds to compile
        const STUCK_TIMEOUT_MS = 60000; // 60 seconds stuck = start checking

        const pollInterval = setInterval(async () => {
            try {
                // ═══════════════════════════════════════════════════════════════════════════
                // v5.2 STUCK DETECTION: Intelligent completion with frontend fallback
                // If at 95%+ for too long, intelligently complete with fallback options
                // ═══════════════════════════════════════════════════════════════════════════
                if (this.state.progress >= 95 && this.state.progress < 100 && !this.state.redirecting) {
                    if (!stuckAt95Timer) {
                        stuckAt95Timer = Date.now();
                        console.log('[Stuck Detection] Progress at 95%+, starting timer...');
                    } else {
                        const stuckDuration = Date.now() - stuckAt95Timer;
                        console.log(`[Stuck Detection] At ${Math.round(this.state.progress)}% for ${Math.round(stuckDuration / 1000)}s`);

                        if (stuckDuration >= STUCK_TIMEOUT_MS) {
                            // We're stuck - check if services are actually ready
                            const backendHealthy = await this.checkBackendHealth();
                            const frontendReady = await this.checkFrontendReady();

                            if (backendHealthy && frontendReady) {
                                // Both services ready - complete and redirect to frontend
                                console.log('[JARVIS] ✅ Stuck detection: All services ready, forcing completion');
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
                            } else if (backendHealthy && !frontendReady) {
                                // v6.0: Backend ready but frontend still compiling
                                console.log('[Stuck Detection] Backend ready, frontend compiling...');

                                // v6.0: Show fallback button after 90s stuck (increased from 30s)
                                // Webpack compilation can take 60-90+ seconds
                                if (stuckDuration >= 90000) {
                                    this.showBackendFallbackButton();
                                }

                                // v6.0: Only give up if frontendOptional is set (increased from 60s to 120s)
                                // CRITICAL: Don't auto-redirect to backend just because webpack is slow
                                if (this.config.frontendOptional || stuckDuration >= 120000) {
                                    console.log('[JARVIS] ✅ Stuck detection: Backend ready, completing without frontend');
                                    clearInterval(pollInterval);

                                    // Show helpful message instead of redirecting to broken frontend
                                    await this.handleFrontendUnavailable();
                                    return;
                                }
                            }
                        }
                    }
                } else {
                    // Reset timer if progress changes
                    stuckAt95Timer = null;
                }

                // ═══════════════════════════════════════════════════════════════════════════
                // v210.0: FALLBACK: Standard health polling
                // Enhanced to update progress when loading server is not running
                // ═══════════════════════════════════════════════════════════════════════════
                const timeSinceLoadingUpdate = Date.now() - this.state.lastUpdate;
                const loadingServerActive = timeSinceLoadingUpdate < 10000 && this._discoveredLoadingPort !== null;
                
                if (loadingServerActive && this.state.progress < 95) {
                    // Loading server is active and we're not stuck, let it handle completion
                    consecutiveHealthyChecks = 0;
                    return;
                }

                const backendHealthy = await this.checkBackendHealth();

                if (backendHealthy) {
                    consecutiveHealthyChecks++;
                    console.log(`[Health Polling] Backend healthy (${consecutiveHealthyChecks}/${requiredConsecutiveChecks})`);

                    // v210.0: Update progress based on backend health when no loading server
                    if (!loadingServerActive && this.state.progress < 95) {
                        // Simulate progress based on consecutive healthy checks
                        // Each check represents ~10-15% progress towards completion
                        const estimatedProgress = Math.min(95, 70 + (consecutiveHealthyChecks * 8));
                        if (estimatedProgress > this.state.progress) {
                            console.log(`[v210.0] Backend healthy - updating progress: ${this.state.progress}% → ${estimatedProgress}%`);
                            this.state.targetProgress = estimatedProgress;
                            this.state.message = `Backend ready (verifying: ${consecutiveHealthyChecks}/${requiredConsecutiveChecks})...`;
                            this.updateProgressBar();
                        }
                    }

                    if (consecutiveHealthyChecks >= requiredConsecutiveChecks) {
                        const frontendReady = await this.checkFrontendReady();

                        if (!frontendReady) {
                            console.log('[Health Polling] Backend ready but frontend not yet available, waiting...');
                            // v210.0: Set progress to 95% and show it's waiting for frontend
                            if (this.state.progress < 95) {
                                this.state.targetProgress = 95;
                                this.state.message = 'Backend ready, waiting for frontend...';
                                this.updateProgressBar();
                            }
                            this.showBackendFallbackButton();
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
                    
                    // v210.0: Try to re-discover loading server if backend isn't healthy yet
                    if (this._retryLoadingServerDiscovery && !this._discoveredLoadingPort) {
                        console.log('[v210.0] Retrying loading server discovery...');
                        this._portDiscoveryAttempted = false;
                        const port = await this.discoverLoadingServerPort();
                        if (this._discoveredLoadingPort) {
                            console.log(`[v210.0] ✅ Loading server found on retry: ${port}`);
                            // Connect to the discovered loading server
                            await this.connectWebSocket();
                            this.startPolling();
                        }
                    }
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

    /**
     * v210.0: Discover which port the loading server is actually running on.
     * Tries candidate ports in order until one responds.
     */
    async discoverLoadingServerPort() {
        if (this._discoveredLoadingPort) {
            return this._discoveredLoadingPort;
        }

        console.log(`[v210.0] 🔍 Discovering loading server port from candidates: [${this._loadingServerCandidatePorts.join(', ')}]`);

        for (const port of this._loadingServerCandidatePorts) {
            try {
                const healthUrl = `${this.config.httpProtocol}//${this.config.hostname}:${port}/health`;
                console.log(`[v210.0] Trying port ${port}...`);
                
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 2000);
                
                const response = await fetch(healthUrl, {
                    method: 'GET',
                    signal: controller.signal,
                    mode: 'cors',
                    credentials: 'omit'
                });
                
                clearTimeout(timeoutId);
                
                if (response.ok) {
                    console.log(`[v210.0] ✅ Loading server found on port ${port}`);
                    this._discoveredLoadingPort = port;
                    this.config.loadingServerPort = port;
                    return port;
                }
            } catch (error) {
                // Port not responding, try next
                console.debug(`[v210.0] Port ${port} not responding: ${error.name}`);
            }
        }

        // No port found - fall back to configured port
        console.warn(`[v210.0] ⚠️ No loading server found, using configured port: ${this.config.loadingServerPort}`);
        return this.config.loadingServerPort;
    }

    async connectWebSocket() {
        if (this.state.reconnectAttempts >= this.config.reconnect.maxAttempts) {
            console.warn('[WebSocket] Max reconnection attempts reached');
            // CRITICAL: Check if system is already running before giving up
            // This handles the case where loading server shut down but system is healthy
            const systemReady = await this.checkFullSystemReady();
            if (systemReady) {
                console.log('[WebSocket] ✅ System already running - redirecting');
                await this.quickRedirectToApp();
            } else {
                console.warn('[WebSocket] System not ready - will rely on health polling fallback');
            }
            return;
        }

        try {
            // v210.0: Discover actual loading server port if not already done
            if (!this._portDiscoveryAttempted) {
                this._portDiscoveryAttempted = true;
                await this.discoverLoadingServerPort();
            }

            // v210.0: Connect to discovered/configured loading server
            const wsUrl = `${this.config.wsProtocol}//${this.config.hostname}:${this.config.loadingServerPort}/ws/progress`;
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

                    // ═══════════════════════════════════════════════════════════
                    // v87.0: WEBSOCKET PONG HANDLER
                    // ═══════════════════════════════════════════════════════════
                    if (data.type === 'heartbeat') {
                        // Respond with pong to keep connection alive
                        this.state.ws.send(JSON.stringify({
                            type: 'pong',
                            timestamp: Date.now(),
                            client_time: Date.now()
                        }));
                        console.debug('[v87.0] ♥️  Sent pong response');
                        return; // Don't process heartbeat as progress update
                    }

                    if (data.type !== 'pong') {
                        // ═══════════════════════════════════════════════════════
                        // v185.0: NORMALIZE WEBSOCKET PAYLOAD
                        // Handle nested data.data structure from loading server
                        // ═══════════════════════════════════════════════════════
                        let normalized;
                        if (data.data) {
                            // WebSocket wraps payload in data.data
                            const inner = data.data;
                            normalized = {
                                stage: inner.stage || inner.phase,
                                message: inner.message,
                                progress: inner.progress,
                                eta: inner.eta,  // v225.0: ML-based ETA from loading server
                                metadata: {
                                    ...(inner.metadata || {}),
                                    // v263.0: Propagate startup_timeout_ms from ProgressState top-level
                                    startup_timeout_ms: inner.startup_timeout_ms || (inner.metadata || {}).startup_timeout_ms,
                                    components: inner.components || (inner.metadata || {}).components,
                                    trinity: inner.trinity || (inner.metadata || {}).trinity,
                                    trinity_ready: inner.trinity_ready ?? (inner.metadata || {}).trinity_ready,
                                    init_progress: inner.init_progress || (inner.metadata || {}).init_progress  // v225.0: Prime v2 phase data
                                }
                            };
                        } else {
                            normalized = data;
                        }
                        this.handleProgressUpdate(normalized);
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

        // EARLY SYSTEM CHECK: After 3 failed attempts, check if system is already running
        // This provides faster recovery when loading server is down but system is healthy
        if (this.state.reconnectAttempts === 3) {
            console.log('[WebSocket] 3 attempts failed - checking if system already running...');
            this.checkFullSystemReady().then(async (ready) => {
                if (ready && !this.state.redirecting) {
                    console.log('[WebSocket] ✅ System already running (early check) - redirecting');
                    await this.quickRedirectToApp();
                }
            });
        }

        // v210.0: Re-discover port after 5 failed attempts (loading server may have restarted on different port)
        if (this.state.reconnectAttempts === 5 || this.state.reconnectAttempts === 10 || this.state.reconnectAttempts === 15) {
            console.log(`[v210.0] ${this.state.reconnectAttempts} attempts failed - re-discovering loading server port...`);
            this._portDiscoveryAttempted = false;
            this._discoveredLoadingPort = null;
        }

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
            // v186.0: Use canonical endpoint /api/progress (alias /api/startup-progress also works)
            const url = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}/api/progress`;

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

        // v87.0: Track successful request for offline mode detection
        this.recordSuccessfulRequest();

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

        // ═══════════════════════════════════════════════════════════════════
        // v185.0: NORMALIZE POLLING PAYLOAD
        // Ensure consistent format before calling handleProgressUpdate
        // ═══════════════════════════════════════════════════════════════════
        // Map phase -> stage for consistency
        if (data.phase && !data.stage) {
            data.stage = data.phase;
        }
        // Ensure metadata exists and includes top-level components/trinity
        data.metadata = data.metadata || {};
        if (data.components) {
            data.metadata.components = data.metadata.components || data.components;
        }
        if (data.trinity) {
            data.metadata.trinity = data.metadata.trinity || data.trinity;
        }
        if (data.trinity_ready !== undefined) {
            data.metadata.trinity_ready = data.metadata.trinity_ready ?? data.trinity_ready;
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

        // v87.0: Track failed request for offline mode detection
        this.recordFailedRequest();

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

            // v263.0: Track progress advancement for stall detection
            if (effectiveProgress > this.state.startupTimeout.lastProgressValue) {
                this.state.startupTimeout.lastProgressAdvanceTime = Date.now();
                this.state.startupTimeout.lastProgressValue = effectiveProgress;
            }
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

            // v263.0: Accept dynamic startup timeout from backend
            // Check both metadata (from supervisor broadcasts) and top-level data
            // (from loading_server to_dict() on WebSocket connect/reconnect)
            const timeoutMs = Number(metadata.startup_timeout_ms || data.startup_timeout_ms);
            if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
                if (!this.state.startupTimeout.negotiated || timeoutMs > this.state.startupTimeout.negotiatedMs) {
                    // v242.1: Coerce to Number to prevent log injection (CWE-117)
                    console.log(`[v263.0] Startup timeout negotiated: ${Number(timeoutMs)}ms (${(timeoutMs / 60000).toFixed(1)}min)`);
                    this.state.startupTimeout.negotiatedMs = timeoutMs;
                    this.state.startupTimeout.negotiated = true;
                }
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

            // v5.0: Two-Tier Agentic Security state
            // Backend sends snake_case, we use camelCase internally
            if (metadata.two_tier) {
                const tt = metadata.two_tier;
                // Update state (handle both snake_case and camelCase from backend)
                this.state.twoTierSecurity = {
                    watchdogReady: tt.watchdog_ready || tt.watchdogReady || this.state.twoTierSecurity.watchdogReady,
                    routerReady: tt.router_ready || tt.routerReady || this.state.twoTierSecurity.routerReady,
                    vbiaAdapterReady: tt.vbia_adapter_ready || tt.vbiaAdapterReady || this.state.twoTierSecurity.vbiaAdapterReady,
                    tier1Operational: tt.tier1_operational || tt.tier1Operational || this.state.twoTierSecurity.tier1Operational,
                    tier2Operational: tt.tier2_operational || tt.tier2Operational || this.state.twoTierSecurity.tier2Operational,
                    watchdogStatus: tt.watchdog_status || tt.watchdogStatus || this.state.twoTierSecurity.watchdogStatus,
                    watchdogMode: tt.watchdog_mode || tt.watchdogMode || this.state.twoTierSecurity.watchdogMode,
                    overallStatus: tt.overall_status || tt.overallStatus || this.state.twoTierSecurity.overallStatus,
                    message: tt.message || ''
                };

                // Update Two-Tier Security UI
                this.updateTwoTierSecurityUI();

                // Log security status changes
                if (this.state.twoTierSecurity.overallStatus === 'operational') {
                    this.addLogEntry('Security', 'Two-Tier Security System operational', 'success');
                } else if (this.state.twoTierSecurity.overallStatus === 'partial') {
                    this.addLogEntry('Security', tt.message || 'Partial security ready', 'warning');
                }
            }

            // ===================================================================
            // v225.0: Prime v2 init_progress Protocol Handler
            // ===================================================================
            const initProgress = metadata.init_progress;
            if (initProgress && initProgress.protocol_version >= 2) {
                this._v2Progress = {
                    overallPct: initProgress.overall_pct,
                    currentPhase: initProgress.current_phase,
                    completedPhases: initProgress.completed_phases,
                    totalPhases: initProgress.total_phases,
                    phases: initProgress.phases || []
                };
                this.updatePhaseTimeline(this._v2Progress);
            }

            // v225.0: ETA Display from ML-based prediction
            if (data.eta && data.eta.seconds != null) {
                const etaEl = document.getElementById('eta-value');
                if (etaEl) {
                    const secs = Math.round(data.eta.seconds);
                    if (secs <= 0) {
                        etaEl.textContent = 'Soon';
                    } else if (secs > 60) {
                        etaEl.textContent = `~${Math.ceil(secs / 60)}m ${secs % 60}s`;
                    } else {
                        etaEl.textContent = `~${secs}s`;
                    }
                    // Show confidence if available
                    const etaDisplay = document.getElementById('eta-display');
                    if (etaDisplay && data.eta.confidence != null) {
                        etaDisplay.title = `Confidence: ${Math.round(data.eta.confidence * 100)}%`;
                    }
                }
            }

            // ===================================================================
            // v9.4: Advanced Intelligence Metadata Handling
            // ===================================================================

            // Data Flywheel (Experience Collection)
            if (metadata.flywheel) {
                const fw = metadata.flywheel;
                this.state.flywheel = {
                    active: fw.active || false,
                    experiences: fw.experiences || 0,
                    trainingSchedule: fw.training_schedule || fw.trainingSchedule || 'none',
                    qualityThreshold: fw.quality_threshold || fw.qualityThreshold || 0.3,
                    status: fw.status || 'inactive'
                };
                if (fw.active) {
                    this.addLogEntry('Flywheel', `Experience collection active: ${fw.experiences || 0} stored`, 'success');
                }
                this.updateAdvancedStatusPanel('flywheel');
            }

            // JARVIS-Prime (Tier-0 Local Brain)
            if (metadata.jarvis_prime || metadata.jarvisPrime) {
                const jp = metadata.jarvis_prime || metadata.jarvisPrime;
                this.state.jarvisPrime = {
                    active: jp.active || false,
                    tier: jp.tier || 'unknown',
                    model: jp.model || jp.model_name || 'none',
                    memoryMb: jp.memory_mb || jp.memoryMb || 0,
                    cloudFallback: jp.cloud_fallback || jp.cloudFallback || false,
                    status: jp.status || 'inactive'
                };
                if (jp.active) {
                    this.addLogEntry('Prime', `JARVIS-Prime ${jp.tier || 'local'}: ${jp.model || 'model'} loaded`, 'success');
                }
                this.updateAdvancedStatusPanel('jarvis_prime');
            }

            // Learning Goals Discovery
            if (metadata.learning_goals || metadata.learningGoals) {
                const lg = metadata.learning_goals || metadata.learningGoals;
                this.state.learningGoals = {
                    active: lg.active || false,
                    activeTopics: lg.active_topics || lg.activeTopics || 0,
                    queuedTopics: lg.queued_topics || lg.queuedTopics || 0,
                    sources: lg.sources || [],
                    lastDiscovery: lg.last_discovery || lg.lastDiscovery || null,
                    status: lg.status || 'inactive'
                };
                if (lg.active_topics || lg.activeTopics) {
                    this.addLogEntry('Learning', `${lg.active_topics || lg.activeTopics} active learning topics`, 'info');
                }
                this.updateAdvancedStatusPanel('learning_goals');
            }

            // Reactor-Core Training
            if (metadata.reactor_core || metadata.reactorCore) {
                const rc = metadata.reactor_core || metadata.reactorCore;
                this.state.reactorCore = {
                    active: rc.active || false,
                    training: rc.training || false,
                    epoch: rc.epoch || 0,
                    loss: rc.loss || 0,
                    progress: rc.progress || 0,
                    eta: rc.eta || null,
                    nextTraining: rc.next_training || rc.nextTraining || null,
                    experiencesQueued: rc.experiences_queued || rc.experiencesQueued || 0,
                    modelVersion: rc.model_version || rc.modelVersion || 'none',
                    status: rc.status || 'inactive'
                };
                if (rc.training) {
                    this.addLogEntry('Training', `Training epoch ${rc.epoch}: loss ${rc.loss?.toFixed(4) || 'N/A'}`, 'info');
                }
                this.updateAdvancedStatusPanel('reactor_core');
            }

            // Model Manager (Auto-download & Selection)
            if (metadata.model_manager || metadata.modelManager) {
                const mm = metadata.model_manager || metadata.modelManager;
                this.state.modelManager = {
                    active: mm.active || false,
                    model: mm.model || mm.model_name || 'none',
                    sizeMb: mm.size_mb || mm.sizeMb || 0,
                    source: mm.source || 'unknown',
                    memoryUsed: mm.memory_used || mm.memoryUsed || 0,
                    downloadProgress: mm.download_progress || mm.downloadProgress || 0,
                    status: mm.status || 'idle'
                };
                if (mm.status === 'downloading') {
                    this.addLogEntry('Model', `Downloading ${mm.model}: ${mm.download_progress || 0}%`, 'info');
                } else if (mm.status === 'ready') {
                    this.addLogEntry('Model', `Model ${mm.model} ready (${mm.size_mb}MB)`, 'success');
                }
                this.updateAdvancedStatusPanel('model_manager');
            }

            // Neural Mesh (Multi-Agent Coordination)
            if (metadata.neural_mesh || metadata.neuralMesh) {
                const nm = metadata.neural_mesh || metadata.neuralMesh;
                this.state.neuralMesh = {
                    active: nm.active || false,
                    production: nm.production || nm.production_mode || false,
                    agents: nm.agents || nm.agents_registered || 0,
                    agentsOnline: nm.agents_online || nm.agentsOnline || 0,
                    messages: nm.messages || nm.messages_published || 0,
                    knowledgeEntries: nm.knowledge_entries || nm.knowledgeEntries || 0,
                    workflows: nm.workflows || nm.workflows_completed || 0,
                    bridgeStatus: nm.bridge_status || nm.bridgeStatus || 'disconnected',
                    status: nm.status || 'inactive'
                };
                if (nm.active) {
                    this.addLogEntry('Mesh', `Neural Mesh: ${nm.agents || 0} agents, ${nm.knowledge_entries || 0} knowledge entries`, 'success');
                }
                this.updateAdvancedStatusPanel('neural_mesh');
            }

            // UAE (Unified Awareness Engine)
            if (metadata.uae) {
                const uae = metadata.uae;
                this.state.uae = {
                    active: uae.active || false,
                    mode: uae.mode || 'standard',
                    visionModel: uae.vision_model || uae.visionModel || 'default',
                    captureInterval: uae.capture_interval || uae.captureInterval || 1000,
                    status: uae.status || 'inactive'
                };
                if (uae.active) {
                    this.addLogEntry('UAE', 'Screen awareness active', 'success');
                }
            }

            // SAI (Spatial Awareness Intelligence)
            if (metadata.sai) {
                const sai = metadata.sai;
                this.state.sai = {
                    active: sai.active || false,
                    spaces: sai.spaces || 0,
                    windows: sai.windows || 0,
                    focusedApp: sai.focused_app || sai.focusedApp || null,
                    status: sai.status || 'inactive'
                };
                if (sai.active) {
                    this.addLogEntry('SAI', `Tracking ${sai.windows || 0} windows across ${sai.spaces || 0} spaces`, 'info');
                }
            }

            // ===================================================================
            // v6.2/v6.3: Enhanced Intelligence Metadata Handling
            // ===================================================================

            // Voice Biometric Authentication System
            if (metadata.voice_biometrics || metadata.voiceBiometrics) {
                const vb = metadata.voice_biometrics || metadata.voiceBiometrics;
                this.state.voiceBiometrics = {
                    active: vb.active || false,
                    status: vb.status || 'idle',
                    ecapaStatus: vb.ecapa_status || vb.ecapaStatus || 'not_loaded',
                    ecapaBackend: vb.ecapa_backend || vb.ecapaBackend || 'unknown',
                    embeddingDimensions: vb.embedding_dimensions || vb.embeddingDimensions || 192,
                    livenessEnabled: vb.liveness_enabled || vb.livenessEnabled || false,
                    antiSpoofingReady: vb.anti_spoofing_ready || vb.antiSpoofingReady || false,
                    replayDetectionReady: vb.replay_detection_ready || vb.replayDetectionReady || false,
                    deepfakeDetectionReady: vb.deepfake_detection_ready || vb.deepfakeDetectionReady || false,
                    livenessAccuracy: vb.liveness_accuracy || vb.livenessAccuracy || 99.8,
                    speakerCacheStatus: vb.speaker_cache_status || vb.speakerCacheStatus || 'empty',
                    cachedSamples: vb.cached_samples || vb.cachedSamples || 0,
                    targetSamples: vb.target_samples || vb.targetSamples || 59,
                    cachePopulationPercent: vb.cache_population_percent || vb.cachePopulationPercent || 0,
                    tier1Threshold: vb.tier1_threshold || vb.tier1Threshold || 70.0,
                    tier2Threshold: vb.tier2_threshold || vb.tier2Threshold || 85.0,
                    highSecurityThreshold: vb.high_security_threshold || vb.highSecurityThreshold || 95.0,
                    chromadbVoicePatterns: vb.chromadb_voice_patterns || vb.chromadbVoicePatterns || false,
                    behavioralBiometricsReady: vb.behavioral_biometrics_ready || vb.behavioralBiometricsReady || false
                };
                if (vb.status === 'ready') {
                    this.addLogEntry('VoiceBio', `ECAPA-TDNN loaded (${vb.embedding_dimensions || 192}-dim), Liveness: ${vb.liveness_accuracy || 99.8}%`, 'success');
                } else if (vb.anti_spoofing_ready) {
                    this.addLogEntry('VoiceBio', 'Anti-spoofing & liveness detection ready', 'success');
                } else if (vb.cached_samples) {
                    this.addLogEntry('VoiceBio', `Speaker cache: ${vb.cached_samples}/${vb.target_samples || 59} samples (${(vb.cache_population_percent || 0).toFixed(1)}%)`, 'info');
                }
                this.updateAdvancedStatusPanel('voice_biometrics');
            }

            // Intelligent Voice Narrator
            if (metadata.narrator) {
                const nr = metadata.narrator;
                this.state.narrator = {
                    active: nr.active || false,
                    status: nr.status || 'idle',
                    enabled: nr.enabled !== undefined ? nr.enabled : true,
                    voiceEnabled: nr.voice_enabled || nr.voiceEnabled || false,
                    contextualMessages: nr.contextual_messages || nr.contextualMessages !== undefined ? nr.contextualMessages : true,
                    lastAnnouncement: nr.last_announcement || nr.lastAnnouncement || null,
                    announcementCount: nr.announcement_count || nr.announcementCount || 0,
                    milestonesAnnounced: nr.milestones_announced || nr.milestonesAnnounced || [],
                    claudeIntegration: nr.claude_integration || nr.claudeIntegration || false,
                    langfuseTracking: nr.langfuse_tracking || nr.langfuseTracking || false
                };
                if (nr.status === 'ready') {
                    this.addLogEntry('Narrator', 'Intelligent voice announcements ready', 'success');
                } else if (nr.last_announcement) {
                    this.addLogEntry('Narrator', `Announcement: "${nr.last_announcement}"`, 'info');
                }
                this.updateAdvancedStatusPanel('narrator');
            }

            // Cost Optimization & Helicone Integration
            if (metadata.cost_optimization || metadata.costOptimization) {
                const co = metadata.cost_optimization || metadata.costOptimization;
                this.state.costOptimization = {
                    active: co.active || false,
                    status: co.status || 'idle',
                    heliconeEnabled: co.helicone_enabled || co.heliconeEnabled || false,
                    totalApiCalls: co.total_api_calls || co.totalApiCalls || 0,
                    cachedCalls: co.cached_calls || co.cachedCalls || 0,
                    cacheHitRate: co.cache_hit_rate || co.cacheHitRate || 0.0,
                    estimatedCostUsd: co.estimated_cost_usd || co.estimatedCostUsd || 0.0,
                    estimatedSavingsUsd: co.estimated_savings_usd || co.estimatedSavingsUsd || 0.0,
                    cachingEnabled: co.caching_enabled !== undefined ? co.caching_enabled : (co.cachingEnabled !== undefined ? co.cachingEnabled : true),
                    promptOptimization: co.prompt_optimization || co.promptOptimization || false,
                    modelRouting: co.model_routing || co.modelRouting || false
                };
                if (co.status === 'ready') {
                    const savings = co.estimated_savings_usd || co.estimatedSavingsUsd || 0;
                    const hitRate = co.cache_hit_rate || co.cacheHitRate || 0;
                    this.addLogEntry('CostOpt', `Helicone ready - ${hitRate.toFixed(1)}% cache hit rate, $${savings.toFixed(2)} saved`, 'success');
                } else if (co.helicone_enabled || co.heliconeEnabled) {
                    this.addLogEntry('CostOpt', 'Helicone integration active', 'success');
                }
                this.updateAdvancedStatusPanel('cost_optimization');
            }

            // Cross-Repository Intelligence Coordination
            if (metadata.cross_repo || metadata.crossRepo) {
                const cr = metadata.cross_repo || metadata.crossRepo;
                this.state.crossRepo = {
                    active: cr.active || false,
                    status: cr.status || 'idle',
                    jarvisPrimeConnected: cr.jarvis_prime_connected || cr.jarvisPrimeConnected || false,
                    jarvisPrimePort: cr.jarvis_prime_port || cr.jarvisPrimePort || 8002,
                    jarvisPrimeHealth: cr.jarvis_prime_health || cr.jarvisPrimeHealth || 'unknown',
                    jarvisPrimeTier: cr.jarvis_prime_tier || cr.jarvisPrimeTier || 'unknown',
                    reactorCoreConnected: cr.reactor_core_connected || cr.reactorCoreConnected || false,
                    trainingPipelineActive: cr.training_pipeline_active || cr.trainingPipelineActive || false,
                    modelSyncEnabled: cr.model_sync_enabled || cr.modelSyncEnabled || false,
                    neuralMeshActive: cr.neural_mesh_active || cr.neuralMeshActive || false,
                    neuralMeshCoordinator: cr.neural_mesh_coordinator || cr.neuralMeshCoordinator || 'offline',
                    registeredAgents: cr.registered_agents || cr.registeredAgents || 0,
                    activeConversations: cr.active_conversations || cr.activeConversations || 0,
                    stateSyncEnabled: cr.state_sync_enabled || cr.stateSyncEnabled || false,
                    lastSyncTimestamp: cr.last_sync_timestamp || cr.lastSyncTimestamp || null,
                    syncFailures: cr.sync_failures || cr.syncFailures || 0
                };
                if (cr.status === 'ready') {
                    const connections = [];
                    if (cr.jarvis_prime_connected || cr.jarvisPrimeConnected) connections.push(`Prime:${cr.jarvis_prime_port || cr.jarvisPrimePort || 8002}`);
                    if (cr.reactor_core_connected || cr.reactorCoreConnected) connections.push('Reactor');
                    if (cr.neural_mesh_active || cr.neuralMeshActive) connections.push(`Mesh:${cr.registered_agents || cr.registeredAgents || 0} agents`);
                    this.addLogEntry('CrossRepo', `Connected: ${connections.join(', ') || 'None'}`, 'success');
                } else if (cr.jarvis_prime_connected || cr.jarvisPrimeConnected) {
                    this.addLogEntry('CrossRepo', `JARVIS Prime connected (port ${cr.jarvis_prime_port || cr.jarvisPrimePort || 8002})`, 'success');
                } else if (cr.neural_mesh_active || cr.neuralMeshActive) {
                    this.addLogEntry('CrossRepo', `Neural Mesh active: ${cr.registered_agents || cr.registeredAgents || 0} agents`, 'info');
                }
                this.updateAdvancedStatusPanel('cross_repo');
            }

            // v80.0: PROJECT TRINITY Cross-Repo Loading System
            if (metadata.trinity_phase || metadata.trinityPhase || metadata.v80_enabled || metadata.v80Enabled) {
                const phase = metadata.trinity_phase || metadata.trinityPhase;
                const v80Enabled = metadata.v80_enabled || metadata.v80Enabled;
                const healthMonitorActive = metadata.health_monitor_active || metadata.healthMonitorActive;
                const startupCoordinatorReady = metadata.startup_coordinator_ready || metadata.startupCoordinatorReady;
                const circuitBreakers = metadata.circuit_breakers || metadata.circuitBreakers || [];
                const aggregateHealth = metadata.aggregate_health || metadata.aggregateHealth || {};

                // Update Trinity state
                this.state.trinity = {
                    v80Enabled: v80Enabled || false,
                    phase: phase || 'unknown',
                    healthMonitorActive: healthMonitorActive || false,
                    startupCoordinatorReady: startupCoordinatorReady || false,
                    circuitBreakers: circuitBreakers,
                    overallStatus: aggregateHealth.overall_status || aggregateHealth.overallStatus || 'unknown',
                    healthyComponents: aggregateHealth.healthy_components || aggregateHealth.healthyComponents || 0,
                    totalComponents: aggregateHealth.total_components || aggregateHealth.totalComponents || 3
                };

                // Log Trinity-specific updates
                if (phase) {
                    const phaseNames = {
                        'infrastructure': 'Infrastructure Setup',
                        'jarvis_body': 'JARVIS Body (Execution)',
                        'cross_repo_parallel': 'J-Prime + Reactor (Parallel)',
                        'trinity_sync': 'Trinity Sync',
                        'finalization': 'Finalization'
                    };
                    const phaseName = phaseNames[phase] || phase;
                    this.addLogEntry('Trinity', `Phase: ${phaseName}`, 'info');
                }

                if (healthMonitorActive) {
                    const status = aggregateHealth.overall_status || 'checking';
                    const statusEmoji = status === 'healthy' ? '✅' : status === 'degraded' ? '⚠️' : '❌';
                    this.addLogEntry('Trinity', `${statusEmoji} Health Monitor: ${status} (${circuitBreakers.length} breakers)`, status === 'healthy' ? 'success' : 'warning');
                }

                if (startupCoordinatorReady) {
                    this.addLogEntry('Trinity', '🚀 Startup Coordinator: Ready for parallel orchestration', 'success');
                }

                this.updateAdvancedStatusPanel('trinity');
            }

            // ===================================================================
            // v181.0: UNIFIED SUPERVISOR COMPONENT STATUS
            // Handle components metadata from unified_supervisor.py
            // ===================================================================
            if (metadata.components) {
                const components = metadata.components;

                // Initialize trinity components state if not exists
                if (!this.state.trinityComponents) {
                    this.state.trinityComponents = {
                        jarvis: { status: 'pending' },
                        prime: { status: 'pending' },
                        reactor: { status: 'pending' },
                        trinity: { status: 'pending' },
                        enterprise: { status: 'pending' },
                        overallProgress: 0
                    };
                }

                // Update component states
                if (components.trinity) {
                    this.state.trinityComponents.trinity = components.trinity;
                }
                if (components.jarvis) {
                    this.state.trinityComponents.jarvis = components.jarvis;
                }
                if (components.prime) {
                    this.state.trinityComponents.prime = components.prime;
                }
                if (components.reactor) {
                    this.state.trinityComponents.reactor = components.reactor;
                }
                if (components.enterprise) {
                    this.state.trinityComponents.enterprise = components.enterprise;
                }

                // v185.0: Map snake_case keys to expected camelCase/short names
                // unified_supervisor sends jarvis_prime and reactor_core
                if (components.jarvis_prime) {
                    this.state.trinityComponents.prime = {
                        ...components.jarvis_prime,
                        status: components.jarvis_prime.status === 'ready' ? 'complete' : components.jarvis_prime.status
                    };
                }
                if (components.reactor_core) {
                    this.state.trinityComponents.reactor = {
                        ...components.reactor_core,
                        status: components.reactor_core.status === 'ready' ? 'complete' : components.reactor_core.status
                    };
                }

                // Calculate overall Trinity progress
                const trinityPhases = ['jarvis', 'prime', 'reactor'];
                const completedTrinity = trinityPhases.filter(
                    p => this.state.trinityComponents[p]?.status === 'complete' ||
                        this.state.trinityComponents[p]?.status === 'ready'
                ).length;
                this.state.trinityComponents.overallProgress = Math.round((completedTrinity / trinityPhases.length) * 100);

                // Update Trinity UI
                this.updateTrinityComponentsUI();

                // Log Trinity component status changes
                for (const [name, comp] of Object.entries(components)) {
                    if (['jarvis', 'prime', 'reactor'].includes(name) && comp.status === 'complete') {
                        const labels = { jarvis: 'JARVIS Body', prime: 'J-Prime Mind', reactor: 'Reactor-Core' };
                        this.addLogEntry('Trinity', `${labels[name] || name} ready`, 'success');
                    }
                }
            }

            // v181.0: Handle parallel_duration from parallel phase execution
            if (metadata.parallel_duration !== undefined) {
                this.addLogEntry('Parallel', `Trinity + Enterprise completed in ${metadata.parallel_duration.toFixed(1)}s`, 'success');
            }

            // v181.0: Track Trinity/Enterprise success for status display
            if (metadata.trinity_success !== undefined) {
                this.state.trinitySuccess = metadata.trinity_success;
            }
            if (metadata.enterprise_success !== undefined) {
                this.state.enterpriseSuccess = metadata.enterprise_success;
            }

            // ===================================================================
            // v182.0: TRINITY READINESS VERIFICATION
            // Handle the new trinity metadata object from unified_supervisor
            // This provides structured data for Trinity component status
            // ===================================================================
            if (metadata.trinity) {
                const trinity = metadata.trinity;

                // Update trinityComponents state with v182.0 format
                if (!this.state.trinityComponents) {
                    this.state.trinityComponents = {
                        jarvis: { status: 'pending' },
                        prime: { status: 'pending' },
                        reactor: { status: 'pending' },
                        overallProgress: 0
                    };
                }

                // Map v182.0 trinity object to trinityComponents state
                if (trinity.jarvis_body) {
                    this.state.trinityComponents.jarvis = {
                        status: trinity.jarvis_body.status === 'ready' ? 'complete' : trinity.jarvis_body.status,
                        label: trinity.jarvis_body.label,
                        icon: trinity.jarvis_body.icon
                    };
                }
                if (trinity.jarvis_prime) {
                    this.state.trinityComponents.prime = {
                        status: trinity.jarvis_prime.status === 'ready' ? 'complete' : trinity.jarvis_prime.status,
                        label: trinity.jarvis_prime.label,
                        icon: trinity.jarvis_prime.icon,
                        message: trinity.jarvis_prime.message
                    };
                }
                if (trinity.reactor_core) {
                    this.state.trinityComponents.reactor = {
                        status: trinity.reactor_core.status === 'ready' ? 'complete' : trinity.reactor_core.status,
                        label: trinity.reactor_core.label,
                        icon: trinity.reactor_core.icon,
                        message: trinity.reactor_core.message
                    };
                }

                // v182.0: Track Trinity all_ready status
                this.state.trinityAllReady = trinity.all_ready === true;
                this.state.trinityComponents.overallProgress = trinity.progress || 0;

                // Update Trinity UI
                this.updateTrinityComponentsUI();

                // Log when all Trinity components are ready
                if (trinity.all_ready && !this.state.trinityReadyLogged) {
                    this.addLogEntry('Trinity', '🔺 All Trinity components ready', 'success');
                    this.state.trinityReadyLogged = true;
                }
            }

            // v182.0: Track trinity_ready flag from server
            if (metadata.trinity_ready !== undefined) {
                this.state.trinityAllReady = metadata.trinity_ready === true;
                if (metadata.trinity_ready && !this.state.trinityReadyLogged) {
                    this.addLogEntry('Trinity', '🔺 Trinity verification complete', 'success');
                    this.state.trinityReadyLogged = true;
                }
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

        // ═══════════════════════════════════════════════════════════════════════════
        // v87.0: TRINITY ULTRA ADVANCED INTELLIGENCE HANDLERS
        // ═══════════════════════════════════════════════════════════════════════════

        // v87.0: Predictive ETA Display
        if (data.predictive_eta) {
            this.state.predictiveETA = {
                etaSeconds: data.predictive_eta.eta_seconds,
                confidence: data.predictive_eta.confidence,
                estimatedCompletion: data.predictive_eta.estimated_completion,
                predictionMethod: data.predictive_eta.prediction_method || 'ema_historical_fusion',
                lastUpdate: Date.now()
            };
            this.updateETADisplay();
        }

        // v87.0: Sequence Number Tracking
        if (data.sequence_number !== undefined) {
            const currentSeq = data.sequence_number;
            const lastSeq = this.state.sequencing.lastSequenceNumber;

            if (lastSeq >= 0) {
                const expectedSeq = lastSeq + 1;
                const missed = currentSeq - expectedSeq;

                if (missed > 0) {
                    console.warn(`[v87.0] ⚠️  Missed ${missed} updates! (expected ${expectedSeq}, got ${currentSeq})`);
                    this.state.sequencing.missedUpdates += missed;
                }
            }

            this.state.sequencing.lastSequenceNumber = currentSeq;
            this.state.sequencing.totalUpdates++;
            this.state.sequencing.lastSequenceCheck = Date.now();

            // v87.0: ULTRA - Auto-recover missed updates
            if (missed > 0 && missed <= 10) {
                // Only auto-recover if gap is reasonable (≤10 updates)
                console.log(`[v87.0] 🔄 Auto-recovering ${missed} missed updates...`);
                this.requestMissedUpdates(lastSeq).catch(err => {
                    console.error('[v87.0] Failed to recover missed updates:', err);
                });
            } else if (missed > 10) {
                console.error(`[v87.0] ⚠️  Large gap detected (${missed} updates) - full resync needed`);
                this.fullResync().catch(err => {
                    console.error('[v87.0] Full resync failed:', err);
                });
            }
        }

        // v87.0: Redirect Grace Period Handling
        if (data.redirect_ready !== undefined) {
            this.state.redirectGrace.redirectReady = data.redirect_ready;
            this.state.redirectGrace.secondsUntilRedirect = data.seconds_until_redirect;

            if (data.redirect_ready === false && data.seconds_until_redirect !== null) {
                this.displayRedirectCountdown(data.seconds_until_redirect);
            } else if (data.redirect_ready === true && this.state.progress >= 100) {
                console.log('[v87.0] ✅ Redirect grace period complete - safe to redirect');
            }
        }

        // v87.0: Clock Skew Detection
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
                    console.warn(`[v87.0] ⏰ Clock skew detected: ${(skewMs / 1000).toFixed(1)}s difference`);
                    this.state.clockSkew.detected = true;
                    this.addLogEntry('System', `Clock skew: ${(skewMs / 1000).toFixed(1)}s`, 'warning');
                }
            } else {
                this.state.clockSkew.detected = false;
            }
        }

        // v87.0 ULTRA: Broadcast progress update to other tabs
        if (data.sequence_number !== undefined) {
            this.broadcastToTabs('progress-update', data);
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

        // v87.0 ULTRA: Update parallel component progress UI
        this.updateParallelComponentUI();

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

    /**
     * Update the Two-Tier Security UI elements based on current state.
     * v5.0: Displays watchdog, router, VBIA, and tier status in loading panel.
     */
    updateTwoTierSecurityUI() {
        const state = this.state.twoTierSecurity;

        // Update security badge
        const securityBadge = document.getElementById('security-badge');
        if (securityBadge) {
            securityBadge.className = 'security-badge';
            if (state.overallStatus === 'operational') {
                securityBadge.classList.add('operational');
                securityBadge.textContent = 'Operational';
            } else if (state.overallStatus === 'error') {
                securityBadge.classList.add('error');
                securityBadge.textContent = 'Error';
            } else {
                securityBadge.classList.add('initializing');
                securityBadge.textContent = 'Initializing';
            }
        }

        // Helper to update a security item
        const updateSecurityItem = (elementId, isReady, statusText) => {
            const el = document.getElementById(elementId);
            if (!el) return;
            el.className = 'security-item';
            if (isReady) {
                el.classList.add('ready');
            } else {
                el.classList.add('pending');
            }
            const statusEl = el.querySelector('.security-status');
            if (statusEl) {
                statusEl.textContent = statusText;
            }
        };

        // Update individual security items
        updateSecurityItem('watchdog-status', state.watchdogReady,
            state.watchdogReady ? `Active (${state.watchdogMode || 'monitoring'})` : 'Initializing...');
        updateSecurityItem('router-status', state.routerReady,
            state.routerReady ? 'Ready' : 'Initializing...');
        updateSecurityItem('vbia-status', state.vbiaAdapterReady,
            state.vbiaAdapterReady ? 'Ready (85%)' : 'Initializing...');
        updateSecurityItem('auth-status', state.tier1Operational || state.tier2Operational,
            (state.tier1Operational || state.tier2Operational) ? 'Verified' : 'Initializing...');

        // Update tier indicators
        const tier1 = document.getElementById('tier1-indicator');
        const tier2 = document.getElementById('tier2-indicator');

        if (tier1) {
            tier1.className = 'tier-indicator tier1';
            if (!state.tier1Operational) {
                tier1.classList.add('inactive');
            }
        }

        if (tier2) {
            tier2.className = 'tier-indicator tier2';
            if (!state.tier2Operational) {
                tier2.classList.add('inactive');
            }
        }
    }

    /**
     * v181.0: Update Trinity Components UI
     * Updates the Trinity section in loading.html with component status
     * from unified_supervisor.py broadcasts.
     */
    updateTrinityComponentsUI() {
        const state = this.state.trinityComponents;
        if (!state) return;

        // Update Trinity badge
        const trinityBadge = document.getElementById('trinity-badge');
        if (trinityBadge) {
            trinityBadge.className = 'trinity-badge';
            const allReady = ['jarvis', 'prime', 'reactor'].every(
                c => state[c]?.status === 'complete' || state[c]?.status === 'ready'
            );
            const anyRunning = ['jarvis', 'prime', 'reactor'].some(
                c => state[c]?.status === 'running'
            );

            if (allReady) {
                trinityBadge.classList.add('ready');
                trinityBadge.textContent = 'Ready';
            } else if (anyRunning) {
                trinityBadge.classList.add('initializing');
                trinityBadge.textContent = 'Initializing';
            } else {
                trinityBadge.classList.add('pending');
                trinityBadge.textContent = 'Pending';
            }
        }

        // Helper to update Trinity component item
        const updateTrinityItem = (elementId, componentState) => {
            const el = document.getElementById(elementId);
            if (!el) return;

            const status = componentState?.status || 'pending';
            el.className = 'trinity-item';

            if (status === 'complete' || status === 'ready') {
                el.classList.add('ready');
            } else if (status === 'running') {
                el.classList.add('running');
            } else if (status === 'error' || status === 'failed') {
                el.classList.add('error');
            } else {
                el.classList.add('pending');
            }

            const statusEl = el.querySelector('.trinity-status-text');
            if (statusEl) {
                const statusLabels = {
                    'pending': 'Pending',
                    'running': 'Starting...',
                    'complete': 'Ready',
                    'ready': 'Ready',
                    'error': 'Error',
                    'failed': 'Failed',
                    'warning': 'Warning',
                    'skipped': 'Not Configured',    // Distinct from stopped - component path not set
                    'unavailable': 'Unavailable',   // Component unavailable
                    'stopped': 'Stopped'            // Distinct from skipped - was running but stopped
                };
                statusEl.textContent = statusLabels[status] || status;
            }
        };

        // Update individual Trinity items
        updateTrinityItem('jarvis-status', state.jarvis);
        updateTrinityItem('prime-status', state.prime);
        updateTrinityItem('reactor-status', state.reactor);

        // Update progress bar
        const progressFill = document.getElementById('trinity-progress-fill');
        const progressText = document.getElementById('trinity-progress-text');

        if (progressFill) {
            progressFill.style.width = `${state.overallProgress || 0}%`;
        }
        if (progressText) {
            progressText.textContent = `${state.overallProgress || 0}%`;
        }
    }

    /**
     * v9.4: Update Advanced Intelligence Status Panel
     * Dynamically creates/updates UI for flywheel, prime, learning goals, reactor-core,
     * model manager, and neural mesh status.
     */
    updateAdvancedStatusPanel(systemName) {
        // Get or create the advanced status container
        let advancedPanel = document.getElementById('advanced-intelligence-panel');
        if (!advancedPanel) {
            advancedPanel = this.createAdvancedIntelligencePanel();
            if (!advancedPanel) return;
        }

        // Update specific system based on name
        switch (systemName) {
            case 'flywheel':
                this.updateFlywheelUI(advancedPanel);
                break;
            case 'jarvis_prime':
                this.updateJarvisPrimeUI(advancedPanel);
                break;
            case 'learning_goals':
                this.updateLearningGoalsUI(advancedPanel);
                break;
            case 'reactor_core':
                this.updateReactorCoreUI(advancedPanel);
                break;
            case 'model_manager':
                this.updateModelManagerUI(advancedPanel);
                break;
            case 'neural_mesh':
                this.updateNeuralMeshUI(advancedPanel);
                break;
            case 'trinity':
                this.updateTrinityUI(advancedPanel);
                break;
        }
    }

    /**
     * v80.0: Update PROJECT TRINITY Status UI
     */
    updateTrinityUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let trinityCard = document.getElementById('trinity-card');
        if (!trinityCard) {
            trinityCard = document.createElement('div');
            trinityCard.id = 'trinity-card';
            trinityCard.className = 'ai-system-card';
            grid.appendChild(trinityCard);
        }

        const trinity = this.state.trinity || {};
        const isActive = trinity.v80Enabled || trinity.healthMonitorActive;
        const status = trinity.overallStatus || 'initializing';
        const statusClass = status === 'healthy' ? 'success' : status === 'degraded' ? 'warning' : 'initializing';
        const statusEmoji = status === 'healthy' ? '✅' : status === 'degraded' ? '⚠️' : '🔄';

        trinityCard.innerHTML = `
            <div class="ai-card-header">
                <span class="ai-card-icon">🔺</span>
                <span class="ai-card-name">PROJECT TRINITY</span>
                <span class="ai-card-status ${statusClass}">${statusEmoji}</span>
            </div>
            <div class="ai-card-details">
                <div class="ai-stat">
                    <span class="stat-label">Status:</span>
                    <span class="stat-value ${statusClass}">${status}</span>
                </div>
                <div class="ai-stat">
                    <span class="stat-label">Phase:</span>
                    <span class="stat-value">${trinity.phase || 'unknown'}</span>
                </div>
                <div class="ai-stat">
                    <span class="stat-label">Components:</span>
                    <span class="stat-value">${trinity.healthyComponents || 0}/${trinity.totalComponents || 3}</span>
                </div>
                <div class="ai-stat">
                    <span class="stat-label">Health Monitor:</span>
                    <span class="stat-value ${trinity.healthMonitorActive ? 'success' : 'inactive'}">${trinity.healthMonitorActive ? 'Active' : 'Inactive'}</span>
                </div>
                <div class="ai-stat">
                    <span class="stat-label">Circuit Breakers:</span>
                    <span class="stat-value">${(trinity.circuitBreakers || []).length}</span>
                </div>
            </div>
        `;
    }

    /**
     * Create the advanced intelligence status panel container
     */
    createAdvancedIntelligencePanel() {
        const panelContent = document.getElementById('panel-content');
        if (!panelContent) return null;

        // Check if already exists
        let panel = document.getElementById('advanced-intelligence-panel');
        if (panel) return panel;

        // Create panel
        panel = document.createElement('div');
        panel.id = 'advanced-intelligence-panel';
        panel.className = 'advanced-intelligence';
        panel.innerHTML = `
            <div class="security-header" style="margin-top: 15px; padding-top: 12px; border-top: 1px solid rgba(0, 255, 65, 0.2);">
                <span class="security-title">🧠 Advanced Intelligence</span>
                <span class="security-badge initializing" id="ai-badge">Initializing</span>
            </div>
            <div class="ai-systems-grid" id="ai-systems-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px;"></div>
        `;

        // Insert after two-tier security section
        const twoTier = document.getElementById('two-tier-security');
        if (twoTier) {
            twoTier.after(panel);
        } else {
            panelContent.appendChild(panel);
        }

        return panel;
    }

    /**
     * Update Data Flywheel UI
     */
    updateFlywheelUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let item = document.getElementById('flywheel-status-item');
        if (!item) {
            item = document.createElement('div');
            item.id = 'flywheel-status-item';
            item.className = 'security-item';
            item.innerHTML = `
                <span class="security-icon">🌀</span>
                <div class="security-info">
                    <div class="security-label">Data Flywheel</div>
                    <div class="security-status" id="flywheel-status-text">Initializing...</div>
                </div>
            `;
            grid.appendChild(item);
        }

        const state = this.state.flywheel;
        const statusText = item.querySelector('#flywheel-status-text');
        if (state.active) {
            item.className = 'security-item ready';
            statusText.textContent = `${state.experiences} experiences`;
        } else {
            item.className = 'security-item pending';
            statusText.textContent = state.status || 'Initializing...';
        }

        this.updateAIBadge();
    }

    /**
     * Update JARVIS-Prime UI
     */
    updateJarvisPrimeUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let item = document.getElementById('prime-status-item');
        if (!item) {
            item = document.createElement('div');
            item.id = 'prime-status-item';
            item.className = 'security-item';
            item.innerHTML = `
                <span class="security-icon">🧠</span>
                <div class="security-info">
                    <div class="security-label">JARVIS-Prime</div>
                    <div class="security-status" id="prime-status-text">Initializing...</div>
                </div>
            `;
            grid.appendChild(item);
        }

        const state = this.state.jarvisPrime;
        const statusText = item.querySelector('#prime-status-text');
        if (state.active) {
            item.className = 'security-item ready';
            const tier = state.tier === 'local' ? 'Local' : state.tier === 'cloud' ? 'Cloud' : state.tier;
            statusText.textContent = `${tier}: ${state.model}`;
        } else {
            item.className = 'security-item pending';
            statusText.textContent = state.status || 'Initializing...';
        }

        this.updateAIBadge();
    }

    /**
     * Update Learning Goals UI
     */
    updateLearningGoalsUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let item = document.getElementById('learning-status-item');
        if (!item) {
            item = document.createElement('div');
            item.id = 'learning-status-item';
            item.className = 'security-item';
            item.innerHTML = `
                <span class="security-icon">📚</span>
                <div class="security-info">
                    <div class="security-label">Learning Goals</div>
                    <div class="security-status" id="learning-status-text">Initializing...</div>
                </div>
            `;
            grid.appendChild(item);
        }

        const state = this.state.learningGoals;
        const statusText = item.querySelector('#learning-status-text');
        if (state.active) {
            item.className = 'security-item ready';
            statusText.textContent = `${state.activeTopics} active, ${state.queuedTopics} queued`;
        } else {
            item.className = 'security-item pending';
            statusText.textContent = state.status || 'Initializing...';
        }

        this.updateAIBadge();
    }

    /**
     * Update Reactor-Core UI
     */
    updateReactorCoreUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let item = document.getElementById('reactor-status-item');
        if (!item) {
            item = document.createElement('div');
            item.id = 'reactor-status-item';
            item.className = 'security-item';
            item.innerHTML = `
                <span class="security-icon">⚛️</span>
                <div class="security-info">
                    <div class="security-label">Reactor-Core</div>
                    <div class="security-status" id="reactor-status-text">Initializing...</div>
                </div>
            `;
            grid.appendChild(item);
        }

        const state = this.state.reactorCore;
        const statusText = item.querySelector('#reactor-status-text');
        if (state.training) {
            item.className = 'security-item ready';
            statusText.textContent = `Epoch ${state.epoch}: ${(state.loss || 0).toFixed(4)}`;
        } else if (state.active) {
            item.className = 'security-item ready';
            statusText.textContent = `${state.experiencesQueued} queued`;
        } else {
            item.className = 'security-item pending';
            statusText.textContent = state.status || 'Initializing...';
        }

        this.updateAIBadge();
    }

    /**
     * Update Model Manager UI
     */
    updateModelManagerUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let item = document.getElementById('model-mgr-status-item');
        if (!item) {
            item = document.createElement('div');
            item.id = 'model-mgr-status-item';
            item.className = 'security-item';
            item.innerHTML = `
                <span class="security-icon">📦</span>
                <div class="security-info">
                    <div class="security-label">Model Manager</div>
                    <div class="security-status" id="model-mgr-status-text">Initializing...</div>
                </div>
            `;
            grid.appendChild(item);
        }

        const state = this.state.modelManager;
        const statusText = item.querySelector('#model-mgr-status-text');
        if (state.status === 'downloading') {
            item.className = 'security-item pending';
            statusText.textContent = `Downloading: ${state.downloadProgress}%`;
        } else if (state.status === 'ready') {
            item.className = 'security-item ready';
            statusText.textContent = `${state.model} (${state.sizeMb}MB)`;
        } else {
            item.className = 'security-item pending';
            statusText.textContent = state.status || 'Initializing...';
        }

        this.updateAIBadge();
    }

    /**
     * Update Neural Mesh UI
     */
    updateNeuralMeshUI(panel) {
        const grid = panel.querySelector('#ai-systems-grid');
        if (!grid) return;

        let item = document.getElementById('mesh-status-item');
        if (!item) {
            item = document.createElement('div');
            item.id = 'mesh-status-item';
            item.className = 'security-item';
            item.innerHTML = `
                <span class="security-icon">🕸️</span>
                <div class="security-info">
                    <div class="security-label">Neural Mesh</div>
                    <div class="security-status" id="mesh-status-text">Initializing...</div>
                </div>
            `;
            grid.appendChild(item);
        }

        const state = this.state.neuralMesh;
        const statusText = item.querySelector('#mesh-status-text');
        if (state.active) {
            item.className = 'security-item ready';
            statusText.textContent = `${state.agents} agents, ${state.knowledgeEntries} KB`;
        } else {
            item.className = 'security-item pending';
            statusText.textContent = state.status || 'Initializing...';
        }

        this.updateAIBadge();
    }

    /**
     * Update the Advanced Intelligence badge based on system states
     */
    updateAIBadge() {
        const badge = document.getElementById('ai-badge');
        if (!badge) return;

        const systems = [
            this.state.flywheel?.active,
            this.state.jarvisPrime?.active,
            this.state.learningGoals?.active,
            this.state.reactorCore?.active,
            this.state.modelManager?.status === 'ready',
            this.state.neuralMesh?.active
        ];

        const activeCount = systems.filter(Boolean).length;
        const totalCount = systems.length;

        badge.className = 'security-badge';
        if (activeCount === 0) {
            badge.classList.add('initializing');
            badge.textContent = 'Initializing';
        } else if (activeCount < totalCount) {
            badge.classList.add('initializing');
            badge.textContent = `${activeCount}/${totalCount} Active`;
        } else {
            badge.classList.add('operational');
            badge.textContent = 'Operational';
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

        // v225.0: Update SVG circular progress ring around arc reactor
        const ring = document.getElementById('progress-ring-fill');
        if (ring) {
            const circumference = 597; // 2 * Math.PI * 95 ≈ 597
            const offset = circumference - (displayProgress / 100) * circumference;
            ring.style.strokeDashoffset = offset;

            // Intensify glow as progress increases
            const glowIntensity = 4 + (displayProgress / 100) * 12;
            ring.style.filter = `drop-shadow(0 0 ${glowIntensity}px rgba(0, 255, 65, ${0.4 + displayProgress / 200}))`;
        }
    }

    // v225.0: Update phase timeline from v2 init_progress protocol data
    updatePhaseTimeline(v2Data) {
        const timeline = document.getElementById('phase-timeline');
        const fill = document.getElementById('timeline-fill');
        if (!timeline || !fill) return;

        // Update fill width based on completed phases
        const completedPct = v2Data.totalPhases > 0
            ? (v2Data.completedPhases / v2Data.totalPhases) * 100
            : 0;
        fill.style.width = `${completedPct}%`;

        // Update individual dot wrappers (wrapper carries active/completed class for CSS)
        const wrappers = timeline.querySelectorAll('.timeline-dot-wrapper');
        const phaseNames = Object.keys(PHASE_LABELS);

        wrappers.forEach((wrapper, i) => {
            const dot = wrapper.querySelector('.timeline-dot');
            if (!dot) return;

            // Reset classes
            wrapper.classList.remove('active', 'completed');
            dot.className = 'timeline-dot';

            if (i < v2Data.completedPhases) {
                wrapper.classList.add('completed');
                dot.classList.add('completed');
            } else if (i === v2Data.completedPhases) {
                wrapper.classList.add('active');
                dot.classList.add('active');
            }

            // Update tooltip with detailed phase data
            if (v2Data.phases && v2Data.phases[i]) {
                const phaseData = v2Data.phases[i];
                const phaseName = phaseNames[i] || `Phase ${i + 1}`;
                const label = PHASE_LABELS[phaseName];
                wrapper.title = `${label ? label.full : phaseName}: ${phaseData.status || 'pending'}`;
            }
        });
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
        this.state.progress = 96;
        this.state.targetProgress = 96;
        this.updateProgressBar();

        // ═══════════════════════════════════════════════════════════════════════════
        // v182.0 STEP 2: Wait for TRINITY components to be ready (if enabled)
        // ═══════════════════════════════════════════════════════════════════════════
        if (this.config.isUnifiedSupervisor) {
            console.log('[Complete] Step 2: Verifying Trinity components...');
            this.updateStatusText('Verifying Trinity integration...', 'loading');

            const trinityReady = await this.waitForTrinityReady();
            if (!trinityReady) {
                console.warn('[Complete] Trinity not fully ready - proceeding with available components');
                this.addLogEntry('Trinity', 'Some Trinity components may still be initializing', 'warning');
            } else {
                console.log('[Complete] ✓ Trinity components verified');
            }
        }

        this.state.progress = 97;
        this.state.targetProgress = 97;
        this.updateProgressBar();

        // ═══════════════════════════════════════════════════════════════════════════
        // STEP 3: Wait for FRONTEND to be responding
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[Complete] Step 3: Waiting for frontend...');
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
        // STEP 4: FINAL verification - make sure backend STILL responds after frontend wait
        // ═══════════════════════════════════════════════════════════════════════════
        console.log('[Complete] Step 4: Final system verification...');
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
                        // v6.0: Accept more status values as "ready" to match backend progressive readiness
                        // The backend returns ready=true for: ready, degraded, warming_up, websocket_ready
                        const isOperational =
                            readyData.ready === true ||
                            readyData.operational === true ||
                            status === 'ready' ||
                            status === 'operational' ||
                            status === 'warming_up' ||    // ML can warm in background
                            status === 'websocket_ready' || // WebSocket = interactive
                            status === 'degraded';        // Some services unavailable but core works

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
        // NOTE: The unified WebSocket endpoint is at /ws, not /ws/chat
        const wsUrl = `${wsProtocol}//${this.config.hostname}:${backendPort}/ws`;

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

    /**
     * v182.0: Wait for Trinity components to be ready.
     *
     * This ensures all Trinity components (JARVIS Body, J-Prime Mind, Reactor-Core)
     * are fully initialized before proceeding with redirect.
     *
     * The unified_supervisor already waits for Trinity on the server side,
     * but this provides a client-side safety net and visual feedback.
     *
     * @returns {Promise<boolean>} True if Trinity is ready or timeout reached
     */
    async waitForTrinityReady() {
        const config = {
            maxWaitTime: 15000,     // 15 seconds max wait
            checkInterval: 500,     // Check every 500ms
        };

        const startTime = Date.now();

        while (Date.now() - startTime < config.maxWaitTime) {
            // Check if Trinity is already marked as ready
            if (this.state.trinityAllReady === true) {
                console.log('[Trinity Wait] ✓ Trinity marked as ready');
                return true;
            }

            // Check trinityComponents state
            if (this.state.trinityComponents) {
                const jarvisReady = this.state.trinityComponents.jarvis?.status === 'complete' ||
                    this.state.trinityComponents.jarvis?.status === 'ready';
                const primeStatus = this.state.trinityComponents.prime?.status;
                const reactorStatus = this.state.trinityComponents.reactor?.status;

                const primeReady = primeStatus === 'complete' || primeStatus === 'ready';
                const primeSkipped = primeStatus === 'skipped' || primeStatus === 'unavailable';
                const reactorReady = reactorStatus === 'complete' || reactorStatus === 'ready';
                const reactorSkipped = reactorStatus === 'skipped' || reactorStatus === 'unavailable';

                // JARVIS Body (backend) is REQUIRED
                // Prime and Reactor can be ready OR skipped/unavailable
                const primeAcceptable = primeReady || primeSkipped;
                const reactorAcceptable = reactorReady || reactorSkipped;

                if (jarvisReady && primeAcceptable && reactorAcceptable) {
                    // Build accurate message based on actual component states
                    const notConfigured = [];
                    if (primeSkipped) notConfigured.push('Prime');
                    if (reactorSkipped) notConfigured.push('Reactor');

                    if (notConfigured.length > 0) {
                        const message = `[Trinity Wait] ✓ Backend ready; ${notConfigured.join(' and ')} not configured`;
                        console.log(message + ' (set JARVIS_PRIME_PATH/REACTOR_CORE_PATH if needed)');
                    } else {
                        console.log('[Trinity Wait] ✓ All Trinity components ready');
                    }
                    this.state.trinityAllReady = true;
                    return true;
                }

                const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
                console.log(`[Trinity Wait] Waiting... (${elapsed}s) - ` +
                    `JARVIS:${jarvisReady}, Prime:${primeAcceptable}(ready:${primeReady},skipped:${primeSkipped}), ` +
                    `Reactor:${reactorAcceptable}(ready:${reactorReady},skipped:${reactorSkipped})`);
            }

            await this.sleep(config.checkInterval);
        }

        // Timeout - check one more time
        if (this.state.trinityAllReady === true) {
            return true;
        }

        console.warn('[Trinity Wait] Timeout - Trinity may not be fully ready');
        return false;
    }

    async waitForFrontendWithRetries() {
        /**
         * v5.2: Intelligent frontend waiting with graceful fallback.
         *
         * This is the CRITICAL gate that prevents redirecting to an unavailable frontend.
         * However, v5.2 adds intelligent fallback when frontend isn't available.
         *
         * Strategy:
         * - Quick initial checks (frontend might already be up)
         * - Exponential backoff to avoid hammering
         * - Maximum wait time before activating fallback mode
         * - Clear status updates to the user
         * - v5.2: Graceful fallback with user options when frontend unavailable
         */
        // v6.0: Increased timeout and improved messages
        // Webpack compilation can take 30-60+ seconds on first run
        const config = {
            maxWaitTime: this.config.frontendWaitTimeoutMs || 120000, // 120 seconds max (v6.0: increased from 90s)
            initialDelay: 500,          // Start with 500ms between checks
            maxDelay: 3000,             // Cap delay at 3 seconds
            backoffMultiplier: 1.5,     // Increase delay by 50% each attempt
            slowThreshold: this.config.frontendSlowThresholdSecs || 30, // Show webpack message after 30s
            webpackCompilePhrase: 45,   // v6.0: Show "webpack compiling" message after 45s
        };

        const startTime = Date.now();
        let attempt = 0;
        let delay = config.initialDelay;

        console.log('[Frontend Wait] Starting intelligent wait for frontend...');
        this.updateStatusText('Waiting for frontend...', 'loading');
        if (this.elements.statusMessage) {
            this.elements.statusMessage.textContent = 'Starting user interface...';
        }

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
            // v6.0: More accurate messaging about webpack compilation phases
            if (elapsed < 10) {
                if (this.elements.statusMessage) {
                    this.elements.statusMessage.textContent = 'Starting user interface...';
                }
            } else if (elapsed < config.slowThreshold) {
                if (this.elements.statusMessage) {
                    this.elements.statusMessage.textContent = 'Frontend is initializing...';
                }
                this.updateStatusText('Starting frontend...', 'loading');
            } else if (elapsed < config.webpackCompilePhrase) {
                // v6.0: Show webpack message after slow threshold
                if (this.elements.statusMessage) {
                    this.elements.statusMessage.textContent = `Webpack compiling (${elapsed}s)...`;
                }
                this.updateStatusText('Compiling frontend...', 'loading');
            } else {
                // v6.0: After webpackCompilePhrase, show more helpful message
                if (this.elements.statusMessage) {
                    this.elements.statusMessage.textContent = `Still compiling frontend (${elapsed}s)...`;
                }
                this.updateStatusText('Compilation in progress...', 'loading');

                // v5.2: Show the fallback button (after slowThreshold)
                // so user can proceed to backend if they want
                this.showBackendFallbackButton();
            }

            // Update progress to show we're making progress in waiting
            const waitProgress = 95 + Math.min(4, (elapsed / config.maxWaitTime * 1000) * 4); // 95% -> 99%
            this.state.progress = waitProgress;
            this.state.targetProgress = waitProgress;
            this.updateProgressBar();

            // Wait with exponential backoff
            await this.sleep(delay);
            delay = Math.min(delay * config.backoffMultiplier, config.maxDelay);
        }

        // v5.2: Frontend didn't become ready - activate graceful fallback
        console.warn(`[Frontend Wait] ✗ Frontend failed to become ready after ${config.maxWaitTime / 1000}s`);
        console.log('[Frontend Wait] Activating graceful fallback mode...');

        // Handle frontend unavailability gracefully
        await this.handleFrontendUnavailable();

        // Return false to indicate frontend is not ready
        // The caller should handle this appropriately
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

    /**
     * v5.2: Get frontend_optional setting from URL parameters or environment.
     * This allows the supervisor to signal that frontend is optional.
     *
     * URL param: ?frontend_optional=true
     * This is set when starting in headless/API-only mode.
     */
    getFrontendOptionalFromURL() {
        try {
            const urlParams = new URLSearchParams(window.location.search);
            const param = urlParams.get('frontend_optional');
            if (param !== null) {
                return param.toLowerCase() === 'true';
            }
            // Default: frontend is NOT optional (require it for redirect)
            return false;
        } catch (e) {
            console.debug('[Config] Error parsing URL params:', e);
            return false;
        }
    }

    /**
     * v5.2: Update frontend_optional setting dynamically.
     * Called when supervisor signals that frontend is optional.
     */
    setFrontendOptional(optional) {
        this.config.frontendOptional = optional;
        console.log(`[Config] Frontend optional mode: ${optional}`);
    }

    /**
     * v5.2: Handle frontend unavailability gracefully.
     * When frontend is not available after timeout, provide helpful fallback.
     *
     * Options:
     * 1. Redirect to backend API docs/health page
     * 2. Show inline message with instructions
     * 3. Keep showing loading with periodic retry
     */
    async handleFrontendUnavailable() {
        console.log('[Fallback] Frontend unavailable - activating fallback mode');

        // v6.0: Check one more time if frontend is actually ready
        // Webpack might have just finished compiling
        const isReady = await this.checkFrontendReady();
        if (isReady) {
            console.log('[Fallback] Frontend is now ready! Redirecting...');
            const frontendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
            window.location.href = frontendUrl;
            return;
        }

        // Update UI to show helpful message
        if (this.elements.statusMessage) {
            this.elements.statusMessage.innerHTML = `
                <span style="color: #f59e0b;">Frontend is still starting</span>
            `;
        }

        // v6.0: Show more helpful message about webpack compilation
        if (this.elements.subtitle) {
            this.elements.subtitle.innerHTML = `
                <span style="font-size: 0.9rem;">
                    Backend is ready! Frontend (webpack) is still compiling.<br>
                    <span style="color: #888; font-size: 0.8rem;">
                        This can take 60-90+ seconds on first start.
                    </span>
                </span>
            `;
        }

        // Add a "Continue to Backend" button
        this.showBackendFallbackButton();

        // v6.0: CRITICAL - Only auto-redirect if frontend_optional is EXPLICITLY set
        // This prevents auto-redirecting to backend when webpack is just slow
        if (this.config.frontendOptional === true) {
            console.log('[Fallback] Frontend optional EXPLICITLY set - redirecting to backend in 3s');
            await this.sleep(3000);
            const backendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.backendPort}`;
            window.location.href = backendUrl;
        } else {
            // v6.0: Start background retry for frontend
            console.log('[Fallback] Starting background retry for frontend...');
            this._startFrontendRetry();
        }
    }

    /**
     * v6.0: Background retry for frontend when it's slow to start.
     * Keeps checking periodically and redirects when ready.
     */
    _startFrontendRetry() {
        // Don't start multiple retries
        if (this._frontendRetryInterval) return;

        let attempts = 0;
        const maxAttempts = 30; // 30 attempts * 5 seconds = 2.5 minutes
        const retryInterval = 5000; // Check every 5 seconds

        this._frontendRetryInterval = setInterval(async () => {
            attempts++;
            console.log(`[Fallback] Retry #${attempts} - checking frontend...`);

            const isReady = await this.checkFrontendReady();
            if (isReady) {
                clearInterval(this._frontendRetryInterval);
                this._frontendRetryInterval = null;
                console.log('[Fallback] Frontend ready! Redirecting...');
                const frontendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
                window.location.href = frontendUrl;
            } else if (attempts >= maxAttempts) {
                clearInterval(this._frontendRetryInterval);
                this._frontendRetryInterval = null;
                console.warn('[Fallback] Max retries reached - frontend may be unavailable');
                // Update message to be more definitive
                if (this.elements.statusMessage) {
                    this.elements.statusMessage.innerHTML = `
                        <span style="color: #ef4444;">Frontend failed to start</span>
                    `;
                }
            } else if (attempts % 6 === 0) {
                // Update status every 30 seconds
                if (this.elements.subtitle) {
                    this.elements.subtitle.innerHTML = `
                        <span style="font-size: 0.9rem;">
                            Still waiting for frontend... (${attempts * 5}s)<br>
                            <span style="color: #888; font-size: 0.8rem;">
                                Use the button below to skip to backend.
                            </span>
                        </span>
                    `;
                }
            }
        }, retryInterval);
    }

    /**
     * v5.2: Show a button to manually redirect to backend.
     */
    showBackendFallbackButton() {
        // v225.0: Suppress fallback buttons — progress ring + phase timeline provide status
        return;
        const existingButton = document.getElementById('backend-fallback-btn');
        if (existingButton) return; // Already shown

        const backendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.backendPort}`;

        const buttonContainer = document.createElement('div');
        buttonContainer.id = 'backend-fallback-container';
        buttonContainer.style.cssText = `
            margin-top: 20px;
            text-align: center;
            animation: fadeIn 0.5s ease-out;
        `;

        const button = document.createElement('button');
        button.id = 'backend-fallback-btn';
        button.innerHTML = '🔧 Continue to Backend API';
        button.style.cssText = `
            background: linear-gradient(135deg, #0066cc, #004499);
            border: none;
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            box-shadow: 0 4px 15px rgba(0, 102, 204, 0.3);
            transition: all 0.3s ease;
        `;
        button.onmouseover = () => {
            button.style.transform = 'translateY(-2px)';
            button.style.boxShadow = '0 6px 20px rgba(0, 102, 204, 0.4)';
        };
        button.onmouseout = () => {
            button.style.transform = 'translateY(0)';
            button.style.boxShadow = '0 4px 15px rgba(0, 102, 204, 0.3)';
        };
        button.onclick = () => {
            window.location.href = backendUrl;
        };

        buttonContainer.appendChild(button);

        // Add retry button too
        const retryButton = document.createElement('button');
        retryButton.innerHTML = '🔄 Retry Frontend';
        retryButton.style.cssText = `
            background: transparent;
            border: 1px solid #444;
            color: #888;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            margin-left: 12px;
            transition: all 0.3s ease;
        `;
        retryButton.onmouseover = () => {
            retryButton.style.borderColor = '#666';
            retryButton.style.color = '#aaa';
        };
        retryButton.onmouseout = () => {
            retryButton.style.borderColor = '#444';
            retryButton.style.color = '#888';
        };
        retryButton.onclick = async () => {
            retryButton.disabled = true;
            retryButton.innerHTML = '⏳ Checking...';
            const isReady = await this.checkFrontendReady();
            if (isReady) {
                buttonContainer.remove();
                const frontendUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.mainAppPort}`;
                window.location.href = frontendUrl;
            } else {
                retryButton.innerHTML = '🔄 Retry Frontend';
                retryButton.disabled = false;
            }
        };

        buttonContainer.appendChild(retryButton);

        // Insert after progress bar
        const progressContainer = document.querySelector('.progress-container');
        if (progressContainer && progressContainer.parentNode) {
            progressContainer.parentNode.insertBefore(buttonContainer, progressContainer.nextSibling);
        } else {
            document.body.appendChild(buttonContainer);
        }
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

            // v263.0: Progress-aware dynamic timeout (replaces hardcoded 600s)
            //
            // OLD: if (totalTime > 600000) → show error
            //   Problem: Backend can legitimately take 15-40 min with GCP/Trinity
            //
            // NEW: Timeout only fires when BOTH conditions are true:
            //   1. Total time exceeds backend-negotiated deadline
            //   2. Progress has been completely stalled (no advancement) for stallThresholdMs
            //
            // This means: if GCP VM provisioning takes 20 min but progress keeps
            // advancing (5%→10%→15%...), we never timeout. We only timeout when
            // the system is truly stuck AND has exceeded its own stated deadline.
            if (this.state.progress < 100 && !this.state.startupTimeout.timeoutShown) {
                const st = this.state.startupTimeout;
                const timeSinceProgressAdvance = Date.now() - st.lastProgressAdvanceTime;
                const isStalled = timeSinceProgressAdvance > st.stallThresholdMs;
                const pastDeadline = totalTime > st.negotiatedMs;

                // Log stall warnings periodically (every ~30s of stall)
                if (isStalled) {
                    const lastWarn = st._lastStallWarnTime || 0;
                    if (Date.now() - lastWarn >= 30000) {
                        st._lastStallWarnTime = Date.now();
                        const stallMin = (timeSinceProgressAdvance / 60000).toFixed(1);
                        const deadlineMin = (st.negotiatedMs / 60000).toFixed(1);
                        const totalMin = (totalTime / 60000).toFixed(1);
                        console.warn(
                            `[v263.0] Progress stalled for ${stallMin}min at ${st.lastProgressValue}%. ` +
                            `Total: ${totalMin}min, Deadline: ${deadlineMin}min, Past deadline: ${pastDeadline}`
                        );
                    }
                }

                if (isStalled && pastDeadline) {
                    st.timeoutShown = true;
                    const stallMin = (timeSinceProgressAdvance / 60000).toFixed(1);
                    const totalMin = (totalTime / 60000).toFixed(1);
                    this.showError(
                        `Startup timed out after ${totalMin}min ` +
                        `(progress stalled at ${Math.round(st.lastProgressValue)}% for ${stallMin}min). ` +
                        `Please check terminal logs.`
                    );
                }
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

    // ═══════════════════════════════════════════════════════════════════════════
    // v87.0: TRINITY ULTRA ADVANCED INTELLIGENCE METHODS
    // ═══════════════════════════════════════════════════════════════════════════

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

        console.log(`[v87.0] 📊 ETA: ${etaText} (${confidencePercent}% confidence, ${eta.predictionMethod})`);

        // Add to operations log
        this.addLogEntry('Analytics', `ETA: ${etaText} ${confidenceIndicator}`, 'info');
    }

    /**
     * v87.0: Display redirect countdown during grace period
     */
    displayRedirectCountdown(secondsRemaining) {
        if (secondsRemaining === null || secondsRemaining < 0) return;

        if (secondsRemaining > 0) {
            this.state.redirectGrace.countdownActive = true;
            console.log(`[v87.0] ⏳ Redirecting in ${secondsRemaining.toFixed(1)}s...`);
        } else {
            this.state.redirectGrace.countdownActive = false;
        }
    }

    /**
     * v87.0 ULTRA: Poll unified Trinity health status with circuit breaker & smart caching
     */
    async pollUnifiedHealth() {
        const now = Date.now();
        const timeSinceLastPoll = now - (this.state.unifiedHealth.lastPoll || 0);

        // Poll every 5 seconds
        if (timeSinceLastPoll < this.state.unifiedHealth.pollInterval) {
            return;
        }

        try {
            // v87.0 ULTRA: Use circuit breaker to prevent cascading failures
            await this.executeWithCircuitBreaker('unifiedHealth', async () => {
                const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;

                // v87.0 ULTRA: Use smart caching to minimize requests
                const health = await this.fetchWithCache(
                    `${loadingUrl}/api/health/unified`,
                    {
                        method: 'GET',
                        headers: { 'Accept': 'application/json' },
                        signal: AbortSignal.timeout(3000)
                    }
                );

                // v87.0 ULTRA: Atomic state update
                await this.atomicStateUpdate(() => {
                    this.state.unifiedHealth = {
                        overallHealth: health.overall_health || 0,
                        state: health.state || 'unknown',
                        components: health.components || {},
                        circuitBreakers: health.circuit_breakers || {},
                        lastPoll: now,
                        pollInterval: 5000
                    };
                });

                console.log(`[v87.0] 🏥 Health: ${health.state} (${health.overall_health}%)${health._cached ? ' [CACHED]' : ''}`);
            });
        } catch (error) {
            console.debug('[v87.0] Health polling error:', error.message);
            // Circuit breaker will handle failure tracking
        }
    }

    /**
     * v87.0 ULTRA: Check supervisor heartbeat with circuit breaker
     */
    async checkSupervisorHeartbeat() {
        try {
            // v87.0 ULTRA: Use circuit breaker
            await this.executeWithCircuitBreaker('supervisorHeartbeat', async () => {
                const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;

                const response = await fetch(`${loadingUrl}/api/supervisor/heartbeat`, {
                    method: 'GET',
                    headers: { 'Accept': 'application/json' },
                    signal: AbortSignal.timeout(3000)
                });

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }

                const status = await response.json();

                // v87.0 ULTRA: Atomic state update
                await this.atomicStateUpdate(() => {
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
                        this.addLogEntry('System', 'Supervisor process crashed - check logs', 'error');
                    }

                    // Log recovery
                    if (isAlive && !wasAlive) {
                        console.log('[v87.0] ✅ Supervisor recovered!');
                        this.addLogEntry('System', 'Supervisor process recovered', 'success');
                    }
                });
            });
        } catch (error) {
            console.debug('[v87.0] Supervisor heartbeat check error:', error.message);
            // Circuit breaker handles failure tracking
        }
    }

    /**
     * v263.0: Fetch startup configuration from loading server.
     * Gets the backend-negotiated startup timeout so we don't use a hardcoded limit.
     * Called once at init, and the timeout is also continuously updated via
     * WebSocket progress messages (startup_timeout_ms in metadata).
     */
    async fetchStartupConfig() {
        try {
            const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;
            const response = await fetch(`${loadingUrl}/api/startup-config`, {
                method: 'GET',
                headers: { 'Accept': 'application/json' },
                signal: AbortSignal.timeout(3000)
            });

            if (response.ok) {
                const config = await response.json();
                if (config.startup_timeout_ms && config.startup_timeout_ms > 0) {
                    this.state.startupTimeout.negotiatedMs = config.startup_timeout_ms;
                    this.state.startupTimeout.negotiated = true;
                    if (config.stall_timeout_ms) {
                        this.state.startupTimeout.stallThresholdMs = config.stall_timeout_ms;
                    }
                    console.log(
                        `[v263.0] Startup config received: timeout=${config.startup_timeout_ms}ms ` +
                        `(${(config.startup_timeout_ms / 60000).toFixed(1)}min), ` +
                        `stall=${this.state.startupTimeout.stallThresholdMs}ms`
                    );
                }
            }
        } catch (error) {
            console.debug('[v263.0] Startup config fetch failed (will use WebSocket fallback):', error.message);
        }
    }

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
            this.pollingState.currentInterval = Math.max(this.pollingState.currentInterval, 5000);
            console.log('[v87.0] 🐌 Slow network detected - reducing polling frequency');
        } else if (connection.effectiveType === '4g' || connection.type === 'wifi') {
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

    /**
     * v87.0: Handle offline mode detection
     */
    handleOfflineMode() {
        const consecutiveFailures = this.state.offlineMode.consecutiveFailures;
        const threshold = this.state.offlineMode.threshold;

        if (consecutiveFailures >= threshold && !this.state.offlineMode.active) {
            console.warn('[v87.0] 🔴 Offline mode detected - no successful requests');

            this.state.offlineMode.active = true;
            this.state.offlineMode.detectedAt = Date.now();

            this.addLogEntry('Network', 'Loading server unreachable - check connection', 'error');
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
            this.addLogEntry('Network', 'Connection restored', 'success');
        }
    }

    /**
     * v87.0: Record failed request (increment offline detection)
     */
    recordFailedRequest() {
        this.state.offlineMode.consecutiveFailures++;
        this.handleOfflineMode();
    }

    /**
     * v87.0 ULTRA: Request missed updates from server (atomic recovery)
     *
     * Recovers updates that were missed due to WebSocket disconnection or packet loss.
     * Uses /api/progress/resume endpoint with sequence tracking.
     */
    async requestMissedUpdates(lastSequence) {
        try {
            const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;
            const url = `${loadingUrl}/api/progress/resume?last_sequence=${lastSequence}&include_health=false`;

            const response = await fetch(url, {
                method: 'GET',
                headers: {
                    'Accept': 'application/json',
                    'X-Request-Type': 'missed-update-recovery',
                },
                cache: 'no-cache',
                signal: AbortSignal.timeout(5000),
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const data = await response.json();

            if (data.resume_metadata && data.resume_metadata.missed_updates > 0) {
                console.log(
                    `[v87.0] ✅ Recovered ${data.resume_metadata.missed_updates} missed updates ` +
                    `(sequence ${data.resume_metadata.client_last_sequence} → ${data.resume_metadata.current_sequence})`
                );

                // Process the recovered state update atomically
                await this.atomicStateUpdate(() => {
                    this.handleProgressUpdate(data);
                });

                this.addLogEntry('System', `Recovered ${data.resume_metadata.missed_updates} updates`, 'success');
            } else {
                console.log('[v87.0] No missed updates to recover');
            }

        } catch (error) {
            console.error('[v87.0] ❌ Missed update recovery failed:', error.message);
            this.addLogEntry('System', 'Failed to recover updates', 'error');
            throw error;
        }
    }

    /**
     * v87.0 ULTRA: Full state resync (for large gaps)
     *
     * Performs complete state synchronization when gap is too large (>10 updates).
     * Fetches current state and resets sequence tracking.
     */
    async fullResync() {
        console.log('[v87.0] 🔄 Performing full state resync...');

        try {
            const loadingUrl = `${this.config.httpProtocol}//${this.config.hostname}:${this.config.loadingServerPort}`;
            const url = `${loadingUrl}/api/progress/resume?include_health=true`;

            const response = await fetch(url, {
                method: 'GET',
                headers: {
                    'Accept': 'application/json',
                    'X-Request-Type': 'full-resync',
                },
                cache: 'no-cache',
                signal: AbortSignal.timeout(5000),
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();

            // Atomic state reset
            await this.atomicStateUpdate(() => {
                // Reset sequence tracking
                this.state.sequencing.missedUpdates = 0;
                this.state.sequencing.lastSequenceNumber = data.sequence_number || -1;
                this.state.sequencing.lastSequenceCheck = Date.now();

                // Full state update
                this.handleProgressUpdate(data);
            });

            console.log(`[v87.0] ✅ Full resync complete (sequence: ${data.sequence_number})`);
            this.addLogEntry('System', 'Full state synchronized', 'success');

        } catch (error) {
            console.error('[v87.0] ❌ Full resync failed:', error.message);
            this.addLogEntry('System', 'Full resync failed', 'error');
            throw error;
        }
    }

    /**
     * v87.0 ULTRA: Atomic state update with locking
     *
     * Prevents race conditions when concurrent updates occur.
     * Uses async mutex pattern for thread-safe state modifications.
     */
    async atomicStateUpdate(updateFn) {
        // Simple async mutex using promise chaining
        if (!this._stateLock) {
            this._stateLock = Promise.resolve();
        }

        // Queue this update after previous ones
        const previousLock = this._stateLock;
        let releaseLock;
        this._stateLock = new Promise(resolve => {
            releaseLock = resolve;
        });

        try {
            // Wait for previous update to complete
            await previousLock;

            // Execute update function
            const result = await Promise.resolve(updateFn());

            return result;

        } finally {
            // Release lock
            releaseLock();
        }
    }

    /**
     * v87.0 ULTRA: Smart caching with ETag support
     *
     * Caches responses based on ETag headers to minimize redundant requests.
     * Implements LRU eviction policy with configurable cache size.
     */
    initSmartCache() {
        if (this._smartCache) return;

        this._smartCache = {
            store: new Map(), // url -> {etag, data, timestamp}
            maxSize: 50,
            hits: 0,
            misses: 0,
        };

        console.log('[v87.0] 💾 Smart cache initialized (max size: 50)');
    }

    async fetchWithCache(url, options = {}) {
        this.initSmartCache();

        const cacheKey = url;
        const cached = this._smartCache.store.get(cacheKey);

        // Add If-None-Match header if we have cached ETag
        if (cached && cached.etag) {
            options.headers = options.headers || {};
            options.headers['If-None-Match'] = cached.etag;
        }

        const response = await fetch(url, options);

        // 304 Not Modified - use cache
        if (response.status === 304 && cached) {
            this._smartCache.hits++;
            console.debug(`[v87.0] 💾 Cache HIT: ${url} (${this._smartCache.hits} hits, ${this._smartCache.misses} misses)`);
            return { ...cached.data, _cached: true };
        }

        // 200 OK - cache the response
        if (response.ok) {
            this._smartCache.misses++;
            const data = await response.json();
            const etag = response.headers.get('ETag');

            if (etag) {
                // Store in cache
                this._smartCache.store.set(cacheKey, {
                    etag,
                    data,
                    timestamp: Date.now(),
                });

                // LRU eviction if cache too large
                if (this._smartCache.store.size > this._smartCache.maxSize) {
                    const oldestKey = this._smartCache.store.keys().next().value;
                    this._smartCache.store.delete(oldestKey);
                    console.debug(`[v87.0] 💾 Cache evicted: ${oldestKey}`);
                }
            }

            return data;
        }

        throw new Error(`HTTP ${response.status}`);
    }

    /**
     * v87.0 ULTRA: Circuit breaker for health endpoints
     *
     * Prevents cascading failures by stopping requests to failing endpoints.
     * Implements half-open state for automatic recovery testing.
     */
    initCircuitBreakers() {
        if (this._circuitBreakers) return;

        this._circuitBreakers = {
            unifiedHealth: this.createCircuitBreaker('unified-health', 5, 30000),
            supervisorHeartbeat: this.createCircuitBreaker('supervisor-heartbeat', 5, 30000),
        };

        console.log('[v87.0] ⚡ Circuit breakers initialized');
    }

    createCircuitBreaker(name, threshold = 5, resetTimeout = 30000) {
        return {
            name,
            state: 'closed', // closed, open, half-open
            failures: 0,
            threshold,
            resetTimeout,
            lastFailureTime: null,
            successCount: 0,
        };
    }

    async executeWithCircuitBreaker(breakerName, asyncFn) {
        this.initCircuitBreakers();
        const breaker = this._circuitBreakers[breakerName];

        if (!breaker) {
            // No breaker configured - execute directly
            return await asyncFn();
        }

        // Check breaker state
        if (breaker.state === 'open') {
            const now = Date.now();
            const timeSinceFailure = now - breaker.lastFailureTime;

            if (timeSinceFailure >= breaker.resetTimeout) {
                // Try transitioning to half-open
                console.log(`[v87.0] ⚡ Circuit breaker [${breaker.name}] transitioning to half-open`);
                breaker.state = 'half-open';
                breaker.successCount = 0;
            } else {
                // Still open - reject immediately
                const remainingMs = breaker.resetTimeout - timeSinceFailure;
                throw new Error(`Circuit breaker [${breaker.name}] is OPEN (retry in ${(remainingMs / 1000).toFixed(1)}s)`);
            }
        }

        // Execute function
        try {
            const result = await asyncFn();

            // Success - update breaker
            if (breaker.state === 'half-open') {
                breaker.successCount++;
                if (breaker.successCount >= 2) {
                    // Two successes in half-open = close breaker
                    console.log(`[v87.0] ⚡ Circuit breaker [${breaker.name}] closed after recovery`);
                    breaker.state = 'closed';
                    breaker.failures = 0;
                }
            } else if (breaker.state === 'closed') {
                // Reset failure count on success
                breaker.failures = Math.max(0, breaker.failures - 1);
            }

            return result;

        } catch (error) {
            // Failure - increment counter
            breaker.failures++;
            breaker.lastFailureTime = Date.now();

            if (breaker.failures >= breaker.threshold) {
                // Open the circuit
                console.error(`[v87.0] ⚡ Circuit breaker [${breaker.name}] OPENED after ${breaker.failures} failures`);
                breaker.state = 'open';
                this.addLogEntry('System', `Circuit breaker [${breaker.name}] opened`, 'error');
            }

            throw error;
        }
    }

    /**
     * v87.0 ULTRA: Render parallel component progress (J-Prime, Reactor-Core)
     *
     * Displays individual progress bars for Trinity components when data is available.
     * Creates/updates UI elements dynamically to show component-level progress.
     */
    updateParallelComponentUI() {
        // Only render if we have component data
        if (!this.state.parallelComponents || Object.keys(this.state.parallelComponents).length === 0) {
            return;
        }

        // Find or create container for parallel components
        let componentContainer = document.getElementById('parallel-components-container');
        if (!componentContainer) {
            // Create container if it doesn't exist
            const detailsPanel = this.elements.detailsPanel;
            if (!detailsPanel) return;

            componentContainer = document.createElement('div');
            componentContainer.id = 'parallel-components-container';
            componentContainer.className = 'parallel-components';
            componentContainer.innerHTML = '<h3 style="margin: 10px 0; font-size: 0.9em; opacity: 0.7;">Trinity Components</h3>';

            // Insert before substep list
            const substepList = this.elements.substepList;
            if (substepList && substepList.parentNode) {
                substepList.parentNode.insertBefore(componentContainer, substepList);
            } else {
                detailsPanel.appendChild(componentContainer);
            }
        }

        // Update each component
        Object.entries(this.state.parallelComponents).forEach(([componentName, componentData]) => {
            const componentId = `component-${componentName}`;
            let componentEl = document.getElementById(componentId);

            if (!componentEl) {
                // Create new component element
                componentEl = document.createElement('div');
                componentEl.id = componentId;
                componentEl.className = 'parallel-component';
                componentEl.innerHTML = `
                    <div class="component-header">
                        <span class="component-icon">${this._getComponentIcon(componentName)}</span>
                        <span class="component-name">${this._formatComponentName(componentName)}</span>
                        <span class="component-status"></span>
                    </div>
                    <div class="component-progress-bar">
                        <div class="component-progress-fill"></div>
                        <div class="component-progress-text"></div>
                    </div>
                `;
                componentContainer.appendChild(componentEl);

                // Add inline styles for new element
                componentEl.style.cssText = `
                    margin: 8px 0;
                    padding: 8px;
                    background: rgba(0, 255, 204, 0.05);
                    border-radius: 8px;
                    border: 1px solid rgba(0, 255, 204, 0.2);
                `;
            }

            // Update component state
            const statusEl = componentEl.querySelector('.component-status');
            const fillEl = componentEl.querySelector('.component-progress-fill');
            const textEl = componentEl.querySelector('.component-progress-text');

            if (statusEl) {
                statusEl.textContent = componentData.state || 'unknown';
                statusEl.className = `component-status ${componentData.state}`;
                statusEl.style.cssText = `
                    font-size: 0.8em;
                    padding: 2px 8px;
                    border-radius: 4px;
                    background: ${componentData.state === 'ready' ? 'rgba(0, 255, 0, 0.2)' :
                        componentData.state === 'starting' ? 'rgba(255, 255, 0, 0.2)' :
                            'rgba(255, 0, 0, 0.2)'};
                `;
            }

            if (fillEl && textEl) {
                const progress = componentData.progress || 0;
                fillEl.style.width = `${progress}%`;
                fillEl.style.cssText += `
                    height: 100%;
                    background: linear-gradient(90deg, rgba(0, 255, 204, 0.6), rgba(0, 255, 204, 0.3));
                    border-radius: 4px;
                    transition: width 0.3s ease;
                `;
                textEl.textContent = `${Math.round(progress)}%`;
                textEl.style.cssText = `
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    font-size: 0.8em;
                    font-weight: bold;
                    color: #00ffcc;
                    text-shadow: 0 0 5px rgba(0, 0, 0, 0.8);
                `;
            }

            // Style progress bar container
            const progressBar = componentEl.querySelector('.component-progress-bar');
            if (progressBar && !progressBar.dataset.styled) {
                progressBar.style.cssText = `
                    position: relative;
                    height: 20px;
                    background: rgba(0, 0, 0, 0.3);
                    border-radius: 4px;
                    overflow: hidden;
                    margin-top: 4px;
                `;
                progressBar.dataset.styled = 'true';
            }
        });
    }

    _formatComponentName(componentName) {
        const names = {
            'jarvis_prime': 'JARVIS Prime (Tier-0)',
            'reactor_core': 'Reactor Core (ML)',
            'jarvis': 'JARVIS (Main)',
        };
        return names[componentName] || componentName.replace(/_/g, ' ').toUpperCase();
    }

    /**
     * v87.0 ULTRA: Cross-tab synchronization using BroadcastChannel
     *
     * Synchronizes loading state across multiple browser tabs to prevent
     * duplicate loading screens and coordinate redirect timing.
     */
    initCrossTabSync() {
        if (!window.BroadcastChannel) {
            console.debug('[v87.0] BroadcastChannel not available - cross-tab sync disabled');
            return;
        }

        if (this._tabSyncChannel) return;

        try {
            this._tabSyncChannel = new BroadcastChannel('jarvis-loading-sync');

            // Listen for messages from other tabs
            this._tabSyncChannel.onmessage = (event) => {
                const { type, data, tabId } = event.data;

                // Ignore messages from self
                if (tabId === this._tabId) return;

                console.debug(`[v87.0] 📡 Cross-tab message: ${type} from tab ${tabId}`);

                switch (type) {
                    case 'progress-update':
                        // Another tab got a progress update - sync state
                        if (data.sequence_number > this.state.sequencing.lastSequenceNumber) {
                            console.log('[v87.0] 🔄 Syncing progress from another tab');
                            this.handleProgressUpdate(data);
                        }
                        break;

                    case 'redirect-complete':
                        // Another tab redirected - we should too
                        console.log('[v87.0] 🔄 Another tab redirected - following');
                        setTimeout(async () => {
                            if (this.state.progress >= 100) {
                                await this.quickRedirectToApp();
                            }
                        }, 500);
                        break;

                    case 'tab-closed':
                        // Another tab closed - we might be the last one
                        console.log(`[v87.0] Tab ${tabId} closed`);
                        break;
                }
            };

            // Generate unique tab ID
            this._tabId = `tab-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

            // Announce this tab exists
            this.broadcastToTabs('tab-opened', { timestamp: Date.now() });

            console.log(`[v87.0] 📡 Cross-tab sync initialized (tab: ${this._tabId})`);

            // Clean up on page unload
            window.addEventListener('beforeunload', () => {
                this.broadcastToTabs('tab-closed', { timestamp: Date.now() });
                if (this._tabSyncChannel) {
                    this._tabSyncChannel.close();
                }
            });

        } catch (error) {
            console.warn('[v87.0] Failed to initialize cross-tab sync:', error);
        }
    }

    broadcastToTabs(type, data) {
        if (!this._tabSyncChannel) return;

        try {
            this._tabSyncChannel.postMessage({
                type,
                data,
                tabId: this._tabId,
                timestamp: Date.now(),
            });
        } catch (error) {
            console.debug('[v87.0] Failed to broadcast to tabs:', error.message);
        }
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
