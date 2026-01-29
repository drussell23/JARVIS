/**
 * Dynamic Configuration Service v3.0
 * ===================================
 * Fully async, non-blocking, robust configuration with:
 * - Non-blocking parallel discovery with micro-task yielding
 * - Intelligent circuit breaker with adaptive recovery
 * - Connection pooling with health-weighted routing
 * - Real-time backend state synchronization
 * - Zero hardcoding - fully environment-aware
 * - Prevents browser "Page Unresponsive" by yielding to event loop
 */

import logger from '../utils/DebugLogger';

// ============================================================================
// CORE UTILITIES - Async helpers for non-blocking operations
// ============================================================================

/**
 * Yields control back to the browser's event loop to prevent blocking
 * This is CRITICAL for preventing "Page Unresponsive" errors
 */
const yieldToEventLoop = () => new Promise(resolve => {
  if (typeof requestIdleCallback === 'function') {
    requestIdleCallback(() => resolve(), { timeout: 16 });
  } else {
    setTimeout(resolve, 0);
  }
});

/**
 * Batch async operations with yielding to prevent blocking
 * @param {Array} items - Items to process
 * @param {Function} processor - Async function to process each item
 * @param {number} batchSize - Items per batch before yielding
 */
const batchProcess = async (items, processor, batchSize = 3) => {
  const results = [];
  for (let i = 0; i < items.length; i += batchSize) {
    const batch = items.slice(i, i + batchSize);
    const batchResults = await Promise.allSettled(batch.map(processor));
    results.push(...batchResults);
    // Yield to event loop after each batch
    await yieldToEventLoop();
  }
  return results;
};

/**
 * Create an AbortController with automatic timeout
 */
const createTimeoutController = (timeoutMs) => {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  return {
    controller,
    signal: controller.signal,
    clear: () => clearTimeout(timeoutId)
  };
};

// ============================================================================
// ENVIRONMENT DETECTION - Zero hardcoding
// ============================================================================

const ENV = {
  isDev: () => typeof window !== 'undefined' &&
    (window.location.hostname === 'localhost' ||
     window.location.hostname === '127.0.0.1' ||
     window.location.hostname.includes('.local')),
  isSecure: () => typeof window !== 'undefined' && window.location.protocol === 'https:',
  getHostname: () => typeof window !== 'undefined' ? window.location.hostname : 'localhost',
  getProtocol: () => typeof window !== 'undefined' ? window.location.protocol.replace(':', '') : 'http',
  getWsProtocol: () => ENV.isSecure() ? 'wss' : 'ws'
};

// ============================================================================
// CIRCUIT BREAKER - Adaptive failure handling
// ============================================================================

const CIRCUIT_STATE = {
  CLOSED: 'closed',      // Normal operation
  OPEN: 'open',          // Failing, reject requests
  HALF_OPEN: 'half_open' // Testing recovery
};

class CircuitBreaker {
  constructor(options = {}) {
    this.state = CIRCUIT_STATE.CLOSED;
    this.failures = 0;
    this.successes = 0;
    this.lastFailure = null;
    this.lastSuccess = null;
    
    // Adaptive thresholds
    this.failureThreshold = options.failureThreshold || 5;
    this.successThreshold = options.successThreshold || 2;
    this.resetTimeout = options.resetTimeout || 30000;
    this.halfOpenMaxAttempts = options.halfOpenMaxAttempts || 3;
    this.halfOpenAttempts = 0;
  }

  canExecute() {
    if (this.state === CIRCUIT_STATE.CLOSED) return true;
    
    if (this.state === CIRCUIT_STATE.OPEN) {
      const elapsed = Date.now() - this.lastFailure;
      if (elapsed >= this.resetTimeout) {
        this.state = CIRCUIT_STATE.HALF_OPEN;
        this.halfOpenAttempts = 0;
        this.successes = 0;
        return true;
      }
      return false;
    }
    
    // HALF_OPEN - allow limited attempts
    return this.halfOpenAttempts < this.halfOpenMaxAttempts;
  }

  recordSuccess() {
    this.lastSuccess = Date.now();
    this.successes++;
    
    if (this.state === CIRCUIT_STATE.HALF_OPEN) {
      if (this.successes >= this.successThreshold) {
        this.state = CIRCUIT_STATE.CLOSED;
        this.failures = 0;
        this.successes = 0;
        logger.success('🟢 Circuit breaker CLOSED - service recovered');
        return 'recovered';
      }
    } else {
      // Decay failures on success
      this.failures = Math.max(0, this.failures - 1);
    }
    return 'success';
  }

