/**
 * JarvisConnectionService v3.0 - Unified Connection Management
 * =============================================================
 * Single source of truth for JARVIS backend connectivity with:
 * - Non-blocking async connection management
 * - Coordinated service discovery via DynamicConfigService
 * - Intelligent WebSocket management with health monitoring
 * - React hooks for seamless UI integration
 * - Zero hardcoding - fully dynamic
 */

import React from 'react';
import DynamicWebSocketClient from './DynamicWebSocketClient';
import configService from './DynamicConfigService';

// ============================================================================
// CONNECTION STATES
// ============================================================================

export const ConnectionState = {
  INITIALIZING: 'initializing',
  DISCOVERING: 'discovering',
  CONNECTING: 'connecting',
  ONLINE: 'online',
  OFFLINE: 'offline',
  RECONNECTING: 'reconnecting',
  ERROR: 'error'
};

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Yields to event loop to prevent blocking
 */
const yieldToEventLoop = () => new Promise(resolve => setTimeout(resolve, 0));

/**
 * Create timeout controller for fetch operations
 */
const fetchWithTimeout = async (url, options = {}, timeoutMs = 5000) => {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      mode: 'cors',
      credentials: 'omit'
    });
    clearTimeout(timeoutId);
    return response;
  } catch (error) {
    clearTimeout(timeoutId);
    throw error;
  }
};

// ============================================================================
// JARVIS CONNECTION SERVICE
// ============================================================================

class JarvisConnectionService {
  constructor() {
    // State
    this.connectionState = ConnectionState.INITIALIZING;
    this.backendUrl = null;
    this.wsUrl = null;
    this.lastError = null;
    this.backendMode = 'unknown';
    
    // WebSocket client
    this.wsClient = null;
    
    // Event listeners
    this.listeners = new Map();
    
    // Health monitoring
    this.healthConfig = {
      checkInterval: 30000,
      timeout: 8000,              // Increased from 5000ms
      maxFailures: 5              // Increased from 3 - more tolerant
    };
    this.healthCheckTimer = null;
    this.consecutiveFailures = 0;
    
    // Discovery state - more aggressive retry
    this.discoveryState = {
      attempts: 0,
      maxAttempts: 15,            // Increased from 10
      inProgress: false,
      lastAttemptTime: null
    };
    
    // Initialize asynchronously (non-blocking)
    this._initializeAsync();
  }

  // ==========================================================================
  // INITIALIZATION
  // ==========================================================================

  async _initializeAsync() {
    console.log('[JarvisConnection] Initializing...');
    
    // Subscribe to config service events
    this._subscribeToConfigService();
    
    // Yield to prevent blocking
    await yieldToEventLoop();
    
    // Try fast path first (recently verified backend)
    const fastPathSuccess = await this._tryFastPath();
    if (fastPathSuccess) {
      console.log('[JarvisConnection] ✅ Fast-path connection successful');
      return;
    }
    
    // Start normal discovery
    this._setState(ConnectionState.DISCOVERING);
    await this._discoverBackend();
  }

  _subscribeToConfigService() {
    // Config ready - backend discovered
    configService.on('config-ready', (config) => {
      console.log('[JarvisConnection] Config ready:', config.API_BASE_URL);
      if (config.API_BASE_URL && config.API_BASE_URL !== this.backendUrl) {
        this.backendUrl = config.API_BASE_URL;
        this.wsUrl = config.WS_BASE_URL;
        this._connectToBackend();
      }
    });
    
    // Backend state updates
    configService.on('backend-state', (state) => {
      console.log('[JarvisConnection] Backend state:', state);
      if (state.ready && this.connectionState !== ConnectionState.ONLINE) {
        this._setState(ConnectionState.ONLINE);
      }
      this.backendMode = state.mode || 'unknown';
      this._emit('modeChange', { mode: this.backendMode });
    });
    
    // Backend fully ready
    configService.on('backend-ready', (state) => {
      console.log('[JarvisConnection] Backend ready');
      this.backendMode = state.mode || 'full';
      if (this.connectionState !== ConnectionState.ONLINE) {
        this._connectToBackend();
      }
    });
    
    // Discovery failed
    configService.on('discovery-failed', ({ reason }) => {
      console.warn('[JarvisConnection] Discovery failed:', reason);
      this._handleDiscoveryFailed();
    });
    
    // Startup progress
    configService.on('startup-progress', (progress) => {
      this._emit('startupProgress', progress);
    });
  }

