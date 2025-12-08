/**
 * Vision Connection Handler v2.0
 * ==============================
 * Advanced real-time workspace monitoring with:
 * - Robust WebSocket reconnection with exponential backoff
 * - Connection state machine
 * - Message queuing during disconnection
 * - Health monitoring and heartbeat
 * - Dynamic endpoint configuration
 * - Event-driven architecture
 */

import configService from '../services/DynamicConfigService';

// Connection states
const CONNECTION_STATE = {
  DISCONNECTED: 'disconnected',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  RECONNECTING: 'reconnecting',
  FAILED: 'failed'
};

class VisionConnection {
  constructor(onWorkspaceUpdate, onActionExecuted) {
    this.visionSocket = null;
    this.workspaceData = null;

    // Connection state machine
    this.connectionState = CONNECTION_STATE.DISCONNECTED;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 10;
    this.baseReconnectDelay = 1000;
    this.maxReconnectDelay = 30000;

    // Callbacks
    this.onWorkspaceUpdate = onWorkspaceUpdate || (() => {});
    this.onActionExecuted = onActionExecuted || (() => {});
    this.onConnectionStateChange = null;

    // Monitoring state
    this.monitoringActive = false;
    this.autonomousMode = false;

    // Health monitoring
    this.heartbeatInterval = null;
    this.heartbeatTimeout = 30000;
    this.lastHeartbeat = null;
    this.missedHeartbeats = 0;
    this.maxMissedHeartbeats = 3;

    // Message queue for offline handling
    this.messageQueue = [];
    this.maxQueueSize = 100;

    // Event listeners
    this.eventListeners = new Map();

    // Connection metrics
    this.metrics = {
      connectTime: null,
      disconnectCount: 0,
      totalMessages: 0,
      lastError: null
    };
  }

  /**
   * Get WebSocket URL dynamically
   */
  async _getWebSocketUrl() {
    // Wait for config if not ready
    try {
      await configService.waitForConfig(5000);
    } catch {
      console.warn('[VisionConnection] Config timeout, using fallback');
    }

    const baseUrl = configService.getWebSocketUrl();
    if (baseUrl) {
      return `${baseUrl}/vision/ws/vision`;
    }

    // Fallback inference
    const hostname = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
    const protocol = typeof window !== 'undefined'
      ? (window.location.protocol === 'https:' ? 'wss' : 'ws')
      : 'ws';

    return `${protocol}://${hostname}:8000/vision/ws/vision`;
  }

  /**
   * Connect to Vision WebSocket
   */
  async connect() {
    if (this.connectionState === CONNECTION_STATE.CONNECTING) {
      console.log('[VisionConnection] Already connecting...');
      return;
    }

    try {
      this._setConnectionState(CONNECTION_STATE.CONNECTING);
      console.log('[VisionConnection] Connecting to Vision WebSocket...');

      const wsUrl = await this._getWebSocketUrl();
      console.log('[VisionConnection] Using URL:', wsUrl);

      this.visionSocket = new WebSocket(wsUrl);
      this._setupSocketHandlers();

    } catch (error) {
      console.error('[VisionConnection] Connection error:', error);
      this._setConnectionState(CONNECTION_STATE.FAILED);
      this.metrics.lastError = error.message;
      this.attemptReconnect();
    }
  }

  /**
   * Setup WebSocket event handlers
   */
  _setupSocketHandlers() {
    this.visionSocket.onopen = () => {
      console.log('[VisionConnection] Connected!');
      this._setConnectionState(CONNECTION_STATE.CONNECTED);
      this.reconnectAttempts = 0;
      this.metrics.connectTime = Date.now();

      // Start heartbeat monitoring
      this._startHeartbeat();

      // Request initial workspace analysis
      this.requestWorkspaceAnalysis();

      // Process queued messages
      this._processMessageQueue();

      // Emit connected event
      this._emit('connected', { timestamp: Date.now() });
    };

    this.visionSocket.onmessage = (event) => {
      this.metrics.totalMessages++;
      this.lastHeartbeat = Date.now();
      this.missedHeartbeats = 0;

      try {
        const data = JSON.parse(event.data);
        this.handleVisionMessage(data);
      } catch (error) {
        console.error('[VisionConnection] Error parsing message:', error);
      }
    };

    this.visionSocket.onerror = (error) => {
      console.error('[VisionConnection] WebSocket error:', error);
      this.metrics.lastError = 'WebSocket error';
      this._emit('error', { error, timestamp: Date.now() });
    };

    this.visionSocket.onclose = (event) => {
      console.log('[VisionConnection] Disconnected:', event.code, event.reason);
      this._setConnectionState(CONNECTION_STATE.DISCONNECTED);
      this.metrics.disconnectCount++;

      // Stop heartbeat
      this._stopHeartbeat();

      // Emit disconnected event
      this._emit('disconnected', {
        code: event.code,
        reason: event.reason,
        wasClean: event.wasClean,
        timestamp: Date.now()
      });

      // Attempt reconnect unless intentionally closed
      if (event.code !== 1000) {
        this.attemptReconnect();
      }
    };
  }