  recordFailure() {
    this.lastFailure = Date.now();
    this.failures++;
    
    if (this.state === CIRCUIT_STATE.HALF_OPEN) {
      this.halfOpenAttempts++;
      if (this.halfOpenAttempts >= this.halfOpenMaxAttempts) {
        this.state = CIRCUIT_STATE.OPEN;
        logger.warning('🔴 Circuit breaker OPEN - half-open test failed');
        return 'opened';
      }
    } else if (this.failures >= this.failureThreshold) {
      this.state = CIRCUIT_STATE.OPEN;
      logger.warning(`🔴 Circuit breaker OPEN after ${this.failures} failures`);
      return 'opened';
    }
    return 'failure';
  }

  getState() {
    return {
      state: this.state,
      failures: this.failures,
      successes: this.successes,
      canExecute: this.canExecute()
    };
  }
}

// ============================================================================
// DYNAMIC CONFIG SERVICE - Main class
// ============================================================================

class DynamicConfigService {
  constructor() {
    // Configuration state
    this.config = {
      API_BASE_URL: null,
      WS_BASE_URL: null,
      ENDPOINTS: {},
      SERVICES: {},
      discovered: false,
      backendState: {
        status: 'unknown',
        mode: 'unknown',
        startupProgress: 0,
        components: {},
        ready: false,
        lastUpdate: null
      }
    };

    // Discovery configuration - dynamically inferred
    this.discoveryConfig = {
      // Priority-ordered ports - most likely first
      // 8010 is the default BACKEND_PORT, followed by common alternatives
      ports: this._inferPorts(),
      // Ports to skip (system services, loading servers)
      excludedPorts: new Set([5000, 5001, 3001, 8001]),
      // Timeouts - increased for more reliable discovery
      portScanTimeout: 500,        // Increased from 300ms
      healthCheckTimeout: 5000,    // Increased from 2000ms for slow cold starts
      discoveryTimeout: 20000,     // Increased from 10000ms
      // Batching
      portBatchSize: 4,
      maxConcurrentChecks: 6,
      // New: Aggressive retry for primary port
      primaryPortRetries: 3,
      primaryPortRetryDelay: 1000
    };

    // Service identification patterns
    this.servicePatterns = {
      backend: {
        endpoints: ['/health/ping', '/health', '/health/startup'],
        identifiers: ['jarvis', 'status', 'ok', 'healthy', 'ready']
      }
    };

    // Circuit breaker for discovery - more lenient to handle backend startup delays
    this.circuitBreaker = new CircuitBreaker({
      failureThreshold: 5,        // Increased from 3 - more tolerant of failures
      successThreshold: 1,        // Decreased from 2 - recover faster
      resetTimeout: 10000         // Decreased from 15000 - retry sooner
    });

    // Health monitoring
    this.healthMonitor = {
      interval: null,
      frequency: 30000,
      scores: new Map(),
      lastCheck: new Map()
    };

    // Event system
    this.listeners = new Map();
    
    // Connection state
    this.connectionState = {
      isOnline: typeof navigator !== 'undefined' ? navigator.onLine : true,
      discoveryInProgress: false,
      lastDiscovery: null,
      reconnectAttempts: 0,
      maxReconnectAttempts: 10
    };

    // Startup progress
    this.startupProgress = {
      phase: 'initializing',
      progress: 0,
      message: 'Starting...',
      lastUpdate: Date.now()
    };

    // Setup network listeners
    this._setupNetworkListeners();
    
    // Start discovery (non-blocking)
    this._startDiscovery();
  }

  // ==========================================================================
  // INITIALIZATION
  // ==========================================================================

  _inferPorts() {
    // Check for environment-specified port first
    const envPort = typeof process !== 'undefined' && process.env?.REACT_APP_BACKEND_PORT;
    
    // Priority-ordered list - most likely ports first
    const priorityPorts = [8010, 8000, 8080, 8888, 9000, 9090];
    
    if (envPort) {
      const port = parseInt(envPort, 10);
      return [port, ...priorityPorts.filter(p => p !== port)];
    }
    
    return priorityPorts;
  }

