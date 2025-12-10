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
      timeout: 5000,
      maxFailures: 3
    };
    this.healthCheckTimer = null;
    this.consecutiveFailures = 0;
    
    // Discovery state
    this.discoveryState = {
      attempts: 0,
      maxAttempts: 10,
      inProgress: false
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

  async _tryFastPath() {
    try {
      // Check localStorage for recently verified backend
      const verified = localStorage.getItem('jarvis_backend_verified') === 'true';
      const verifiedAt = parseInt(localStorage.getItem('jarvis_backend_verified_at') || '0');
      const timeSinceVerification = Date.now() - verifiedAt;
      
      // Only use fast path if verified within 30 seconds
      if (!verified || timeSinceVerification > 30000) {
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
    if (this.discoveryState.attempts < this.discoveryState.maxAttempts) {
      // Calculate exponential backoff
      const delay = Math.min(
        3000 * Math.pow(1.5, this.discoveryState.attempts - 1),
        30000
      );
      
      console.log(`[JarvisConnection] Retrying discovery in ${Math.round(delay / 1000)}s`);
      this._setState(ConnectionState.RECONNECTING);
      
      setTimeout(() => this._discoverBackend(), delay);
    } else {
      console.error('[JarvisConnection] Max discovery attempts reached');
      this._setState(ConnectionState.OFFLINE);
      this.lastError = 'Could not find backend service';
      this._emit('error', { message: this.lastError });
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
      
      // Initialize WebSocket
      await this._initializeWebSocket();
      
      // Start health monitoring
      this._startHealthMonitoring();
      
      // Update state
      this._setState(ConnectionState.ONLINE);
      this.consecutiveFailures = 0;
      
      console.log(`[JarvisConnection] ✅ Connected (${this.backendMode} mode)`);
      
    } catch (error) {
      console.error('[JarvisConnection] Connection failed:', error.message);
      this.lastError = error.message;
      this._setState(ConnectionState.ERROR);
      this._scheduleReconnect();
    }
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
          path: `${this.wsUrl}/vision/ws`,
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
    
    // Connect to main WebSocket
    try {
      await this.wsClient.connect(`${this.wsUrl}/ws`);
    } catch (error) {
      console.warn('[JarvisConnection] WebSocket connection failed:', error.message);
      // Don't throw - HTTP health check passed, WS might still work later
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

  async sendCommand(command, options = {}) {
    const message = {
      type: 'command',
      text: command,
      mode: options.mode || 'manual',
      metadata: options.metadata || {},
      timestamp: Date.now()
    };
    
    if (options.audioData) {
      message.audio_data = options.audioData.audio;
      message.sample_rate = options.audioData.sampleRate;
      message.mime_type = options.audioData.mimeType;
    }
    
    if (options.reliable !== false) {
      return this.wsClient?.sendReliable(message, 'jarvis', options.timeout || 10000);
    }
    return this.wsClient?.send(message, 'jarvis');
  }

  async send(message, options = {}) {
    if (options.reliable) {
      return this.wsClient?.sendReliable(message, options.capability, options.timeout || 5000);
    }
    return this.wsClient?.send(message, options.capability);
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