  /**
   * Handle incoming vision messages
   */
  handleVisionMessage(data) {
    console.log('[VisionConnection] Message:', data.type);

    switch (data.type) {
      case 'initial_state':
        this.handleInitialState(data);
        break;

      case 'workspace_update':
        this.handleWorkspaceUpdate(data);
        break;

      case 'action_executed':
        this.handleActionExecuted(data);
        break;

      case 'workspace_analysis':
        this.handleWorkspaceAnalysis(data);
        break;

      case 'vision_status_update':
        this.handleVisionStatusUpdate(data);
        break;

      case 'config_updated':
        console.log('[VisionConnection] Config updated:', data);
        break;

      case 'heartbeat':
      case 'pong':
        this.lastHeartbeat = Date.now();
        this.missedHeartbeats = 0;
        break;

      case 'error':
        console.error('[VisionConnection] Server error:', data.message);
        this._emit('server-error', data);
        break;

      default:
        console.log('[VisionConnection] Unknown message type:', data.type);
    }
  }

  handleInitialState(data) {
    console.log('[VisionConnection] Initial workspace state received');
    this.monitoringActive = data.monitoring_active;
    this.autonomousMode = data.autonomous_mode;

    this.onWorkspaceUpdate({
      type: 'initial',
      workspace: data.workspace,
      timestamp: data.timestamp
    });
  }

  handleWorkspaceUpdate(data) {
    const windows = data.windows?.length || 0;
    const notifications = data.notifications?.length || 0;
    console.log(`[VisionConnection] Update: ${windows} windows, ${notifications} notifications`);

    this.workspaceData = data;

    this.onWorkspaceUpdate({
      type: 'update',
      windows: data.windows,
      notifications: data.notifications,
      suggestions: data.suggestions,
      autonomousActions: data.autonomous_actions,
      stats: data.stats,
      timestamp: data.timestamp
    });

    // Announce notifications in autonomous mode
    if (notifications > 0 && this.autonomousMode) {
      this.announceNotifications(data.notifications);
    }
  }

  handleActionExecuted(data) {
    console.log('[VisionConnection] Action executed:', data.action);

    this.onActionExecuted({
      action: data.action,
      timestamp: data.timestamp
    });
  }

  handleWorkspaceAnalysis(data) {
    console.log('[VisionConnection] Analysis received');

    this.onWorkspaceUpdate({
      type: 'analysis',
      analysis: data.analysis,
      timestamp: data.timestamp
    });
  }

  handleVisionStatusUpdate(data) {
    console.log('[VisionConnection] Status update:', data.status);

    this.monitoringActive = data.status?.connected || false;

    this.onWorkspaceUpdate({
      type: 'status_update',
      status: data.status,
      timestamp: data.timestamp || new Date().toISOString()
    });
  }

  /**
   * Announce notifications via speech synthesis
   */
  announceNotifications(notifications) {
    if (!notifications?.length || !window.speechSynthesis) return;

    const summary = notifications.slice(0, 3).join(', ');
    const message = `I've detected ${notifications.length} notification${notifications.length > 1 ? 's' : ''}: ${summary}`;

    const utterance = new SpeechSynthesisUtterance(message);
    utterance.volume = 0.7;
    window.speechSynthesis.speak(utterance);
  }