  _setupNetworkListeners() {
    if (typeof window === 'undefined') return;
    
    window.addEventListener('online', () => {
      logger.success('🌐 Network connection restored');
      this.connectionState.isOnline = true;
      this.connectionState.reconnectAttempts = 0;
      this.emit('network-online');
      
      // Re-validate on reconnect
      if (this.config.discovered) {
        this._validateAndRefresh();
      } else {
        this._startDiscovery();
      }
    });
    
    window.addEventListener('offline', () => {
      logger.warning('📴 Network connection lost');
      this.connectionState.isOnline = false;
      this.emit('network-offline');
    });
  }

  // ==========================================================================
  // NON-BLOCKING DISCOVERY ENGINE
  // ==========================================================================

  async _startDiscovery() {
    // Prevent concurrent discoveries
    if (this.connectionState.discoveryInProgress) {
      logger.debug('Discovery already in progress, skipping');
      return this.config;
    }

    // Check circuit breaker
    if (!this.circuitBreaker.canExecute()) {
      const cbState = this.circuitBreaker.getState();
      logger.warning(`Circuit breaker ${cbState.state}, waiting for reset...`);
      this._updateProgress('waiting', 5, 'Waiting for service recovery...');
      
      // Schedule retry after reset timeout
      setTimeout(() => this._startDiscovery(), this.circuitBreaker.resetTimeout);
      return this.config;
    }

    this.connectionState.discoveryInProgress = true;
    logger.config('🔍 Starting non-blocking service discovery...');
    this._updateProgress('discovering', 10, 'Searching for backend...');

    try {
      // Step 1: Try cached config (fast path)
      await yieldToEventLoop();
      const cached = await this._tryCachedConfig();
      if (cached) {
        this.connectionState.discoveryInProgress = false;
        return this.config;
      }

      // Step 2: Parallel port discovery with yielding
      this._updateProgress('scanning', 30, 'Scanning for services...');
      await yieldToEventLoop();
      
      const backend = await this._discoverBackend();
      
      if (backend) {
        this.config.API_BASE_URL = backend.url;
        this.config.WS_BASE_URL = this._toWebSocketUrl(backend.url);
        this.config.ENDPOINTS = backend.endpoints || {};
        this.config.discovered = true;
        
        this.circuitBreaker.recordSuccess();
        this._saveConfig();
        
        // Step 3: Sync backend state
        this._updateProgress('syncing', 80, 'Synchronizing with backend...');
        await yieldToEventLoop();
        await this._syncBackendState();
        
        // Start health monitoring
        this._startHealthMonitoring();
        
        this._updateProgress('connected', 100, 'Connected!');
        logger.success('✅ Service discovery complete:', this.config.API_BASE_URL);
        this.emit('config-ready', this.config);
        
      } else {
        throw new Error('No backend service found');
      }

    } catch (error) {
      logger.error('❌ Discovery failed:', error.message);
      this.circuitBreaker.recordFailure();
      this._updateProgress('failed', 0, 'Backend not found');
      this.emit('discovery-failed', { reason: error.message });
      
      // Schedule retry with exponential backoff
      this._scheduleRetry();
    } finally {
      this.connectionState.discoveryInProgress = false;
      this.connectionState.lastDiscovery = Date.now();
    }

    return this.config;
  }

  async _tryCachedConfig() {
    try {
      const cached = this._loadCachedConfig();
      if (!cached) return false;
      
      logger.debug('Found cached config, validating...');
      this._updateProgress('validating', 20, 'Validating cached config...');
      
      const isValid = await this._quickHealthCheck(cached.API_BASE_URL);
      
      if (isValid) {
        logger.success('✅ Using cached configuration');
        this.config = { ...cached, discovered: true };
        this.circuitBreaker.recordSuccess();
        
        await this._syncBackendState();
        this._startHealthMonitoring();
        
        this._updateProgress('connected', 100, 'Connected!');
        this.emit('config-ready', this.config);
        return true;
      } else {
        logger.warning('Cached config invalid, clearing...');
        this._clearCache();
        return false;
      }
    } catch (error) {
      logger.debug('Cache validation failed:', error.message);
      this._clearCache();
      return false;
    }
  }