  // ==========================================================================
  // FAST PATH CONNECTION
  // ==========================================================================

  /**
   * Extract URL parameters for cross-origin fast-path connection.
   * Loading page (port 3001) passes backend state via URL params since
   * localStorage is origin-specific and doesn't work cross-port.
   */
  _getUrlFastPathParams() {
    try {
      const urlParams = new URLSearchParams(window.location.search);
      const jarvisReady = urlParams.get('jarvis_ready');
      const backendPort = urlParams.get('backend_port');
      const timestamp = urlParams.get('ts');

      if (jarvisReady === '1' && backendPort) {
        const timeSinceRedirect = timestamp ? Date.now() - parseInt(timestamp) : 0;
        return {
          valid: true,
          backendPort: parseInt(backendPort),
          timeSinceRedirect,
          timestamp: parseInt(timestamp) || Date.now()
        };
      }
      return { valid: false };
    } catch (error) {
      console.debug('[JarvisConnection] URL param parsing error:', error.message);
      return { valid: false };
    }
  }

  /**
   * Clean URL parameters after consuming them (keep URL clean)
   */
  _cleanUrlParams() {
    try {
      const url = new URL(window.location.href);
      const paramsToRemove = ['jarvis_ready', 'backend_port', 'ts'];
      let modified = false;

      paramsToRemove.forEach(param => {
        if (url.searchParams.has(param)) {
          url.searchParams.delete(param);
          modified = true;
        }
      });

      if (modified) {
        // Replace URL without reloading page
        window.history.replaceState({}, '', url.toString());
        console.log('[JarvisConnection] ✓ Cleaned fast-path URL parameters');
      }
    } catch (error) {
      // Non-critical, ignore
    }
  }

  async _tryFastPath() {
    try {
      // PRIORITY 1: Check URL parameters (from loading page redirect)
      // This is the primary fast-path since localStorage is origin-specific
      const urlParams = this._getUrlFastPathParams();

      if (urlParams.valid) {
        console.log(`[JarvisConnection] 🚀 URL fast-path: Backend ready on port ${urlParams.backendPort} (redirect ${Math.round(urlParams.timeSinceRedirect / 1000)}s ago)`);

        // Construct URLs from the port
        const backendUrl = `http://localhost:${urlParams.backendPort}`;
        const wsUrl = `ws://localhost:${urlParams.backendPort}`;

        // Quick health verification (should be instant since loading page verified it)
        const isHealthy = await this._quickHealthCheck(backendUrl);

        if (isHealthy) {
          this.backendUrl = backendUrl;
          this.wsUrl = wsUrl;

          await this._initializeWebSocket();
          this._startHealthMonitoring();

          this._setState(ConnectionState.ONLINE);
          this.consecutiveFailures = 0;

          // Save to localStorage for future page refreshes
          this._saveVerifiedState();

          // Clean URL params after consuming
          this._cleanUrlParams();

          console.log('[JarvisConnection] ✅ URL fast-path connection successful - INSTANT READY');
          return true;
        } else {
          console.warn('[JarvisConnection] URL fast-path health check failed, falling back');
          this._cleanUrlParams();
        }
      }

      // PRIORITY 2: Check localStorage for recently verified backend
      // This handles page refreshes within the same origin
      const verified = localStorage.getItem('jarvis_backend_verified') === 'true';
      const verifiedAt = parseInt(localStorage.getItem('jarvis_backend_verified_at') || '0');
      const timeSinceVerification = Date.now() - verifiedAt;

      // Use localStorage fast path if verified within 5 minutes (300 seconds)
      // This handles page refreshes and tab switches
      if (!verified || timeSinceVerification > 300000) {
        return false;
      }

      const backendUrl = localStorage.getItem('jarvis_backend_url');
      const wsUrl = localStorage.getItem('jarvis_backend_ws_url');

      if (!backendUrl || !wsUrl) {
        return false;
      }

      console.log(`[JarvisConnection] Fast-path: Backend verified ${Math.round(timeSinceVerification / 1000)}s ago`);

      // Quick health verification
      const isHealthy = await this._quickHealthCheck(backendUrl);
      if (!isHealthy) {
        this._clearFastPathState();
        return false;
      }

      // Set URLs and connect
      this.backendUrl = backendUrl;
      this.wsUrl = wsUrl;

      await this._initializeWebSocket();
      this._startHealthMonitoring();

      this._setState(ConnectionState.ONLINE);
      this.consecutiveFailures = 0;

      // Clear fast-path state (one-time use)
      this._clearFastPathState();

      return true;
    } catch (error) {
      console.warn('[JarvisConnection] Fast-path failed:', error.message);
      this._clearFastPathState();
      this._cleanUrlParams();
      return false;
    }
  }

