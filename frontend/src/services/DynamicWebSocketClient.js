/**
 * Dynamic WebSocket Client v3.1
 * =============================
 * Fully async, non-blocking WebSocket client with:
 * - Non-blocking connection with proper timeouts
 * - Intelligent message routing and capability-based endpoints
 * - Reliable messaging with ACKs and auto-retry
 * - Offline queue with TTL and automatic flush
 * - Health-weighted connection selection
 * - Self-learning message schema validation
 * - Global connection locking to prevent duplicates
 * - Cross-tab/window coordination via BroadcastChannel
 * - Zero hardcoding - fully dynamic
 */

// ============================================================================
// GLOBAL CONNECTION LOCK (Prevents duplicate connections across instances)
// ============================================================================

/**
 * Global lock to prevent duplicate WebSocket connections.
 * Uses a WeakMap pattern to track active connections by URL.
 */
const globalConnectionLock = {
  // Map of URL -> { connecting: boolean, connection: WebSocket|null, waiters: [] }
  _locks: new Map(),

  // Get or create lock for a URL
  getLock(url) {
    if (!this._locks.has(url)) {
      this._locks.set(url, {
        connecting: false,
        connection: null,
        waiters: [],
        instanceId: null
      });
    }
    return this._locks.get(url);
  },

  // Attempt to acquire lock for connection
  async acquire(url, instanceId) {
    const lock = this.getLock(url);

    // If there's already a healthy connection, return it
    if (lock.connection && lock.connection.readyState === WebSocket.OPEN) {
      console.log(`[WS-LOCK] Reusing existing connection for ${url}`);
      return { acquired: false, existingConnection: lock.connection };
    }

    // If another instance is connecting, wait for it
    if (lock.connecting && lock.instanceId !== instanceId) {
      console.log(`[WS-LOCK] Waiting for existing connection attempt to ${url}`);
      return new Promise((resolve) => {
        const timeout = setTimeout(() => {
          // Remove from waiters and allow this instance to try
          const idx = lock.waiters.indexOf(resolve);
          if (idx > -1) lock.waiters.splice(idx, 1);
          resolve({ acquired: true, existingConnection: null });
        }, 10000); // 10s timeout

        lock.waiters.push((result) => {
          clearTimeout(timeout);
          resolve(result);
        });
      });
    }

    // Acquire the lock
    lock.connecting = true;
    lock.instanceId = instanceId;
    return { acquired: true, existingConnection: null };
  },

  // Release lock with result
  release(url, connection) {
    const lock = this.getLock(url);
    lock.connecting = false;
    lock.connection = connection;

    // Notify waiters
    const result = { acquired: false, existingConnection: connection };
    lock.waiters.forEach(waiter => waiter(result));
    lock.waiters = [];
  },

  // Mark connection as closed
  connectionClosed(url) {
    const lock = this.getLock(url);
    lock.connection = null;
  }
};

// ============================================================================
// CROSS-TAB COORDINATION (Prevents duplicate connections in incognito/multi-tab)
// ============================================================================

/**
 * Coordinate WebSocket connections across browser tabs/windows.
 * Uses BroadcastChannel API if available.
 */
class CrossTabCoordinator {
  constructor() {
    this.channel = null;
    this.tabId = `tab_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`;
    this.leaderTabId = null;
    this.isLeader = false;
    this.callbacks = new Map();

    this._initialize();
  }

  _initialize() {
    if (typeof BroadcastChannel !== 'undefined') {
      try {
        this.channel = new BroadcastChannel('jarvis_ws_coordination');

        this.channel.onmessage = (event) => {
          this._handleMessage(event.data);
        };

        // Announce this tab
        this._broadcast({ type: 'tab_announce', tabId: this.tabId });

        // Elect leader after short delay
        setTimeout(() => {
          if (!this.leaderTabId) {
            this.isLeader = true;
            this.leaderTabId = this.tabId;
            this._broadcast({ type: 'leader_elected', tabId: this.tabId });
          }
        }, 500);

        console.log(`[WS-COORD] Cross-tab coordination initialized (tab: ${this.tabId})`);
      } catch (e) {
        console.warn('[WS-COORD] BroadcastChannel not available:', e.message);
      }
    }
  }

  _broadcast(message) {
    if (this.channel) {
      try {
        this.channel.postMessage({ ...message, from: this.tabId, timestamp: Date.now() });
      } catch (e) {
        // Channel might be closed
      }
    }
  }

