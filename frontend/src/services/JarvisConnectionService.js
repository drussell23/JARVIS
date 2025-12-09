/**
 * JarvisConnectionService - Unified Connection Management for JARVIS
 * ====================================================================
 * Combines backend discovery, WebSocket management, and status tracking
 * into a single, robust service with React hooks for easy integration.
 * 
 * Features:
 * - Auto-discovery of backend services
 * - Reliable WebSocket with ACKs, retry, offline queue
 * - Connection state management with event emitting
 * - Health monitoring with predictive healing
 * - React hooks for clean component integration
 */

import React from 'react';
import DynamicWebSocketClient from './DynamicWebSocketClient';
import configService from './DynamicConfigService';

// Connection States
export const ConnectionState = {
  INITIALIZING: 'initializing',
  DISCOVERING: 'discovering',
  CONNECTING: 'connecting',
  ONLINE: 'online',
  OFFLINE: 'offline',
  RECONNECTING: 'reconnecting',
  ERROR: 'error'
};

class JarvisConnectionService {
  constructor() {
    // State
    this.connectionState = ConnectionState.INITIALIZING;
    this.backendUrl = null;
    this.wsUrl = null;
    this.lastError = null;
    this.backendMode = 'unknown'; // 'minimal' or 'full'
    
    // WebSocket Client
    this.wsClient = null;
    
    // Event listeners
    this.listeners = new Map();
    
    // Health monitoring
    this.healthCheckInterval = null;
    this.healthCheckFrequency = 5000; // 5 seconds
    this.consecutiveFailures = 0;
    this.maxConsecutiveFailures = 3;
    
    // Discovery state
    this.discoveryAttempts = 0;
    this.maxDiscoveryAttempts = 10;
    this.discoveryInterval = null;
    
    // Initialize
    this._initialize();
  }

  /**
   * Initialize the service
   */
  async _initialize() {
    console.log('[JarvisConnection] Initializing...');
    
    // Listen for config service events
    configService.on('config-ready', (config) => this._handleConfigReady(config));
    configService.on('backend-state', (state) => this._handleBackendState(state));
    configService.on('backend-ready', (state) => this._handleBackendReady(state));
    configService.on('discovery-failed', () => this._handleDiscoveryFailed());
    
    // FAST PATH: Check if backend was just verified by loading page
    // This prevents "CONNECTING TO BACKEND..." showing after loading completes
    const fastPathResult = await this._tryFastPathConnection();
    if (fastPathResult) {
      console.log('[JarvisConnection] ✅ Fast-path connection successful (backend was just verified)');
      return;
    }
    
    // Normal discovery flow
    this._setState(ConnectionState.DISCOVERING);
    await this._discoverBackend();
  }

  /**
   * Try fast-path connection using persisted backend state from loading page
   * Returns true if successful, false if should fall back to normal discovery
   */
  async _tryFastPathConnection() {
    try {
      const backendVerified = localStorage.getItem('jarvis_backend_verified') === 'true';
      const verifiedAt = parseInt(localStorage.getItem('jarvis_backend_verified_at') || '0');
      const timeSinceVerification = Date.now() - verifiedAt;
      
      // Only use fast path if verified within last 30 seconds
      const FAST_PATH_TIMEOUT = 30000;
      
      if (!backendVerified || timeSinceVerification > FAST_PATH_TIMEOUT) {
        console.log('[JarvisConnection] Fast-path not available (not recently verified)');
        return false;
      }
      
      const backendUrl = localStorage.getItem('jarvis_backend_url');
      const wsUrl = localStorage.getItem('jarvis_backend_ws_url');
      const backendPort = localStorage.getItem('jarvis_backend_port');
      
      if (!backendUrl || !wsUrl) {
        console.log('[JarvisConnection] Fast-path not available (missing URLs)');
        return false;
      }
      
      console.log(`[JarvisConnection] Fast-path: Backend was verified ${Math.round(timeSinceVerification / 1000)}s ago`);
      console.log(`[JarvisConnection] Fast-path: Using ${backendUrl}`);
      
      // Set URLs directly (skip discovery)
      this.backendUrl = backendUrl;
      this.wsUrl = wsUrl;
      
      // DON'T set state to CONNECTING - that shows "CONNECTING TO BACKEND..."
      // Instead, stay in INITIALIZING state during the quick verification
      // The UI shows "INITIALIZING..." which is less jarring than "CONNECTING TO BACKEND..."
      
      // Quick health check (should be fast since backend was just verified)
      const health = await this._checkBackendHealth();
      if (!health.ok) {
        console.warn('[JarvisConnection] Fast-path health check failed, falling back to discovery');
        this._clearFastPathState();
        return false;
      }
      
      // Update backend mode
      this.backendMode = health.mode || 'full';
      
      // Initialize WebSocket client
      await this._initializeWebSocket();
      
      // Start health monitoring
      this._startHealthMonitoring();
      
      // Go directly to ONLINE (skipping CONNECTING state entirely)
      this._setState(ConnectionState.ONLINE);
      this.consecutiveFailures = 0;
      
      // Clear fast-path state (one-time use)
      this._clearFastPathState();
      
      console.log(`[JarvisConnection] ✅ Fast-path connected to backend (${this.backendMode} mode)`);
      return true;
      
    } catch (error) {
      console.warn('[JarvisConnection] Fast-path failed:', error.message);
      this._clearFastPathState();
      return false;
    }
  }