  _clearFastPathState() {
    try {
      localStorage.removeItem('jarvis_backend_verified');
      localStorage.removeItem('jarvis_backend_verified_at');
    } catch {
      // Ignore
    }
  }

  _saveVerifiedState() {
    try {
      localStorage.setItem('jarvis_backend_verified', 'true');
      localStorage.setItem('jarvis_backend_verified_at', Date.now().toString());
      localStorage.setItem('jarvis_backend_url', this.backendUrl);
      localStorage.setItem('jarvis_backend_ws_url', this.wsUrl);
    } catch {
      // Ignore localStorage errors
    }
  }

  // ==========================================================================
  // BACKEND DISCOVERY
  // ==========================================================================

  async _discoverBackend() {
    if (this.discoveryState.inProgress) {
      return;
    }
    
    this.discoveryState.inProgress = true;
    this.discoveryState.attempts++;
    
    console.log(`[JarvisConnection] Discovery attempt ${this.discoveryState.attempts}`);
    
    try {
      // Wait for config service to discover backend
      const config = await configService.waitForConfig(15000);
      
      if (config?.API_BASE_URL) {
        this.backendUrl = config.API_BASE_URL;
        this.wsUrl = config.WS_BASE_URL;
        await this._connectToBackend();
      } else {
        throw new Error('No backend configuration found');
      }
    } catch (error) {
      console.warn('[JarvisConnection] Discovery failed:', error.message);
      this._handleDiscoveryFailed();
    } finally {
      this.discoveryState.inProgress = false;
    }
  }

  _handleDiscoveryFailed() {
    this.discoveryState.lastAttemptTime = Date.now();
    
    if (this.discoveryState.attempts < this.discoveryState.maxAttempts) {
      // Calculate backoff - start with shorter delays, then increase
      // First few attempts are quick (1-2s), then back off gradually
      const baseDelay = this.discoveryState.attempts < 3 ? 1500 : 3000;
      const delay = Math.min(
        baseDelay * Math.pow(1.3, Math.max(0, this.discoveryState.attempts - 3)),
        30000
      );
      
      console.log(`[JarvisConnection] Retrying discovery in ${Math.round(delay / 1000)}s (attempt ${this.discoveryState.attempts}/${this.discoveryState.maxAttempts})`);
      this._setState(ConnectionState.RECONNECTING);
      
      setTimeout(() => this._discoverBackend(), delay);
    } else {
      console.error('[JarvisConnection] Max discovery attempts reached');
      this._setState(ConnectionState.OFFLINE);
      this.lastError = 'Could not find backend service';
      this._emit('error', { message: this.lastError });
      
      // Even after max attempts, set up a long-term retry (every 30s)
      // This handles the case where backend starts after frontend
      console.log('[JarvisConnection] Setting up background retry every 30s');
      setTimeout(() => {
        if (this.connectionState === ConnectionState.OFFLINE) {
          this.discoveryState.attempts = 0; // Reset attempts
          this._discoverBackend();
        }
      }, 30000);
    }
  }

  // ==========================================================================
  // BACKEND CONNECTION
  // ==========================================================================

