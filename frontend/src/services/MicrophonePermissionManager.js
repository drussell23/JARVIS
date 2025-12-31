/**
 * Unified Microphone Permission Manager
 *
 * A singleton service that manages microphone permission state across all components.
 * Prevents race conditions and infinite retry loops by providing:
 * - Centralized permission state tracking
 * - Pre-check capability before any getUserMedia call
 * - Event-driven permission change notifications
 * - Async locking to prevent concurrent permission requests
 * - Browser-specific guidance for enabling permissions
 */

class MicrophonePermissionManager {
  constructor() {
    // Singleton check
    if (MicrophonePermissionManager.instance) {
      return MicrophonePermissionManager.instance;
    }
    MicrophonePermissionManager.instance = this;

    // =========================================================================
    // Permission State
    // =========================================================================
    this.state = {
      permission: 'unknown',  // 'unknown' | 'prompt' | 'granted' | 'denied'
      lastChecked: null,
      deniedAt: null,
      deniedCount: 0,
      isHardDenied: false,    // True if user clicked "Block" or browser setting
      lastError: null,
      deviceAvailable: null,  // null = unchecked, true = has mic, false = no mic
    };

    // =========================================================================
    // Lock State - Prevents concurrent permission requests
    // =========================================================================
    this.lock = {
      isLocked: false,
      lockOwner: null,
      waitQueue: [],
    };

    // =========================================================================
    // Event Subscribers
    // =========================================================================
    this.subscribers = new Set();

    // =========================================================================
    // Browser Info
    // =========================================================================
    this.browser = this._detectBrowser();

    // =========================================================================
    // Initialize
    // =========================================================================
    this._initializePermissionMonitoring();
  }

  // ===========================================================================
  // Public API
  // ===========================================================================

  /**
   * Check if microphone can be used (non-blocking check).
   * Returns immediately with current known state.
   */
  canUseMicrophone() {
    // If hard denied, always return false
    if (this.state.isHardDenied) {
      return false;
    }

    // If permission is denied, return false
    if (this.state.permission === 'denied') {
      return false;
    }

    // If denied recently (within 30 seconds), return false
    if (this.state.deniedAt && (Date.now() - this.state.deniedAt) < 30000) {
      return false;
    }

    // If denied multiple times, return false (requires manual intervention)
    if (this.state.deniedCount >= 2) {
      return false;
    }

    // Otherwise, potentially usable
    return true;
  }

  /**
   * Get current permission state (synchronous).
   */
  getState() {
    return { ...this.state };
  }

  /**
   * Check permission state with fresh query (async).
   * Updates internal state and returns result.
   */
  async checkPermission() {
    try {
      // Use Permissions API if available
      if (navigator.permissions?.query) {
        const result = await navigator.permissions.query({ name: 'microphone' });
        this._updateState({ permission: result.state, lastChecked: Date.now() });
        return result.state;
      }

      // Fallback: check if we have any audio devices
      if (navigator.mediaDevices?.enumerateDevices) {
        const devices = await navigator.mediaDevices.enumerateDevices();
        const hasAudioInput = devices.some(d => d.kind === 'audioinput');
        this._updateState({
          deviceAvailable: hasAudioInput,
          lastChecked: Date.now()
        });

        if (!hasAudioInput) {
          return 'unavailable';
        }
      }

      // Can't determine - assume prompt
      return 'prompt';
    } catch (error) {
      console.warn('[MicPermissionManager] Permission check failed:', error);
      return 'unknown';
    }
  }

  /**
   * Request microphone permission with proper locking.
   * Prevents concurrent requests that cause race conditions.
   *
   * @param {string} requesterId - Identifier for who is requesting (for debugging)
   * @param {object} options - getUserMedia options
   * @returns {Promise<{success: boolean, stream?: MediaStream, error?: string}>}
   */
  async requestPermission(requesterId = 'unknown', options = {}) {
    // Quick pre-check - don't even try if hard denied
    if (!this.canUseMicrophone()) {
      console.log(`[MicPermissionManager] Request from ${requesterId} blocked - permission not available`);
      return {
        success: false,
        error: 'permission_denied',
        reason: this._getDenialReason(),
        instructions: this.getPermissionInstructions(),
      };
    }

    // Acquire lock to prevent concurrent requests
    const lockAcquired = await this._acquireLock(requesterId);
    if (!lockAcquired) {
      console.log(`[MicPermissionManager] Request from ${requesterId} - waiting for lock`);
    }

    try {
      // Fresh permission check before requesting
      const currentState = await this.checkPermission();

      if (currentState === 'denied') {
        this._handleDenial('permission_api_denied');
        return {
          success: false,
          error: 'permission_denied',
          reason: 'Browser permission is set to denied',
          instructions: this.getPermissionInstructions(),
        };
      }

      if (currentState === 'unavailable') {
        return {
          success: false,
          error: 'no_device',
          reason: 'No microphone device found',
        };
      }

      // Attempt getUserMedia
      const audioConstraints = {
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          ...options.audio,
        }
      };

      console.log(`[MicPermissionManager] Requesting getUserMedia for ${requesterId}`);
      const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);