  /**
   * Clear fast-path state from localStorage
   */
  _clearFastPathState() {
    try {
      localStorage.removeItem('jarvis_backend_verified');
      localStorage.removeItem('jarvis_backend_verified_at');
      // Keep jarvis_backend_url and jarvis_backend_ws_url for fallback
    } catch (e) {
      // Ignore
    }
  }

  /**
   * Discover backend services
   */
  async _discoverBackend() {
    this.discoveryAttempts++;
    console.log(`[JarvisConnection] Discovery attempt ${this.discoveryAttempts}/${this.maxDiscoveryAttempts}`);
    
    try {
      // Check if config is already available
      const apiUrl = configService.getApiUrl();
      if (apiUrl) {
        this.backendUrl = apiUrl;
        this.wsUrl = configService.getWebSocketUrl();
        await this._connectToBackend();
        return;
      }
      
      // Wait for config with timeout
      const config = await configService.waitForConfig(10000);
      if (config && config.API_BASE_URL) {
        this.backendUrl = config.API_BASE_URL;
        this.wsUrl = config.WS_BASE_URL;
        await this._connectToBackend();
      } else {
        throw new Error('No backend configuration found');
      }
    } catch (error) {
      console.warn('[JarvisConnection] Discovery failed:', error.message);
      this._handleDiscoveryFailed();
    }
  }

  /**
   * Connect to the discovered backend
   */
  async _connectToBackend() {
    this._setState(ConnectionState.CONNECTING);
    
    try {
      // Verify backend is healthy
      const health = await this._checkBackendHealth();
      if (!health.ok) {
        throw new Error(`Backend unhealthy: ${health.error}`);
      }
      
      // Update backend mode
      this.backendMode = health.mode || 'full';
      
      // Initialize WebSocket client
      await this._initializeWebSocket();
      
      // Start health monitoring
      this._startHealthMonitoring();
      
      // Set online
      this._setState(ConnectionState.ONLINE);
      this.consecutiveFailures = 0;
      
      console.log(`[JarvisConnection] ✅ Connected to backend (${this.backendMode} mode)`);
      
    } catch (error) {
      console.error('[JarvisConnection] Connection failed:', error);
      this.lastError = error.message;
      this._setState(ConnectionState.ERROR);
      
      // Schedule retry
      this._scheduleReconnect();
    }
  }

  /**
   * Check backend health
   */
  async _checkBackendHealth() {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      
      const response = await fetch(`${this.backendUrl}/health`, {
        signal: controller.signal
      });
      
      clearTimeout(timeout);
      
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

  /**
   * Initialize WebSocket client
   */
  async _initializeWebSocket() {
    // Create client if not exists
    if (!this.wsClient) {
      this.wsClient = new DynamicWebSocketClient({
        autoDiscover: false, // We handle discovery
        reconnectStrategy: 'exponential',
        maxReconnectAttempts: 10,
        heartbeatInterval: 30000,
        dynamicRouting: true,
        messageValidation: true
      });
      
      // Set up endpoints manually
      // Note: The main unified WebSocket is at /ws (handles all capabilities)
      // JARVIS voice streaming is at /voice/jarvis/stream
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
          capabilities: ['vision', 'monitoring', 'analysis'],
          priority: 8
        }
      ];
    }
    
