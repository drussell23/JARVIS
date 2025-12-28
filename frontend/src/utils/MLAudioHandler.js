/**
 * ML-Enhanced Audio Handler for JARVIS
 * Integrates with backend ML audio manager for intelligent error recovery
 */

import configService from '../services/DynamicConfigService';
import { getJarvisConnectionService, ConnectionState as JarvisConnState } from '../services/JarvisConnectionService';

class MLAudioHandler {
    constructor() {
        this.ws = null;
        this._unsubscribeJarvisMessage = null;
        this.errorHistory = [];
        this.recoveryStrategies = new Map();
        this.connectionAttempts = 0;
        this.maxConnectionAttempts = 10;
        this.backoffMultiplier = 1.5;
        this.currentBackoffDelay = 1000;
        this.isConnecting = false;
        this.metrics = {
            errors: 0,
            recoveries: 0,
            startTime: Date.now()
        };

        // Configuration (loaded dynamically)
        this.config = {
            enableML: true,
            autoRecovery: true,
            maxRetries: 5,
            retryDelays: [100, 500, 1000, 2000, 5000],
            anomalyThreshold: 0.8,
            predictionThreshold: 0.7
        };

        // Wait for config service AND backend to be ready before initializing
        // This prevents WebSocket connection spam during startup
        this.backendReady = false;
        this.initializeWhenBackendReady();

        // Browser detection
        this.browserInfo = this.detectBrowser();

        // =====================================================================
        // PERMISSION STATE TRACKING - Prevents infinite retry loops
        // =====================================================================
        // In Chrome Incognito mode, once permission is denied, the browser
        // remembers this for the session and auto-denies subsequent requests.
        // This tracking prevents useless retry loops.
        // =====================================================================
        this.permissionState = 'unknown';
        this.permissionDeniedAt = null;  // Timestamp of last denial
        this.permissionDenialCount = 0;  // Number of times denied this session
        this.isPermissionPermanentlyDenied = false;  // True after user denies in prompt
        
        // Start async permission state monitoring
        this.checkPermissionState();
        this.startPermissionMonitoring();
    }
    
    /**
     * Start monitoring permission state changes using the Permissions API.
     * This allows us to detect when the user changes permissions in browser settings.
     */
    async startPermissionMonitoring() {
        try {
            if (!navigator.permissions || !navigator.permissions.query) {
                console.log('Permissions API not available - using fallback detection');
                return;
            }
            
            const permission = await navigator.permissions.query({ name: 'microphone' });
            
            // Update state immediately
            this.updatePermissionState(permission.state);
            
            // Listen for changes (e.g., user changes in browser settings)
            permission.addEventListener('change', () => {
                console.log(`🎤 Permission state changed: ${this.permissionState} → ${permission.state}`);
                this.updatePermissionState(permission.state);
                
                // If permission was granted after being denied, reset denial tracking
                if (permission.state === 'granted') {
                    this.permissionDeniedAt = null;
                    this.permissionDenialCount = 0;
                    this.isPermissionPermanentlyDenied = false;
                    console.log('✅ Microphone permission granted - denial tracking reset');
                }
            });
            
        } catch (error) {
            console.warn('Could not start permission monitoring:', error);
        }
    }
    
    /**
     * Update permission state and related tracking variables.
     */
    updatePermissionState(state) {
        this.permissionState = state;
        
        if (state === 'denied') {
            this.isPermissionPermanentlyDenied = true;
            this.permissionDeniedAt = Date.now();
            this.permissionDenialCount++;
        }
    }
    
    /**
     * Check if we should skip retrying permission requests.
     * Returns true if permission was recently denied and retrying would be useless.
     */
    shouldSkipPermissionRetry() {
        // If permission is permanently denied, don't retry
        if (this.isPermissionPermanentlyDenied) {
            console.log('⛔ Permission is permanently denied - skipping retry');
            return true;
        }
        
        // If permission state is 'denied', don't retry
        if (this.permissionState === 'denied') {
            console.log('⛔ Permission state is denied - skipping retry');
            return true;
        }
        
        // If denied recently (within 30 seconds), don't retry
        if (this.permissionDeniedAt && (Date.now() - this.permissionDeniedAt) < 30000) {
            console.log('⛔ Permission denied recently - skipping retry (wait 30s)');
            return true;
        }
        
        // If denied multiple times this session, don't retry automatically
        if (this.permissionDenialCount >= 2) {
            console.log(`⛔ Permission denied ${this.permissionDenialCount} times - manual intervention required`);
            return true;
        }
        
        return false;
    }