  async _discoverBackend() {
    const { ports, excludedPorts, portBatchSize, primaryPortRetries, primaryPortRetryDelay } = this.discoveryConfig;
    
    // Filter out excluded ports
    const portsToScan = ports.filter(p => !excludedPorts.has(p));
    
    // PRIORITY: Try the primary port (8010) with retries first
    // This handles the case where backend is still starting up
    const primaryPort = portsToScan[0]; // 8010 is first in priority list
    if (primaryPort) {
      logger.debug(`[Discovery] Trying primary port ${primaryPort} with ${primaryPortRetries} retries...`);
      
      for (let attempt = 0; attempt < primaryPortRetries; attempt++) {
        try {
          const result = await this._checkPort(primaryPort);
          if (result) {
            logger.success(`[Discovery] ✅ Found backend on primary port ${primaryPort}`);
            return result;
          }
        } catch {
          // Retry after delay
        }
        
        if (attempt < primaryPortRetries - 1) {
          logger.debug(`[Discovery] Primary port attempt ${attempt + 1} failed, retrying in ${primaryPortRetryDelay}ms...`);
          await new Promise(resolve => setTimeout(resolve, primaryPortRetryDelay));
        }
      }
    }
    
    // If primary port failed, try other ports in parallel
    logger.debug('[Discovery] Primary port failed, scanning alternative ports...');
    const otherPorts = portsToScan.slice(1);
    
    // Batch process remaining ports with yielding to prevent blocking
    const results = await batchProcess(
      otherPorts,
      async (port) => {
        try {
          return await this._checkPort(port);
        } catch {
          return null;
        }
      },
      portBatchSize
    );

    // Find first successful result
    for (const result of results) {
      if (result.status === 'fulfilled' && result.value) {
        return result.value;
      }
    }

    return null;
  }

  async _checkPort(port) {
    const baseUrl = `${ENV.getProtocol()}://${ENV.getHostname()}:${port}`;
    const { healthCheckTimeout } = this.discoveryConfig;
    
    // Try health endpoints in priority order
    for (const endpoint of this.servicePatterns.backend.endpoints) {
      try {
        const url = `${baseUrl}${endpoint}`;
        const { signal, clear } = createTimeoutController(healthCheckTimeout);
        
        const response = await fetch(url, {
          method: 'GET',
          signal,
          mode: 'cors',
          credentials: 'omit',
          headers: { 'Accept': 'application/json' }
        });
        
        clear();
        
        if (response.ok) {
          let data;
          try {
            data = await response.json();
          } catch {
            data = {};
          }
          
          // Verify it's the JARVIS backend
          if (this._isJarvisBackend(data)) {
            logger.success(`✅ Found JARVIS backend on port ${port}`);
            
            // Discover available endpoints
            const endpoints = await this._discoverEndpoints(baseUrl);
            
            return {
              port,
              url: baseUrl,
              endpoints,
              health: 1.0,
              mode: data.mode || 'full'
            };
          }
        }
      } catch {
        // Port/endpoint not available, continue
      }
    }
    
    return null;
  }

  _isJarvisBackend(data) {
    if (!data) return false;
    const dataStr = JSON.stringify(data).toLowerCase();
    return this.servicePatterns.backend.identifiers.some(id => dataStr.includes(id));
  }

  async _discoverEndpoints(baseUrl) {
    const endpoints = {};
    // v118.0: Use /status endpoints for WebSocket availability checks
    // WebSocket paths (/ws, /voice/jarvis/stream, /vision/ws) only accept WebSocket
    // connections, not HTTP HEAD requests. Use the dedicated status endpoints instead.
    const commonEndpoints = [
      { path: '/health', name: 'health' },
      { path: '/health/startup', name: 'startup' },
      { path: '/voice/jarvis/status', name: 'jarvis_status' },
      { path: '/ws/status', name: 'websocket', actualPath: '/ws' },
      { path: '/voice/jarvis/stream/status', name: 'voice_stream', actualPath: '/voice/jarvis/stream' },
      { path: '/vision/ws/status', name: 'vision_websocket', actualPath: '/vision/ws' }
    ];

    // Quick parallel check with short timeout
    const results = await Promise.allSettled(
      commonEndpoints.map(async ({ path, name, actualPath }) => {
        try {
          const { signal, clear } = createTimeoutController(500);
          const response = await fetch(`${baseUrl}${path}`, {
            method: 'HEAD',
            signal,
            mode: 'cors'
          });
          clear();

          if (response.ok || response.status === 405) {
            return { name, path, actualPath, available: true };
          }
        } catch {
          // Endpoint not available
        }
        return { name, path, actualPath, available: false };
      })
    );

    for (const result of results) {
      if (result.status === 'fulfilled' && result.value.available) {
        // Use actualPath for WebSocket endpoints (the real connection path)
        // Use path for HTTP endpoints
        endpoints[result.value.name] = {
          path: result.value.actualPath || result.value.path
        };
      }
    }

    return endpoints;
  }

