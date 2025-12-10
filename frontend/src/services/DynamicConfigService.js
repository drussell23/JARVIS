/**
 * Dynamic Configuration Service v2.0
 * ===================================
 * Advanced zero-hardcode configuration with:
 * - Intelligent multi-strategy discovery
 * - Circuit breaker pattern with adaptive thresholds
 * - Backend startup state synchronization
 * - WebSocket connection state management
 * - Real-time health scoring with exponential backoff
 * - Environment-aware configuration inference
 * - Startup progress tracking for UI sync
 */

import logger from '../utils/DebugLogger';

// Environment detection (no hardcoding)
const ENV = {
  isDev: () => typeof window !== 'undefined' &&
    (window.location.hostname === 'localhost' ||
     window.location.hostname === '127.0.0.1' ||
     window.location.hostname.includes('.local')),
  isSecure: () => typeof window !== 'undefined' && window.location.protocol === 'https:',
  getHostname: () => typeof window !== 'undefined' ? window.location.hostname : 'localhost',
  getProtocol: () => typeof window !== 'undefined' ? window.location.protocol.replace(':', '') : 'http'
};

// Circuit Breaker States
const CIRCUIT_STATE = {
  CLOSED: 'closed',      // Normal operation
  OPEN: 'open',          // Failing, reject requests
  HALF_OPEN: 'half_open' // Testing if service recovered
};

class DynamicConfigService {
  constructor() {
    this.config = {
      API_BASE_URL: null,
      WS_BASE_URL: null,
      ENDPOINTS: {},
      SERVICES: {},
      discovered: false,
      // Backend startup state sync
      backendState: {
        status: 'unknown',
        mode: 'unknown',
        startupProgress: 0,
        components: {},
        lastUpdate: null
      }
    };

    // Discovery configuration - dynamically ordered by environment
    this.commonPorts = this._inferPorts();
    // Excluded ports:
    // - 5000, 5001: macOS Control Center
    // - 3001: JARVIS Loading Server (not the backend!)
    // Excluded ports: 
    // - 5000, 5001: Flask dev servers
    // - 3001: Old loading server
    // - 8001: WebSocket proxy/loading server (NOT the main JARVIS backend!)
    this.excludedPorts = [5000, 5001, 3001, 8001];
    this.discoveryTimeout = 500;
    this.maxRetries = 3;

    // Service patterns for identification
    this.servicePatterns = {
      backend: {
        endpoints: ['/health/ping', '/health', '/health/startup', '/api/health', '/docs'],
        identifiers: ['jarvis', 'api', 'backend', 'fastapi', 'status', 'ok']
      },
      websocket: {
        endpoints: ['/ws', '/voice/jarvis/stream', '/vision/ws'],
        identifiers: ['websocket', 'ws', 'realtime']
      }
    };

    // Health monitoring with adaptive intervals
    this.healthCheckInterval = 30000;
    this.healthScores = new Map();
    this.lastHealthCheck = new Map();
    this.healthCheckTimer = null;

    // Circuit breaker configuration
    this.circuitBreaker = {
      state: CIRCUIT_STATE.CLOSED,
      failures: 0,
      threshold: 5,           // Failures before opening
      resetTimeout: 30000,    // Time before trying again
      lastFailure: null,
      successThreshold: 2,    // Successes needed to close
      halfOpenSuccesses: 0
    };

    // Startup progress tracking
    this.startupProgress = {
      phase: 'initializing',
      progress: 0,
      message: 'Starting configuration discovery...',
      components: {},
      lastUpdate: Date.now()
    };

    // Event listeners with priority support
    this.listeners = new Map();
    this.listenerPriorities = new Map();

    // Connection state
    this.connectionState = {
      isOnline: navigator.onLine,
      lastOnlineCheck: Date.now(),
      reconnectAttempts: 0,
      maxReconnectAttempts: 10
    };

    // Listen for online/offline events
    if (typeof window !== 'undefined') {
      window.addEventListener('online', () => this._handleOnline());
      window.addEventListener('offline', () => this._handleOffline());
    }

    // Auto-discovery on instantiation
    this.discover();
  }