    // Connect to unified WebSocket endpoint (handles all communication)
    try {
      await this.wsClient.connect(`${this.wsUrl}/ws`);
      
      // Set up message handlers
      this.wsClient.on('*', (data, endpoint) => {
        this._emit('message', { data, endpoint });
      });
      
      this.wsClient.on('response', (data) => {
        this._emit('response', data);
      });
      
      this.wsClient.on('jarvis_response', (data) => {
        this._emit('jarvis_response', data);
      });
      
      this.wsClient.on('workflow_progress', (data) => {
        this._emit('workflow_progress', data);
      });
      
      this.wsClient.on('vbi_progress', (data) => {
        this._emit('vbi_progress', data);
      });
      
      this.wsClient.on('proactive_suggestion', (data) => {
        this._emit('proactive_suggestion', data);
      });
      
    } catch (error) {
      console.warn('[JarvisConnection] WebSocket connection failed, will retry');
      throw error;
    }
  }

  /**
   * Start health monitoring
   */
  _startHealthMonitoring() {
    if (this.healthCheckInterval) {
      clearInterval(this.healthCheckInterval);
    }
    
    this.healthCheckInterval = setInterval(async () => {
      const health = await this._checkBackendHealth();
      
      if (!health.ok) {
        this.consecutiveFailures++;
        console.warn(`[JarvisConnection] Health check failed (${this.consecutiveFailures}/${this.maxConsecutiveFailures})`);
        
        if (this.consecutiveFailures >= this.maxConsecutiveFailures) {
          this._handleConnectionLost();
        }
      } else {
        this.consecutiveFailures = 0;
        
        // Check for mode changes
        if (health.mode !== this.backendMode) {
          console.log(`[JarvisConnection] Backend mode changed: ${this.backendMode} -> ${health.mode}`);
          this.backendMode = health.mode;
          this._emit('modeChange', { mode: health.mode });
        }
      }
    }, this.healthCheckFrequency);
  }

  /**
   * Stop health monitoring
   */
  _stopHealthMonitoring() {
    if (this.healthCheckInterval) {
      clearInterval(this.healthCheckInterval);
      this.healthCheckInterval = null;
    }
  }

  /**
   * Handle connection lost
   */
  _handleConnectionLost() {
    console.warn('[JarvisConnection] Connection lost');
    this._stopHealthMonitoring();
    this._setState(ConnectionState.OFFLINE);
    this._scheduleReconnect();
  }

  /**
   * Schedule reconnection attempt
   */
  _scheduleReconnect() {
    this._setState(ConnectionState.RECONNECTING);
    
    const delay = Math.min(5000 * Math.pow(1.5, this.consecutiveFailures), 30000);
    console.log(`[JarvisConnection] Reconnecting in ${delay}ms...`);
    
    setTimeout(async () => {
      await this._connectToBackend();
    }, delay);
  }

  /**
   * Handle config ready event
   */
  _handleConfigReady(config) {
    if (config.API_BASE_URL && config.API_BASE_URL !== this.backendUrl) {
      console.log('[JarvisConnection] Config updated, reconnecting...');
      this.backendUrl = config.API_BASE_URL;
      this.wsUrl = config.WS_BASE_URL;
      this._connectToBackend();
    }
  }

  /**
   * Handle backend state update
   */
  _handleBackendState(state) {
    if (state.ready && this.connectionState !== ConnectionState.ONLINE) {
      console.log('[JarvisConnection] Backend ready signal received');
      this._connectToBackend();
    }
  }

  /**
   * Handle backend ready event
   */
  _handleBackendReady(state) {
    console.log('[JarvisConnection] Backend fully ready');
    if (this.connectionState !== ConnectionState.ONLINE) {
      this._connectToBackend();
    }
  }

  /**
   * Handle discovery failed
   */
  _handleDiscoveryFailed() {
    if (this.discoveryAttempts < this.maxDiscoveryAttempts) {
      const delay = Math.min(3000 * Math.pow(1.5, this.discoveryAttempts), 30000);
      console.log(`[JarvisConnection] Retrying discovery in ${delay}ms...`);
      
      setTimeout(() => {
        this._discoverBackend();
      }, delay);
    } else {
      console.error('[JarvisConnection] Max discovery attempts reached');
      this._setState(ConnectionState.OFFLINE);
      this.lastError = 'Could not find backend service';
    }
  }

  /**
   * Set connection state and emit event
   */
  _setState(newState) {
    if (this.connectionState !== newState) {
      const oldState = this.connectionState;
      this.connectionState = newState;
      console.log(`[JarvisConnection] State: ${oldState} -> ${newState}`);
      this._emit('stateChange', { oldState, newState, state: newState });
    }
  }

  /**
   * Emit an event to all listeners
   */
  _emit(event, data) {
    const handlers = this.listeners.get(event) || [];
    const allHandlers = this.listeners.get('*') || [];
    
    [...handlers, ...allHandlers].forEach(handler => {
      try {
        handler(data);
      } catch (error) {
        console.error(`[JarvisConnection] Event handler error for ${event}:`, error);
      }
    });
  }

  // ============ Public API ============

  /**
   * Get current connection state
   */
  getState() {
    return this.connectionState;
  }

  /**
   * Check if connected
   */
  isConnected() {
    return this.connectionState === ConnectionState.ONLINE;
  }

  /**
   * Get backend mode
   */
  getMode() {
    return this.backendMode;
  }

  /**
   * Get last error
   */
  getLastError() {
    return this.lastError;
  }

  /**
   * Subscribe to events
   */
  on(event, handler) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, []);
    }
    this.listeners.get(event).push(handler);
    
    return () => this.off(event, handler);
  }

  /**
   * Unsubscribe from events
   */
  off(event, handler) {
    const handlers = this.listeners.get(event);
    if (handlers) {
      const index = handlers.indexOf(handler);
      if (index > -1) {
        handlers.splice(index, 1);
      }
    }
  }

  /**
   * Send a command to JARVIS
   */
  async sendCommand(command, options = {}) {
    if (!this.isConnected()) {
      console.warn('[JarvisConnection] Not connected, queueing command');
    }
    
    const message = {
      type: 'command',
      text: command,
      mode: options.mode || 'manual',
      metadata: options.metadata || {},
      timestamp: Date.now()
    };
    
    // Include audio data if provided
    if (options.audioData) {
      message.audio_data = options.audioData.audio;
      message.sample_rate = options.audioData.sampleRate;
      message.mime_type = options.audioData.mimeType;
    }
    
    // Use reliable send for commands
    if (options.reliable !== false) {
      return this.wsClient.sendReliable(message, 'jarvis', options.timeout || 10000);
    } else {
      return this.wsClient.send(message, 'jarvis');
    }
  }

  /**
   * Send a message (any type)
   */
  async send(message, options = {}) {
    if (options.reliable) {
      return this.wsClient.sendReliable(message, options.capability, options.timeout || 5000);
    }
    return this.wsClient.send(message, options.capability);
  }

  /**
   * Subscribe to a specific message type
   */
  subscribe(messageType, handler) {
    if (this.wsClient) {
      this.wsClient.on(messageType, handler);
    }
    return () => {
      // Note: DynamicWebSocketClient doesn't have off() yet, but handlers are cleaned on destroy
    };
  }

  /**
   * Get the raw WebSocket connection (for backward compatibility)
   */
  getWebSocket() {
    if (!this.wsClient) return null;
    // Return the first open connection
    for (const [_, ws] of this.wsClient.connections) {
      if (ws.readyState === WebSocket.OPEN) {
        return ws;
      }
    }
    return null;
  }

  /**
   * Check if WebSocket is connected
   */
  isWebSocketConnected() {
    const ws = this.getWebSocket();
    return ws && ws.readyState === WebSocket.OPEN;
  }

  /**
   * Get connection statistics
   */
  getStats() {
    return {
      state: this.connectionState,
      backendUrl: this.backendUrl,
      mode: this.backendMode,
      consecutiveFailures: this.consecutiveFailures,
      wsStats: this.wsClient?.getStats() || null
    };
  }

  /**
   * Force reconnection
   */
  async reconnect() {
    console.log('[JarvisConnection] Manual reconnect requested');
    this._stopHealthMonitoring();
    this.consecutiveFailures = 0;
    this.discoveryAttempts = 0;
    await this._discoverBackend();
  }

  /**
   * Cleanup
   */
  destroy() {
    this._stopHealthMonitoring();
    if (this.wsClient) {
      this.wsClient.destroy();
    }
    this.listeners.clear();
  }
}