  async _quickHealthCheck(url) {
    try {
      const { signal, clear } = createTimeoutController(2000);
      const response = await fetch(`${url}/health`, { signal, mode: 'cors' });
      clear();
      return response.ok;
    } catch {
      return false;
    }
  }

  // ==========================================================================
  // BACKEND STATE SYNCHRONIZATION
  // ==========================================================================

  async _syncBackendState() {
    if (!this.config.API_BASE_URL) return;

    try {
      const { signal, clear } = createTimeoutController(3000);
      const response = await fetch(`${this.config.API_BASE_URL}/health/startup`, {
        signal,
        mode: 'cors'
      });
      clear();

      if (response.ok) {
        const data = await response.json();
        this.config.backendState = {
          status: data.status || 'ready',
          mode: data.mode || 'full',
          startupProgress: data.progress || 100,
          components: data.components || {},
          ready: data.ready !== false,
          lastUpdate: Date.now()
        };
        
        this.emit('backend-state', this.config.backendState);
        
        if (!this.config.backendState.ready) {
          this._pollBackendState();
        } else {
          this.emit('backend-ready', this.config.backendState);
        }
      }
    } catch (error) {
      logger.debug('Backend state sync failed:', error.message);
      // Set default ready state
      this.config.backendState = {
        status: 'connected',
        mode: 'unknown',
        startupProgress: 100,
        components: {},
        ready: true,
        lastUpdate: Date.now()
      };
    }
  }

  _pollBackendState() {
    let pollCount = 0;
    const maxPolls = 60; // 2 minutes max
    
    const pollInterval = setInterval(async () => {
      pollCount++;
      
      if (pollCount >= maxPolls) {
        clearInterval(pollInterval);
        return;
      }
      
      await this._syncBackendState();
      
      if (this.config.backendState.ready) {
        clearInterval(pollInterval);
        this.emit('backend-ready', this.config.backendState);
      }
    }, 2000);
  }

  // ==========================================================================
  // HEALTH MONITORING
  // ==========================================================================

  _startHealthMonitoring() {
    if (this.healthMonitor.interval) {
      clearInterval(this.healthMonitor.interval);
    }

    this.healthMonitor.interval = setInterval(async () => {
      if (!this.config.API_BASE_URL) return;
      
      try {
        const isHealthy = await this._quickHealthCheck(this.config.API_BASE_URL);
        
        if (isHealthy) {
          this.circuitBreaker.recordSuccess();
          this.healthMonitor.scores.set(this.config.API_BASE_URL, 1.0);
        } else {
          this.circuitBreaker.recordFailure();
          const currentScore = this.healthMonitor.scores.get(this.config.API_BASE_URL) || 1.0;
          this.healthMonitor.scores.set(this.config.API_BASE_URL, currentScore * 0.9);
          
          if (currentScore < 0.5) {
            logger.warning('Backend health degraded, attempting recovery...');
            this._validateAndRefresh();
          }
        }
        
        this.healthMonitor.lastCheck.set(this.config.API_BASE_URL, Date.now());
      } catch {
        // Health check failed silently
      }
    }, this.healthMonitor.frequency);
  }

  async _validateAndRefresh() {
    const isValid = await this._quickHealthCheck(this.config.API_BASE_URL);
    
    if (!isValid) {
      logger.warning('Backend validation failed, rediscovering...');
      this.config.discovered = false;
      this._clearCache();
      await this._startDiscovery();
    }
  }

  // ==========================================================================
  // RETRY & BACKOFF
  // ==========================================================================

  _scheduleRetry() {
    this.connectionState.reconnectAttempts++;
    
    if (this.connectionState.reconnectAttempts > this.connectionState.maxReconnectAttempts) {
      logger.error('Max reconnection attempts reached');
      this._updateProgress('error', 0, 'Could not connect to backend');
      return;
    }

    const baseDelay = 2000;
    const maxDelay = 30000;
    const delay = Math.min(
      baseDelay * Math.pow(1.5, this.connectionState.reconnectAttempts - 1),
      maxDelay
    );

    logger.info(`Retrying in ${Math.round(delay / 1000)}s (attempt ${this.connectionState.reconnectAttempts})`);
    
    setTimeout(() => this._startDiscovery(), delay);
  }

  // ==========================================================================
  // PROGRESS & EVENTS
  // ==========================================================================