  async _connectToBackend() {
    this._setState(ConnectionState.CONNECTING);
    
    try {
      // Verify backend is healthy
      const health = await this._checkBackendHealth();
      if (!health.ok) {
        throw new Error(`Backend unhealthy: ${health.error}`);
      }
      
      this.backendMode = health.mode || 'full';
      
      // Initialize WebSocket — track whether it actually connected
      const wsConnected = await this._initializeWebSocket();
      
      // Start health monitoring
      this._startHealthMonitoring();
      
      // Mark REST as available (health check passed, /api/command is reachable)
      this._restAvailable = true;
      
      if (wsConnected) {
        // Full connectivity: WebSocket + REST
        this._setState(ConnectionState.ONLINE);
        this.consecutiveFailures = 0;
        console.log(`[JarvisConnection] ✅ Connected (${this.backendMode} mode, WebSocket + REST)`);
      } else {
        // Degraded connectivity: REST only, WebSocket failed
        // Still set ONLINE because we CAN send commands via REST API
        console.warn('[JarvisConnection] ⚠️ WebSocket failed but backend is healthy — using REST fallback');
        this._setState(ConnectionState.ONLINE);
        this.consecutiveFailures = 0;
        
        // Schedule background WebSocket retry (don't block the user)
        this._scheduleWebSocketRetry();
      }
      
      // Save verified state for fast-path on subsequent loads
      this._saveVerifiedState();
      
    } catch (error) {
      console.error('[JarvisConnection] Connection failed:', error.message);
      this.lastError = error.message;
      this._setState(ConnectionState.ERROR);
      this._scheduleReconnect();
    }
  }

  /**
   * Retry WebSocket connection in the background without blocking command sending.
   * REST API remains available while we try to establish WebSocket.
   */
  _scheduleWebSocketRetry() {
    const maxRetries = 5;
    let attempt = 0;
    
    const retry = async () => {
      attempt++;
      if (attempt > maxRetries) {
        console.log('[JarvisConnection] WebSocket retry exhausted — continuing with REST-only mode');
        return;
      }
      
      console.log(`[JarvisConnection] WebSocket retry ${attempt}/${maxRetries}...`);
      try {
        const wsConnected = await this._initializeWebSocket();
        if (wsConnected) {
          console.log('[JarvisConnection] ✅ WebSocket reconnected successfully');
          this._emit('wsReconnected', {});
          return; // Success — stop retrying
        }
      } catch (e) {
        console.debug('[JarvisConnection] WebSocket retry failed:', e.message);
      }
      
      // Exponential backoff: 2s, 4s, 8s, 16s, 32s
      const delay = Math.min(2000 * Math.pow(2, attempt - 1), 32000);
      setTimeout(retry, delay);
    };
    
    // Start first retry after 2 seconds
    setTimeout(retry, 2000);
  }

  async _quickHealthCheck(url) {
    try {
      const response = await fetchWithTimeout(`${url}/health`, {}, 2000);
      return response.ok;
    } catch {
      return false;
    }
  }

  async _checkBackendHealth() {
    try {
      const response = await fetchWithTimeout(
        `${this.backendUrl}/health`,
        { headers: { 'Accept': 'application/json' } },
        this.healthConfig.timeout
      );
      
      if (response.ok) {
        const data = await response.json();
        return {
          ok: true,
          status: data.status,
          mode: data.mode || 'full'
        };
      }
      
      return { ok: false, error: `HTTP ${response.status}` };
    } catch (error) {
      return { ok: false, error: error.message };
    }
  }

  // ==========================================================================
  // WEBSOCKET MANAGEMENT
  // ==========================================================================

  /**
   * Initialize WebSocket connection.
   * @returns {Promise<boolean>} true if WebSocket connected successfully, false if it failed
   */
  async _initializeWebSocket() {
    // Create client if needed
    if (!this.wsClient) {
      this.wsClient = new DynamicWebSocketClient({
        autoDiscover: false,
        reconnectStrategy: 'exponential',
        maxReconnectAttempts: 10,
        heartbeatInterval: 30000,
        connectionTimeout: 10000
      });
      
      // Configure endpoints
      this.wsClient.endpoints = [
        {
          path: `${this.wsUrl}/ws`,
          capabilities: ['voice', 'command', 'jarvis', 'general'],
          priority: 10
        },
        {
          path: `${this.wsUrl}/voice/jarvis/stream`,
          capabilities: ['voice', 'command', 'jarvis'],
          priority: 9
        },
        {
          // Backend mounts vision WS at /vision/ws/vision
          path: `${this.wsUrl}/vision/ws/vision`,
          capabilities: ['vision', 'monitoring'],
          priority: 8
        }
      ];
      
      // Set up event handlers
      this._setupWebSocketHandlers();
    } else {
      // Update endpoints if URL changed
      this.wsClient.endpoints = [
        {
          path: `${this.wsUrl}/ws`,
          capabilities: ['voice', 'command', 'jarvis', 'general'],
          priority: 10
        }
      ];
    }
    
    // Connect to main WebSocket — return success/failure explicitly
    try {
      const ws = await this.wsClient.connect(`${this.wsUrl}/ws`);
      if (ws && ws.readyState === WebSocket.OPEN) {
        console.log('[JarvisConnection] ✅ WebSocket connected');
        return true;
      }
      console.warn('[JarvisConnection] WebSocket created but not OPEN');
      return false;
    } catch (error) {
      console.warn('[JarvisConnection] WebSocket connection failed:', error.message);
      return false;
    }
  }

