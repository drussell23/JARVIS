/**
 * Unified WebSocket Service v3.0
 * ==============================
 * Uses DynamicWebSocketClient for all WebSocket operations
 * Provides a simplified API for components that need WebSocket connectivity
 */

import React from 'react';
import DynamicWebSocketClient, { ConnectionState as WSConnectionState } from './DynamicWebSocketClient';
import configService from './DynamicConfigService';

class UnifiedWebSocketService {
  constructor() {
    this.client = new DynamicWebSocketClient({
      autoDiscover: false,
      reconnectStrategy: 'exponential',
      maxReconnectAttempts: 10,
      heartbeatInterval: 30000,
      connectionTimeout: 10000
    });

    this.subscriptions = new Map();
    this.connectionState = 'disconnected';
    this.isInitialized = false;

    // Maintenance mode state for tracking system updates/restarts
    this.maintenanceMode = false;
    this.maintenanceReason = null; // 'updating' | 'restarting' | 'rollback' | null

    // Update available state for notification badge
    this.updateAvailable = false;
    this.updateInfo = null; // { commits_behind, summary, priority, highlights, security_update, breaking_changes }

    // Local change awareness state (v2.0)
    this.localChangesDetected = false;
    this.localChangeInfo = null; // { changeType, summary, commits_since_start, restart_recommended, restart_reason }

    // Wait for config and then connect
    this._initializeWhenReady();
  }

  async _initializeWhenReady() {
    try {
      // Wait for config service to discover backend
      const config = await configService.waitForConfig(30000);

      if (config?.WS_BASE_URL) {
        // Configure endpoints
        this.client.endpoints = [
          {
            path: `${config.WS_BASE_URL}/ws`,
            capabilities: ['general', 'voice', 'command'],
            priority: 10
          },
          {
            // Backend mounts vision WS at /vision/ws/vision
            path: `${config.WS_BASE_URL}/vision/ws/vision`,
            capabilities: ['vision', 'monitoring'],
            priority: 8
          },
          {
            // Broadcast WebSocket for maintenance mode events from supervisor
            path: `${config.WS_BASE_URL}/api/broadcast/ws`,
            capabilities: ['broadcast', 'maintenance'],
            priority: 5
          }
        ];

        this.isInitialized = true;
        this._setupClientHandlers();
      }
    } catch (error) {
      console.error('[UnifiedWebSocket] Initialization failed:', error.message);
    }
  }