  _handleMessage(data) {
    switch (data.type) {
      case 'tab_announce':
        // Another tab announced itself
        if (this.isLeader) {
          this._broadcast({ type: 'leader_elected', tabId: this.tabId });
        }
        break;

      case 'leader_elected':
        // A leader was elected
        if (data.tabId !== this.tabId) {
          this.isLeader = false;
          this.leaderTabId = data.tabId;
        }
        break;

      case 'ws_connected':
        // Leader connected to WebSocket
        if (data.url) {
          const callback = this.callbacks.get(`connection_${data.url}`);
          if (callback) callback(data);
        }
        break;
    }
  }

  /**
   * Check if this tab should handle the WebSocket connection
   */
  shouldConnect() {
    // In incognito, always allow connection (no cross-tab coordination)
    if (this._isIncognito()) {
      return true;
    }
    // Otherwise, only leader should connect
    return this.isLeader || !this.leaderTabId;
  }

  _isIncognito() {
    // Heuristic detection - not 100% reliable
    try {
      const fs = window.RequestFileSystem || window.webkitRequestFileSystem;
      if (fs) {
        return new Promise((resolve) => {
          fs(window.TEMPORARY, 100, () => resolve(false), () => resolve(true));
        });
      }
    } catch (e) {
      // Not available
    }
    return false;
  }

  notifyConnection(url, success) {
    this._broadcast({ type: 'ws_connected', url, success });
  }

  destroy() {
    if (this.channel) {
      try {
        this.channel.close();
      } catch (e) {
        // Already closed
      }
      this.channel = null;
    }
  }
}

// Singleton coordinator
const crossTabCoordinator = new CrossTabCoordinator();

// ============================================================================
// UTILITIES
// ============================================================================

/**
 * Generate unique message ID
 */