  _setupWebSocketHandlers() {
    // Forward all messages
    this.wsClient.on('*', (data, endpoint) => {
      this._emit('message', { data, endpoint });
    });
    
    // Specific message types
    const messageTypes = [
      'response', 'jarvis_response', 'workflow_progress',
      'vbi_progress', 'proactive_suggestion', 'voice_unlock',
      'transcription', 'error'
    ];
    
    messageTypes.forEach(type => {
      this.wsClient.on(type, (data) => {
        this._emit(type, data);
      });
    });
    
    // Connection events
    this.wsClient.on('connected', (data) => {
      console.log('[JarvisConnection] WebSocket connected:', data.endpoint);
      if (this.connectionState !== ConnectionState.ONLINE) {
        this._setState(ConnectionState.ONLINE);
      }
    });
    
    this.wsClient.on('disconnected', (data) => {
      console.log('[JarvisConnection] WebSocket disconnected:', data.endpoint);
    });
  }

  // ==========================================================================
  // HEALTH MONITORING
  // ==========================================================================

  _startHealthMonitoring() {
    this._stopHealthMonitoring();
    
    this.healthCheckTimer = setInterval(async () => {
      const health = await this._checkBackendHealth();
      
      if (!health.ok) {
        this.consecutiveFailures++;
        console.warn(`[JarvisConnection] Health check failed (${this.consecutiveFailures}/${this.healthConfig.maxFailures})`);
        
        if (this.consecutiveFailures >= this.healthConfig.maxFailures) {
          this._handleConnectionLost();
        }
      } else {
        this.consecutiveFailures = 0;
        
        // Check for mode changes
        if (health.mode !== this.backendMode) {
          console.log(`[JarvisConnection] Mode changed: ${this.backendMode} -> ${health.mode}`);
          this.backendMode = health.mode;
          this._emit('modeChange', { mode: health.mode });
        }
      }
    }, this.healthConfig.checkInterval);
  }

  _stopHealthMonitoring() {
    if (this.healthCheckTimer) {
      clearInterval(this.healthCheckTimer);
      this.healthCheckTimer = null;
    }
  }

  _handleConnectionLost() {
    console.warn('[JarvisConnection] Connection lost');
    this._stopHealthMonitoring();
    this._setState(ConnectionState.OFFLINE);
    this._scheduleReconnect();
  }

  _scheduleReconnect() {
    this._setState(ConnectionState.RECONNECTING);
    
    const delay = Math.min(
      5000 * Math.pow(1.5, this.consecutiveFailures),
      30000
    );
    
    console.log(`[JarvisConnection] Reconnecting in ${Math.round(delay / 1000)}s`);
    
    setTimeout(() => this._connectToBackend(), delay);
  }

  // ==========================================================================
  // STATE MANAGEMENT
  // ==========================================================================

  _setState(newState) {
    if (this.connectionState !== newState) {
      const oldState = this.connectionState;
      this.connectionState = newState;
      console.log(`[JarvisConnection] State: ${oldState} -> ${newState}`);
      this._emit('stateChange', { oldState, newState, state: newState });
    }
  }

  // ==========================================================================
  // EVENT SYSTEM
  // ==========================================================================

  _emit(event, data) {
    const handlers = this.listeners.get(event) || [];
    const allHandlers = this.listeners.get('*') || [];
    
    [...handlers, ...allHandlers].forEach(handler => {
      try {
        handler(data);
      } catch (error) {
        console.error(`[JarvisConnection] Handler error for ${event}:`, error);
      }
    });
  }

