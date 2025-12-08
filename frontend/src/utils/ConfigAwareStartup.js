/**
 * Config-Aware Startup Handler v2.0
 * ==================================
 * Advanced startup orchestration with:
 * - Robust async/await patterns with cancellation
 * - Backend state synchronization
 * - Progress tracking for UI components
 * - Automatic retry with exponential backoff
 * - Request queuing during initialization
 * - Circuit breaker integration
 */

import configService, {
  waitForConfig,
  getBackendState,
  getStartupProgress,
  onStartupProgress,
  onBackendReady,
  onBackendState
} from '../services/DynamicConfigService';

// Startup phases for progress tracking
const STARTUP_PHASE = {
  INITIALIZING: 'initializing',
  DISCOVERING: 'discovering',
  CONNECTING: 'connecting',
  SYNCING: 'syncing',
  READY: 'ready',
  FAILED: 'failed'
};

class ConfigAwareStartup {
  constructor() {
    this.initialized = false;
    this.configReady = false;
    this.pendingCallbacks = [];
    this.pendingRequests = [];
    this.config = null;

    // Enhanced state tracking
    this.state = {
      phase: STARTUP_PHASE.INITIALIZING,
      progress: 0,
      message: 'Initializing JARVIS...',
      backendConnected: false,
      backendReady: false,
      lastError: null,
      startTime: Date.now()
    };

    // Retry configuration
    this.retryConfig = {
      maxRetries: 5,
      baseDelay: 1000,
      maxDelay: 30000,
      currentRetry: 0
    };

    // Request abort controller for cancellation
    this.abortController = null;

    // Initialize immediately
    this.init();
  }

  async init() {
    console.log('[ConfigAwareStartup] Initializing v2.0...');
    this._updateState(STARTUP_PHASE.DISCOVERING, 10, 'Discovering backend services...');

    // Create abort controller for timeout handling
    this.abortController = new AbortController();

    // Subscribe to config service events
    this._subscribeToEvents();

    // Wait for config with robust timeout handling
    try {
      this.config = await this._waitForConfigWithRetry(15000);
      this.configReady = true;

      this._updateState(STARTUP_PHASE.CONNECTING, 50, 'Connecting to backend...');
      console.log('[ConfigAwareStartup] Config ready:', this.config);

      // Process pending callbacks
      await this._processPendingCallbacks();

      // Wait for backend to be fully ready
      await this._waitForBackendReady(30000);

      this._updateState(STARTUP_PHASE.READY, 100, 'JARVIS is ready!');
      this.initialized = true;

      // Process any queued requests
      await this._processQueuedRequests();

    } catch (error) {
      console.error('[ConfigAwareStartup] Initialization failed:', error);
      this._handleInitError(error);
    }
  }

  /**
   * Subscribe to config service events for real-time updates
   */
  _subscribeToEvents() {
    // Listen for startup progress from config service
    onStartupProgress((progress) => {
      if (this.state.phase !== STARTUP_PHASE.READY) {
        this._updateState(
          progress.phase,
          Math.min(progress.progress, 90), // Reserve last 10% for backend ready
          progress.message
        );
      }
    });

    // Listen for backend state changes
    onBackendState((state) => {
      this.state.backendConnected = true;
      if (state.ready) {
        this.state.backendReady = true;
      }
      this._notifyStateChange();
    });

    // Listen for backend fully ready
    onBackendReady((state) => {
      this.state.backendReady = true;
      if (this.state.phase !== STARTUP_PHASE.READY) {
        this._updateState(STARTUP_PHASE.READY, 100, 'JARVIS is ready!');
      }
    });

    // Listen for config updates
    configService.on('config-updated', (newConfig) => {
      console.log('[ConfigAwareStartup] Config updated:', newConfig);
      this.config = newConfig;
      this._notifyStateChange();
    });
  }