// ============ Singleton Instance ============
let serviceInstance = null;

export function getJarvisConnectionService() {
  if (!serviceInstance) {
    serviceInstance = new JarvisConnectionService();
  }
  return serviceInstance;
}

// ============ React Hook ============
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
    
    // Subscribe to state changes
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
    // State
    state,
    mode,
    error,
    isConnected: state === ConnectionState.ONLINE,
    isConnecting: state === ConnectionState.CONNECTING || state === ConnectionState.RECONNECTING,
    isOffline: state === ConnectionState.OFFLINE,
    
    // Actions
    sendCommand: (cmd, opts) => service.sendCommand(cmd, opts),
    send: (msg, opts) => service.send(msg, opts),
    subscribe: (type, handler) => service.subscribe(type, handler),
    reconnect: () => service.reconnect(),
    
    // Service access
    service
  };
}

// ============ Helper to map state to UI status ============
export function connectionStateToJarvisStatus(state) {
  switch (state) {
    case ConnectionState.ONLINE:
      return 'online';
    case ConnectionState.CONNECTING:
    case ConnectionState.DISCOVERING:
      return 'connecting';
    case ConnectionState.RECONNECTING:
      return 'connecting';
    case ConnectionState.INITIALIZING:
      return 'initializing';
    case ConnectionState.OFFLINE:
    case ConnectionState.ERROR:
    default:
      return 'offline';
  }
}

export default JarvisConnectionService;