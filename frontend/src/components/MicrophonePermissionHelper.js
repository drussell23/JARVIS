import React, { useState, useEffect, useCallback, useRef } from 'react';
import './MicrophonePermissionHelper.css';
import microphonePermissionManager from '../services/MicrophonePermissionManager';
import configService from '../services/DynamicConfigService';

/**
 * MicrophonePermissionHelper v4.0
 *
 * Enhanced React component for managing microphone permissions with:
 * - Real-time device monitoring
 * - Platform-specific troubleshooting
 * - Intelligent error recovery
 * - Live device enumeration display
 * - macOS System Settings guidance
 * - Minimizable to a one-line pill, remembered per problem state
 */

// One label per problem state: the panel's heading and its minimized pill.
const STATUS_SUMMARY = {
  prompt: { icon: '🎤', title: 'Microphone Permission Required' },
  denied: { icon: '❌', title: 'Microphone Access Denied' },
  'no-device': { icon: '🎤', title: 'No Microphone Found' },
  'device-busy': { icon: '🔒', title: 'Microphone In Use' },
  'security-error': { icon: '🔐', title: 'Security Error' },
  unsupported: { icon: '❌', title: 'Browser Not Supported' },
  error: { icon: '⚠️', title: 'Error Accessing Microphone' },
};

// The problem state the user minimized the panel for. Kept per state, not as
// a flag: a different problem carries new information and opens expanded.
// Browser storage can be unavailable (private window, blocked site data);
// the panel then simply starts expanded.
const MINIMIZED_KEY = 'jarvis.microphoneHelper.minimizedFor';

const readMinimizedFor = () => {
  try {
    return window.localStorage.getItem(MINIMIZED_KEY);
  } catch {
    return null;
  }
};

const writeMinimizedFor = (status) => {
  try {
    if (status) {
      window.localStorage.setItem(MINIMIZED_KEY, status);
    } else {
      window.localStorage.removeItem(MINIMIZED_KEY);
    }
  } catch {
    // Not remembered across reloads; this session still honours it.
  }
};