  _setupClientHandlers() {
    this.client.on('connected', ({ endpoint }) => {
      this.connectionState = 'connected';
      // Clear maintenance mode on successful reconnection
      if (this.maintenanceMode) {
        this.maintenanceMode = false;
        this.maintenanceReason = null;
        console.log('✅ Reconnected after maintenance');
        this._notifySubscribers('maintenance_mode', {
          active: false,
          reason: null,
          message: 'System is back online'
        });
      }
      console.log('✅ UnifiedWebSocket connected:', endpoint);
      this._notifySubscribers('connection', { state: 'connected', endpoint });
    });

    this.client.on('disconnected', ({ endpoint }) => {
      this.connectionState = 'disconnected';
      console.log('🔌 UnifiedWebSocket disconnected:', endpoint);
      // Only show "disconnected" if not in maintenance mode
      if (!this.maintenanceMode) {
        this._notifySubscribers('connection', { state: 'disconnected', endpoint });
      }
    });

    // ═══════════════════════════════════════════════════════════════════
    // MAINTENANCE MODE - System Update/Restart/Rollback Events
    // ═══════════════════════════════════════════════════════════════════

    // Handle system updating event from supervisor
    this.client.on('system_updating', (data) => {
      console.log('🔄 System entering maintenance mode: updating');
      this.maintenanceMode = true;
      this.maintenanceReason = 'updating';
      this._notifySubscribers('maintenance_mode', {
        active: true,
        reason: 'updating',
        message: data?.message || 'Downloading updates...',
        estimatedTime: data?.estimated_time || 30,
      });
    });

    // Handle system restarting event
    this.client.on('system_restarting', (data) => {
      console.log('🔄 System entering maintenance mode: restarting');
      this.maintenanceMode = true;
      this.maintenanceReason = 'restarting';
      this._notifySubscribers('maintenance_mode', {
        active: true,
        reason: 'restarting',
        message: data?.message || 'Restarting JARVIS core...',
        estimatedTime: data?.estimated_time || 15,
      });
    });

    // Handle system rollback event
    this.client.on('system_rollback', (data) => {
      console.log('🔄 System entering maintenance mode: rollback');
      this.maintenanceMode = true;
      this.maintenanceReason = 'rollback';
      this._notifySubscribers('maintenance_mode', {
        active: true,
        reason: 'rollback',
        message: data?.message || 'Rolling back to previous version...',
        estimatedTime: data?.estimated_time || 20,
      });
    });

    // Handle system back online
    this.client.on('system_online', (data) => {
      console.log('✅ System back online');
      this.maintenanceMode = false;
      this.maintenanceReason = null;
      this._notifySubscribers('maintenance_mode', {
        active: false,
        reason: null,
        message: data?.message || 'JARVIS is back online',
      });
    });

    // ═══════════════════════════════════════════════════════════════════
    // UPDATE NOTIFICATION - "Update Available" Badge/Modal Events
    // ═══════════════════════════════════════════════════════════════════

    // Handle update_available event from supervisor
    this.client.on('update_available', (data) => {
      console.log('📦 Update available notification received:', data);
      this.updateAvailable = true;
      this.updateInfo = {
        commits_behind: data?.commits_behind || 0,
        summary: data?.summary || 'Updates available',
        priority: data?.priority || 'medium',
        highlights: data?.highlights || [],
        security_update: data?.security_update || false,
        breaking_changes: data?.breaking_changes || false,
        remote_sha: data?.remote_sha || null,
        local_sha: data?.local_sha || null,
        timestamp: data?.timestamp || new Date().toISOString(),
      };
      this._notifySubscribers('update_available', {
        available: true,
        ...this.updateInfo,
      });
    });

    // Handle update_dismissed event (user clicked "later")
    this.client.on('update_dismissed', (data) => {
      console.log('📭 Update notification dismissed');
      this.updateAvailable = false;
      this.updateInfo = null;
      this._notifySubscribers('update_available', {
        available: false,
      });
    });

    // Handle update_progress event (during update process)
    this.client.on('update_progress', (data) => {
      console.log('📊 Update progress:', data);
      this._notifySubscribers('update_progress', {
        phase: data?.phase || 'unknown',
        message: data?.message || 'Updating...',
        progress: data?.progress || 0,
      });
    });

    // Clear update notification when entering maintenance mode for update
    this.client.on('system_updating', () => {
      // Clear the badge - user is acting on the update
      this.updateAvailable = false;
      this.updateInfo = null;
    });

    // ═══════════════════════════════════════════════════════════════════
    // LOCAL CHANGE AWARENESS - Code Changes/Push/Commit Events (v2.0)
    // ═══════════════════════════════════════════════════════════════════

    // Handle local commit detected
    this.client.on('local_commit_detected', (data) => {
      console.log('📝 Local commit detected:', data);
      this._handleLocalChange(data, 'commit');
    });

    // Handle local push detected
    this.client.on('local_push_detected', (data) => {
      console.log('📤 Local push detected:', data);
      this._handleLocalChange(data, 'push');
    });

    // Handle code changes detected (uncommitted)
    this.client.on('code_changes_detected', (data) => {
      console.log('📁 Code changes detected:', data);
      this._handleLocalChange(data, 'uncommitted');
    });

    // Handle generic local changes
    this.client.on('local_changes_detected', (data) => {
      console.log('🔄 Local changes detected:', data);
      this._handleLocalChange(data, 'changes');
    });
  }

  /**
   * Handle local change events (v2.0)
   */
  _handleLocalChange(data, changeType) {
    this.localChangesDetected = true;
    this.localChangeInfo = {
      changeType: changeType,
      summary: data?.summary || 'Code changes detected',
      commits_since_start: data?.commits_since_start || 0,
      uncommitted_files: data?.uncommitted_files || 0,
      modified_files: data?.modified_files || [],
      current_branch: data?.current_branch || null,
      restart_recommended: data?.restart_recommended || false,
      restart_reason: data?.restart_reason || null,
      detected_at: data?.detected_at || new Date().toISOString(),
    };

    this._notifySubscribers('local_changes', {
      detected: true,
      ...this.localChangeInfo,
    });
  }