  on(event, handler) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, []);
    }
    this.listeners.get(event).push(handler);
    return () => this.off(event, handler);
  }

  off(event, handler) {
    const handlers = this.listeners.get(event);
    if (handlers) {
      const index = handlers.indexOf(handler);
      if (index > -1) handlers.splice(index, 1);
    }
  }

  // ==========================================================================
  // PUBLIC API
  // ==========================================================================

  getState() {
    return this.connectionState;
  }

  isConnected() {
    return this.connectionState === ConnectionState.ONLINE;
  }

  getMode() {
    return this.backendMode;
  }

  getLastError() {
    return this.lastError;
  }

  getWebSocket() {
    if (!this.wsClient) return null;
    for (const [, ws] of this.wsClient.connections) {
      if (ws.readyState === WebSocket.OPEN) {
        return ws;
      }
    }
    return null;
  }

  isWebSocketConnected() {
    const ws = this.getWebSocket();
    return ws && ws.readyState === WebSocket.OPEN;
  }

  /**
   * Send a command to JARVIS with intelligent routing.
   * 
   * Routing priority:
   *   1. WebSocket (real-time, bidirectional) — preferred
   *   2. REST API (/api/command) — fallback when WebSocket unavailable
   *   3. Queue for retry — when both are down
   * 
   * @param {string} command - The command text
   * @param {object} options - Options: mode, metadata, audioData, timeout, onProgress
   * @returns {Promise<{success: boolean, response?: string, route: string}>}
   */
  async sendCommand(command, options = {}) {
    const message = {
      type: 'command',
      text: command,
      mode: options.mode || 'manual',
      metadata: { source: 'text_input', timestamp: Date.now(), ...options.metadata },
      timestamp: Date.now()
    };
    
    if (options.audioData) {
      message.audio_data = options.audioData.audio;
      message.sample_rate = options.audioData.sampleRate;
      message.mime_type = options.audioData.mimeType;
    }

    // Strategy 1: Try WebSocket (preferred — real-time bidirectional)
    const ws = this.getWebSocket();
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        if (options.reliable !== false && this.wsClient) {
          await this.wsClient.sendReliable(message, 'jarvis', options.timeout || 10000);
        } else {
          ws.send(JSON.stringify(message));
        }
        console.log('[JarvisConnection] ✅ Command sent via WebSocket');
        return { success: true, route: 'websocket' };
      } catch (wsError) {
        console.warn('[JarvisConnection] WebSocket send failed:', wsError.message, '— falling back to REST');
      }
    }

    // Strategy 2: Try REST API (/api/command) — synchronous fallback
    if (this.backendUrl && this._restAvailable !== false) {
      try {
        const result = await this._sendViaREST(command, options);
        return result;
      } catch (restError) {
        console.warn('[JarvisConnection] REST send failed:', restError.message);
      }
    }

    // Strategy 3: Queue for later if both paths are down
    if (this.wsClient && this.wsClient.offlineQueue) {
      this.wsClient.offlineQueue.push({ ...message, _queuedAt: Date.now() });
      console.log('[JarvisConnection] Command queued for retry when connection is restored');
      return { success: false, route: 'queued', error: 'No connection available — command queued for retry' };
    }

    return { success: false, route: 'none', error: 'No connection to JARVIS backend' };
  }

  /**
   * Send a command via REST API (/api/command).
   * This is the fallback when WebSocket is unavailable.
   * Works because HTTP health check passed — the backend IS reachable.
   */
  async _sendViaREST(command, options = {}) {
    const url = `${this.backendUrl}/api/command`;
    console.log(`[JarvisConnection] Sending command via REST: ${url}`);
    
    try {
      const response = await fetchWithTimeout(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command: command,
          text: command,
          mode: options.mode || 'manual',
          metadata: { source: 'rest_fallback', timestamp: Date.now(), ...options.metadata }
        })
      }, options.timeout || 30000);

      if (!response.ok) {
        throw new Error(`REST API returned ${response.status}`);
      }

      const data = await response.json();
      console.log('[JarvisConnection] ✅ Command sent via REST API');
      
      // Emit the response as a message event so the UI handles it the same way
      this._emit('message', {
        data: {
          type: 'response',
          text: data.response || data.result || data.text || 'Command processed.',
          status: data.success !== false ? 'success' : 'error',
          command_type: data.command_type || 'command',
          speak: data.speak !== false,
          routing: { routed_to: 'rest_fallback' }
        }
      });

      return { 
        success: true, 
        route: 'rest', 
        response: data.response || data.result || data.text || 'Command processed.' 
      };
    } catch (error) {
      this._restAvailable = false;
      // Re-enable REST check after 10 seconds (it might have been a transient failure)
      setTimeout(() => { this._restAvailable = true; }, 10000);
      throw error;
    }
  }

  async send(message, options = {}) {
    // Try WebSocket first
    if (this.wsClient) {
      const ws = this.getWebSocket();
      if (ws && ws.readyState === WebSocket.OPEN) {
        if (options.reliable) {
          return this.wsClient.sendReliable(message, options.capability, options.timeout || 5000);
        }
        return this.wsClient.send(message, options.capability);
      }
    }
    
    // Fall back to REST for command-type messages
    if (message.type === 'command' && message.text && this.backendUrl) {
      return this._sendViaREST(message.text, options);
    }
    
    throw new Error('No connection available');
  }

  subscribe(messageType, handler) {
    return this.wsClient?.on(messageType, handler);
  }

  getStats() {
    return {
      state: this.connectionState,
      backendUrl: this.backendUrl,
      mode: this.backendMode,
      consecutiveFailures: this.consecutiveFailures,
      wsStats: this.wsClient?.getStats() || null
    };
  }

  async reconnect() {
    console.log('[JarvisConnection] Manual reconnect requested');
    this._stopHealthMonitoring();
    this.consecutiveFailures = 0;
    this.discoveryState.attempts = 0;
    await this._discoverBackend();
  }

  destroy() {
    this._stopHealthMonitoring();
    this.wsClient?.destroy();
    this.listeners.clear();
  }
}