  /**
   * Wait for config with retry logic
   */
  async _waitForConfigWithRetry(timeout) {
    const startTime = Date.now();

    while (this.retryConfig.currentRetry < this.retryConfig.maxRetries) {
      try {
        const remainingTime = timeout - (Date.now() - startTime);
        if (remainingTime <= 0) {
          throw new Error('Config discovery timeout');
        }

        const config = await Promise.race([
          waitForConfig(Math.min(remainingTime, 5000)),
          this._createTimeoutPromise(remainingTime)
        ]);

        // Reset retry counter on success
        this.retryConfig.currentRetry = 0;
        return config;

      } catch (error) {
        this.retryConfig.currentRetry++;
        const delay = this._calculateBackoff();

        console.warn(`[ConfigAwareStartup] Retry ${this.retryConfig.currentRetry}/${this.retryConfig.maxRetries} in ${delay}ms`);
        this._updateState(
          STARTUP_PHASE.DISCOVERING,
          10 + (this.retryConfig.currentRetry * 5),
          `Retrying connection (${this.retryConfig.currentRetry}/${this.retryConfig.maxRetries})...`
        );

        await new Promise(resolve => setTimeout(resolve, delay));
      }
    }

    // All retries exhausted, use fallback
    console.warn('[ConfigAwareStartup] Using fallback config');
    return this._getFallbackConfig();
  }

  /**
   * Wait for backend to be fully ready
   */
  async _waitForBackendReady(timeout) {
    if (this.state.backendReady) {
      return true;
    }

    this._updateState(STARTUP_PHASE.SYNCING, 70, 'Synchronizing with backend...');

    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      const backendState = getBackendState();

      if (backendState.ready) {
        this.state.backendReady = true;
        return true;
      }

      // Update progress based on backend state
      if (backendState.startupProgress) {
        const progress = 70 + (backendState.startupProgress * 0.3); // 70-100%
        this._updateState(
          STARTUP_PHASE.SYNCING,
          Math.min(progress, 99),
          backendState.status === 'minimal'
            ? 'Backend starting (minimal mode)...'
            : `Backend ${Math.round(backendState.startupProgress)}% ready...`
        );
      }

      await new Promise(resolve => setTimeout(resolve, 500));
    }