  /**
   * Connect to a specific capability endpoint
   */
  async connect(capability = 'general') {
    if (!this.isInitialized) {
      await this._initializeWhenReady();
    }

    try {
      await this.client.connect(capability);
      return true;
    } catch (error) {
      console.error('[UnifiedWebSocket] Connect failed:', error.message);
      return false;
    }
  }

  /**
   * Subscribe to a message type
   */
  subscribe(messageType, handler) {
    this.client.on(messageType, handler);

    if (!this.subscriptions.has(messageType)) {
      this.subscriptions.set(messageType, new Set());
    }
    this.subscriptions.get(messageType).add(handler);

    return () => this.unsubscribe(messageType, handler);
  }

  /**
   * Unsubscribe from a message type
   */
  unsubscribe(messageType, handler) {
    this.client.off(messageType, handler);

    const handlers = this.subscriptions.get(messageType);
    if (handlers) {
      handlers.delete(handler);
      if (handlers.size === 0) {
        this.subscriptions.delete(messageType);
      }
    }
  }

  /**
   * Send a message
   */
  async send(message, capability = null) {
    return this.client.send(message, capability);
  }

  /**
   * Send a reliable message (waits for ACK)
   */
  async sendReliable(message, capability = null, timeout = 5000) {
    return this.client.sendReliable(message, capability, timeout);
  }

  /**
   * Request workspace analysis
   */
  async requestWorkspaceAnalysis() {
    return this.send({
      type: 'request_workspace_analysis',
      timestamp: new Date().toISOString()
    }, 'vision');
  }

  /**
   * Set monitoring interval
   */
  async setMonitoringInterval(interval) {
    return this.send({
      type: 'set_monitoring_interval',
      interval
    }, 'vision');
  }

  /**
   * Execute an action
   */
  async executeAction(action) {
    return this.send({
      type: 'execute_action',
      action
    }, 'vision');
  }

  /**
   * Send vision command
   */
  async sendVisionCommand(command) {
    return this.send({
      type: 'vision_command',
      command
    }, 'vision');
  }

  /**
   * Send Claude vision query
   */
  async sendClaudeVision(query) {
    return this.send({
      type: 'claude_vision',
      query
    }, 'vision');
  }

  /**
   * Enable/disable autonomous mode
   */
  async setAutonomousMode(enabled) {
    return this.send({
      type: 'set_autonomous_mode',
      enabled
    }, 'vision');
  }

  /**
   * Get connection statistics
   */
  getStats() {
    return this.client.getStats();
  }

  /**
   * Check if connected
   */
  isConnected() {
    return this.connectionState === 'connected';
  }

  /**
   * Disconnect from all endpoints
   */
  disconnect() {
    this.client.destroy();
    this.connectionState = 'disconnected';
  }

  /**
   * Check if system is in maintenance mode (updating/restarting/rollback)
   */
  isInMaintenanceMode() {
    return this.maintenanceMode;
  }

  /**
   * Get maintenance mode details
   */
  getMaintenanceStatus() {
    return {
      active: this.maintenanceMode,
      reason: this.maintenanceReason,
    };
  }

  /**
   * Check if an update is available
   */
  isUpdateAvailable() {
    return this.updateAvailable;
  }

  /**
   * Get update information
   */
  getUpdateInfo() {
    return this.updateInfo;
  }

  /**
   * Dismiss the update notification (user clicked "later")
   */
  dismissUpdate() {
    this.updateAvailable = false;
    this.updateInfo = null;
    this._notifySubscribers('update_available', {
      available: false,
    });
  }

  /**
   * Check if local changes have been detected (v2.0)
   */
  hasLocalChanges() {
    return this.localChangesDetected;
  }

  /**
   * Get local change information (v2.0)
   */
  getLocalChangeInfo() {
    return this.localChangeInfo;
  }

  /**
   * Dismiss local changes notification (v2.0)
   */
  dismissLocalChanges() {
    this.localChangesDetected = false;
    this.localChangeInfo = null;
    this._notifySubscribers('local_changes', {
      detected: false,
    });
  }