  /**
   * Infer optimal port order based on environment
   */
  _inferPorts() {
    const envPort = typeof process !== 'undefined' && process.env?.REACT_APP_BACKEND_PORT;
    // NOTE: 3001 is the LOADING SERVER, not the backend! Don't include it here.
    // The loading server has a /health endpoint that looks like a backend but isn't.
    const defaultPorts = [8010, 8000, 8011, 8001, 8080, 8888];

    if (envPort) {
      const port = parseInt(envPort, 10);
      // Put environment-specified port first
      return [port, ...defaultPorts.filter(p => p !== port)];
    }

    return defaultPorts;
  }

  _handleOnline() {
    logger.success('🌐 Network connection restored');
    this.connectionState.isOnline = true;
    this.connectionState.reconnectAttempts = 0;
    this.emit('network-online');

    // Re-validate config when coming back online
    if (this.config.discovered) {
      this.validateConfig(this.config).then(isValid => {
        if (!isValid) {
          logger.warning('Config invalid after reconnect, rediscovering...');
          this.discover();
        }
      });
    }
  }

  _handleOffline() {
    logger.warning('📴 Network connection lost');
    this.connectionState.isOnline = false;
    this.emit('network-offline');
  }

  async discover() {
    logger.config('🔍 Starting automatic service discovery...');
    this._updateStartupProgress('discovering', 10, 'Searching for backend services...');

    // Check circuit breaker
    if (!this._checkCircuitBreaker()) {
      logger.warning('Circuit breaker OPEN, waiting for reset...');
      this._updateStartupProgress('waiting', 5, 'Waiting for service recovery...');
      return this.config;
    }

    // Try to load from localStorage first
    const cached = this.loadCachedConfig();
    if (cached) {
      logger.info('Found cached config, validating...');
      this._updateStartupProgress('validating', 20, 'Validating cached configuration...');

      const isValid = await this.validateConfig(cached);
      if (isValid) {
        logger.success('✅ Using cached configuration', cached);
        this.config = { ...cached, discovered: true };
        this._recordCircuitSuccess();
        this._updateStartupProgress('connected', 100, 'Connected to backend!');
        this.emit('config-ready', this.config);

        // Sync backend state immediately
        await this._syncBackendState();

        // Still run discovery in background to update
        this.backgroundDiscovery();
        return this.config;
      } else {
        logger.warning('❌ Cached config validation failed, will rediscover');
        this._recordCircuitFailure();
      }
    }

    logger.config('No valid cached config, starting fresh discovery...');
    this._updateStartupProgress('scanning', 30, 'Scanning for available services...');

    // Full discovery with circuit breaker awareness
    const services = await this.discoverServices();

    if (services.backend) {
      this.config.API_BASE_URL = services.backend.url;
      this.config.WS_BASE_URL = this._inferWebSocketUrl(services.backend.url);
      this.config.ENDPOINTS = services.backend.endpoints;
      this.config.discovered = true;

      // Record success
      this._recordCircuitSuccess();

      // Save to cache
      this.saveConfig();

      // Start health monitoring
      this.startHealthMonitoring();

      // Sync backend state
      this._updateStartupProgress('syncing', 80, 'Synchronizing with backend...');
      await this._syncBackendState();

      this._updateStartupProgress('connected', 100, 'Connected to backend!');
      logger.success('✅ Service discovery complete:', this.config);
      this.emit('config-ready', this.config);
    } else {
      logger.error('❌ No backend service found');
      this._recordCircuitFailure();
      this._updateStartupProgress('failed', 0, 'Backend not found. Retrying...');
      this.emit('discovery-failed', { reason: 'No backend found' });

      // Exponential backoff retry
      const retryDelay = Math.min(5000 * Math.pow(2, this.connectionState.reconnectAttempts), 60000);
      this.connectionState.reconnectAttempts++;

      if (this.connectionState.reconnectAttempts <= this.connectionState.maxReconnectAttempts) {
        logger.info(`Retrying discovery in ${retryDelay}ms (attempt ${this.connectionState.reconnectAttempts})`);
        setTimeout(() => this.discover(), retryDelay);
      }
    }

    return this.config;
  }