      // Success!
      this._handleSuccess();
      console.log(`[MicPermissionManager] Permission granted for ${requesterId}`);

      return {
        success: true,
        stream,
      };

    } catch (error) {
      // Handle specific error types
      if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
        this._handleDenial('user_denied');
        return {
          success: false,
          error: 'permission_denied',
          reason: error.message,
          instructions: this.getPermissionInstructions(),
        };
      }

      if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
        this._updateState({ deviceAvailable: false });
        return {
          success: false,
          error: 'no_device',
          reason: 'No microphone found',
        };
      }

      if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
        return {
          success: false,
          error: 'device_busy',
          reason: 'Microphone is in use by another application',
        };
      }

      // Unknown error
      return {
        success: false,
        error: 'unknown',
        reason: error.message,
      };

    } finally {
      this._releaseLock(requesterId);
    }
  }

  /**
   * Mark permission as denied (called when external error occurs).
   * This allows other components to inform the manager of denial.
   */
  markAsDenied(reason = 'external') {
    this._handleDenial(reason);
  }

  /**
   * Reset denial state (for user-initiated retry).
   */
  resetDenialState() {
    this._updateState({
      deniedAt: null,
      deniedCount: 0,
      isHardDenied: false,
      lastError: null,
    });
    console.log('[MicPermissionManager] Denial state reset');
    this._notifySubscribers('reset');
  }

  /**
   * Get browser-specific instructions for enabling microphone.
   */
  getPermissionInstructions() {
    const instructions = {
      chrome: [
        'Click the lock/tune icon (🔒) in the address bar',
        'Click "Site settings"',
        'Set Microphone to "Allow"',
        'Reload the page',
      ],
      firefox: [
        'Click the lock icon (🔒) in the address bar',
        'Click "Connection secure" → "More information"',
        'Go to "Permissions" tab',
        'Find Microphone and click "Allow"',
      ],
      safari: [
        'Go to Safari → Preferences → Websites',
        'Select "Microphone" from the sidebar',
        'Set this website to "Allow"',
      ],
      edge: [
        'Click the lock icon (🔒) in the address bar',
        'Click "Permissions for this site"',
        'Set Microphone to "Allow"',
      ],
      default: [
        'Open browser settings',
        'Navigate to Privacy & Security → Site Settings',
        'Find Microphone permissions',
        'Allow access for this website',
        'Reload the page',
      ],
    };

    return {
      browser: this.browser.name,
      steps: instructions[this.browser.name] || instructions.default,
    };
  }

  /**
   * Subscribe to permission state changes.
   */
  subscribe(callback) {
    this.subscribers.add(callback);
    return () => this.subscribers.delete(callback);
  }

  // ===========================================================================
  // Private Methods
  // ===========================================================================

  async _initializePermissionMonitoring() {
    try {
      // Initial check
      await this.checkPermission();

      // Set up Permissions API listener if available
      if (navigator.permissions?.query) {
        const permissionStatus = await navigator.permissions.query({ name: 'microphone' });

        permissionStatus.addEventListener('change', () => {
          const newState = permissionStatus.state;
          console.log(`[MicPermissionManager] Permission changed: ${this.state.permission} → ${newState}`);

          if (newState === 'granted') {
            this._handleSuccess();
          } else if (newState === 'denied') {
            this._handleDenial('browser_setting');
          }

          this._updateState({ permission: newState });
          this._notifySubscribers('change');
        });
      }

      // Listen for device changes
      if (navigator.mediaDevices?.addEventListener) {
        navigator.mediaDevices.addEventListener('devicechange', async () => {
          const devices = await navigator.mediaDevices.enumerateDevices();
          const hasAudioInput = devices.some(d => d.kind === 'audioinput');

          if (hasAudioInput !== this.state.deviceAvailable) {
            console.log(`[MicPermissionManager] Device change: ${hasAudioInput ? 'microphone connected' : 'microphone disconnected'}`);
            this._updateState({ deviceAvailable: hasAudioInput });
            this._notifySubscribers('device_change');
          }
        });
      }

    } catch (error) {
      console.warn('[MicPermissionManager] Failed to initialize monitoring:', error);
    }
  }

  _handleDenial(reason) {
    const now = Date.now();
    this._updateState({
      permission: 'denied',
      deniedAt: now,
      deniedCount: this.state.deniedCount + 1,
      lastError: reason,
      isHardDenied: this.state.deniedCount >= 1 || reason === 'browser_setting',
    });

    console.warn(`[MicPermissionManager] Permission denied (${reason}), count: ${this.state.deniedCount}`);
    this._notifySubscribers('denied');
  }

  _handleSuccess() {
    this._updateState({
      permission: 'granted',
      deniedAt: null,
      deniedCount: 0,
      isHardDenied: false,
      lastError: null,
      deviceAvailable: true,
    });
    this._notifySubscribers('granted');
  }

  _updateState(updates) {
    this.state = { ...this.state, ...updates };
  }

  _getDenialReason() {
    if (this.state.isHardDenied) {
      return 'Microphone permission is blocked in browser settings';
    }
    if (this.state.deniedCount >= 2) {
      return 'Permission denied multiple times - please enable in browser settings';
    }
    if (this.state.deniedAt && (Date.now() - this.state.deniedAt) < 30000) {
      return 'Permission was recently denied - please wait or enable in browser settings';
    }
    return 'Microphone permission not granted';
  }

  _notifySubscribers(event) {
    for (const callback of this.subscribers) {
      try {
        callback(event, this.state);
      } catch (error) {
        console.error('[MicPermissionManager] Subscriber error:', error);
      }
    }
  }

  async _acquireLock(requesterId, timeout = 5000) {
    if (!this.lock.isLocked) {
      this.lock.isLocked = true;
      this.lock.lockOwner = requesterId;
      return true;
    }

    // Already locked - wait in queue
    return new Promise((resolve) => {
      const timeoutId = setTimeout(() => {
        // Remove from queue and fail
        const idx = this.lock.waitQueue.findIndex(w => w.requesterId === requesterId);
        if (idx >= 0) {
          this.lock.waitQueue.splice(idx, 1);
        }
        resolve(false);
      }, timeout);

      this.lock.waitQueue.push({
        requesterId,
        resolve: () => {
          clearTimeout(timeoutId);
          this.lock.isLocked = true;
          this.lock.lockOwner = requesterId;
          resolve(true);
        },
      });
    });
  }

  _releaseLock(requesterId) {
    if (this.lock.lockOwner !== requesterId) {
      return;
    }

    if (this.lock.waitQueue.length > 0) {
      const next = this.lock.waitQueue.shift();
      next.resolve();
    } else {
      this.lock.isLocked = false;
      this.lock.lockOwner = null;
    }
  }

  _detectBrowser() {
    const ua = navigator.userAgent;
    let name = 'default';
    let version = '';

    if (ua.includes('Chrome') && !ua.includes('Edg')) {
      name = 'chrome';
      version = ua.match(/Chrome\/(\d+)/)?.[1] || '';
    } else if (ua.includes('Safari') && !ua.includes('Chrome')) {
      name = 'safari';
      version = ua.match(/Version\/(\d+)/)?.[1] || '';
    } else if (ua.includes('Firefox')) {
      name = 'firefox';
      version = ua.match(/Firefox\/(\d+)/)?.[1] || '';
    } else if (ua.includes('Edg')) {
      name = 'edge';
      version = ua.match(/Edg\/(\d+)/)?.[1] || '';
    }

    return { name, version, ua };
  }
}

// Create and export singleton instance
const microphonePermissionManager = new MicrophonePermissionManager();

// Also expose globally for debugging
if (typeof window !== 'undefined') {
  window.microphonePermissionManager = microphonePermissionManager;
}

export default microphonePermissionManager;
export { MicrophonePermissionManager };