  /**
   * Notify all subscribers of an event
   */
  _notifySubscribers(eventType, data) {
    const handlers = this.subscriptions.get(eventType);
    if (handlers) {
      handlers.forEach(handler => {
        try {
          handler(data);
        } catch (error) {
          console.error('[UnifiedWebSocket] Subscriber error:', error);
        }
      });
    }
  }
}

// Singleton instance
let serviceInstance = null;

export function getUnifiedWebSocketService() {
  if (!serviceInstance) {
    serviceInstance = new UnifiedWebSocketService();
  }
  return serviceInstance;
}

// React hook for using the service
export function useUnifiedWebSocket() {
  const [connected, setConnected] = React.useState(false);
  const [stats, setStats] = React.useState(null);
  const [maintenanceMode, setMaintenanceMode] = React.useState(false);
  const [maintenanceReason, setMaintenanceReason] = React.useState(null);
  const [maintenanceMessage, setMaintenanceMessage] = React.useState(null);
  // Update available state
  const [updateAvailable, setUpdateAvailable] = React.useState(false);
  const [updateInfo, setUpdateInfo] = React.useState(null);
  // Local change awareness state (v2.0)
  const [localChangesDetected, setLocalChangesDetected] = React.useState(false);
  const [localChangeInfo, setLocalChangeInfo] = React.useState(null);
  const service = React.useMemo(() => getUnifiedWebSocketService(), []);

  React.useEffect(() => {
    // Subscribe to connection changes
    const unsubscribeConnection = service.subscribe('connection', (data) => {
      setConnected(data.state === 'connected');
    });

    // Subscribe to maintenance mode changes
    const unsubscribeMaintenance = service.subscribe('maintenance_mode', (data) => {
      setMaintenanceMode(data.active);
      setMaintenanceReason(data.reason);
      setMaintenanceMessage(data.message);
    });

    // Subscribe to update available notifications
    const unsubscribeUpdate = service.subscribe('update_available', (data) => {
      setUpdateAvailable(data.available);
      if (data.available) {
        setUpdateInfo({
          commits_behind: data.commits_behind,
          summary: data.summary,
          priority: data.priority,
          highlights: data.highlights,
          security_update: data.security_update,
          breaking_changes: data.breaking_changes,
          timestamp: data.timestamp,
        });
      } else {
        setUpdateInfo(null);
      }
    });

    // Subscribe to local change notifications (v2.0)
    const unsubscribeLocalChanges = service.subscribe('local_changes', (data) => {
      setLocalChangesDetected(data.detected);
      if (data.detected) {
        setLocalChangeInfo({
          changeType: data.changeType,
          summary: data.summary,
          commits_since_start: data.commits_since_start,
          uncommitted_files: data.uncommitted_files,
          modified_files: data.modified_files,
          restart_recommended: data.restart_recommended,
          restart_reason: data.restart_reason,
          detected_at: data.detected_at,
        });
      } else {
        setLocalChangeInfo(null);
      }
    });

    // Initial connection state
    setConnected(service.isConnected());
    setMaintenanceMode(service.isInMaintenanceMode());
    setUpdateAvailable(service.isUpdateAvailable());
    setUpdateInfo(service.getUpdateInfo());
    setLocalChangesDetected(service.hasLocalChanges?.() || false);
    setLocalChangeInfo(service.getLocalChangeInfo?.() || null);

    // Update stats periodically
    const interval = setInterval(() => {
      setStats(service.getStats());
    }, 5000);

    return () => {
      unsubscribeConnection();
      unsubscribeMaintenance();
      unsubscribeUpdate();
      unsubscribeLocalChanges();
      clearInterval(interval);
    };
  }, [service]);

  return {
    service,
    connected,
    stats,
    maintenanceMode,
    maintenanceReason,
    maintenanceMessage,
    // Update available state
    updateAvailable,
    updateInfo,
    dismissUpdate: () => service.dismissUpdate(),
    // Local change awareness (v2.0)
    localChangesDetected,
    localChangeInfo,
    dismissLocalChanges: () => service.dismissLocalChanges?.(),
    // Actions
    connect: (capability) => service.connect(capability),
    disconnect: () => service.disconnect(),
    send: (message, capability) => service.send(message, capability),
    sendReliable: (message, capability, timeout) => service.sendReliable(message, capability, timeout),
    subscribe: (messageType, handler) => service.subscribe(messageType, handler)
  };
}

export default UnifiedWebSocketService;