    async initializeWhenBackendReady() {
        try {
            // First wait for config service to discover backend
            await configService.waitForConfig();
            console.log('ML Audio: Config discovered, waiting for backend to be ready...');
            
            // Now wait for backend to be FULLY ready (not just discovered)
            // This prevents WebSocket connection spam during startup
            const maxWaitTime = 60000; // 60 seconds max
            const startTime = Date.now();
            
            while (Date.now() - startTime < maxWaitTime) {
                const backendState = configService.getBackendState();
                
                // Check if backend is truly ready (startup complete, WebSocket available)
                if (backendState.ready || backendState.status === 'ready') {
                    console.log('ML Audio: Backend is READY, initializing WebSocket...');
                    this.backendReady = true;
                    break;
                }
                
                // Also check startup progress > 90% as an indicator
                const startupProgress = configService.getStartupProgress();
                if (startupProgress && startupProgress.progress >= 90) {
                    console.log(`ML Audio: Backend startup at ${startupProgress.progress}%, initializing...`);
                    this.backendReady = true;
                    break;
                }
                
                // Wait and check again
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
            
            if (!this.backendReady) {
                // Timeout - try anyway but with warnings
                console.warn('ML Audio: Backend readiness timeout - attempting connection anyway');
                this.backendReady = true;
            }

            // Load configuration from backend
            await this.loadConfiguration();

            // Initialize WebSocket connection to ML backend
            // Delay slightly to ensure backend WebSocket endpoints are ready
            setTimeout(() => this.connectToMLBackend(), 3000);
        } catch (error) {
            console.error('ML Audio: Failed to initialize', error);
            // Retry initialization with longer delay
            setTimeout(() => this.initializeWhenBackendReady(), 10000);
        }
    }
    
    // Legacy method name for compatibility
    async initializeWhenReady() {
        return this.initializeWhenBackendReady();
    }

    async loadConfiguration() {
        try {
            const apiUrl = configService.getApiUrl('audio/ml/config');
            if (!apiUrl) {
                console.warn('ML Audio: API URL not available yet');
                return;
            }
            
            const response = await fetch(apiUrl);
            if (response.ok) {
                const config = await response.json();
                this.config = { ...this.config, ...config };
                console.log('Loaded ML audio configuration:', this.config);
            }
        } catch (error) {
            console.warn('Using default ML audio configuration');
        }
    }

    async connectToMLBackend() {
        // Don't attempt connection if backend isn't ready yet
        if (!this.backendReady) {
            // Silent skip - will be called again after backend is ready
            return;
        }
        
        if (this.isConnecting || this.connectionAttempts >= this.maxConnectionAttempts) {
            if (this.connectionAttempts >= this.maxConnectionAttempts) {
                // Only warn once
                if (!this._maxAttemptsWarned) {
                    console.debug('ML Audio: Max connection attempts reached, operating without ML features');
                    this._maxAttemptsWarned = true;
                }
            }
            return;
        }

        this.isConnecting = true;
        this.connectionAttempts++;

        try {
            // ---------------------------------------------------------------------
            // PREFERRED: Reuse the SINGLE unified WebSocket from JarvisConnectionService
            // ---------------------------------------------------------------------
            // CRITICAL: Avoid creating competing /ws sockets (can trip backend rate limits
            // and cause reconnect loops across the app). We piggyback on the existing
            // connection and subscribe via the service event bus.
            try {
                const connectionService = getJarvisConnectionService();
                if (connectionService && connectionService.getState() === JarvisConnState.ONLINE) {
                    const sharedWs = connectionService.getWebSocket();
                    if (sharedWs && sharedWs.readyState === WebSocket.OPEN) {
                        this.ws = sharedWs;

                        // Subscribe once to unified message stream (no ws.onmessage overwrite!)
                        if (!this._unsubscribeJarvisMessage) {
                            this._unsubscribeJarvisMessage = connectionService.on('message', ({ data }) => {
                                try {
                                    // Only handle ML-relevant messages
                                    if (data && typeof data === 'object') {
                                        this.handleMLMessage(data);
                                    }
                                } catch {
                                    // Silent
                                }
                            });
                        }

                        if (this.connectionAttempts === 1) {
                            console.log('✅ ML Audio: Using shared WebSocket from JarvisConnectionService');
                        }

                        this.connectionAttempts = 0;
                        this.currentBackoffDelay = 1000;
                        this.isConnecting = false;
                        this._maxAttemptsWarned = false;
                        this.sendTelemetry('connection', { status: 'connected_shared' });
                        return;
                    }
                }

                // If the main service is still starting up, don't open a parallel /ws socket.
                if (connectionService && (
                    connectionService.getState() === JarvisConnState.INITIALIZING ||
                    connectionService.getState() === JarvisConnState.DISCOVERING ||
                    connectionService.getState() === JarvisConnState.CONNECTING ||
                    connectionService.getState() === JarvisConnState.RECONNECTING
                )) {
                    this.isConnecting = false;
                    setTimeout(() => this.connectToMLBackend(), 3000);
                    return;
                }
            } catch {
                // Ignore and fall back to ML-specific socket below
            }

            // Prefer the ML-specific endpoint. Only fall back to /ws if needed.
            // (If /ws is already in use by the main app, this avoids competing connections.)
            let wsUrl = configService.getWebSocketUrl('audio/ml/stream');
            if (!wsUrl) {
                wsUrl = configService.getWebSocketUrl('ws');
            }
            if (!wsUrl) {
                // Silent debug log, not warning
                if (this.connectionAttempts === 1) {
                    console.debug('ML Audio: Waiting for WebSocket URL...');
                }
                this.isConnecting = false;
                setTimeout(() => this.connectToMLBackend(), 5000);
                return;
            }
            
            // Only log on first connection attempt
            if (this.connectionAttempts === 1) {
                console.log('ML Audio: Connecting to backend...');
            }
            
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log('✅ ML Audio: Connected to backend WebSocket');
                this.connectionAttempts = 0;
                this.currentBackoffDelay = 1000;
                this.isConnecting = false;
                this._maxAttemptsWarned = false;
                this.sendTelemetry('connection', { status: 'connected' });
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    this.handleMLMessage(data);
                } catch (e) {
                    // Ignore non-JSON messages silently
                }
            };

            this.ws.onerror = (error) => {
                // Silent - errors will be handled by onclose
            };

            this.ws.onclose = () => {
                this.isConnecting = false;
                if (this.connectionAttempts < this.maxConnectionAttempts) {
                    // Exponential backoff with jitter to prevent thundering herd
                    const jitter = Math.random() * 1000;
                    const delay = Math.min(this.currentBackoffDelay + jitter, 30000);
                    
                    // Only log every 5th attempt to reduce spam
                    if (this.connectionAttempts % 5 === 0) {
                        console.debug(`ML Audio: Reconnecting (attempt ${this.connectionAttempts}/${this.maxConnectionAttempts})`);
                    }
                    
                    setTimeout(() => this.connectToMLBackend(), delay);
                    this.currentBackoffDelay = Math.floor(this.currentBackoffDelay * this.backoffMultiplier);
                }
            };
        } catch (error) {
            this.isConnecting = false;
            // Silent reconnect
            setTimeout(() => this.connectToMLBackend(), 5000);
        }
    }

    handleMLMessage(data) {
        switch (data.type) {
            case 'prediction':
                this.handlePrediction(data.prediction);
                break;
            case 'strategy':
                this.handleStrategy(data.strategy);
                break;
            case 'anomaly':
                this.handleAnomaly(data.anomaly);
                break;
            case 'metrics':
                this.updateMetrics(data.metrics);
                break;
        }
    }

    async handleAudioError(error, recognition) {
        // Handle common expected errors silently
        const silentErrors = ['no-speech', 'aborted', 'network'];
        const errorCode = error.error || error.name;
        
        if (silentErrors.includes(errorCode)) {
            // Update metrics silently without console spam
            this.metrics[`${errorCode}Events`] = (this.metrics[`${errorCode}Events`] || 0) + 1;
            
            // Only log actual problems, not routine events
            if (errorCode === 'network' && this.metrics.networkEvents % 10 === 1) {
                console.debug('ML Audio: Intermittent network issue (monitoring)');
            }
        } else {
            // Log actual unexpected errors
            console.warn('ML Audio: Unexpected error:', errorCode);
            this.metrics.errors++;
        }

        // Create error context
        const context = {
            error_code: error.error || error.name,
            browser: this.browserInfo.name,
            browser_version: this.browserInfo.version,
            timestamp: new Date().toISOString(),
            session_duration: Date.now() - this.metrics.startTime,
            retry_count: this.getRetryCount(error.error),
            permission_state: this.permissionState,
            user_agent: navigator.userAgent,
            audio_context_state: this.getAudioContextState(),
            previous_errors: this.getRecentErrors(5)
        };

        // Record error
        this.errorHistory.push({
            ...context,
            resolved: false
        });

        // Send to ML backend
        const response = await this.sendErrorToBackend(context);

        if (response) {
            // Check if response contains a strategy object with action
            if (response.strategy && response.strategy.action) {
                // Execute ML-recommended strategy
                return await this.executeStrategy(response.strategy, error, recognition);
            } else if (response.success === false) {
                // Backend couldn't provide a strategy, log the response
                console.log('ML backend response:', response);
                // Fallback to local strategy
                return await this.executeLocalStrategy(error, recognition);
            }
        } else {
            // No response from backend, fallback to local strategy
            return await this.executeLocalStrategy(error, recognition);
        }
        
        // Default return if nothing else worked
        return { success: false, message: 'No recovery strategy available' };
    }

    async executeLocalStrategy(error, recognition) {
        // Local fallback strategies when ML backend is unavailable
        console.log('Executing local recovery strategy for:', error.error);
        
        switch (error.error) {
            case 'no-speech':
                // No-speech is normal, just return success
                return { success: true, message: 'No speech detected (normal)' };
                
            case 'audio-capture':
            case 'not-allowed':
                // Try to request permission
                try {
                    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    stream.getTracks().forEach(track => track.stop());
                    return { success: true, message: 'Permission granted', newContext: true };
                } catch (e) {
                    return { success: false, message: 'Permission denied' };
                }
                
            case 'network':
                // Network error - implement retry logic
                console.log('Handling network error with retry strategy');
                
                // Stop current recognition
                try {
                    recognition.stop();
                } catch (e) {
                    console.log('Recognition already stopped');
                }
                
                // Wait a bit then restart
                await new Promise(resolve => setTimeout(resolve, 1000));
                
                try {
                    recognition.start();
                    return { 
                        success: true, 
                        message: 'Network error recovered - recognition restarted',
                        strategy: 'network_retry'
                    };
                } catch (e) {
                    console.error('Failed to restart after network error:', e);
                    return { 
                        success: false, 
                        message: 'Network error - unable to restart recognition',
                        needsManualRestart: true
                    };
                }
                
            default:
                // Unknown error
                return { success: false, message: `Unknown error: ${error.error}` };
        }
    }

    async sendErrorToBackend(context) {
        try {
            const apiUrl = configService.getApiUrl('audio/ml/error');
            if (!apiUrl) {
                console.warn('ML Audio: API URL not available for error endpoint');
                return null;
            }
            const response = await fetch(apiUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(context)
            });

            if (response.ok) {
                return await response.json();
            }
        } catch (error) {
            console.error('Failed to send error to ML backend:', error);
        }
        return null;
    }

    async executeStrategy(strategy, error, recognition) {
        console.log('Executing ML strategy:', strategy);

        // Handle case where strategy doesn't have an action property
        if (!strategy || !strategy.action) {
            console.warn('Invalid strategy format:', strategy);
            return { success: false, message: 'Invalid strategy format' };
        }

        const action = strategy.action;

        // Ensure action has a type
        if (!action.type) {
            console.warn('Strategy action missing type:', action);
            return { success: false, message: 'Strategy action missing type' };
        }

        switch (action.type) {
            case 'request_media_permission':
                return await this.requestPermissionWithRetry(action.params);

            case 'show_instructions':
                return this.showInstructions(action.params);

            case 'restart_audio_context':
                return await this.restartAudioContext(recognition);

            case 'enable_text_fallback':
                return this.enableTextFallback(action.params);

            case 'show_system_settings':
                return this.showSystemSettings(action.params);

            case 'network_retry':
                // Network error recovery with exponential backoff
                return await this.handleNetworkRetry(action.params, recognition);

            default:
                console.warn('Unknown strategy action:', action.type);
                return { success: false };
        }
    }

    async executeLocalStrategy(error, recognition) {
        // Only log for non-no-speech errors
        if (error.error !== 'no-speech') {
            console.log('Executing local fallback strategy for error:', error.error);
        }

        // Local strategy based on error type
        switch (error.error) {
            case 'not-allowed':
            case 'permission-denied':
                // =============================================================
                // CRITICAL: Check if we should skip retry to prevent infinite loop
                // =============================================================
                // In Chrome Incognito and after user denial, retrying is useless
                // and creates an infinite loop that trips the circuit breaker.
                // =============================================================
                if (this.shouldSkipPermissionRetry()) {
                    // Track the denial
                    this.updatePermissionState('denied');
                    
                    // Show instructions to user instead of retrying
                    this.showMicrophonePermissionHelp();
                    
                    return {
                        success: false,
                        message: 'Microphone permission denied - manual intervention required',
                        skipRestart: true,  // CRITICAL: Prevent restart loop
                        permissionDenied: true,
                        action: 'show_instructions'
                    };
                }
                
                // First time denial - try to request permission once
                return await this.requestPermissionWithRetry({ maxAttempts: 1 });

            case 'no-speech':
                // No speech detected is often not a critical error
                return {
                    success: true,
                    message: 'No speech detected - continuing to listen'
                };

            case 'audio-capture':
                // Check if this is actually a permission issue
                if (this.shouldSkipPermissionRetry()) {
                    return {
                        success: false,
                        message: 'Audio capture failed - permission may be denied',
                        skipRestart: true,
                        permissionDenied: true
                    };
                }
                // Try to restart audio context
                return await this.restartAudioContext(recognition);

            case 'network':
            case 'service-not-allowed':
                // Network errors might resolve on their own
                return {
                    success: false,
                    message: 'Network or service error - will retry automatically'
                };

            case 'aborted':
                // Recognition was aborted (likely due to restart)
                // This is expected during microphone restarts, treat as success
                return {
                    success: true,
                    message: 'Recognition aborted - normal during restart cycle',
                    skipRestart: true  // Don't trigger another restart
                };

            default:
                console.warn('No local strategy for error type:', error.error);
                return {
                    success: false,
                    message: `Unhandled error type: ${error.error}`
                };
        }
    }
    
    /**
     * Show helpful instructions for enabling microphone permission.
     * This is called when automatic retry has failed or is skipped.
     */
    showMicrophonePermissionHelp() {
        console.log('📋 Showing microphone permission help to user');
        
        // Dispatch an event that the UI can listen to
        const event = new CustomEvent('microphonePermissionDenied', {
            detail: {
                browser: this.browserInfo.name,
                isIncognito: this.detectIncognitoMode(),
                instructions: this.getPermissionInstructions()
            }
        });
        window.dispatchEvent(event);
    }
    
    /**
     * Detect if browser is in incognito/private mode.
     * Note: This is heuristic-based and may not be 100% accurate.
     */
    detectIncognitoMode() {
        return new Promise((resolve) => {
            // Chrome Incognito detection
            if (window.webkitRequestFileSystem) {
                window.webkitRequestFileSystem(
                    window.TEMPORARY, 1,
                    () => resolve(false),
                    () => resolve(true)
                );
            } else {
                // Firefox/Safari detection using storage estimate
                navigator.storage?.estimate?.().then(({ quota }) => {
                    // In private mode, quota is usually limited
                    resolve(quota < 120000000);
                }).catch(() => resolve(false));
            }
        });
    }
    
    /**
     * Get browser-specific instructions for enabling microphone.
     */
    getPermissionInstructions() {
        const browser = this.browserInfo.name.toLowerCase();
        
        const instructions = {
            chrome: [
                'Click the 🔒 lock icon in the address bar',
                'Select "Site settings"',
                'Set Microphone to "Allow"',
                'Reload the page'
            ],
            firefox: [
                'Click the 🔒 lock icon in the address bar',
                'Click "Connection secure" then "More information"',
                'Go to Permissions tab',
                'Allow Microphone access'
            ],
            safari: [
                'Go to Safari → Preferences → Websites',
                'Select Microphone from the sidebar',
                'Allow access for this website'
            ],
            default: [
                'Open browser settings',
                'Go to Privacy & Security → Site Settings',
                'Allow Microphone access for this site',
                'Reload the page'
            ]
        };
        
        return instructions[browser] || instructions.default;
    }

    async requestPermissionWithRetry(params = {}) {
        const { 
            retryDelays = this.config.retryDelays,
            maxAttempts = retryDelays.length  // Allow limiting attempts
        } = params;
        
        // =============================================================
        // PRE-CHECK: Use Permissions API to check current state
        // =============================================================
        // This avoids unnecessary getUserMedia calls that won't succeed
        // =============================================================
        try {
            if (navigator.permissions && navigator.permissions.query) {
                const permission = await navigator.permissions.query({ name: 'microphone' });
                
                if (permission.state === 'denied') {
                    console.log('⛔ Permissions API reports microphone is denied - skipping getUserMedia');
                    this.updatePermissionState('denied');
                    
                    return {
                        success: false,
                        message: 'Microphone permission is denied in browser settings',
                        skipRestart: true,
                        permissionDenied: true,
                        needsManualReset: true
                    };
                }
                
                if (permission.state === 'granted') {
                    console.log('✅ Permissions API reports microphone is already granted');
                    // Still try getUserMedia to get actual stream access
                }
                
                // 'prompt' state means we can try to request
            }
        } catch (permError) {
            // Permissions API not available, proceed with getUserMedia
            console.log('Permissions API check failed, proceeding with getUserMedia');
        }

        // Limit attempts to prevent infinite loops
        const actualMaxAttempts = Math.min(maxAttempts, retryDelays.length, 3);  // Cap at 3
        
        for (let i = 0; i < actualMaxAttempts; i++) {
            try {
                console.log(`Requesting microphone permission (attempt ${i + 1}/${actualMaxAttempts})`);

                const stream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true
                    }
                });

                // Success! Clean up and report
                stream.getTracks().forEach(track => track.stop());

                // Reset denial tracking on success
                this.permissionState = 'granted';
                this.permissionDeniedAt = null;
                this.permissionDenialCount = 0;
                this.isPermissionPermanentlyDenied = false;

                this.sendTelemetry('recovery', {
                    method: 'request_permission',
                    attempts: i + 1,
                    success: true
                });

                this.metrics.recoveries++;

                return {
                    success: true,
                    message: 'Microphone permission granted',
                    attempts: i + 1,
                    newContext: true  // Signal that UI should restart recognition
                };

            } catch (error) {
                console.error(`Permission attempt ${i + 1} failed:`, error.name, error.message);
                
                // Check if this is a hard denial (user clicked deny or settings-based)
                if (error.name === 'NotAllowedError') {
                    // Update denial tracking
                    this.updatePermissionState('denied');
                    
                    // If user explicitly denied (not just a timing issue), stop immediately
                    if (error.message.includes('Permission denied') || 
                        error.message.includes('denied') ||
                        this.permissionDenialCount >= 2) {
                        console.log('🚫 Hard permission denial detected - stopping retries');
                        
                        return {
                            success: false,
                            message: 'Microphone permission denied by user or browser',
                            skipRestart: true,  // CRITICAL: Prevent restart loop
                            permissionDenied: true,
                            needsManualReset: true,
                            attempts: i + 1
                        };
                    }
                }

                // Only retry if not at the last attempt
                if (i < actualMaxAttempts - 1) {
                    const delay = retryDelays[Math.min(i, retryDelays.length - 1)];
                    console.log(`Retrying in ${delay}ms...`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                }
            }
        }

        // All attempts failed
        this.updatePermissionState('denied');
        
        return {
            success: false,
            message: 'Failed to obtain microphone permission after retries',
            skipRestart: true,  // Prevent restart loop after all retries exhausted
            permissionDenied: true,
            attempts: actualMaxAttempts
        };
    }

    showInstructions(params) {
        const { instructions, browser } = params;

        // Create or update instruction UI
        let instructionDiv = document.getElementById('ml-audio-instructions');
        if (!instructionDiv) {
            instructionDiv = document.createElement('div');
            instructionDiv.id = 'ml-audio-instructions';
            instructionDiv.className = 'ml-audio-instructions';
            document.body.appendChild(instructionDiv);
        }

        // Generate instruction HTML
        const html = `
            <div class="ml-instructions-container">
                <div class="ml-instructions-header">
                    <span class="ml-icon">🎤</span>
                    <h3>Microphone Permission Required</h3>
                    <button class="ml-close" onclick="this.parentElement.parentElement.parentElement.remove()">×</button>
                </div>
                <div class="ml-instructions-body">
                    <p>To use voice commands, please grant microphone access:</p>
                    <ol>
                        ${instructions.map(step => `<li>${step}</li>`).join('')}
                    </ol>
                    <div class="ml-browser-specific">
                        <img src="/images/browsers/${browser}.svg" alt="${browser}" />
                        <span>Instructions for ${browser}</span>
                    </div>
                </div>
                <div class="ml-instructions-footer">
                    <button class="ml-retry-button" onclick="window.mlAudioHandler.retryPermission()">
                        🔄 Retry Permission
                    </button>
                    <button class="ml-text-mode-button" onclick="window.mlAudioHandler.enableTextMode()">
                        ⌨️ Use Text Mode
                    </button>
                </div>
            </div>
        `;

        instructionDiv.innerHTML = html;
        instructionDiv.style.display = 'block';

        // Add animation
        requestAnimationFrame(() => {
            instructionDiv.classList.add('ml-show');
        });

        return { success: true, message: 'Instructions displayed' };
    }

    async restartAudioContext(recognition) {
        try {
            // Stop current recognition
            if (recognition && recognition.stop) {
                recognition.stop();
            }

            // Create new audio context
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            const newContext = new AudioContext();

            // Test with getUserMedia
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const source = newContext.createMediaStreamSource(stream);

            // Verify it works
            const analyser = newContext.createAnalyser();
            source.connect(analyser);

            // Clean up test
            stream.getTracks().forEach(track => track.stop());

            this.sendTelemetry('recovery', {
                method: 'restart_audio_context',
                success: true
            });

            return {
                success: true,
                message: 'Audio context restarted successfully',
                newContext
            };

        } catch (error) {
            console.error('Failed to restart audio context:', error);
            return {
                success: false,
                message: 'Audio context restart failed'
            };
        }
    }

    async handleNetworkRetry(params, recognition) {
        /**
         * Handle network error recovery with intelligent retry logic.
         *
         * Implements:
         * - Exponential backoff delay
         * - Maximum retry limit
         * - Circuit breaker integration
         * - Connection health monitoring
         */
        const { delay = 1000, max_retries = 3, retry_count = 0 } = params;

        console.log(`[ML Audio] Network retry strategy: attempt ${retry_count + 1}/${max_retries}, delay ${delay}ms`);

        // Check if we've exceeded retry limit
        if (retry_count >= max_retries) {
            console.warn('[ML Audio] Max network retries exceeded');
            return {
                success: false,
                message: 'Maximum network retry attempts exceeded',
                needsManualIntervention: true
            };
        }

        try {
            // Stop current recognition
            if (recognition && recognition.stop) {
                try {
                    recognition.stop();
                } catch (e) {
                    // Recognition may already be stopped
                    console.debug('[ML Audio] Recognition already stopped');
                }
            }

            // Wait for specified delay (exponential backoff from backend)
            await new Promise(resolve => setTimeout(resolve, delay));

            // Verify network connectivity before restarting
            const isOnline = navigator.onLine;
            if (!isOnline) {
                console.warn('[ML Audio] Network still offline, waiting...');

                // Wait for online event (max 5 seconds)
                const waitForOnline = new Promise((resolve, reject) => {
                    const timeout = setTimeout(() => {
                        window.removeEventListener('online', onlineHandler);
                        reject(new Error('Network timeout'));
                    }, 5000);

                    const onlineHandler = () => {
                        clearTimeout(timeout);
                        window.removeEventListener('online', onlineHandler);
                        resolve();
                    };

                    window.addEventListener('online', onlineHandler);
                });

                try {
                    await waitForOnline;
                    console.log('[ML Audio] Network came back online');
                } catch {
                    return {
                        success: false,
                        message: 'Network still offline after timeout',
                        shouldRetry: retry_count + 1 < max_retries
                    };
                }
            }

            // Restart recognition
            if (recognition && recognition.start) {
                try {
                    recognition.start();

                    this.sendTelemetry('recovery', {
                        method: 'network_retry',
                        success: true,
                        retry_count: retry_count + 1,
                        delay
                    });

                    return {
                        success: true,
                        message: `Network recovery successful after ${retry_count + 1} attempts`,
                        retry_count: retry_count + 1
                    };
                } catch (startError) {
                    console.error('[ML Audio] Failed to restart recognition:', startError);

                    // If this wasn't the last retry, suggest retrying
                    if (retry_count + 1 < max_retries) {
                        return {
                            success: false,
                            message: 'Failed to restart recognition, will retry',
                            shouldRetry: true,
                            retry_count: retry_count + 1
                        };
                    }

                    return {
                        success: false,
                        message: 'Failed to restart recognition after all retries',
                        needsManualIntervention: true
                    };
                }
            }

            return {
                success: false,
                message: 'Recognition object not available'
            };

        } catch (error) {
            console.error('[ML Audio] Network retry error:', error);

            this.sendTelemetry('recovery', {
                method: 'network_retry',
                success: false,
                retry_count: retry_count + 1,
                error: error.message
            });

            return {
                success: false,
                message: `Network retry failed: ${error.message}`,
                shouldRetry: retry_count + 1 < max_retries
            };
        }
    }

    enableTextFallback(params) {
        // Emit event for UI to handle
        window.dispatchEvent(new CustomEvent('enableTextFallback', {
            detail: params
        }));

        this.sendTelemetry('fallback', {
            mode: 'text',
            reason: 'audio_error'
        });

        return {
            success: true,
            message: 'Text input mode enabled'
        };
    }

    showSystemSettings(params) {
        const { os, setting } = params;

        // OS-specific guidance
        const guides = {
            macos: {
                microphone_permissions: [
                    'Open System Preferences',
                    'Go to Security & Privacy → Privacy',
                    'Select Microphone from the left sidebar',
                    'Ensure your browser is checked ✓',
                    'Restart your browser after changes'
                ]
            },
            windows: {
                microphone_permissions: [
                    'Open Settings (Win + I)',
                    'Go to Privacy → Microphone',
                    'Ensure "Allow apps to access your microphone" is ON',
                    'Scroll down and ensure your browser is allowed',
                    'Restart your browser after changes'
                ]
            }
        };

        const instructions = guides[os]?.[setting] || ['Check system microphone settings'];

        return this.showInstructions({
            instructions,
            browser: 'system'
        });
    }

    // Predictive capabilities
    async predictAudioIssue() {
        if (!this.config.enableML) return null;

        const context = {
            browser: this.browserInfo.name,
            time_of_day: new Date().getHours(),
            day_of_week: new Date().getDay(),
            error_history: this.getRecentErrors(10),
            session_duration: Date.now() - this.metrics.startTime,
            permission_state: this.permissionState
        };

        try {
            const apiUrl = configService.getApiUrl('audio/ml/predict');
            if (!apiUrl) {
                console.warn('ML Audio: API URL not available for predict endpoint');
                return null;
            }
            const response = await fetch(apiUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(context)
            });

            if (response.ok) {
                const prediction = await response.json();

                if (prediction.probability > this.config.predictionThreshold) {
                    this.handlePrediction(prediction);
                }

                return prediction;
            }
        } catch (error) {
            console.error('Prediction request failed:', error);
        }

        return null;
    }

    handlePrediction(prediction) {
        console.log('ML Prediction:', prediction);

        if (prediction.probability > this.config.predictionThreshold) {
            // Proactive mitigation
            window.dispatchEvent(new CustomEvent('audioIssuePredicted', {
                detail: {
                    prediction,
                    suggestedAction: prediction.recommended_action
                }
            }));
        }
    }

    handleAnomaly(anomaly) {
        console.warn('Audio anomaly detected:', anomaly);

        // Log for analysis
        this.sendTelemetry('anomaly', anomaly);

        // Notify UI
        window.dispatchEvent(new CustomEvent('audioAnomaly', {
            detail: anomaly
        }));
    }

    // Utility methods
    detectBrowser() {
        const ua = navigator.userAgent;
        let name = 'unknown';
        let version = '';

        if (ua.includes('Chrome') && !ua.includes('Edg')) {
            name = 'chrome';
            version = ua.match(/Chrome\/(\d+)/)?.[1] || '';
        } else if (ua.includes('Safari') && !ua.includes('Chrome')) {
            name = 'safari';
            version = ua.match(/Version\/(\d+)/)?.[1] || '';
        } else if (ua.includes('Firefox')) {
            name = 'firefox';
            version = ua.match(/Firefox\/(\d+)/)?.[1] || '';
        } else if (ua.includes('Edg')) {
            name = 'edge';
            version = ua.match(/Edg\/(\d+)/)?.[1] || '';
        }

        return { name, version, ua };
    }

    async checkPermissionState() {
        if ('permissions' in navigator) {
            try {
                const result = await navigator.permissions.query({ name: 'microphone' });
                this.permissionState = result.state;

                result.addEventListener('change', () => {
                    this.permissionState = result.state;
                    this.sendTelemetry('permission_change', { state: result.state });
                });
            } catch (error) {
                console.warn('Permission API not fully supported');
            }
        }
    }

    getAudioContextState() {
        if (window.AudioContext || window.webkitAudioContext) {
            try {
                const context = new (window.AudioContext || window.webkitAudioContext)();
                const state = context.state;
                context.close();
                return state;
            } catch (error) {
                return 'error';
            }
        }
        return 'unsupported';
    }

    getRetryCount(errorCode) {
        return this.errorHistory.filter(e => e.error_code === errorCode).length;
    }

    getRecentErrors(count) {
        return this.errorHistory.slice(-count).map(e => ({
            code: e.error_code,
            timestamp: e.timestamp,
            resolved: e.resolved
        }));
    }

    sendTelemetry(event, data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            // Use ml_audio_stream type for unified WebSocket compatibility
            this.ws.send(JSON.stringify({
                type: 'ml_audio_stream',
                subtype: 'telemetry',
                event,
                data,
                timestamp: new Date().toISOString()
            }));
        }
    }

    updateMetrics(metrics) {
        console.log('ML Audio Metrics:', metrics);
        this.metrics = { ...this.metrics, ...metrics };

        // Emit metrics update event
        window.dispatchEvent(new CustomEvent('audioMetricsUpdate', {
            detail: this.metrics
        }));
    }

    // Public methods for UI integration
    async retryPermission() {
        return await this.requestPermissionWithRetry({});
    }

    enableTextMode() {
        return this.enableTextFallback({ showKeyboard: true });
    }

    getMetrics() {
        return {
            ...this.metrics,
            errorRate: this.metrics.errors / Math.max((Date.now() - this.metrics.startTime) / 60000, 1),
            recoveryRate: this.metrics.recoveries / Math.max(this.metrics.errors, 1)
        };
    }
}

// Create singleton instance
const mlAudioHandler = new MLAudioHandler();

// Make available globally for debugging
window.mlAudioHandler = mlAudioHandler;

export default mlAudioHandler;