  /**
   * Infer WebSocket URL from HTTP URL with protocol awareness
   */
  _inferWebSocketUrl(httpUrl) {
    if (!httpUrl) return null;
    return httpUrl
      .replace('https://', 'wss://')
      .replace('http://', 'ws://');
  }

  /**
   * Update startup progress for UI sync
   */
  _updateStartupProgress(phase, progress, message, components = null) {
    this.startupProgress = {
      phase,
      progress,
      message,
      components: components || this.startupProgress.components,
      lastUpdate: Date.now()
    };
    this.emit('startup-progress', this.startupProgress);
  }

  /**
   * Sync backend startup state for accurate UI display
   */
  async _syncBackendState() {
    if (!this.config.API_BASE_URL) return;

    try {
      // Try startup endpoint first (more detailed)
      const startupUrl = `${this.config.API_BASE_URL}/health/startup`;
      const response = await this.fetchWithTimeout(startupUrl, 3000, true);

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

        // Emit backend state update
        this.emit('backend-state', this.config.backendState);

        // If not fully ready, poll for updates
        if (!this.config.backendState.ready) {
          this._pollBackendState();
        }
      }
    } catch (error) {
      logger.debug('Backend state sync failed:', error.message);
      // Set a default state
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

  /**
   * Poll backend state until fully ready
   */
  _pollBackendState() {
    const pollInterval = setInterval(async () => {
      await this._syncBackendState();

      if (this.config.backendState.ready) {
        clearInterval(pollInterval);
        this.emit('backend-ready', this.config.backendState);
      }
    }, 2000);

    // Stop polling after 2 minutes
    setTimeout(() => clearInterval(pollInterval), 120000);
  }

  /**
   * Circuit Breaker: Check if requests should be allowed
   */
  _checkCircuitBreaker() {
    const cb = this.circuitBreaker;

    if (cb.state === CIRCUIT_STATE.CLOSED) {
      return true;
    }

    if (cb.state === CIRCUIT_STATE.OPEN) {
      // Check if reset timeout has passed
      const elapsed = Date.now() - cb.lastFailure;
      if (elapsed >= cb.resetTimeout) {
        logger.info('Circuit breaker transitioning to HALF_OPEN');
        cb.state = CIRCUIT_STATE.HALF_OPEN;
        cb.halfOpenSuccesses = 0;
        return true;
      }
      return false;
    }

    // HALF_OPEN - allow one request to test
    return true;
  }

  /**
   * Circuit Breaker: Record a failure
   */
  _recordCircuitFailure() {
    const cb = this.circuitBreaker;
    cb.failures++;
    cb.lastFailure = Date.now();

    if (cb.state === CIRCUIT_STATE.HALF_OPEN) {
      logger.warning('Circuit breaker re-opening after half-open failure');
      cb.state = CIRCUIT_STATE.OPEN;
      cb.halfOpenSuccesses = 0;
    } else if (cb.failures >= cb.threshold) {
      logger.warning(`Circuit breaker OPEN after ${cb.failures} failures`);
      cb.state = CIRCUIT_STATE.OPEN;
      this.emit('circuit-open', { failures: cb.failures });
    }
  }

  /**
   * Circuit Breaker: Record a success
   */
  _recordCircuitSuccess() {
    const cb = this.circuitBreaker;

    if (cb.state === CIRCUIT_STATE.HALF_OPEN) {
      cb.halfOpenSuccesses++;
      if (cb.halfOpenSuccesses >= cb.successThreshold) {
        logger.success('Circuit breaker CLOSED after recovery');
        cb.state = CIRCUIT_STATE.CLOSED;
        cb.failures = 0;
        cb.halfOpenSuccesses = 0;
        this.emit('circuit-closed');
      }
    } else {
      // Reset failure count on success in closed state
      cb.failures = Math.max(0, cb.failures - 1);
    }
  }

  /**
   * Get current circuit breaker state
   */
  getCircuitBreakerState() {
    return { ...this.circuitBreaker };
  }

  /**
   * Get current startup progress
   */
  getStartupProgress() {
    return { ...this.startupProgress };
  }

  /**
   * Get backend state
   */
  getBackendState() {
    return { ...this.config.backendState };
  }

  async discoverServices() {
    const discovered = {};

    // Parallel port scanning for efficiency
    const scanPromises = this.commonPorts.map(port =>
      this.scanPort(port).catch(() => null)
    );

    const results = await Promise.all(scanPromises);

    // Process results - keep first (highest priority) port for each service type
    for (const service of results) {
      if (service) {
        // Don't overwrite if we already found this service type on a higher-priority port
        if (!discovered[service.type]) {
          discovered[service.type] = service;
          this.healthScores.set(service.url, 1.0);
        } else {
          console.log(`⏩ Skipping ${service.type} on port ${service.port} - already found on port ${discovered[service.type].port}`);
        }
      }
    }

    // If no backend found on common ports, do a deeper scan
    if (!discovered.backend) {
      console.log('🔍 Deep scanning for backend service...');
      discovered.backend = await this.deepScan();
    }

    this.config.SERVICES = discovered;
    return discovered;
  }

  async scanPort(port) {
    // Skip excluded ports
    if (this.excludedPorts.includes(port)) {
      logger.debug(`Skipping excluded port ${port}`);
      return null;
    }

    const baseUrl = `http://localhost:${port}`;
    // Only log in debug mode to reduce console noise
    // logger.debug(`Scanning port ${port}...`);

    // Try to identify service type
    for (const [serviceType, patterns] of Object.entries(this.servicePatterns)) {
      for (const endpoint of patterns.endpoints) {
        try {
          const url = `${baseUrl}${endpoint}`;
          const response = await this.fetchWithTimeout(url, this.discoveryTimeout, true);

          if (response.ok) {
            // Analyze response to confirm service type
            let data;
            try {
              data = await response.json();
            } catch {
              data = await response.text();
            }

            const isMatch = this.identifyService(data, patterns.identifiers);

            if (isMatch || endpoint === '/health') {
              logger.success(`✅ Found ${serviceType} service on port ${port}`);
              logger.api(`Response from ${url}:`, typeof data === 'object' ? JSON.stringify(data).substring(0, 200) : data.substring(0, 200));

              // Discover all endpoints
              const endpoints = await this.discoverEndpoints(baseUrl);

              return {
                type: serviceType,
                port,
                url: baseUrl,
                endpoints,
                health: 1.0,
                lastSeen: new Date()
              };
            }
          }
        } catch (error) {
          // Continue scanning silently - these errors are expected
        }
      }
    }

    return null;
  }

  async discoverEndpoints(baseUrl) {
    const endpoints = {};

    logger.debug(`🔍 Discovering endpoints at ${baseUrl}...`);

    // Common API endpoints to check
    const endpointsToCheck = [
      { path: '/health', name: 'health' },
      { path: '/api/health', name: 'health' },
      { path: '/audio/ml/config', name: 'ml_audio_config' },
      { path: '/audio/ml/stream', name: 'ml_audio_stream' },
      { path: '/voice/jarvis/status', name: 'jarvis_status' },
      { path: '/voice/jarvis/activate', name: 'jarvis_activate', method: 'POST' },
      { path: '/api/wake-word/status', name: 'wake_word_status' },
      { path: '/vision/ws/vision', name: 'vision_websocket' },
      { path: '/ws', name: 'websocket' },
      { path: '/openapi.json', name: 'openapi' },
      { path: '/docs', name: 'docs' }
    ];

    // Check endpoints in parallel
    const checks = endpointsToCheck.map(async ({ path, name, method = 'HEAD' }) => {
      try {
        // Use appropriate method for discovery
        let discoveryMethod = 'GET';
        if (method === 'POST' || name === 'jarvis_activate') {
          // For POST endpoints, try GET first for discovery
          discoveryMethod = 'GET';
        }

        const options = {
          method: discoveryMethod,
          headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
          }
        };

        const response = await this.fetchWithTimeout(`${baseUrl}${path}`, 500, true, options);

        // Accept 200, 204, 401, 405 (method not allowed means endpoint exists)
        if (response.ok || response.status === 401 || response.status === 405) {
          endpoints[name] = { path, method: endpointsToCheck.find(e => e.name === name)?.method || 'GET' };
          return { name, path, status: response.status, method };
        }
      } catch {
        // Endpoint doesn't exist
      }
      return null;
    });

    const results = await Promise.all(checks);

    // Log discovered endpoints
    const found = results.filter(r => r !== null);

    // Check if this is minimal mode based on discovered endpoints
    const hasAdvancedEndpoints = found.some(e =>
      e.name === 'ml_audio_config' ||
      e.name === 'wake_word_status' ||
      e.name === 'vision_websocket'
    );

    if (found.length > 0 && !hasAdvancedEndpoints) {
      logger.info(`⚡ Backend running in MINIMAL MODE at ${baseUrl}`);
      logger.info(`  ✅ Found ${found.length} basic endpoints`);
      logger.info(`  ⏳ Advanced features will be available when full mode starts`);
      logger.info(`  📌 Available endpoints:`, found.map(e => e.name).join(', '));
    } else if (found.length > 0) {
      logger.api(`📍 Discovered ${found.length} endpoints at ${baseUrl} (FULL MODE):`, found);
    } else {
      logger.warning(`⚠️  No endpoints found at ${baseUrl}`);
    }

    return endpoints;
  }