// ============================================================================
// SINGLETON INSTANCE
// ============================================================================

let serviceInstance = null;

export function getJarvisConnectionService() {
  if (!serviceInstance) {
    serviceInstance = new JarvisConnectionService();
  }
  return serviceInstance;
}

// ============================================================================
// REACT HOOK
// ============================================================================

export function useJarvisConnection() {
  const [state, setState] = React.useState(ConnectionState.INITIALIZING);
  const [mode, setMode] = React.useState('unknown');
  const [error, setError] = React.useState(null);
  
  const service = React.useMemo(() => getJarvisConnectionService(), []);
  
  React.useEffect(() => {
    // Get initial state
    setState(service.getState());
    setMode(service.getMode());
    setError(service.getLastError());
    
    // Subscribe to changes
    const unsubscribeState = service.on('stateChange', ({ state: newState }) => {
      setState(newState);
      setError(service.getLastError());
    });
    
    const unsubscribeMode = service.on('modeChange', ({ mode: newMode }) => {
      setMode(newMode);
    });
    
    return () => {
      unsubscribeState();
      unsubscribeMode();
    };
  }, [service]);
  
  return {
    state,
    mode,
    error,
    isConnected: state === ConnectionState.ONLINE,
    isConnecting: state === ConnectionState.CONNECTING || state === ConnectionState.RECONNECTING,
    isOffline: state === ConnectionState.OFFLINE,
    
    sendCommand: (cmd, opts) => service.sendCommand(cmd, opts),
    send: (msg, opts) => service.send(msg, opts),
    subscribe: (type, handler) => service.subscribe(type, handler),
    reconnect: () => service.reconnect(),
    
    service
  };
}

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

export function connectionStateToJarvisStatus(state) {
  switch (state) {
    case ConnectionState.ONLINE:
      return 'online';
    case ConnectionState.CONNECTING:
    case ConnectionState.DISCOVERING:
      return 'connecting';
    case ConnectionState.RECONNECTING:
      return 'reconnecting';
    case ConnectionState.INITIALIZING:
      return 'initializing';
    case ConnectionState.OFFLINE:
    case ConnectionState.ERROR:
    default:
      return 'offline';
  }
}

export default JarvisConnectionService;