    // Timeout - but still usable
    console.warn('[ConfigAwareStartup] Backend ready timeout, proceeding anyway');
    return false;
  }

  /**
   * Calculate exponential backoff delay
   */
  _calculateBackoff() {
    const delay = this.retryConfig.baseDelay * Math.pow(2, this.retryConfig.currentRetry - 1);
    return Math.min(delay, this.retryConfig.maxDelay);
  }

  /**
   * Create a timeout promise for race conditions
   */
  _createTimeoutPromise(timeout) {
    return new Promise((_, reject) => {
      setTimeout(() => reject(new Error('Timeout')), timeout);
    });
  }

  /**
   * Get fallback config when discovery fails
   */
  _getFallbackConfig() {
    // Infer from environment
    const protocol = window.location.protocol === 'https:' ? 'https' : 'http';
    const wsProtocol = protocol === 'https' ? 'wss' : 'ws';
    const hostname = window.location.hostname || 'localhost';

    // Try common ports
    const port = 8000; // Default JARVIS port

    return {
      API_BASE_URL: `${protocol}://${hostname}:${port}`,
      WS_BASE_URL: `${wsProtocol}://${hostname}:${port}`,
      discovered: false,
      fallback: true
    };
  }

  /**
   * Handle initialization error
   */
  _handleInitError(error) {
    this.state.lastError = error.message;
    this._updateState(STARTUP_PHASE.FAILED, 0, `Connection failed: ${error.message}`);

    // Use fallback config
    this.config = this._getFallbackConfig();
    this.configReady = true;
    this.initialized = true;

    // Still process callbacks with fallback
    this._processPendingCallbacks();
  }

  /**
   * Update internal state and notify listeners
   */
  _updateState(phase, progress, message) {
    this.state.phase = phase;
    this.state.progress = progress;
    this.state.message = message;
    this._notifyStateChange();
  }

  /**
   * Notify all state change listeners
   */
  _notifyStateChange() {
    // Dispatch custom event for React components
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('jarvis-startup-state', {
        detail: this.getState()
      }));
    }
  }

  /**
   * Process pending callbacks
   */
  async _processPendingCallbacks() {
    const callbacks = [...this.pendingCallbacks];
    this.pendingCallbacks = [];

    for (const cb of callbacks) {
      try {
        await cb(this.config);
      } catch (error) {
        console.error('[ConfigAwareStartup] Callback error:', error);
      }
    }
  }

  /**
   * Process queued requests
   */
  async _processQueuedRequests() {
    const requests = [...this.pendingRequests];
    this.pendingRequests = [];

    for (const { endpoint, options, resolve, reject } of requests) {
      try {
        const result = await this.fetch(endpoint, options);
        resolve(result);
      } catch (error) {
        reject(error);
      }
    }
  }

  /**
   * Execute callback when config is ready
   */
  whenReady(callback) {
    if (this.configReady && this.config) {
      Promise.resolve(callback(this.config)).catch(console.error);
    } else {
      this.pendingCallbacks.push(callback);
    }
  }

  /**
   * Get API URL (waits for config if needed)
   */
  async getApiUrl(endpoint = '') {
    await this.ensureReady();

    const baseUrl = this.config?.API_BASE_URL || configService.getApiUrl();
    if (!baseUrl) {
      throw new Error('API URL not available');
    }

    return endpoint
      ? `${baseUrl}/${endpoint.replace(/^\//, '')}`
      : baseUrl;
  }

  /**
   * Get WebSocket URL (waits for config if needed)
   */
  async getWebSocketUrl(endpoint = '') {
    await this.ensureReady();

    const baseUrl = this.config?.WS_BASE_URL || configService.getWebSocketUrl();
    if (!baseUrl) {
      throw new Error('WebSocket URL not available');
    }

    return endpoint
      ? `${baseUrl}/${endpoint.replace(/^\//, '')}`
      : baseUrl;
  }

  /**
   * Ensure startup is complete
   */
  async ensureReady(timeout = 15000) {
    if (this.configReady && this.config) {
      return this.config;
    }

    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      if (this.configReady && this.config) {
        return this.config;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    // Use fallback
    this.config = this._getFallbackConfig();
    this.configReady = true;
    return this.config;
  }

  /**
   * Wait for full readiness (alias for ensureReady)
   */
  async waitForReady(timeout = 15000) {
    return this.ensureReady(timeout);
  }

  /**
   * Make API call with auto-discovery and retry
   */
  async fetch(endpoint, options = {}) {
    // Queue request if not ready
    if (!this.configReady) {
      return new Promise((resolve, reject) => {
        this.pendingRequests.push({ endpoint, options, resolve, reject });
      });
    }

    const url = await this.getApiUrl(endpoint);
    console.log(`[ConfigAwareStartup] Fetching: ${url}`);

    const fetchOptions = {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers
      }
    };

    // Add timeout if not provided
    if (!options.signal) {
      const controller = new AbortController();
      fetchOptions.signal = controller.signal;
      setTimeout(() => controller.abort(), options.timeout || 30000);
    }

    try {
      const response = await fetch(url, fetchOptions);

      // Trigger rediscovery on connection errors
      if (response.status === 404 || response.status >= 500) {
        console.warn(`[ConfigAwareStartup] Request failed (${response.status}), triggering rediscovery`);
        configService.discover();
      }

      return response;
    } catch (error) {
      console.error('[ConfigAwareStartup] Fetch error:', error);

      // Trigger rediscovery on network errors
      if (error.name !== 'AbortError') {
        configService.discover();
      }

      throw error;
    }
  }

  /**
   * Get current startup state
   */
  getState() {
    return {
      ...this.state,
      configReady: this.configReady,
      initialized: this.initialized,
      elapsedTime: Date.now() - this.state.startTime
    };
  }

  /**
   * Get current config
   */
  getConfig() {
    return this.config ? { ...this.config } : null;
  }

  /**
   * Check if fully ready
   */
  isReady() {
    return this.initialized && this.configReady && this.state.phase === STARTUP_PHASE.READY;
  }

  /**
   * Cancel ongoing initialization
   */
  cancel() {
    if (this.abortController) {
      this.abortController.abort();
    }
  }

  /**
   * Reset and reinitialize
   */
  async reset() {
    this.cancel();
    this.initialized = false;
    this.configReady = false;
    this.config = null;
    this.state.phase = STARTUP_PHASE.INITIALIZING;
    this.retryConfig.currentRetry = 0;
    this.pendingCallbacks = [];
    this.pendingRequests = [];

    await this.init();
  }
}

// Create singleton instance
const configAwareStartup = new ConfigAwareStartup();

// Export convenience functions
export const whenConfigReady = (callback) => configAwareStartup.whenReady(callback);
export const getApiUrl = async (endpoint) => configAwareStartup.getApiUrl(endpoint);
export const getWebSocketUrl = async (endpoint) => configAwareStartup.getWebSocketUrl(endpoint);
export const configFetch = async (endpoint, options) => configAwareStartup.fetch(endpoint, options);
export const getStartupState = () => configAwareStartup.getState();
export const isReady = () => configAwareStartup.isReady();
export const ensureReady = (timeout) => configAwareStartup.ensureReady(timeout);

// Export phase constants
export { STARTUP_PHASE };

export default configAwareStartup;
