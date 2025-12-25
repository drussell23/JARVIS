/**
 * TrainingStatus Component - Real-Time Training Progress Display
 * =============================================================
 *
 * Connects to the Reactor-Core feedback WebSocket to display
 * live training progress in the JARVIS UI.
 *
 * Features:
 * - Real-time progress updates via WebSocket
 * - Animated progress bar with stage indicators
 * - Minimized/expanded view toggle
 * - Auto-hide when no training active
 * - Cinematic JARVIS styling
 *
 * Architecture:
 *   Reactor-Core → WebSocket → This Component → UI Display
 *
 * @author JARVIS AI System
 * @version 1.0.0 (Feedback Loop)
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import configService from '../services/DynamicConfigService';
import './TrainingStatus.css';

// Training stage configurations with icons and colors
const STAGE_CONFIG = {
  idle: { icon: '💤', label: 'Idle', color: '#888888' },
  data_prep: { icon: '📦', label: 'Data Prep', color: '#00BFFF' },
  ingesting: { icon: '📥', label: 'Ingesting', color: '#00CED1' },
  formatting: { icon: '📝', label: 'Formatting', color: '#20B2AA' },
  distilling: { icon: '🧪', label: 'Distilling', color: '#00FA9A' },
  fine_tuning: { icon: '🔧', label: 'Fine-Tuning', color: '#FFD700' },
  training: { icon: '🧠', label: 'Training', color: '#FF8C00' },
  evaluating: { icon: '📊', label: 'Evaluating', color: '#FF69B4' },
  evaluation: { icon: '📊', label: 'Evaluating', color: '#FF69B4' },
  exporting: { icon: '📤', label: 'Exporting', color: '#DA70D6' },
  quantizing: { icon: '⚡', label: 'Quantizing', color: '#BA55D3' },
  deploying: { icon: '🚀', label: 'Deploying', color: '#9370DB' },
  completed: { icon: '✅', label: 'Complete', color: '#00FF41' },
  failed: { icon: '❌', label: 'Failed', color: '#FF4444' },
  cancelled: { icon: '🚫', label: 'Cancelled', color: '#888888' },
};

// WebSocket reconnection settings
const WS_RECONNECT_DELAY = 3000;
const WS_MAX_RECONNECT_ATTEMPTS = 5;

const TrainingStatus = () => {
  // State
  const [status, setStatus] = useState(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isMinimized, setIsMinimized] = useState(false);
  const [showPanel, setShowPanel] = useState(false);
  const [recentUpdates, setRecentUpdates] = useState([]);

  // Refs
  const wsRef = useRef(null);
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef(null);

  // Get stage configuration
  const getStageConfig = useCallback((stage) => {
    return STAGE_CONFIG[stage] || STAGE_CONFIG.idle;
  }, []);

  // Connect to WebSocket
  const connectWebSocket = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return; // Already connected
    }

    try {
      // Get WebSocket URL from config service
      const wsBaseUrl = configService.getWebSocketUrl() ||
        `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.hostname}:8010`;

      const wsUrl = `${wsBaseUrl}/reactor-core/training/ws`;
      console.log('[TrainingStatus] Connecting to WebSocket:', wsUrl);

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        console.log('[TrainingStatus] WebSocket connected');
        setIsConnected(true);
        reconnectAttempts.current = 0;
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          console.log('[TrainingStatus] Received:', data.type);

          if (data.type === 'training_status' || data.type === 'current_state') {
            const trainingData = data.data || data;

            // Update status
            setStatus(trainingData);

            // Show panel when training is active
            if (trainingData.status === 'running' || trainingData.progress > 0) {
              setShowPanel(true);
            }

            // Add to recent updates (keep last 10)
            setRecentUpdates(prev => {
              const newUpdate = {
                id: Date.now(),
                stage: trainingData.stage,
                progress: trainingData.progress,
                message: trainingData.message,
                timestamp: new Date().toLocaleTimeString(),
              };
              return [newUpdate, ...prev.slice(0, 9)];
            });

            // Auto-hide after completion
            if (trainingData.status === 'completed') {
              setTimeout(() => {
                setShowPanel(false);
              }, 10000); // Hide after 10 seconds
            }
          } else if (data.type === 'connected') {
            console.log('[TrainingStatus] Server acknowledged connection');
          }
        } catch (e) {
          console.error('[TrainingStatus] Failed to parse message:', e);
        }
      };

      ws.onerror = (error) => {
        console.error('[TrainingStatus] WebSocket error:', error);
      };

      ws.onclose = () => {
        console.log('[TrainingStatus] WebSocket closed');
        setIsConnected(false);
        wsRef.current = null;

        // Attempt reconnection
        if (reconnectAttempts.current < WS_MAX_RECONNECT_ATTEMPTS) {
          reconnectAttempts.current++;
          console.log(`[TrainingStatus] Reconnecting (${reconnectAttempts.current}/${WS_MAX_RECONNECT_ATTEMPTS})...`);

          reconnectTimer.current = setTimeout(() => {
            connectWebSocket();
          }, WS_RECONNECT_DELAY);
        }
      };
    } catch (error) {
      console.error('[TrainingStatus] Failed to create WebSocket:', error);
    }
  }, []);

  // Initialize WebSocket connection
  useEffect(() => {
    // Small delay to ensure config is ready
    const initTimer = setTimeout(() => {
      connectWebSocket();
    }, 1000);

    return () => {
      clearTimeout(initTimer);
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connectWebSocket]);

  // Send ping to keep connection alive
  useEffect(() => {
    const pingInterval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'ping' }));
      }
    }, 30000);

    return () => clearInterval(pingInterval);
  }, []);

  // Don't render if no training active and panel is hidden
  if (!showPanel && !status?.status) {
    return null;
  }

  // Get current stage config
  const stageConfig = getStageConfig(status?.stage || 'idle');
  const progress = status?.progress || 0;
  const isActive = status?.status === 'running';
  const isComplete = status?.status === 'completed';
  const isFailed = status?.status === 'failed';

  return (
    <div className={`training-status-container ${isMinimized ? 'minimized' : ''} ${isActive ? 'active' : ''}`}>
      {/* Minimized View */}
      {isMinimized ? (
        <div className="training-minimized" onClick={() => setIsMinimized(false)}>
          <span className="training-mini-icon">{stageConfig.icon}</span>
          <span className="training-mini-progress">{progress.toFixed(0)}%</span>
          <div
            className="training-mini-bar"
            style={{
              width: `${progress}%`,
              backgroundColor: stageConfig.color
            }}
          />
        </div>
      ) : (
        /* Expanded View */
        <div className="training-panel">
          {/* Header */}
          <div className="training-header">
            <div className="training-title">
              <span className="training-icon">🧠</span>
              <span>Neural Training</span>
              {status?.job_id && (
                <span className="training-job-id">#{status.job_id}</span>
              )}
            </div>
            <div className="training-controls">
              <button
                className="training-minimize-btn"
                onClick={() => setIsMinimized(true)}
                title="Minimize"
              >
                ─
              </button>
              <button
                className="training-close-btn"
                onClick={() => setShowPanel(false)}
                title="Close"
              >
                ×
              </button>
            </div>
          </div>

          {/* Progress Section */}
          <div className="training-progress-section">
            {/* Stage Indicator */}
            <div className="training-stage">
              <span
                className="stage-icon"
                style={{ color: stageConfig.color }}
              >
                {stageConfig.icon}
              </span>
              <span className="stage-label">{stageConfig.label}</span>
            </div>

            {/* Progress Bar */}
            <div className="training-progress-container">
              <div
                className={`training-progress-bar ${isActive ? 'active' : ''}`}
                style={{
                  width: `${progress}%`,
                  backgroundColor: stageConfig.color,
                  boxShadow: `0 0 10px ${stageConfig.color}, 0 0 20px ${stageConfig.color}40`
                }}
              >
                {isActive && <div className="progress-pulse" />}
              </div>
              <span className="training-progress-text">
                {progress.toFixed(1)}%
              </span>
            </div>

            {/* Status Message */}
            <div className="training-message">
              {status?.message || 'Waiting for training data...'}
            </div>
          </div>

          {/* Metrics (if available) */}
          {status?.metrics && Object.keys(status.metrics).length > 0 && (
            <div className="training-metrics">
              {status.metrics.loss !== undefined && (
                <div className="metric">
                  <span className="metric-label">Loss</span>
                  <span className="metric-value">{status.metrics.loss.toFixed(4)}</span>
                </div>
              )}
              {status.metrics.eval_accuracy !== undefined && (
                <div className="metric">
                  <span className="metric-label">Accuracy</span>
                  <span className="metric-value">{(status.metrics.eval_accuracy * 100).toFixed(1)}%</span>
                </div>
              )}
              {status.metrics.examples_trained !== undefined && (
                <div className="metric">
                  <span className="metric-label">Examples</span>
                  <span className="metric-value">{status.metrics.examples_trained}</span>
                </div>
              )}
            </div>
          )}

          {/* Recent Updates Log */}
          {recentUpdates.length > 0 && (
            <div className="training-log">
              <div className="log-header">Recent Updates</div>
              <div className="log-entries">
                {recentUpdates.slice(0, 5).map((update) => (
                  <div key={update.id} className="log-entry">
                    <span className="log-time">{update.timestamp}</span>
                    <span className="log-stage">{getStageConfig(update.stage).icon}</span>
                    <span className="log-message">{update.message || update.stage}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Status Footer */}
          <div className="training-footer">
            <div className={`connection-status ${isConnected ? 'connected' : 'disconnected'}`}>
              <span className="connection-dot" />
              {isConnected ? 'Live' : 'Reconnecting...'}
            </div>
            {isComplete && (
              <div className="completion-badge">
                <span>Training Complete</span>
              </div>
            )}
            {isFailed && (
              <div className="failure-badge">
                <span>Training Failed</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default TrainingStatus;