  identifyService(data, identifiers) {
    const dataStr = typeof data === 'string' ? data.toLowerCase() : JSON.stringify(data).toLowerCase();
    return identifiers.some(id => dataStr.includes(id));
  }

  async fetchWithTimeout(url, timeout, silent = false, options = {}) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);

    try {
      const response = await fetch(url, {
        signal: controller.signal,
        mode: 'cors',
        credentials: 'omit',
        ...options
      });
      clearTimeout(timeoutId);
      return response;
    } catch (error) {
      clearTimeout(timeoutId);
      // Only throw if not in silent mode (used during discovery)
      if (!silent) {
        throw error;
      }
      // Return a fake failed response for silent mode
      return { ok: false, status: 0 };
    }
  }

  async deepScan() {
    console.log('🔍 Performing deep scan (ports 3000-9999)...');

    // Scan in chunks to avoid overwhelming the system
    const chunkSize = 100;
    const startPort = 3000;
    const endPort = 9999;

    for (let port = startPort; port <= endPort; port += chunkSize) {
      const chunk = [];
      for (let p = port; p < Math.min(port + chunkSize, endPort + 1); p++) {
        // Skip excluded ports in deep scan
        if (!this.excludedPorts.includes(p)) {
          chunk.push(this.quickPortCheck(p));
        }
      }

      const results = await Promise.all(chunk);
      const openPorts = results.filter(p => p !== null);

      // Check each open port
      for (const openPort of openPorts) {
        const service = await this.scanPort(openPort);
        if (service && service.type === 'backend') {
          return service;
        }
      }
    }

    return null;
  }

  async quickPortCheck(port) {
    try {
      const response = await this.fetchWithTimeout(`http://localhost:${port}/health`, 200, true);
      return response.ok ? port : null;
    } catch {
      return null;
    }
  }

  // Configuration API
  getApiUrl(endpoint = '') {
    if (!this.config.API_BASE_URL) {
      logger.warning('API URL not yet discovered, config:', this.config);
      return null;
    }

    // Check if endpoint is a known name
    if (endpoint && this.config.ENDPOINTS[endpoint]) {
      const endpointConfig = this.config.ENDPOINTS[endpoint];
      // Handle both string (legacy) and object (new) endpoint formats
      const path = typeof endpointConfig === 'string' ? endpointConfig : endpointConfig.path;
      return `${this.config.API_BASE_URL}${path}`;
    }

    // Return base URL + endpoint
    return endpoint ? `${this.config.API_BASE_URL}/${endpoint.replace(/^\//, '')}` : this.config.API_BASE_URL;
  }

  // Get the HTTP method for a known endpoint
  getEndpointMethod(endpoint) {
    if (!endpoint || !this.config.ENDPOINTS[endpoint]) {
      return 'GET'; // Default to GET
    }

    const endpointConfig = this.config.ENDPOINTS[endpoint];
    if (typeof endpointConfig === 'string') {
      return 'GET'; // Legacy format, default to GET
    }

    return endpointConfig.method || 'GET';
  }

  getWebSocketUrl(endpoint = '') {
    if (!this.config.WS_BASE_URL) {
      logger.warning('WebSocket URL not yet discovered, config:', this.config);
      return null;
    }

    // Check for known websocket endpoints
    if (endpoint && this.config.ENDPOINTS[`${endpoint}_websocket`]) {
      return `${this.config.WS_BASE_URL}${this.config.ENDPOINTS[`${endpoint}_websocket`]}`;
    }

    return endpoint ? `${this.config.WS_BASE_URL}/${endpoint.replace(/^\//, '')}` : this.config.WS_BASE_URL;
  }

  async waitForConfig(timeout = 30000) {
    if (this.config.discovered) {
      return this.config;
    }

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

  // Health monitoring and self-healing
  async startHealthMonitoring() {
    setInterval(async () => {
      for (const [url, score] of this.healthScores) {
        try {
          const response = await this.fetchWithTimeout(url + '/health', 1000);

          if (response.ok) {
            // Increase health score
            this.healthScores.set(url, Math.min(1.0, score * 1.1));
          } else {
            // Decrease health score
            this.healthScores.set(url, score * 0.9);

            if (score < 0.5) {
              console.warn(`⚠️ Service unhealthy: ${url}`);
              this.attemptHealing(url);
            }
          }
        } catch {
          this.healthScores.set(url, score * 0.8);
        }

        this.lastHealthCheck.set(url, new Date());
      }
    }, this.healthCheckInterval);
  }

  async attemptHealing(url) {
    console.log(`🔧 Attempting to heal service: ${url}`);

    // Strategy 1: Re-discover on different port
    const service = Object.values(this.config.SERVICES).find(s => s.url === url);
    if (service) {
      // Check nearby ports
      const originalPort = service.port;
      const portsToCheck = [
        originalPort + 1, originalPort - 1,
        originalPort + 10, originalPort - 10,
        originalPort + 1000, originalPort - 1000
      ];

      for (const port of portsToCheck) {
        const newService = await this.scanPort(port);
        if (newService && newService.type === service.type) {
          console.log(`✅ Found service on new port: ${port}`);

          // Update configuration
          service.port = port;
          service.url = newService.url;

          if (service.type === 'backend') {
            this.config.API_BASE_URL = newService.url;
            this.config.WS_BASE_URL = newService.url.replace('http://', 'ws://');
          }

          this.emit('service-relocated', { service, oldPort: originalPort, newPort: port });
          this.saveConfig();
          return;
        }
      }
    }

    // Strategy 2: Full re-discovery
    console.log('🔍 Full re-discovery triggered');
    this.discover();
  }

  // Event system
  on(event, callback) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, []);
    }
    this.listeners.get(event).push(callback);
  }

  once(event, callback) {
    const wrapper = (...args) => {
      callback(...args);
      this.off(event, wrapper);
    };
    this.on(event, wrapper);
  }

  off(event, callback) {
    if (this.listeners.has(event)) {
      const callbacks = this.listeners.get(event);
      const index = callbacks.indexOf(callback);
      if (index > -1) {
        callbacks.splice(index, 1);
      }
    }
  }

  emit(event, data) {
    logger.debug(`Emitting event: ${event}`, data);
    if (this.listeners.has(event)) {
      this.listeners.get(event).forEach(callback => {
        try {
          callback(data);
        } catch (error) {
          logger.error(`Error in event listener for ${event}:`, error);
        }
      });
    }
  }

  // Persistence
  saveConfig() {
    try {
      localStorage.setItem('jarvis_dynamic_config', JSON.stringify({
        ...this.config,
        savedAt: new Date().toISOString()
      }));
    } catch (error) {
      console.error('Failed to save config:', error);
    }
  }

  loadCachedConfig() {
    try {
      const cached = localStorage.getItem('jarvis_dynamic_config');
      if (cached) {
        const config = JSON.parse(cached);

        // Check if cache is fresh (less than 5 minutes old)
        const savedAt = new Date(config.savedAt);
        const age = Date.now() - savedAt.getTime();

        if (age < 5 * 60 * 1000) {  // 5 minutes
          logger.debug('Loading cached config, age:', Math.floor(age / 1000), 'seconds');
          return config;
        } else {
          logger.info('Cached config is stale, clearing...');
          this.clearCache();
        }
      }
    } catch (error) {
      console.error('Failed to load cached config:', error);
      this.clearCache();
    }
    return null;
  }

  async validateConfig(config) {
    try {
      // Quick health check on the cached URL
      const response = await this.fetchWithTimeout(config.API_BASE_URL + '/health', 1000);
      if (!response.ok) {
        logger.warning(`Cached config validation failed with status ${response.status}`);
        // Clear invalid cache
        this.clearCache();
        return false;
      }
      return true;
    } catch (error) {
      logger.error('Cached config validation error:', error);
      // Clear invalid cache
      this.clearCache();
      return false;
    }
  }

  // Clear cache method
  clearCache() {
    try {
      localStorage.removeItem('jarvis_dynamic_config');
      logger.info('Cleared stale config cache');
    } catch (error) {
      logger.error('Failed to clear cache:', error);
    }
  }

  // Background discovery
  async backgroundDiscovery() {
    // Run discovery in background to catch service changes
    setInterval(async () => {
      const services = await this.discoverServices();

      // Check for changes
      let hasChanges = false;

      for (const [type, service] of Object.entries(services)) {
        const existing = this.config.SERVICES[type];
        // Check if service is valid (not null)
        if (service) {
          if (!existing || existing.port !== service.port) {
            hasChanges = true;
            console.log(`🔄 Service ${type} changed: port ${existing?.port} -> ${service.port}`);
          }
        } else if (existing) {
          // Service was available but is now gone
          hasChanges = true;
          console.log(`⚠️ Service ${type} is no longer available`);
        }
      }

      if (hasChanges) {
        this.config.SERVICES = services;
        if (services.backend) {
          this.config.API_BASE_URL = services.backend.url;
          this.config.WS_BASE_URL = services.backend.url.replace('http://', 'ws://');
          this.config.ENDPOINTS = services.backend.endpoints;
        }

        this.saveConfig();
        this.emit('config-updated', this.config);
      }
    }, 300000);  // Every 5 minutes (reduce frequency to minimize console noise)
  }
}

// Create singleton instance
const configService = new DynamicConfigService();

// Export convenience functions for common operations
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

// Export circuit breaker states for external use
export { CIRCUIT_STATE };

// Export for use in other modules
export default configService;