  /**
   * Attempt reconnection with exponential backoff
   */
  attemptReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error('[VisionConnection] Max reconnect attempts reached');
      this._setConnectionState(CONNECTION_STATE.FAILED);
      this._emit('reconnect-failed', {
        attempts: this.reconnectAttempts,
        timestamp: Date.now()
      });
      return;
    }

    this._setConnectionState(CONNECTION_STATE.RECONNECTING);
    this.reconnectAttempts++;

    // Calculate delay with exponential backoff and jitter
    const baseDelay = this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts - 1);
    const jitter = Math.random() * 1000;
    const delay = Math.min(baseDelay + jitter, this.maxReconnectDelay);

    console.log(`[VisionConnection] Reconnecting in ${Math.round(delay)}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);

    this._emit('reconnecting', {
      attempt: this.reconnectAttempts,
      maxAttempts: this.maxReconnectAttempts,
      delay,
      timestamp: Date.now()
    });

    setTimeout(() => {
      this.connect();
    }, delay);
  }

  /**
   * Send message to server with queue support
   */
  send(message) {
    const payload = typeof message === 'string' ? message : JSON.stringify(message);

    if (this.isConnected() && this.visionSocket.readyState === WebSocket.OPEN) {
      this.visionSocket.send(payload);
      return true;
    }

    // Queue message for later
    if (this.messageQueue.length < this.maxQueueSize) {
      this.messageQueue.push({ payload, timestamp: Date.now() });
      console.log('[VisionConnection] Message queued');
      return false;
    }

    console.warn('[VisionConnection] Message queue full, dropping message');
    return false;
  }

  /**
   * Process queued messages after reconnection
   */
  _processMessageQueue() {
    if (this.messageQueue.length === 0) return;

    console.log(`[VisionConnection] Processing ${this.messageQueue.length} queued messages`);

    const queue = [...this.messageQueue];
    this.messageQueue = [];

    // Filter out stale messages (older than 5 minutes)
    const now = Date.now();
    const validMessages = queue.filter(m => now - m.timestamp < 300000);

    validMessages.forEach(m => {
      try {
        this.visionSocket.send(m.payload);
      } catch (error) {
        console.error('[VisionConnection] Failed to send queued message:', error);
      }
    });
  }

  /**
   * Start heartbeat monitoring
   */
  _startHeartbeat() {
    this._stopHeartbeat();

    this.lastHeartbeat = Date.now();

    this.heartbeatInterval = setInterval(() => {
      // Send ping
      this.send({ type: 'ping', timestamp: Date.now() });

      // Check for missed heartbeats
      const elapsed = Date.now() - this.lastHeartbeat;
      if (elapsed > this.heartbeatTimeout) {
        this.missedHeartbeats++;
        console.warn(`[VisionConnection] Missed heartbeat ${this.missedHeartbeats}/${this.maxMissedHeartbeats}`);

        if (this.missedHeartbeats >= this.maxMissedHeartbeats) {
          console.error('[VisionConnection] Connection appears dead, reconnecting...');
          this.disconnect();
          this.attemptReconnect();
        }
      }
    }, 10000);
  }

  /**
   * Stop heartbeat monitoring
   */
  _stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  /**
   * Request workspace analysis
   */
  requestWorkspaceAnalysis() {
    this.send({ type: 'request_workspace_analysis' });
  }

  /**
   * Set monitoring interval
   */
  setMonitoringInterval(interval) {
    this.send({
      type: 'set_monitoring_interval',
      interval
    });
  }

  /**
   * Execute an action
   */
  executeAction(action) {
    this.send({
      type: 'execute_action',
      action
    });
  }

  /**
   * Disconnect WebSocket
   */
  disconnect() {
    this._stopHeartbeat();

    if (this.visionSocket) {
      try {
        this.visionSocket.close(1000, 'Client disconnect');
      } catch {}
      this.visionSocket = null;
    }

    this._setConnectionState(CONNECTION_STATE.DISCONNECTED);
    this.monitoringActive = false;
  }

  /**
   * Check if connected
   */
  isConnected() {
    return this.connectionState === CONNECTION_STATE.CONNECTED &&
           this.visionSocket &&
           this.visionSocket.readyState === WebSocket.OPEN;
  }

  /**
   * Get workspace stats
   */
  getWorkspaceStats() {
    if (this.workspaceData?.stats) {
      return this.workspaceData.stats;
    }
    return {
      window_count: 0,
      notification_count: 0,
      action_count: 0
    };
  }

  /**
   * Get latest notifications
   */
  getLatestNotifications() {
    return this.workspaceData?.notifications || [];
  }

  /**
   * Get autonomous actions
   */
  getAutonomousActions() {
    return this.workspaceData?.autonomous_actions || [];
  }

  /**
   * Get connection metrics
   */
  getMetrics() {
    return {
      ...this.metrics,
      state: this.connectionState,
      reconnectAttempts: this.reconnectAttempts,
      queueSize: this.messageQueue.length,
      missedHeartbeats: this.missedHeartbeats,
      uptime: this.metrics.connectTime ? Date.now() - this.metrics.connectTime : 0
    };
  }

  /**
   * Set connection state and notify
   */
  _setConnectionState(state) {
    const previousState = this.connectionState;
    this.connectionState = state;

    if (previousState !== state) {
      console.log(`[VisionConnection] State: ${previousState} -> ${state}`);

      if (this.onConnectionStateChange) {
        this.onConnectionStateChange(state, previousState);
      }

      this._emit('state-change', {
        current: state,
        previous: previousState,
        timestamp: Date.now()
      });
    }
  }

  /**
   * Event system
   */
  on(event, callback) {
    if (!this.eventListeners.has(event)) {
      this.eventListeners.set(event, []);
    }
    this.eventListeners.get(event).push(callback);
    return () => this.off(event, callback);
  }

  off(event, callback) {
    if (this.eventListeners.has(event)) {
      const listeners = this.eventListeners.get(event);
      const index = listeners.indexOf(callback);
      if (index > -1) {
        listeners.splice(index, 1);
      }
    }
  }

  _emit(event, data) {
    if (this.eventListeners.has(event)) {
      this.eventListeners.get(event).forEach(cb => {
        try {
          cb(data);
        } catch (error) {
          console.error(`[VisionConnection] Event handler error for ${event}:`, error);
        }
      });
    }
  }

  /**
   * Cleanup
   */
  destroy() {
    this.disconnect();
    this.eventListeners.clear();
    this.messageQueue = [];
  }
}

export default VisionConnection;
