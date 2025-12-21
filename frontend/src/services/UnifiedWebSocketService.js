/**
 * Unified WebSocket Service v3.0 - Zero-Touch Edition
 * =====================================================
 * Uses DynamicWebSocketClient for all WebSocket operations
 * Provides a simplified API for components that need WebSocket connectivity
 * 
 * v3.0 Features:
 * - Zero-Touch autonomous update status tracking
 * - Dead Man's Switch (DMS) monitoring state
 * - Update classification awareness (security/critical/minor/major)
 * - Prime Directives violation notifications
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
    this.maintenanceReason = null; // 'updating' | 'restarting' | 'rollback' | 'zero_touch' | 'dms_rollback' | null

    // Update available state for notification badge
    this.updateAvailable = false;
    this.updateInfo = null; // { commits_behind, summary, priority, highlights, security_update, breaking_changes, classification, zeroTouchEligible }

    // Local change awareness state (v2.0)
    this.localChangesDetected = false;
    this.localChangeInfo = null; // { changeType, summary, commits_since_start, restart_recommended, restart_reason }
    
    // v3.0: Zero-Touch Autonomous Update State
    this.zeroTouchActive = false;
    this.zeroTouchStatus = null; // { state, classification, message, validationProgress, filesValidated, totalFiles, commits, filesChanged, validationReport }
    
    // v3.0: Dead Man's Switch State
    this.dmsActive = false;
    this.dmsStatus = null; // { healthScore, probationRemaining, probationTotal, consecutiveFailures, state }
    
    // v3.0: Prime Directives State
    this.primeDirectiveViolation = null; // { type, action, file, limit, timestamp }
    
    // v5.0: Hot Reload (Dev Mode) State
    this.hotReloadActive = false;
    this.hotReloadStatus = null; // { state, fileCount, fileTypes, target, message, progress }
    this.devModeEnabled = false;

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

    // ═══════════════════════════════════════════════════════════════════
    // v3.0: ZERO-TOUCH AUTONOMOUS UPDATE EVENTS
    // ═══════════════════════════════════════════════════════════════════

    // Zero-Touch update initiated
    this.client.on('zero_touch_initiated', (data) => {
      console.log('🤖 Zero-Touch autonomous update initiated:', data);
      this.zeroTouchActive = true;
      this.maintenanceMode = true;
      this.maintenanceReason = 'zero_touch';
      this.zeroTouchStatus = {
        state: 'initiated',
        classification: data?.classification || null,
        message: data?.message || 'Autonomous update starting...',
        commits: data?.commits || 0,
        filesChanged: data?.files_changed || 0,
      };
      this._notifySubscribers('zero_touch', { active: true, ...this.zeroTouchStatus });
      this._notifySubscribers('maintenance_mode', {
        active: true,
        reason: 'zero_touch',
        message: this.zeroTouchStatus.message,
      });
    });

    // Zero-Touch staging phase
    this.client.on('zero_touch_staging', (data) => {
      console.log('📦 Zero-Touch staging:', data);
      this.zeroTouchStatus = {
        ...this.zeroTouchStatus,
        state: 'staging',
        message: data?.message || 'Staging update for validation...',
      };
      this._notifySubscribers('zero_touch', { active: true, ...this.zeroTouchStatus });
    });

    // Zero-Touch validation phase
    this.client.on('zero_touch_validating', (data) => {
      console.log('🔍 Zero-Touch validating:', data);
      this.zeroTouchStatus = {
        ...this.zeroTouchStatus,
        state: 'validating',
        message: data?.message || 'Validating code...',
        validationProgress: data?.progress || 0,
        filesValidated: data?.files_validated || 0,
        totalFiles: data?.total_files || 0,
      };
      this._notifySubscribers('zero_touch', { active: true, ...this.zeroTouchStatus });
    });

    // Zero-Touch validation complete
    this.client.on('zero_touch_validation_complete', (data) => {
      console.log('✓ Zero-Touch validation complete:', data);
      this.zeroTouchStatus = {
        ...this.zeroTouchStatus,
        state: data?.passed ? 'applying' : 'validation_failed',
        validationProgress: 100,
        validationReport: data?.report || null,
        message: data?.passed ? 'Validation passed, applying update...' : 'Validation failed',
      };
      this._notifySubscribers('zero_touch', { active: true, ...this.zeroTouchStatus });
    });

    // Zero-Touch applying
    this.client.on('zero_touch_applying', (data) => {
      console.log('⚡ Zero-Touch applying:', data);
      this.zeroTouchStatus = {
        ...this.zeroTouchStatus,
        state: 'applying',
        message: data?.message || 'Applying validated update...',
      };
      this._notifySubscribers('zero_touch', { active: true, ...this.zeroTouchStatus });
    });

    // Zero-Touch complete (success)
    this.client.on('zero_touch_complete', (data) => {
      console.log('✅ Zero-Touch complete:', data);
      this.zeroTouchStatus = {
        ...this.zeroTouchStatus,
        state: 'dms_monitoring',
        message: data?.message || 'Update applied. Monitoring stability...',
        newVersion: data?.new_version || null,
      };
      this._notifySubscribers('zero_touch', { active: true, ...this.zeroTouchStatus });
    });

    // Zero-Touch failed
    this.client.on('zero_touch_failed', (data) => {
      console.log('❌ Zero-Touch failed:', data);
      this.zeroTouchActive = false;
      this.maintenanceMode = false;
      this.maintenanceReason = null;
      this.zeroTouchStatus = {
        ...this.zeroTouchStatus,
        state: 'failed',
        message: data?.message || 'Autonomous update failed',
        error: data?.error || null,
      };
      this._notifySubscribers('zero_touch', { active: false, ...this.zeroTouchStatus });
      this._notifySubscribers('maintenance_mode', { active: false, reason: null });
    });

    // Zero-Touch blocked (pre-flight check failed)
    this.client.on('zero_touch_blocked', (data) => {
      console.log('🚫 Zero-Touch blocked:', data);
      this._notifySubscribers('zero_touch_blocked', {
        reason: data?.reason || 'Pre-flight check failed',
        willRetryAt: data?.will_retry_at || null,
      });
    });

    // ═══════════════════════════════════════════════════════════════════
    // v3.0: DEAD MAN'S SWITCH (DMS) EVENTS
    // ═══════════════════════════════════════════════════════════════════

    // DMS probation started
    this.client.on('dms_probation_start', (data) => {
      console.log('🎯 DMS probation started:', data);
      this.dmsActive = true;
      this.dmsStatus = {
        state: 'monitoring',
        healthScore: 1.0,
        probationRemaining: data?.probation_seconds || 30,
        probationTotal: data?.probation_seconds || 30,
        consecutiveFailures: 0,
      };
      this._notifySubscribers('dms_status', { active: true, ...this.dmsStatus });
    });

    // DMS heartbeat update
    this.client.on('dms_heartbeat', (data) => {
      console.log('💓 DMS heartbeat:', data);
      this.dmsStatus = {
        ...this.dmsStatus,
        healthScore: data?.health_score ?? this.dmsStatus?.healthScore ?? 1.0,
        probationRemaining: data?.remaining_seconds ?? this.dmsStatus?.probationRemaining ?? 0,
        consecutiveFailures: data?.consecutive_failures ?? 0,
      };
      this._notifySubscribers('dms_status', { active: true, ...this.dmsStatus });
    });

    // DMS probation passed (version committed as stable)
    this.client.on('dms_probation_passed', (data) => {
      console.log('✅ DMS probation passed:', data);
      this.dmsActive = false;
      this.zeroTouchActive = false;
      this.maintenanceMode = false;
      this.maintenanceReason = null;
      this.dmsStatus = { ...this.dmsStatus, state: 'passed' };
      this.zeroTouchStatus = { ...this.zeroTouchStatus, state: 'complete' };
      this._notifySubscribers('dms_status', { active: false, ...this.dmsStatus });
      this._notifySubscribers('zero_touch', { active: false, ...this.zeroTouchStatus });
      this._notifySubscribers('maintenance_mode', { 
        active: false, 
        reason: null,
        message: data?.message || 'Update verified stable' 
      });
    });

    // DMS rollback triggered
    this.client.on('dms_rollback_triggered', (data) => {
      console.log('🔄 DMS rollback triggered:', data);
      this.maintenanceReason = 'dms_rollback';
      this.dmsStatus = { ...this.dmsStatus, state: 'rolling_back' };
      this._notifySubscribers('dms_status', { active: true, ...this.dmsStatus });
      this._notifySubscribers('maintenance_mode', {
        active: true,
        reason: 'dms_rollback',
        message: data?.message || 'Stability check failed. Rolling back...',
      });
    });

    // DMS rollback complete
    this.client.on('dms_rollback_complete', (data) => {
      console.log('✅ DMS rollback complete:', data);
      this.dmsActive = false;
      this.zeroTouchActive = false;
      this.maintenanceMode = false;
      this.maintenanceReason = null;
      this.dmsStatus = null;
      this.zeroTouchStatus = null;
      this._notifySubscribers('dms_status', { active: false });
      this._notifySubscribers('zero_touch', { active: false });
      this._notifySubscribers('maintenance_mode', { 
        active: false, 
        reason: null,
        message: data?.message || 'Rolled back to previous stable version' 
      });
    });

    // ═══════════════════════════════════════════════════════════════════
    // v3.0: PRIME DIRECTIVES EVENTS
    // ═══════════════════════════════════════════════════════════════════

    // Prime directive violation
    this.client.on('prime_directive_violation', (data) => {
      console.log('⚠️ Prime directive violation:', data);
      this.primeDirectiveViolation = {
        type: data?.type || 'unknown',
        action: data?.action || null,
        file: data?.file || null,
        limit: data?.limit || null,
        timestamp: new Date().toISOString(),
      };
      this._notifySubscribers('prime_directive_violation', this.primeDirectiveViolation);
    });

    // Handle generic local changes
    this.client.on('local_changes_detected', (data) => {
      console.log('🔄 Local changes detected:', data);
      this._handleLocalChange(data, 'changes');
    });

    // ═══════════════════════════════════════════════════════════════════
    // v5.0: HOT RELOAD (DEV MODE) EVENTS
    // ═══════════════════════════════════════════════════════════════════

    // Dev mode status
    this.client.on('dev_mode_status', (data) => {
      console.log('🔥 Dev mode status:', data);
      this.devModeEnabled = data?.enabled || false;
      this._notifySubscribers('dev_mode', { enabled: this.devModeEnabled });
    });

    // Hot reload - file changes detected
    this.client.on('hot_reload_detected', (data) => {
      console.log('🔥 Hot reload: changes detected:', data);
      this.hotReloadActive = true;
      this.hotReloadStatus = {
        state: 'detected',
        fileCount: data?.file_count || 0,
        fileTypes: data?.file_types || [],
        target: data?.target || 'backend',
        message: data?.message || 'Code changes detected',
        changedFiles: data?.changed_files || [],
        timestamp: new Date().toISOString(),
      };
      this._notifySubscribers('hot_reload', { active: true, ...this.hotReloadStatus });
    });

    // Hot reload - restarting
    this.client.on('hot_reload_restarting', (data) => {
      console.log('🔥 Hot reload: restarting:', data);
      this.hotReloadActive = true;
      this.maintenanceMode = true;
      this.maintenanceReason = 'hot_reload';
      this.hotReloadStatus = {
        ...this.hotReloadStatus,
        state: 'restarting',
        message: data?.message || 'Applying your changes...',
        target: data?.target || 'backend',
      };
      this._notifySubscribers('hot_reload', { active: true, ...this.hotReloadStatus });
      this._notifySubscribers('maintenance_mode', {
        active: true,
        reason: 'hot_reload',
        message: this.hotReloadStatus.message,
        estimatedTime: 10,
      });
    });

    // Hot reload - rebuilding (frontend)
    this.client.on('hot_reload_rebuilding', (data) => {
      console.log('🔥 Hot reload: rebuilding frontend:', data);
      this.hotReloadStatus = {
        ...this.hotReloadStatus,
        state: 'rebuilding',
        message: data?.message || 'Rebuilding frontend...',
        target: 'frontend',
        progress: 0,
      };
      this._notifySubscribers('hot_reload', { active: true, ...this.hotReloadStatus });
    });

    // Hot reload - progress update
    this.client.on('hot_reload_progress', (data) => {
      this.hotReloadStatus = {
        ...this.hotReloadStatus,
        progress: data?.progress || 0,
        message: data?.message || this.hotReloadStatus?.message,
      };
      this._notifySubscribers('hot_reload', { active: true, ...this.hotReloadStatus });
    });

    // Hot reload - complete
    this.client.on('hot_reload_complete', (data) => {
      console.log('✅ Hot reload complete:', data);
      this.hotReloadActive = false;
      this.maintenanceMode = false;
      this.maintenanceReason = null;
      this.hotReloadStatus = {
        ...this.hotReloadStatus,
        state: 'complete',
        message: data?.message || 'Changes applied successfully',
        duration: data?.duration || null,
      };
      this._notifySubscribers('hot_reload', { active: false, ...this.hotReloadStatus });
      this._notifySubscribers('maintenance_mode', {
        active: false,
        reason: null,
        message: 'Hot reload complete - JARVIS is back online',
      });
      
      // Clear status after a brief delay
      setTimeout(() => {
        this.hotReloadStatus = null;
        this._notifySubscribers('hot_reload', { active: false, status: null });
      }, 5000);
    });

    // Hot reload - failed
    this.client.on('hot_reload_failed', (data) => {
      console.log('❌ Hot reload failed:', data);
      this.hotReloadActive = false;
      this.hotReloadStatus = {
        ...this.hotReloadStatus,
        state: 'failed',
        message: data?.message || 'Hot reload failed',
        error: data?.error || null,
      };
      this._notifySubscribers('hot_reload', { active: false, ...this.hotReloadStatus });
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
  // v3.0: Zero-Touch autonomous update state
  const [zeroTouchActive, setZeroTouchActive] = React.useState(false);
  const [zeroTouchStatus, setZeroTouchStatus] = React.useState(null);
  // v3.0: Dead Man's Switch state
  const [dmsActive, setDmsActive] = React.useState(false);
  const [dmsStatus, setDmsStatus] = React.useState(null);
  // v3.0: Prime Directives state
  const [primeDirectiveViolation, setPrimeDirectiveViolation] = React.useState(null);
  // v5.0: Hot Reload (Dev Mode) state
  const [hotReloadActive, setHotReloadActive] = React.useState(false);
  const [hotReloadStatus, setHotReloadStatus] = React.useState(null);
  const [devModeEnabled, setDevModeEnabled] = React.useState(false);
  
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
          classification: data.classification,
          zeroTouchEligible: data.zero_touch_eligible,
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
    
    // v3.0: Subscribe to Zero-Touch updates
    const unsubscribeZeroTouch = service.subscribe('zero_touch', (data) => {
      setZeroTouchActive(data.active);
      if (data.active) {
        setZeroTouchStatus({
          state: data.state,
          classification: data.classification,
          message: data.message,
          validationProgress: data.validationProgress,
          filesValidated: data.filesValidated,
          totalFiles: data.totalFiles,
          commits: data.commits,
          filesChanged: data.filesChanged,
          validationReport: data.validationReport,
          newVersion: data.newVersion,
        });
      } else {
        setZeroTouchStatus(null);
      }
    });
    
    // v3.0: Subscribe to DMS status
    const unsubscribeDms = service.subscribe('dms_status', (data) => {
      setDmsActive(data.active);
      if (data.active) {
        setDmsStatus({
          state: data.state,
          healthScore: data.healthScore,
          probationRemaining: data.probationRemaining,
          probationTotal: data.probationTotal,
          consecutiveFailures: data.consecutiveFailures,
        });
      } else {
        setDmsStatus(null);
      }
    });
    
    // v3.0: Subscribe to Prime Directive violations
    const unsubscribePrimeDirective = service.subscribe('prime_directive_violation', (data) => {
      setPrimeDirectiveViolation(data);
      // Auto-clear after 10 seconds
      setTimeout(() => setPrimeDirectiveViolation(null), 10000);
    });
    
    // v5.0: Subscribe to Hot Reload events
    const unsubscribeHotReload = service.subscribe('hot_reload', (data) => {
      setHotReloadActive(data.active);
      if (data.active || data.status) {
        setHotReloadStatus({
          state: data.state,
          fileCount: data.fileCount,
          fileTypes: data.fileTypes,
          target: data.target,
          message: data.message,
          progress: data.progress,
          changedFiles: data.changedFiles,
          duration: data.duration,
          error: data.error,
        });
      } else if (!data.active && !data.status) {
        setHotReloadStatus(null);
      }
    });
    
    // v5.0: Subscribe to Dev Mode status
    const unsubscribeDevMode = service.subscribe('dev_mode', (data) => {
      setDevModeEnabled(data.enabled);
    });

    // Initial connection state
    setConnected(service.isConnected());
    setMaintenanceMode(service.isInMaintenanceMode());
    setUpdateAvailable(service.isUpdateAvailable());
    setUpdateInfo(service.getUpdateInfo());
    setLocalChangesDetected(service.hasLocalChanges?.() || false);
    setLocalChangeInfo(service.getLocalChangeInfo?.() || null);
    setZeroTouchActive(service.zeroTouchActive || false);
    setZeroTouchStatus(service.zeroTouchStatus || null);
    setDmsActive(service.dmsActive || false);
    setHotReloadActive(service.hotReloadActive || false);
    setHotReloadStatus(service.hotReloadStatus || null);
    setDevModeEnabled(service.devModeEnabled || false);
    setDmsStatus(service.dmsStatus || null);

    // Update stats periodically
    const interval = setInterval(() => {
      setStats(service.getStats());
    }, 5000);

    return () => {
      unsubscribeConnection();
      unsubscribeMaintenance();
      unsubscribeUpdate();
      unsubscribeLocalChanges();
      unsubscribeZeroTouch();
      unsubscribeDms();
      unsubscribePrimeDirective();
      unsubscribeHotReload();
      unsubscribeDevMode();
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
    // v3.0: Zero-Touch autonomous update
    zeroTouchActive,
    zeroTouchStatus,
    // v3.0: Dead Man's Switch
    dmsActive,
    dmsStatus,
    // v3.0: Prime Directives
    primeDirectiveViolation,
    // v5.0: Hot Reload (Dev Mode)
    hotReloadActive,
    hotReloadStatus,
    devModeEnabled,
    // Actions
    connect: (capability) => service.connect(capability),
    disconnect: () => service.disconnect(),
    send: (message, capability) => service.send(message, capability),
    sendReliable: (message, capability, timeout) => service.sendReliable(message, capability, timeout),
    subscribe: (messageType, handler) => service.subscribe(messageType, handler)
  };
}

export default UnifiedWebSocketService;