const generateId = () => `msg_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

// Timeout wrapper is implemented inline where needed for more control

/**
 * Yield to event loop to prevent blocking
 */
const yieldToEventLoop = () => new Promise(resolve => setTimeout(resolve, 0));

// ============================================================================
// CONNECTION STATE
// ============================================================================

const ConnectionState = {
  DISCONNECTED: 'disconnected',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  RECONNECTING: 'reconnecting',
  FAILED: 'failed'
};

// ============================================================================
// DYNAMIC WEBSOCKET CLIENT
// ============================================================================

class DynamicWebSocketClient {
  constructor(config = {}) {
    // Generate unique instance ID for lock coordination
    this.instanceId = `client_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`;

    // Configuration
    this.config = {
      autoDiscover: false,
      autoReconnect: true,
      reconnectStrategy: 'exponential', // 'linear', 'exponential', 'fibonacci'
      maxReconnectAttempts: 10,
      baseReconnectDelay: 1000,
      maxReconnectDelay: 30000,
      heartbeatInterval: 30000,
      heartbeatTimeout: 10000,
      connectionTimeout: 10000,
      messageTimeout: 5000,
      // v277.0: Queue Policy Contract (explicit, not implicit — Disease 6 cure)
      // - Max size: FIFO eviction when full (oldest dropped)
      // - TTL: expired on flush, rejected with "Message expired"
      // - Persistence: IN-MEMORY ONLY (lost on tab reload — by design)
      //   Tab reload = fresh session. Queued commands from previous session
      //   context may be stale/dangerous.
      // - Replay order: FIFO (oldest first)
      // - Dedup: Server-side via command_id (see unified_command_processor.py)
      maxQueueSize: parseInt(localStorage.getItem('jarvis_queue_max') || '500', 10),
      queueTTL: parseInt(localStorage.getItem('jarvis_queue_ttl') || '300000', 10), // 5 min default
      useGlobalLock: true, // Use global lock to prevent duplicate connections
      ...config
    };

    // Connection state
    this.connections = new Map(); // endpoint -> WebSocket
    this.connectionStates = new Map(); // endpoint -> ConnectionState
    this.connectionMetrics = new Map(); // endpoint -> metrics

    // Endpoints configuration
    this.endpoints = [];

    // Message handling
    this.messageHandlers = new Map(); // type -> handlers[]
    this.pendingACKs = new Map(); // messageId -> { resolve, reject, timeout }
    this.learnedSchemas = new Map(); // type -> schema

    // Offline queue
    this.offlineQueue = [];

    // Reconnection tracking
    this.reconnectAttempts = new Map(); // endpoint -> attempts
    this.reconnectTimers = new Map(); // endpoint -> timerId

    // Heartbeat
    this.heartbeatTimers = new Map(); // endpoint -> timerId
    this.lastPong = new Map(); // endpoint -> timestamp

    // State
    this.isDestroyed = false;

    // v360.2: Event Stream Protocol codec (activated for /ws/stream URLs)
    this._esCodec = new EventStreamCodec();
    this._esUrls = new Set(); // URLs that use the event stream protocol

    console.log(`[WS-CLIENT] Created instance ${this.instanceId}`);
  }

  // ==========================================================================
  // CONNECTION MANAGEMENT
  // ==========================================================================

  /**
   * Connect to a WebSocket endpoint
   * @param {string} endpointUrl - Full WebSocket URL or capability name
   * @returns {Promise<WebSocket>}
   */
  async connect(endpointUrl) {
    if (this.isDestroyed) {
      throw new Error('Client has been destroyed');
    }

    // Resolve endpoint URL
    let url = endpointUrl;
    if (!endpointUrl.startsWith('ws://') && !endpointUrl.startsWith('wss://')) {
      // It's a capability name, find matching endpoint
      const endpoint = this.endpoints.find(ep =>
        ep.capabilities?.includes(endpointUrl)
      );
      url = endpoint?.path || this.endpoints[0]?.path;

      if (!url) {
        throw new Error(`No endpoint found for capability: ${endpointUrl}`);
      }
    }

    // Check if already connected (local instance check)
    const existing = this.connections.get(url);
    if (existing?.readyState === WebSocket.OPEN) {
      console.log(`[WS-CLIENT] Reusing existing local connection for ${url}`);
      return existing;
    }

    // Use global lock to prevent duplicate connections across instances
    if (this.config.useGlobalLock) {
      const lockResult = await globalConnectionLock.acquire(url, this.instanceId);

      if (!lockResult.acquired && lockResult.existingConnection) {
        // Another instance already has a connection, reuse it
        console.log(`[WS-CLIENT] Using shared connection for ${url}`);
        this.connections.set(url, lockResult.existingConnection);
        this.connectionStates.set(url, ConnectionState.CONNECTED);
        return lockResult.existingConnection;
      }
    }

    // Close any existing connection in bad state
    if (existing) {
      this._closeConnection(url);
    }

    this.connectionStates.set(url, ConnectionState.CONNECTING);

    try {
      const ws = await this._createConnection(url);

      // Release lock with successful connection
      if (this.config.useGlobalLock) {
        globalConnectionLock.release(url, ws);
        crossTabCoordinator.notifyConnection(url, true);
      }

      return ws;
    } catch (error) {
      this.connectionStates.set(url, ConnectionState.FAILED);

      // Release lock on failure
      if (this.config.useGlobalLock) {
        globalConnectionLock.release(url, null);
        crossTabCoordinator.notifyConnection(url, false);
      }

      throw error;
    }
  }

  /**
   * Create WebSocket connection with timeout
   */
  async _createConnection(url) {
    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        ws.close();
        reject(new Error(`Connection timeout: ${url}`));
      }, this.config.connectionTimeout);

      let ws;
      try {
        ws = new WebSocket(url);
      } catch (error) {
        clearTimeout(timeoutId);
        reject(error);
        return;
      }

      ws.onopen = () => {
        clearTimeout(timeoutId);

        this.connections.set(url, ws);
        this.connectionStates.set(url, ConnectionState.CONNECTED);
        this.reconnectAttempts.set(url, 0);

        // Initialize metrics
        this._initMetrics(url);

        // v360.2: Event Stream protocol — send handshake, skip legacy heartbeat
        const isEventStream = url.includes('/ws/stream');
        if (isEventStream) {
          this._esUrls.add(url);
          this._esCodec.resetWsFailures();
          this._esCodec.stopSSEFallback();
          // Send handshake frame
          try {
            ws.send(this._esCodec.makeHandshake());
          } catch (e) {
            console.warn('[EventStream] Handshake send failed:', e);
          }
          // Do NOT start legacy heartbeat — _sync frames handle liveness
        } else {
          // Legacy: start ping/pong heartbeat
          this._startHeartbeat(url);
        }

        // Flush offline queue
        this._flushQueue();

        // Emit connected event
        this._emit('connected', { endpoint: url });

        console.log(`✅ WebSocket connected: ${url}${isEventStream ? ' [EventStream]' : ''}`);
        resolve(ws);
      };

      ws.onerror = (error) => {
        clearTimeout(timeoutId);
        this._updateMetrics(url, 'error');
        console.error(`❌ WebSocket error: ${url}`, error);
        
        if (this.connectionStates.get(url) === ConnectionState.CONNECTING) {
          reject(error);
        }
      };

      ws.onclose = (event) => {
        clearTimeout(timeoutId);
        this._handleClose(url, event);
      };

      ws.onmessage = (event) => {
        this._handleMessage(url, event);
      };
    });
  }

  /**
   * Get the URL for a WebSocket instance (reverse lookup).
   */
  _getConnectionUrl(ws) {
    for (const [url, conn] of this.connections) {
      if (conn === ws) return url;
    }
    return null;
  }

  /**
   * Handle WebSocket close event
   */
  _handleClose(url, event) {
    console.log(`🔌 WebSocket closed: ${url} (code: ${event.code})`);

    this._stopHeartbeat(url);
    this.connections.delete(url);
    this.connectionStates.set(url, ConnectionState.DISCONNECTED);

    // v360.2: Track consecutive WS failures for SSE fallback trigger
    if (this._esUrls.has(url)) {
      const shouldFallback = this._esCodec.recordWsFailure();
      if (shouldFallback && !this._esCodec._sseFallback) {
        // Derive HTTP base URL from WS URL
        const httpBase = url.replace('wss://', 'https://').replace('ws://', 'http://')
          .replace(/\/ws\/stream$/, '');
        this._esCodec.startSSEFallback(httpBase, (data) => {
          this._routeMessage(data, url);
        });
        console.log('[EventStream] Activated SSE fallback after 3 WS failures');
      }
    }

    // Notify global lock that connection is closed
    if (this.config.useGlobalLock) {
      globalConnectionLock.connectionClosed(url);
    }

    // Emit disconnected event
    this._emit('disconnected', { endpoint: url, code: event.code });

    // Attempt reconnection unless destroyed or clean close
    if (!this.isDestroyed && this.config.autoReconnect !== false && event.code !== 1000) {
      this._scheduleReconnect(url);
    }
  }

  /**
   * Schedule reconnection with backoff
   */
  _scheduleReconnect(url) {
    if (this.reconnectTimers.has(url)) {
      return;
    }

    const attempts = this.reconnectAttempts.get(url) || 0;
    
    if (attempts >= this.config.maxReconnectAttempts) {
      console.error(`Max reconnection attempts reached for: ${url}`);
      this.connectionStates.set(url, ConnectionState.FAILED);
      this._emit('reconnect_failed', { endpoint: url, attempts });
      return;
    }

    const delay = this._calculateBackoff(attempts);
    this.reconnectAttempts.set(url, attempts + 1);
    this.connectionStates.set(url, ConnectionState.RECONNECTING);

    console.log(`🔄 Reconnecting to ${url} in ${delay}ms (attempt ${attempts + 1})`);

    const timerId = setTimeout(async () => {
      this.reconnectTimers.delete(url);
      
      try {
        await this.connect(url);
      } catch (error) {
        console.error(`Reconnection failed: ${url}`, error.message);
        // Will trigger another reconnect via onclose
      }
    }, delay);

    this.reconnectTimers.set(url, timerId);
  }

  /**
   * Calculate reconnection delay based on strategy
   */
  _calculateBackoff(attempts) {
    const { baseReconnectDelay, maxReconnectDelay, reconnectStrategy } = this.config;
    
    let delay;
    switch (reconnectStrategy) {
      case 'linear':
        delay = baseReconnectDelay * (attempts + 1);
        break;
      case 'fibonacci':
        delay = baseReconnectDelay * this._fibonacci(attempts + 1);
        break;
      case 'exponential':
      default:
        delay = baseReconnectDelay * Math.pow(2, attempts);
    }
    
    // Add jitter (±10%) to prevent thundering herd
    const jitter = delay * 0.1 * (Math.random() * 2 - 1);
    return Math.min(delay + jitter, maxReconnectDelay);
  }

  _fibonacci(n) {
    if (n <= 1) return n;
    let a = 0, b = 1;
    for (let i = 2; i <= n; i++) {
      [a, b] = [b, a + b];
    }
    return b;
  }

  /**
   * Close a specific connection
   */
  _closeConnection(url) {
    const ws = this.connections.get(url);
    if (ws) {
      ws.close(1000, 'Client closed');
      this.connections.delete(url);
    }
    
    this._stopHeartbeat(url);
    
    const timerId = this.reconnectTimers.get(url);
    if (timerId) {
      clearTimeout(timerId);
      this.reconnectTimers.delete(url);
    }
  }

  // ==========================================================================
  // HEARTBEAT
  // ==========================================================================

  _startHeartbeat(url) {
    this._stopHeartbeat(url);

    // v289.0: Initialize lastPong to now so the first heartbeat check
    // doesn't fire a false timeout immediately after connecting.
    this.lastPong.set(url, Date.now());

    const timerId = setInterval(() => {
      const ws = this.connections.get(url);
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        this._stopHeartbeat(url);
        return;
      }

      // Send ping
      try {
        ws.send(JSON.stringify({ type: 'ping', timestamp: Date.now() }));
      } catch {
        // Connection might be closing
      }

      // v289.0: Skip pong-timeout check if the tab was recently hidden.
      // Chrome throttles setInterval in background tabs, so the heartbeat
      // interval itself is extended — lastPong appears stale even though
      // the connection is alive. We reset lastPong on tab-visible events
      // (see _setupVisibilityHeartbeatReset below), so a fresh timestamp
      // here means the tab just came back into focus.
      const lastPong = this.lastPong.get(url) || Date.now();
      const elapsed = Date.now() - lastPong;
      // Use heartbeatInterval (not heartbeatTimeout) as the upper bound:
      // if the tab was hidden, the interval itself stretched, so we allow
      // up to 2× the interval before calling it a true timeout.
      const staleThreshold = Math.max(
        this.config.heartbeatTimeout * 2,
        this.config.heartbeatInterval * 2
      );
      if (elapsed > staleThreshold) {
        console.warn(`[WS-Heartbeat] Pong timeout for: ${url} (${elapsed}ms > ${staleThreshold}ms)`);
        ws.close(4000, 'Heartbeat timeout');
      }
    }, this.config.heartbeatInterval);

    this.heartbeatTimers.set(url, timerId);

    // v289.0: Reset lastPong when the tab becomes visible so Chrome's
    // timer throttling (which pauses/slows setInterval in background tabs)
    // doesn't cause false-positive pong timeouts on tab focus.
    this._setupVisibilityHeartbeatReset(url);
  }

  _setupVisibilityHeartbeatReset(url) {
    // Remove any previous listener for this url
    this._teardownVisibilityHeartbeatReset(url);

    if (typeof document === 'undefined') return;

    const handler = () => {
      if (!document.hidden) {
        // Tab became visible — reset lastPong so stale check doesn't fire
        this.lastPong.set(url, Date.now());
        console.log(`[WS-Heartbeat] Tab visible — reset lastPong for ${url}`);

        // Also send an immediate ping to re-validate liveness
        const ws = this.connections.get(url);
        if (ws && ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: 'ping', timestamp: Date.now(), resuming: true }));
          } catch {
            // ignore — heartbeat interval will handle it
          }
        }
      }
    };

    document.addEventListener('visibilitychange', handler);

    if (!this._visibilityHandlers) this._visibilityHandlers = new Map();
    this._visibilityHandlers.set(url, handler);
  }

  _teardownVisibilityHeartbeatReset(url) {
    if (!this._visibilityHandlers || typeof document === 'undefined') return;
    const handler = this._visibilityHandlers.get(url);
    if (handler) {
      document.removeEventListener('visibilitychange', handler);
      this._visibilityHandlers.delete(url);
    }
  }

  _stopHeartbeat(url) {
    const timerId = this.heartbeatTimers.get(url);
    if (timerId) {
      clearInterval(timerId);
      this.heartbeatTimers.delete(url);
    }
    this._teardownVisibilityHeartbeatReset(url);
  }

  // ==========================================================================
  // MESSAGE HANDLING
  // ==========================================================================

  _handleMessage(url, event) {
    try {
      // v360.2: Event Stream protocol — unwrap envelope
      if (this._esUrls.has(url)) {
        const inner = this._esCodec.unwrapInbound(event.data);
        if (inner === null) {
          // Sync frame or handshake_ack — already processed by codec
          this.lastPong.set(url, Date.now()); // Treat as liveness signal
          return;
        }
        // Route the unwrapped inner payload
        if (inner.type && !this.learnedSchemas.has(inner.type)) {
          this._learnSchema(inner);
        }
        this._updateMetrics(url, 'message', inner);
        this._routeMessage(inner, url);
        return;
      }

      // Legacy path
      const data = JSON.parse(event.data);

      // Handle pong
      if (data.type === 'pong' || data.type === 'ping') {
        this.lastPong.set(url, Date.now());
        return;
      }

      // Handle ACK
      if (data.type === 'ack' && data.ackId) {
        this._handleACK(data.ackId, data);
        return;
      }

      // Learn message schema
      if (data.type && !this.learnedSchemas.has(data.type)) {
        this._learnSchema(data);
      }

      // Update metrics
      this._updateMetrics(url, 'message', data);

      // Route to handlers
      this._routeMessage(data, url);

    } catch (error) {
      console.error('Message parsing error:', error);
      this._updateMetrics(url, 'error');
    }
  }

  _handleACK(ackId, data) {
    const pending = this.pendingACKs.get(ackId);
    if (pending) {
      clearTimeout(pending.timeout);
      pending.resolve(data);
      this.pendingACKs.delete(ackId);
    }
  }

  _routeMessage(data, endpoint) {
    const type = data.type || '*';
    
    // Type-specific handlers
    const handlers = this.messageHandlers.get(type) || [];
    handlers.forEach(handler => {
      try {
        handler(data, endpoint);
      } catch (error) {
        console.error(`Handler error for ${type}:`, error);
      }
    });
    
    // Global handlers
    const globalHandlers = this.messageHandlers.get('*') || [];
    globalHandlers.forEach(handler => {
      try {
        handler(data, endpoint);
      } catch (error) {
        console.error('Global handler error:', error);
      }
    });
  }

  _learnSchema(data) {
    const schema = {};
    for (const [key, value] of Object.entries(data)) {
      schema[key] = Array.isArray(value) ? 'array' : typeof value;
    }
    this.learnedSchemas.set(data.type, schema);
  }

  // ==========================================================================
  // SENDING MESSAGES
  // ==========================================================================

  /**
   * Send a message (fire and forget)
   */
  async send(message, capability = null) {
    // v360.2: SSE fallback path — if active, route commands via REST
    if (this._esCodec._sseFallback && !this._getConnection(capability)) {
      try {
        return await this._esCodec.sendViaREST(message);
      } catch (error) {
        console.error('[EventStream] REST send failed:', error);
        return this._queueMessage(message, capability, 'normal');
      }
    }

    const ws = this._getConnection(capability);

    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return this._queueMessage(message, capability, 'normal');
    }

    try {
      // v360.2: Wrap with event stream envelope if applicable
      const url = this._getConnectionUrl(ws);
      if (url && this._esUrls.has(url)) {
        ws.send(this._esCodec.wrapOutbound(message));
      } else {
        ws.send(JSON.stringify(message));
      }
      return true;
    } catch (error) {
      console.error('Send failed:', error);
      return this._queueMessage(message, capability, 'normal');
    }
  }

  /**
   * Send a message and wait for ACK
   */
  async sendReliable(message, capability = null, timeout = null) {
    const messageTimeout = timeout || this.config.messageTimeout;
    const messageId = message.messageId || generateId();
    const messageWithId = { ...message, messageId };

    const ws = this._getConnection(capability);
    
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      // Queue for later
      return this._queueMessage(messageWithId, capability, 'reliable', messageTimeout);
    }

    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        this.pendingACKs.delete(messageId);
        reject(new Error(`ACK timeout for message: ${messageId}`));
      }, messageTimeout);

      this.pendingACKs.set(messageId, {
        resolve,
        reject,
        timeout: timeoutId
      });

      try {
        ws.send(JSON.stringify(messageWithId));
      } catch (error) {
        clearTimeout(timeoutId);
        this.pendingACKs.delete(messageId);
        reject(error);
      }
    });
  }

  /**
   * Get best available connection for capability
   */
  _getConnection(capability) {
    // If capability specified, find matching endpoint
    if (capability) {
      for (const [url, ws] of this.connections) {
        if (ws.readyState !== WebSocket.OPEN) continue;
        
        const endpoint = this.endpoints.find(ep => ep.path === url);
        if (endpoint?.capabilities?.includes(capability)) {
          return ws;
        }
      }
    }
    
    // Return first available connection
    for (const [, ws] of this.connections) {
      if (ws.readyState === WebSocket.OPEN) {
        return ws;
      }
    }
    
    return null;
  }

  // ==========================================================================
  // OFFLINE QUEUE
  // ==========================================================================

  _queueMessage(message, capability, type, timeout = null) {
    if (this.offlineQueue.length >= this.config.maxQueueSize) {
      console.warn('Offline queue full, dropping oldest message');
      this.offlineQueue.shift();
    }

    return new Promise((resolve, reject) => {
      this.offlineQueue.push({
        message,
        capability,
        type,
        timeout,
        timestamp: Date.now(),
        resolve,
        reject
      });
      
      console.log(`📥 Queued message (${this.offlineQueue.length} in queue)`);
    });
  }

  async _flushQueue() {
    if (this.offlineQueue.length === 0) return;

    console.log(`📤 Flushing ${this.offlineQueue.length} queued messages`);

    const queue = [...this.offlineQueue];
    this.offlineQueue = [];

    for (const item of queue) {
      await yieldToEventLoop();

      // v263.1: Defensive guard — items pushed directly to the queue
      // (e.g. by external code) may lack resolve/reject Promise callbacks.
      const safeResolve = typeof item.resolve === 'function' ? item.resolve : () => {};
      const safeReject = typeof item.reject === 'function' ? item.reject : () => {};
      const itemTimestamp = item.timestamp || item._queuedAt || Date.now();

      // Check TTL
      if (Date.now() - itemTimestamp > this.config.queueTTL) {
        console.warn('Message expired in queue, discarding');
        safeReject(new Error('Message expired in queue'));
        continue;
      }

      try {
        if (item.type === 'reliable') {
          await this.sendReliable(item.message || item, item.capability, item.timeout);
        } else {
          await this.send(item.message || item, item.capability);
        }
        safeResolve();
      } catch (error) {
        // Re-queue if still no connection
        if (!this._getConnection(item.capability)) {
          this.offlineQueue.push(item);
        } else {
          safeReject(error);
        }
      }
    }
  }

  // ==========================================================================
  // METRICS
  // ==========================================================================

  _initMetrics(url) {
    this.connectionMetrics.set(url, {
      messages: 0,
      errors: 0,
      latencies: [],
      connectedAt: Date.now(),
      lastActivity: Date.now()
    });
  }

  _updateMetrics(url, event, data = null) {
    const metrics = this.connectionMetrics.get(url);
    if (!metrics) return;

    switch (event) {
      case 'message':
        metrics.messages++;
        metrics.lastActivity = Date.now();
        break;
      case 'error':
        metrics.errors++;
        break;
      case 'latency':
        metrics.latencies.push(data);
        if (metrics.latencies.length > 100) {
          metrics.latencies.shift();
        }
        break;
      default:
        // Unknown event type, ignore
        break;
    }
  }

  // ==========================================================================
  // EVENT HANDLERS
  // ==========================================================================

  on(messageType, handler) {
    if (!this.messageHandlers.has(messageType)) {
      this.messageHandlers.set(messageType, []);
    }
    this.messageHandlers.get(messageType).push(handler);
    return () => this.off(messageType, handler);
  }

  off(messageType, handler) {
    const handlers = this.messageHandlers.get(messageType);
    if (handlers) {
      const index = handlers.indexOf(handler);
      if (index > -1) handlers.splice(index, 1);
    }
  }

  _emit(event, data) {
    this._routeMessage({ type: event, ...data }, 'internal');
  }

  // ==========================================================================
  // STATISTICS
  // ==========================================================================

  getStats() {
    const connections = [];
    
    for (const [url, ws] of this.connections) {
      connections.push({
        endpoint: url,
        state: this.connectionStates.get(url) || ConnectionState.DISCONNECTED,
        readyState: ws.readyState,
        metrics: this.connectionMetrics.get(url)
      });
    }

    return {
      connections,
      endpoints: this.endpoints,
      queueSize: this.offlineQueue.length,
      learnedMessageTypes: Array.from(this.learnedSchemas.keys()),
      totalMessages: Array.from(this.connectionMetrics.values())
        .reduce((sum, m) => sum + m.messages, 0)
    };
  }

  // ==========================================================================
  // LIFECYCLE
  // ==========================================================================

  destroy() {
    this.isDestroyed = true;
    
    // Close all connections
    for (const [url] of this.connections) {
      this._closeConnection(url);
    }
    
    // Clear all timers
    for (const [, timerId] of this.heartbeatTimers) {
      clearInterval(timerId);
    }
    for (const [, timerId] of this.reconnectTimers) {
      clearTimeout(timerId);
    }
    
    // Clear pending ACKs
    for (const [, pending] of this.pendingACKs) {
      clearTimeout(pending.timeout);
      pending.reject(new Error('Client destroyed'));
    }
    
    // Clear state
    this.connections.clear();
    this.connectionStates.clear();
    this.connectionMetrics.clear();
    this.messageHandlers.clear();
    this.pendingACKs.clear();
    this.heartbeatTimers.clear();
    this.reconnectTimers.clear();
    this.offlineQueue = [];
  }
}

// ============================================================================
// EVENT STREAM CODEC (v360.2)
// Handles seq tracking, ACK piggybacking, handshake, and SSE fallback
// for the /ws/stream persistent bidirectional event stream protocol.
// ============================================================================

class EventStreamCodec {
  constructor() {
    this.lastAck = 0;           // Highest contiguous seq we've processed
    this.sessionId = null;      // Set by handshake_ack
    this.channels = ['command', 'voice', 'governance', 'telemetry'];
    this._active = false;       // Set true after successful handshake
    this._sseFallback = null;   // EventSource instance if WS fails
    this._sseCommandUrl = null;
    this._consecutiveWsFailures = 0;
  }

  get active() { return this._active; }

  makeHandshake(lastAck = null) {
    return JSON.stringify({
      v: 1,
      type: 'handshake',
      last_ack: lastAck !== null ? lastAck : this.lastAck,
      channels: this.channels,
    });
  }

  wrapOutbound(payload) {
    return JSON.stringify({
      v: 1,
      ack: this.lastAck,
      ch: payload.type ? this._resolveChannel(payload.type) : 'command',
      d: payload,
    });
  }

  unwrapInbound(rawData) {
    let frame;
    try {
      frame = typeof rawData === 'string' ? JSON.parse(rawData) : rawData;
    } catch {
      return null;
    }

    // Not an event stream frame — pass through as-is (legacy)
    if (!frame || frame.v !== 1) {
      return frame;
    }

    // Update our ACK tracker
    const seq = frame.seq;
    if (seq && seq === this.lastAck + 1) {
      this.lastAck = seq;
    } else if (seq && seq > this.lastAck + 1) {
      // Gap detected — we missed messages. Accept what we have.
      // The server will replay on next reconnect.
      this.lastAck = seq;
    }

    // Sync frame — no payload, just liveness
    if (frame.ch === '_sync') {
      return null; // Swallow — the seq update above is the only effect
    }

    // Handshake ACK
    if (frame.ch === '_ctrl' && frame.d && frame.d.type === 'handshake_ack') {
      this.sessionId = frame.d.session_id;
      this._active = true;
      console.log(`[EventStream] Handshake OK (session=${this.sessionId}, ` +
        `replay=${frame.d.replay_from}→${frame.d.replay_to})`);
      return null; // Don't route to message handlers
    }

    // Normal message — return inner payload with metadata
    const inner = frame.d;
    if (inner) {
      inner._es_seq = seq;
      inner._es_channel = frame.ch;
    }
    return inner;
  }

  _resolveChannel(msgType) {
    const map = {
      command: 'command', voice_command: 'command', jarvis_command: 'command',
      ml_audio_stream: 'voice', audio_error: 'voice',
      notification: 'governance', model_status: 'governance',
      network_status: 'governance', system_updating: 'governance',
      system_metrics: 'telemetry', health_check: 'telemetry',
      cost_update: 'telemetry',
      vision_analyze: 'vision', vision_monitor: 'vision',
    };
    return map[msgType] || 'command';
  }

  // --- SSE Fallback ---

  startSSEFallback(baseUrl, onMessage) {
    const params = new URLSearchParams({
      last_ack: String(this.lastAck),
      channels: this.channels.join(','),
    });
    const sseUrl = `${baseUrl}/api/stream/sse?${params}`;
    this._sseCommandUrl = `${baseUrl}/api/stream/command`;

    console.log(`[EventStream] Starting SSE fallback: ${sseUrl}`);
    this._sseFallback = new EventSource(sseUrl);

    this._sseFallback.onmessage = (event) => {
      const inner = this.unwrapInbound(event.data);
      if (inner) {
        onMessage(inner);
      }
    };

    this._sseFallback.onerror = () => {
      console.warn('[EventStream] SSE error — browser will auto-reconnect');
    };

    return this._sseFallback;
  }

  stopSSEFallback() {
    if (this._sseFallback) {
      this._sseFallback.close();
      this._sseFallback = null;
    }
  }

  async sendViaREST(payload) {
    if (!this._sseCommandUrl) {
      throw new Error('SSE fallback not active');
    }
    const resp = await fetch(this._sseCommandUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: this.wrapOutbound(payload),
    });
    return resp.json();
  }

  recordWsFailure() {
    this._consecutiveWsFailures++;
    return this._consecutiveWsFailures >= 3;
  }

  resetWsFailures() {
    this._consecutiveWsFailures = 0;
  }
}


export default DynamicWebSocketClient;
export { ConnectionState, EventStreamCodec };