const MicrophonePermissionHelper = ({ onPermissionGranted }) => {
  // =========================================================================
  // State
  // =========================================================================
  const [permissionStatus, setPermissionStatus] = useState('checking');
  const [deviceStatus, setDeviceStatus] = useState(null);
  const [troubleshootingSteps, setTroubleshootingSteps] = useState([]);
  const [showInstructions, setShowInstructions] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [lastError, setLastError] = useState(null);
  // The desktop the browser runs on, as the backend sees it (/host/session):
  // over Remote Desktop the microphone is the CLIENT's, so "no microphone"
  // means the client is not redirecting one.
  const [hostSession, setHostSession] = useState(null);
  const [minimizedFor, setMinimizedFor] = useState(readMinimizedFor);

  const minimize = useCallback(() => {
    setMinimizedFor(permissionStatus);
    writeMinimizedFor(permissionStatus);
  }, [permissionStatus]);

  const expand = useCallback(() => {
    setMinimizedFor(null);
    writeMinimizedFor(null);
  }, []);

  // Track if we've already notified parent of permission grant
  const hasNotifiedParent = useRef(false);

  // =========================================================================
  // Initialize and Subscribe to Permission Manager
  // =========================================================================
  useEffect(() => {
    let unsubscribe = null;

    const initialize = async () => {
      console.log('[MicrophonePermissionHelper] v4.0 Initializing...');

      // Subscribe to permission manager events
      unsubscribe = microphonePermissionManager.subscribe((event, state) => {
        console.log(`[MicrophonePermissionHelper] Event: ${event}`, state);

        if (event === 'granted') {
          setPermissionStatus('granted');
          // Solved: a later problem is news again, shown in full.
          writeMinimizedFor(null);
          notifyParent();
        } else if (event === 'denied') {
          setPermissionStatus('denied');
        } else if (event === 'device_availability_change') {
          // A microphone arrived or left: re-judge, not just re-list
          checkMicrophonePermission();
        } else if (event === 'device_change') {
          // Refresh device status on change
          refreshDeviceStatus();
        } else if (event === 'reset') {
          // Manager state was reset, check again
          checkMicrophonePermission();
        }
      });

      const apiUrl = configService.getApiUrl();
      if (apiUrl) {
        fetch(`${apiUrl}/host/session`)
          .then((r) => (r.ok ? r.json() : null))
          .then((session) => setHostSession(session))
          .catch(() => {});
      }

      // Initial permission check
      await checkMicrophonePermission();
    };

    initialize();

    return () => {
      if (unsubscribe) {
        unsubscribe();
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // =========================================================================
  // Notify Parent When Permission Granted
  // =========================================================================
  const notifyParent = useCallback(() => {
    if (!hasNotifiedParent.current && onPermissionGranted) {
      hasNotifiedParent.current = true;
      console.log('[MicrophonePermissionHelper] Notifying parent of permission grant');
      onPermissionGranted();
    }
  }, [onPermissionGranted]);

  useEffect(() => {
    if (permissionStatus === 'granted') {
      notifyParent();
    }
  }, [permissionStatus, notifyParent]);

  // =========================================================================
  // Refresh Device Status
  // =========================================================================
  const refreshDeviceStatus = useCallback(async () => {
    try {
      const status = await microphonePermissionManager.getDeviceStatus();
      setDeviceStatus(status);
      setTroubleshootingSteps(status.troubleshooting || []);
      return status;
    } catch (error) {
      console.error('[MicrophonePermissionHelper] Error getting device status:', error);
      return null;
    }
  }, []);

  // =========================================================================
  // Check Microphone Permission
  // =========================================================================
  const checkMicrophonePermission = useCallback(async () => {
    try {
      console.log('[MicrophonePermissionHelper] Checking microphone permission...');

      // Check if mediaDevices API is available
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setPermissionStatus('unsupported');
        return;
      }

      // Get detailed device status
      const status = await refreshDeviceStatus();

      // Check permission status via Permissions API
      if (navigator.permissions && navigator.permissions.query) {
        try {
          const permission = await navigator.permissions.query({ name: 'microphone' });

          // Permission to use a microphone is not a microphone: granted
          // with no input device is 'no-device', or the page would start
          // recognition, fail capture, and come straight back here.
          const usable = (state, deviceState) =>
            (state === 'granted' && deviceState?.devices && deviceState.devices.count === 0)
              ? 'no-device' : state;

          console.log(`[MicrophonePermissionHelper] Permission API state: ${permission.state}`);
          setPermissionStatus(usable(permission.state, status));

          // Listen for permission changes
          permission.addEventListener('change', async () => {
            console.log(`[MicrophonePermissionHelper] Permission changed: ${permission.state}`);
            const latest = await refreshDeviceStatus();
            setPermissionStatus(usable(permission.state, latest));
          });

        } catch (e) {
          // Safari doesn't support 'microphone' permission query
          console.log('[MicrophonePermissionHelper] Permissions API not available, falling back to prompt');
          setPermissionStatus('prompt');
        }
      } else {
        // Directly show prompt state if Permissions API not available
        setPermissionStatus('prompt');
      }

    } catch (error) {
      console.error('[MicrophonePermissionHelper] Error checking permission:', error);
      setPermissionStatus('error');
      setLastError(error.message);
    }
  }, [refreshDeviceStatus, notifyParent]);

  // =========================================================================
  // Request Microphone Access
  // =========================================================================
  const requestMicrophoneAccess = useCallback(async () => {
    try {
      console.log('[MicrophonePermissionHelper] User requesting microphone access...');
      setIsRetrying(true);
      setLastError(null);

      // Reset manager's denial state when user explicitly requests permission
      microphonePermissionManager.resetDenialState();

      // Request through the unified permission manager
      const result = await microphonePermissionManager.requestPermission('MicrophonePermissionHelper', {
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        }
      });

      setIsRetrying(false);

      if (result.success) {
        // Clean up the stream (manager returns it)
        if (result.stream) {
          result.stream.getTracks().forEach(track => track.stop());
        }
        console.log('[MicrophonePermissionHelper] Permission granted!');
        setPermissionStatus('granted');
        notifyParent();
        return;
      }

      // Handle specific errors
      console.log('[MicrophonePermissionHelper] Permission request result:', result);
      setLastError(result.reason || result.error);

      switch (result.error) {
        case 'permission_denied':
          setPermissionStatus('denied');
          break;
        case 'no_device':
          setPermissionStatus('no-device');
          break;
        case 'device_busy':
          setPermissionStatus('device-busy');
          break;
        case 'security':
          setPermissionStatus('security-error');
          break;
        case 'unsupported':
          setPermissionStatus('unsupported');
          break;
        default:
          setPermissionStatus('error');
      }

      // Refresh device status after error
      await refreshDeviceStatus();

    } catch (error) {
      console.error('[MicrophonePermissionHelper] Permission request error:', error);
      setIsRetrying(false);
      setLastError(error.message);

      if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
        setPermissionStatus('denied');
        microphonePermissionManager.markAsDenied('user_denied_via_helper');
      } else if (error.name === 'NotFoundError') {
        setPermissionStatus('no-device');
      } else if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
        setPermissionStatus('device-busy');
      } else if (error.name === 'SecurityError') {
        setPermissionStatus('security-error');
      } else {
        setPermissionStatus('error');
      }

      await refreshDeviceStatus();
    }
  }, [notifyParent, refreshDeviceStatus]);

  // =========================================================================
  // Render Device List
  // =========================================================================
  const renderDeviceList = () => {
    if (!deviceStatus?.devices?.devices) {
      return <span className="device-checking">Checking devices...</span>;
    }

    const devices = deviceStatus.devices.devices;

    if (devices.length === 0) {
      return (
        <span className="no-devices">
          No microphones detected.
          {deviceStatus.permission !== 'granted' && (
            <span className="device-hint"> (Grant permission to see device names)</span>
          )}
        </span>
      );
    }

    return (
      <ul className="device-list">
        {devices.map((device, i) => (
          <li key={device.deviceId || i}>
            {device.label}
          </li>
        ))}
      </ul>
    );
  };

  // =========================================================================
  // Render Troubleshooting Steps
  // =========================================================================
  const renderTroubleshootingSteps = () => {
    if (troubleshootingSteps.length === 0) return null;

    return (
      <div className="troubleshooting-section">
        <h4>Troubleshooting Steps:</h4>
        <ol className="troubleshooting-steps">
          {troubleshootingSteps.map((step, i) => (
            <li key={i} className={`priority-${step.priority}`}>
              <strong>{step.issue}:</strong>
              <span className="step-action">{step.action}</span>
              {step.detail && <p className="step-detail">{step.detail}</p>}
            </li>
          ))}
        </ol>
      </div>
    );
  };

  // =========================================================================
  // Render Platform-Specific Help
  // =========================================================================
  const renderPlatformHelp = () => {
    const platform = deviceStatus?.platform?.os || 'unknown';
    const browser = deviceStatus?.browser?.name || 'default';

    if (platform === 'macos') {
      return (
        <div className="platform-help macos">
          <h4>macOS Permission Required</h4>
          <p>Your browser needs microphone access at the system level:</p>
          <ol>
            <li>Open <strong>System Settings</strong> (or System Preferences)</li>
            <li>Go to <strong>Privacy & Security</strong> → <strong>Microphone</strong></li>
            <li>Find <strong>{getBrowserDisplayName(browser)}</strong> in the list</li>
            <li>Toggle the switch to <strong>ON</strong></li>
            <li>You may need to restart your browser</li>
          </ol>
          <button
            onClick={() => window.open('x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone', '_blank')}
            className="open-settings-btn"
          >
            Open System Settings
          </button>
        </div>
      );
    }

    if (platform === 'windows') {
      return (
        <div className="platform-help windows">
          <h4>Windows Permission Required</h4>
          <p>Your browser needs microphone access:</p>
          <ol>
            <li>Open <strong>Settings</strong></li>
            <li>Go to <strong>Privacy</strong> → <strong>Microphone</strong></li>
            <li>Ensure "Allow apps to access your microphone" is <strong>ON</strong></li>
            <li>Find <strong>{getBrowserDisplayName(browser)}</strong> and enable access</li>
          </ol>
        </div>
      );
    }

    return null;
  };

  // =========================================================================
  // Helper Functions
  // =========================================================================
  const getBrowserDisplayName = (browser) => {
    const names = {
      chrome: 'Google Chrome',
      safari: 'Safari',
      firefox: 'Firefox',
      edge: 'Microsoft Edge',
    };
    return names[browser] || 'Your browser';
  };

  // =========================================================================
  // Render - Return null if granted
  // =========================================================================
  if (permissionStatus === 'granted') {
    return null;
  }

  const summary = STATUS_SUMMARY[permissionStatus];

  // =========================================================================
  // Minimized Render - one line; click to see the full help again
  // =========================================================================
  if (summary && minimizedFor === permissionStatus) {
    return (
      <div className="microphone-permission-helper minimized">
        <button
          type="button"
          className="mic-helper-pill"
          onClick={expand}
          title="Show microphone help"
        >
          <span className="mic-helper-pill-icon">{summary.icon}</span>
          <span className="mic-helper-pill-title">{summary.title}</span>
          <span className="mic-helper-pill-action">Details</span>
        </button>
      </div>
    );
  }

  // =========================================================================
  // Main Render
  // =========================================================================
  return (
    <div className="microphone-permission-helper">
      {summary && (
        <button
          type="button"
          className="mic-helper-minimize"
          onClick={minimize}
          title="Minimize"
          aria-label="Minimize microphone help"
        >
          –
        </button>
      )}
      <div className="permission-status">

        {/* Checking State */}
        {permissionStatus === 'checking' && (
          <div className="status-checking">
            <div className="spinner"></div>
            <p>Checking microphone access...</p>
          </div>
        )}

        {/* Prompt State - Ready to Request */}
        {permissionStatus === 'prompt' && (
          <div className="status-prompt">
            <div className="icon">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>JARVIS needs microphone access for voice commands.</p>
            <button
              onClick={requestMicrophoneAccess}
              className="permission-button primary"
              disabled={isRetrying}
            >
              {isRetrying ? 'Requesting...' : 'Grant Microphone Access'}
            </button>

            <div className="device-info">
              <strong>Available Devices:</strong>
              {renderDeviceList()}
            </div>
          </div>
        )}

        {/* Denied State */}
        {permissionStatus === 'denied' && (
          <div className="status-denied">
            <div className="icon error">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>Permission was blocked. Please enable microphone access to use voice commands.</p>

            {renderPlatformHelp()}

            <button
              onClick={() => setShowInstructions(!showInstructions)}
              className="help-button"
            >
              {showInstructions ? 'Hide Instructions' : 'Show Browser Instructions'}
            </button>

            {showInstructions && (
              <div className="browser-instructions">
                <h4>Enable Microphone in Your Browser:</h4>
                <ol>
                  {microphonePermissionManager.getPermissionInstructions().steps.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              </div>
            )}

            <button
              onClick={requestMicrophoneAccess}
              className="retry-button"
              disabled={isRetrying}
            >
              {isRetrying ? 'Trying...' : 'Try Again'}
            </button>
          </div>
        )}

        {/* No Device State */}
        {permissionStatus === 'no-device' && (
          <div className="status-no-device">
            <div className="icon warning">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>No microphone device was detected on your system.</p>

            <div className="device-info">
              <strong>Detected Audio Devices:</strong>
              {renderDeviceList()}
            </div>

            {hostSession?.remote_session ? (
              <div className="suggestions">
                <h4>This {hostSession.os} desktop is in a Remote Desktop session</h4>
                <p>The microphone is the one on the computer you are connecting from, and
                  your Remote Desktop client is not sending it to this session.</p>
                <ul>
                  <li>In your Remote Desktop client, edit this PC's connection and turn on
                    <strong> microphone redirection</strong> (Devices &amp; Audio → Microphone).</li>
                  <li>Allow the Remote Desktop client to use the microphone in the client
                    computer's privacy settings.</li>
                  <li>Reconnect to the session; JARVIS picks the microphone up automatically.</li>
                </ul>
              </div>
            ) : (
              <div className="suggestions">
                <h4>Things to Try:</h4>
                <ul>
                  <li>Connect a USB microphone or headset</li>
                  <li>Check your headphone/microphone jack connection</li>
                  <li>Ensure your built-in microphone is enabled in System Settings</li>
                  <li>Close other apps that might be using the microphone</li>
                </ul>
              </div>
            )}

            <button
              onClick={requestMicrophoneAccess}
              className="retry-button"
              disabled={isRetrying}
            >
              {isRetrying ? 'Checking...' : 'Check Again'}
            </button>

            <p className="auto-retry-note">
              We'll automatically detect when you connect a microphone.
            </p>
          </div>
        )}

        {/* Device Busy State */}
        {permissionStatus === 'device-busy' && (
          <div className="status-device-busy">
            <div className="icon warning">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>Your microphone is being used by another application.</p>

            <div className="suggestions">
              <h4>Things to Try:</h4>
              <ul>
                <li>Close video conferencing apps (Zoom, Teams, Meet)</li>
                <li>Close other browser tabs using the microphone</li>
                <li>Check for recording software running in the background</li>
                <li>Restart your browser</li>
              </ul>
            </div>

            <button
              onClick={requestMicrophoneAccess}
              className="retry-button"
              disabled={isRetrying}
            >
              {isRetrying ? 'Trying...' : 'Try Again'}
            </button>
          </div>
        )}

        {/* Security Error State */}
        {permissionStatus === 'security-error' && (
          <div className="status-security-error">
            <div className="icon error">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>Microphone access requires a secure connection (HTTPS).</p>

            {!deviceStatus?.isSecureContext && (
              <div className="security-warning">
                <strong>Current context is not secure.</strong>
                <p>
                  You are accessing this page via HTTP. Microphone access is only
                  available on HTTPS or localhost.
                </p>
              </div>
            )}

            <div className="suggestions">
              <h4>Solutions:</h4>
              <ul>
                <li>Access this page via <code>https://</code></li>
                <li>For development, use <code>localhost</code> instead of IP address</li>
                <li>Configure your development server with HTTPS</li>
              </ul>
            </div>
          </div>
        )}

        {/* Unsupported Browser State */}
        {permissionStatus === 'unsupported' && (
          <div className="status-unsupported">
            <div className="icon error">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>Your browser doesn't support microphone access.</p>
            <p>Please use a modern browser:</p>
            <ul className="browser-list">
              <li>Google Chrome (recommended)</li>
              <li>Mozilla Firefox</li>
              <li>Safari</li>
              <li>Microsoft Edge</li>
            </ul>
          </div>
        )}

        {/* Generic Error State */}
        {permissionStatus === 'error' && (
          <div className="status-error">
            <div className="icon error">{summary.icon}</div>
            <h3>{summary.title}</h3>
            <p>Something went wrong while trying to access the microphone.</p>

            {lastError && (
              <p className="error-detail">Error: {lastError}</p>
            )}

            {renderTroubleshootingSteps()}

            <button
              onClick={requestMicrophoneAccess}
              className="retry-button"
              disabled={isRetrying}
            >
              {isRetrying ? 'Trying...' : 'Try Again'}
            </button>
          </div>
        )}
      </div>

      {/* Always show troubleshooting tips for non-granted states */}
      {permissionStatus !== 'checking' && permissionStatus !== 'prompt' && (
        <div className="troubleshooting-tips">
          <details>
            <summary>Advanced Troubleshooting</summary>
            <ul>
              <li>Run diagnostics in console: <code>window.microphonePermissionManager.runDiagnostics()</code></li>
              <li>Check browser console for detailed error logs</li>
              <li>Try incognito/private window (fresh permissions)</li>
              <li>Clear site data and try again</li>
            </ul>
          </details>
        </div>
      )}
    </div>
  );
};

export default MicrophonePermissionHelper;