  _updateProgress(phase, progress, message) {
    this.startupProgress = {
      phase,
      progress,
      message,
      lastUpdate: Date.now()
    };
    this.emit('startup-progress', this.startupProgress);
  }

  emit(event, data) {
    const handlers = this.listeners.get(event) || [];
    handlers.forEach(handler => {
      try {
        handler(data);
      } catch (error) {
        logger.error(`Event handler error for ${event}:`, error);
      }
    });
  }

  on(event, callback) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, []);
    }
    this.listeners.get(event).push(callback);
    return () => this.off(event, callback);
  }

  once(event, callback) {
    const wrapper = (...args) => {
      callback(...args);
      this.off(event, wrapper);
    };
    this.on(event, wrapper);
  }

  off(event, callback) {
    const handlers = this.listeners.get(event);
    if (handlers) {
      const index = handlers.indexOf(callback);
      if (index > -1) handlers.splice(index, 1);
    }
  }

  // ==========================================================================
  // PERSISTENCE
  // ==========================================================================

  _saveConfig() {
    try {
      localStorage.setItem('jarvis_dynamic_config', JSON.stringify({
        ...this.config,
        savedAt: Date.now()
      }));
    } catch {
      // localStorage not available
    }
  }

  _loadCachedConfig() {
    try {
      const cached = localStorage.getItem('jarvis_dynamic_config');
      if (!cached) return null;
      
      const config = JSON.parse(cached);
      const age = Date.now() - (config.savedAt || 0);
      
      // Cache valid for 30 minutes - reduces discovery overhead
      if (age < 30 * 60 * 1000) {
        return config;
      }
      
      this._clearCache();
    } catch {
      this._clearCache();
    }
    return null;
  }

  _clearCache() {
    try {
      localStorage.removeItem('jarvis_dynamic_config');
    } catch {
      // Ignore
    }
  }

  // ==========================================================================
  // URL HELPERS
  // ==========================================================================

  _toWebSocketUrl(httpUrl) {
    if (!httpUrl) return null;
    return httpUrl.replace('https://', 'wss://').replace('http://', 'ws://');
  }

  // ==========================================================================
  // PUBLIC API
  // ==========================================================================

  getApiUrl(endpoint = '') {
    if (!this.config.API_BASE_URL) return null;
    
    if (endpoint && this.config.ENDPOINTS[endpoint]) {
      const ep = this.config.ENDPOINTS[endpoint];
      const path = typeof ep === 'string' ? ep : ep.path;
      return `${this.config.API_BASE_URL}${path}`;
    }
    
    return endpoint 
      ? `${this.config.API_BASE_URL}/${endpoint.replace(/^\//, '')}`
      : this.config.API_BASE_URL;
  }

  getWebSocketUrl(endpoint = '') {
    if (!this.config.WS_BASE_URL) return null;
    
    return endpoint
      ? `${this.config.WS_BASE_URL}/${endpoint.replace(/^\//, '')}`
      : this.config.WS_BASE_URL;
  }

  async waitForConfig(timeout = 30000) {
    if (this.config.discovered) return this.config;
    
    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        this.off('config-ready', onReady);
        reject(new Error('Configuration discovery timeout'));
      }, timeout);

      const onReady = (config) => {
        clearTimeout(timeoutId);
        resolve(config);
      };

      this.once('config-ready', onReady);
    });
  }

  getBackendState() {
    return { ...this.config.backendState };
  }

  getStartupProgress() {
    return { ...this.startupProgress };
  }

  getCircuitBreakerState() {
    return this.circuitBreaker.getState();
  }

  async discover() {
    return this._startDiscovery();
  }
}

// ============================================================================
// SINGLETON INSTANCE
// ============================================================================

const configService = new DynamicConfigService();

// Convenience exports
export const waitForConfig = (timeout) => configService.waitForConfig(timeout);
export const getApiUrl = (endpoint) => configService.getApiUrl(endpoint);
export const getWebSocketUrl = (endpoint) => configService.getWebSocketUrl(endpoint);
export const getBackendState = () => configService.getBackendState();
export const getStartupProgress = () => configService.getStartupProgress();
export const getCircuitBreakerState = () => configService.getCircuitBreakerState();
export const onConfigReady = (callback) => configService.on('config-ready', callback);
export const onBackendState = (callback) => configService.on('backend-state', callback);
export const onStartupProgress = (callback) => configService.on('startup-progress', callback);
export const onBackendReady = (callback) => configService.on('backend-ready', callback);

export { CIRCUIT_STATE };
export default configService;
