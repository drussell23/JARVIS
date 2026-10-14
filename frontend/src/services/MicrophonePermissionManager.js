/**
 * Unified Microphone Permission Manager v4.0
 *
 * A singleton service that manages microphone permission state across all components.
 *
 * v4.0 ENHANCEMENTS:
 * - Proper distinction between permission denied vs no physical device
 * - Real-time device monitoring with event notifications
 * - macOS-aware permission guidance
 * - Intelligent retry with exponential backoff
 * - Browser permission vs OS permission detection
 * - Proactive device availability checking
 * - Self-healing permission recovery
 *
 * Features:
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

    // v4.0: Enhanced debugging
    this._debug = true;
    this._version = '4.0.0';
    this._log(`MicrophonePermissionManager v${this._version} initializing...`);

    // =========================================================================
    // Permission State (v4.0 Enhanced)
    // =========================================================================
    this.state = {
      permission: 'unknown',      // 'unknown' | 'prompt' | 'granted' | 'denied'
      lastChecked: null,
      deniedAt: null,
      deniedCount: 0,
      isHardDenied: false,        // True if user clicked "Block" or browser setting
      lastError: null,
      lastErrorName: null,        // v4.0: Track specific error name
      deviceAvailable: null,      // null = unchecked, true = has mic, false = no mic
      deviceCount: 0,             // v4.0: Number of audio input devices
      devices: [],                // v4.0: List of available devices
      osPermissionBlocked: false, // v4.0: macOS/Windows OS-level permission blocked
      browserPermissionBlocked: false, // v4.0: Browser site setting blocked
      isSecureContext: typeof window !== 'undefined' ? window.isSecureContext : false,
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
    // Retry Configuration (v4.0)
    // =========================================================================
    this.retryConfig = {
      maxRetries: 3,
      baseDelayMs: 1000,
      maxDelayMs: 10000,
      currentRetry: 0,
      lastRetryTime: null,
    };

    // =========================================================================
    // Device Monitoring (v4.0)
    // =========================================================================
    this.deviceMonitor = {
      isMonitoring: false,
      checkIntervalMs: 2000,
      intervalId: null,
    };

    // =========================================================================
    // Event Subscribers
    // =========================================================================
    this.subscribers = new Set();

    // =========================================================================
    // Browser Info
    // =========================================================================
    this.browser = this._detectBrowser();
    this.platform = this._detectPlatform();

    // =========================================================================
    // Initialize
    // =========================================================================
    this._initializePermissionMonitoring();
    this._startDeviceMonitoring();
  }

  // ===========================================================================
  // v3.0: Debug Logging
  // ===========================================================================
  _log(message, data = null) {
    if (this._debug) {
      const timestamp = new Date().toISOString().split('T')[1].split('.')[0];
      if (data) {
        console.log(`[MicPerm ${timestamp}] ${message}`, data);
      } else {
        console.log(`[MicPerm ${timestamp}] ${message}`);
      }
    }
  }

  _error(message, error = null) {
    const timestamp = new Date().toISOString().split('T')[1].split('.')[0];
    if (error) {
      console.error(`[MicPerm ${timestamp}] ❌ ${message}`, error);
    } else {
      console.error(`[MicPerm ${timestamp}] ❌ ${message}`);
    }
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
   *
   * v3.0: COMPREHENSIVE DEBUGGING + ROBUST DETECTION
   * - Do NOT use enumerateDevices() to determine device availability before permission
   * - Only use getUserMedia errors as authoritative "no device" indicator
   * - Detailed logging at every step
   */
  async checkPermission() {
    this._log('checkPermission() called');

    try {
      // Step 1: Check browser support
      this._log('Step 1: Checking browser support...');
      if (!navigator.mediaDevices) {
        this._error('navigator.mediaDevices not available');
        return 'unsupported';
      }
      if (!navigator.mediaDevices.getUserMedia) {
        this._error('getUserMedia not available');
        return 'unsupported';
      }
      this._log('Step 1: Browser support OK ✓');

      // Step 2: Try Permissions API
      this._log('Step 2: Checking Permissions API...');
      if (navigator.permissions?.query) {
        try {
          const result = await navigator.permissions.query({ name: 'microphone' });
          this._log(`Step 2: Permissions API returned: "${result.state}"`, {
            state: result.state,
            name: result.name
          });

          this._updateState({ permission: result.state, lastChecked: Date.now() });

          // v3.0: Only check device availability if permission is ALREADY granted
          if (result.state === 'granted') {
            this._log('Step 2a: Permission granted, checking devices...');
            if (navigator.mediaDevices?.enumerateDevices) {
              const devices = await navigator.mediaDevices.enumerateDevices();
              const audioInputs = devices.filter(d => d.kind === 'audioinput');
              this._log(`Step 2a: Found ${audioInputs.length} audio input(s)`, audioInputs.map(d => ({
                deviceId: d.deviceId?.substring(0, 8) + '...',
                label: d.label || '(no label)',
                kind: d.kind
              })));

              this._updateState({ deviceAvailable: audioInputs.length > 0 });

              if (audioInputs.length === 0) {
                this._error('Step 2a: Permission granted but NO audio devices found!');
                return 'unavailable';
              }
            }
          } else {
            this._log(`Step 2b: Permission is "${result.state}", NOT checking devices (unreliable before grant)`);
          }

          return result.state;

        } catch (permError) {
          // Safari doesn't support 'microphone' permission query
          this._log('Step 2: Permissions API query failed (expected on Safari):', permError.message);
        }
      } else {
        this._log('Step 2: Permissions API not available');
      }

      // Step 3: Fallback - assume 'prompt' and let getUserMedia be authoritative
      this._log('Step 3: Falling back to "prompt" state');
      return 'prompt';

    } catch (error) {
      this._error('checkPermission() failed with exception:', error);
      return 'unknown';
    }
  }

  /**
   * Request microphone permission with proper locking.
   * v3.0: Comprehensive debugging + robust error handling
   *
   * @param {string} requesterId - Identifier for who is requesting (for debugging)
   * @param {object} options - getUserMedia options
   * @returns {Promise<{success: boolean, stream?: MediaStream, error?: string}>}
   */
  async requestPermission(requesterId = 'unknown', options = {}) {
    this._log(`requestPermission() called by "${requesterId}"`);
    this._log('Current state:', this.state);

    // Quick pre-check - don't even try if hard denied
    const canUse = this.canUseMicrophone();
    this._log(`canUseMicrophone() = ${canUse}`);

    if (!canUse) {
      const reason = this._getDenialReason();
      this._error(`Request from "${requesterId}" blocked: ${reason}`);
      return {
        success: false,
        error: 'permission_denied',
        reason: reason,
        instructions: this.getPermissionInstructions(),
      };
    }

    // Acquire lock to prevent concurrent requests
    this._log('Acquiring lock...');
    const lockAcquired = await this._acquireLock(requesterId);
    this._log(`Lock acquired: ${lockAcquired}`);

    try {
      // Fresh permission check before requesting
      this._log('Calling checkPermission()...');
      const currentState = await this.checkPermission();
      this._log(`checkPermission() returned: "${currentState}"`);

      if (currentState === 'denied') {
        this._error('Permission is DENIED by browser');
        this._handleDenial('permission_api_denied');
        return {
          success: false,
          error: 'permission_denied',
          reason: 'Browser permission is set to denied',
          instructions: this.getPermissionInstructions(),
        };
      }

      if (currentState === 'unavailable') {
        this._error('No devices available (permission granted but no mic found)');
        return {
          success: false,
          error: 'no_device',
          reason: 'No microphone device found (permission granted, device missing)',
        };
      }

      if (currentState === 'unsupported') {
        this._error('Browser does not support getUserMedia');
        return {
          success: false,
          error: 'unsupported',
          reason: 'Browser does not support microphone access',
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

      this._log(`Calling getUserMedia with constraints:`, audioConstraints);
      this._log('>>> This should trigger browser permission dialog if needed <<<');

      const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);

      // Success!
      this._log('✅ getUserMedia SUCCESS!', {
        streamId: stream.id,
        tracks: stream.getTracks().map(t => ({ kind: t.kind, label: t.label, id: t.id }))
      });

      this._handleSuccess();

      return {
        success: true,
        stream,
      };

    } catch (error) {
      // Handle specific error types with detailed logging
      this._error(`getUserMedia FAILED:`, {
        name: error.name,
        message: error.message,
        stack: error.stack?.split('\n').slice(0, 3).join('\n')
      });

      if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
        this._error('User DENIED permission or browser blocked access');
        this._handleDenial('user_denied');
        return {
          success: false,
          error: 'permission_denied',
          reason: error.message,
          instructions: this.getPermissionInstructions(),
        };
      }

      if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
        this._error('NO MICROPHONE DEVICE FOUND (NotFoundError)');
        this._updateState({ deviceAvailable: false });
        return {
          success: false,
          error: 'no_device',
          reason: 'No microphone found - getUserMedia threw NotFoundError',
        };
      }

      if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
        this._error('Microphone is BUSY or not readable');
        return {
          success: false,
          error: 'device_busy',
          reason: 'Microphone is in use by another application or not readable',
        };
      }

      if (error.name === 'OverconstrainedError') {
        this._error('Audio constraints cannot be satisfied');
        return {
          success: false,
          error: 'overconstrained',
          reason: 'Audio constraints cannot be satisfied by available device',
        };
      }

      if (error.name === 'AbortError') {
        this._error('getUserMedia was aborted');
        return {
          success: false,
          error: 'aborted',
          reason: 'Microphone access request was aborted',
        };
      }

      if (error.name === 'SecurityError') {
        this._error('Security error - possibly not HTTPS or localhost');
        return {
          success: false,
          error: 'security',
          reason: 'Security error - microphone requires HTTPS or localhost',
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
   * A thrown getUserMedia / MediaRecorder error in this manager's result
   * vocabulary ({success, error, reason}), classified once (_classifyError).
   * A denial is recorded; nothing else is.
   */
  resultForError(error) {
    const { category, suggestedAction } = this._classifyError(error || {});
    const reason = error?.message || suggestedAction;
    if (category === 'browser_permission_denied' || category === 'os_permission_denied') {
      this._handleDenial(category);
      return { success: false, error: 'permission_denied', reason, instructions: this.getPermissionInstructions() };
    }
    if (category === 'no_device' || category === 'device_busy' || category === 'security') {
      return { success: false, error: category, reason };
    }
    return { success: false, error: 'unknown', reason };
  }

  /**
   * What a Web Speech recognition error means for the microphone, in this
   * manager's result vocabulary ({success, error, reason}), or null when it
   * says nothing about the microphone (no-speech, network, aborted...).
   *
   * Only 'not-allowed' / 'service-not-allowed' are denials. 'audio-capture'
   * means capture FAILED: with no input device at all (a Remote Desktop
   * session whose client is not redirecting a microphone) it was recorded as
   * a denial, and every later request reported "permission denied".
   */
  async noteRecognitionError(errorCode) {
    if (errorCode === 'not-allowed' || errorCode === 'service-not-allowed') {
      this._handleDenial(errorCode);
      return {
        success: false,
        error: 'permission_denied',
        reason: 'Microphone permission denied',
        instructions: this.getPermissionInstructions(),
      };
    }
    if (errorCode === 'audio-capture') {
      const devices = await this._checkDevices();
      if (!devices || devices.count === 0) {
        return { success: false, error: 'no_device', reason: 'No microphone is available to this browser' };
      }
      return {
        success: false,
        error: 'device_busy',
        reason: 'The microphone could not be captured (in use by another application?)',
      };
    }
    return null;
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

  // ===========================================================================
  // v4.0: Platform Detection
  // ===========================================================================
  _detectPlatform() {
    const ua = navigator.userAgent;
    const platform = navigator.platform || '';

    let os = 'unknown';
    let isMobile = false;

    if (platform.includes('Mac') || ua.includes('Macintosh')) {
      os = 'macos';
    } else if (platform.includes('Win') || ua.includes('Windows')) {
      os = 'windows';
    } else if (platform.includes('Linux') || ua.includes('Linux')) {
      os = 'linux';
    } else if (/iPhone|iPad|iPod/.test(ua)) {
      os = 'ios';
      isMobile = true;
    } else if (/Android/.test(ua)) {
      os = 'android';
      isMobile = true;
    }

    return { os, isMobile, platform };
  }

  // ===========================================================================
  // v4.0: Device Monitoring
  // ===========================================================================
  _startDeviceMonitoring() {
    if (this.deviceMonitor.isMonitoring) return;

    this._log('Starting device monitoring...');
    this.deviceMonitor.isMonitoring = true;

    // Initial device check
    this._checkDevices();

    // Listen for device changes (USB connect/disconnect)
    if (navigator.mediaDevices?.addEventListener) {
      navigator.mediaDevices.addEventListener('devicechange', async () => {
        this._log('Device change detected');
        await this._checkDevices();
        this._notifySubscribers('device_change');
      });
    }

    // Periodic check for devices (catches cases where devicechange doesn't fire)
    this.deviceMonitor.intervalId = setInterval(() => {
      this._checkDevices();
    }, this.deviceMonitor.checkIntervalMs);
  }

  _stopDeviceMonitoring() {
    if (this.deviceMonitor.intervalId) {
      clearInterval(this.deviceMonitor.intervalId);
      this.deviceMonitor.intervalId = null;
    }
    this.deviceMonitor.isMonitoring = false;
  }

  async _checkDevices() {
    try {
      if (!navigator.mediaDevices?.enumerateDevices) {
        return;
      }

      const devices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devices.filter(d => d.kind === 'audioinput');

      // Check if we have real device info (labels) or just placeholders
      const hasRealLabels = audioInputs.some(d => d.label && d.label !== '');

      const deviceInfo = audioInputs.map(d => ({
        deviceId: d.deviceId?.substring(0, 8) || 'unknown',
        label: d.label || '(permission required to see name)',
        groupId: d.groupId?.substring(0, 8) || '',
      }));

      const previousCount = this.state.deviceCount;
      const previousAvailable = this.state.deviceAvailable;

      this._updateState({
        deviceCount: audioInputs.length,
        deviceAvailable: audioInputs.length > 0,
        devices: deviceInfo,
      });

      // Log changes
      if (previousCount !== audioInputs.length) {
        this._log(`Device count changed: ${previousCount} → ${audioInputs.length}`, deviceInfo);
      }

      // Notify if device availability changed
      if (previousAvailable !== null && previousAvailable !== (audioInputs.length > 0)) {
        this._log(`Device availability changed: ${previousAvailable} → ${audioInputs.length > 0}`);
        this._notifySubscribers('device_availability_change');

        // Auto-retry if devices became available
        if (audioInputs.length > 0 && !previousAvailable) {
          this._log('Devices became available, resetting denial state');
          this.resetDenialState();
        }
      }

      return {
        count: audioInputs.length,
        hasLabels: hasRealLabels,
        devices: deviceInfo,
      };

    } catch (error) {
      this._error('Device enumeration failed:', error);
      return { count: 0, hasLabels: false, devices: [] };
    }
  }

  // ===========================================================================
  // v4.0: Enhanced Error Classification
  // ===========================================================================
  _classifyError(error) {
    const errorName = error.name || 'UnknownError';
    const errorMessage = error.message || '';

    const classification = {
      errorName,
      errorMessage,
      category: 'unknown',
      isRecoverable: false,
      requiresUserAction: true,
      suggestedAction: null,
    };

    switch (errorName) {
      case 'NotAllowedError':
      case 'PermissionDeniedError':
        // Could be browser OR OS level denial
        if (errorMessage.includes('system') || errorMessage.includes('OS')) {
          classification.category = 'os_permission_denied';
          classification.suggestedAction = 'Enable microphone access in System Settings';
          this._updateState({ osPermissionBlocked: true });
        } else {
          classification.category = 'browser_permission_denied';
          classification.suggestedAction = 'Click the microphone icon in address bar to allow';
          this._updateState({ browserPermissionBlocked: true });
        }
        break;

      case 'NotFoundError':
      case 'DevicesNotFoundError':
        // No microphone hardware detected
        classification.category = 'no_device';
        classification.isRecoverable = true; // Can recover if device is plugged in
        classification.requiresUserAction = true;
        classification.suggestedAction = 'Connect a microphone and try again';
        break;

      case 'NotReadableError':
      case 'TrackStartError':
        // Device exists but can't be accessed
        classification.category = 'device_busy';
        classification.isRecoverable = true;
        classification.suggestedAction = 'Close other apps using the microphone';
        break;

      case 'OverconstrainedError':
        // Audio constraints can't be satisfied
        classification.category = 'overconstrained';
        classification.isRecoverable = true;
        classification.suggestedAction = 'Try with default audio settings';
        break;

      case 'AbortError':
        classification.category = 'aborted';
        classification.isRecoverable = true;
        classification.requiresUserAction = false;
        break;

      case 'SecurityError':
        classification.category = 'security';
        classification.suggestedAction = 'This page must be served over HTTPS';
        break;

      default:
        classification.category = 'unknown';
        classification.suggestedAction = 'Try reloading the page';
    }

    return classification;
  }

  // ===========================================================================
  // v4.0: Intelligent Retry
  // ===========================================================================
  async _retryWithBackoff(operation, context = 'unknown') {
    const { maxRetries, baseDelayMs, maxDelayMs } = this.retryConfig;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        return await operation();
      } catch (error) {
        const classification = this._classifyError(error);

        // Don't retry non-recoverable errors
        if (!classification.isRecoverable && attempt > 0) {
          throw error;
        }

        if (attempt < maxRetries) {
          const delay = Math.min(baseDelayMs * Math.pow(2, attempt), maxDelayMs);
          this._log(`Retry ${attempt + 1}/${maxRetries} for ${context} in ${delay}ms`);
          await new Promise(resolve => setTimeout(resolve, delay));

          // Re-check devices before retry
          await this._checkDevices();
        } else {
          throw error;
        }
      }
    }
  }

  // ===========================================================================
  // v4.0: Get Detailed Device Status (for UI)
  // ===========================================================================
  async getDeviceStatus() {
    const deviceInfo = await this._checkDevices();
    const permissionState = await this.checkPermission();

    return {
      permission: permissionState,
      devices: deviceInfo,
      platform: this.platform,
      browser: this.browser,
      isSecureContext: this.state.isSecureContext,
      osPermissionBlocked: this.state.osPermissionBlocked,
      browserPermissionBlocked: this.state.browserPermissionBlocked,
      canRequest: this.canUseMicrophone(),
      troubleshooting: this._getTroubleshootingSteps(),
    };
  }

  // ===========================================================================
  // v4.0: Platform-Specific Troubleshooting
  // ===========================================================================
  _getTroubleshootingSteps() {
    const steps = [];
    const { os } = this.platform;
    const { name: browser } = this.browser;

    // Security context check
    if (!this.state.isSecureContext) {
      steps.push({
        priority: 1,
        issue: 'Insecure context',
        action: 'Use HTTPS or localhost',
        detail: 'Microphone access requires a secure context (HTTPS or localhost)',
      });
    }

    // macOS-specific
    if (os === 'macos') {
      steps.push({
        priority: 2,
        issue: 'macOS System Permission',
        action: 'System Settings → Privacy & Security → Microphone',
        detail: `Ensure ${browser === 'chrome' ? 'Google Chrome' : browser === 'safari' ? 'Safari' : browser === 'firefox' ? 'Firefox' : 'your browser'} has a checkmark next to it`,
      });
    }

    // Windows-specific
    if (os === 'windows') {
      steps.push({
        priority: 2,
        issue: 'Windows Microphone Privacy',
        action: 'Settings → Privacy → Microphone',
        detail: 'Ensure "Allow apps to access your microphone" is ON',
      });
    }

    // Browser-specific
    steps.push({
      priority: 3,
      issue: 'Browser Site Permission',
      action: this.getPermissionInstructions().steps.join(' → '),
      detail: 'Allow microphone access for this specific website',
    });

    // Device check
    if (this.state.deviceCount === 0) {
      steps.push({
        priority: 1,
        issue: 'No microphone detected',
        action: 'Connect a microphone',
        detail: 'Ensure your microphone is properly connected and not in use by another app',
      });
    }

    return steps.sort((a, b) => a.priority - b.priority);
  }

  // ===========================================================================
  // v3.0: Comprehensive Diagnostic Method
  // Call this from browser console: window.microphonePermissionManager.runDiagnostics()
  // ===========================================================================
  async runDiagnostics() {
    console.log('═══════════════════════════════════════════════════════════════');
    console.log('🎤 MICROPHONE DIAGNOSTICS v3.0');
    console.log('═══════════════════════════════════════════════════════════════');

    const results = {
      timestamp: new Date().toISOString(),
      browser: this.browser,
      currentState: this.state,
      tests: {}
    };

    // Test 1: Browser API Support
    console.log('\n📋 Test 1: Browser API Support');
    results.tests.browserSupport = {
      mediaDevices: !!navigator.mediaDevices,
      getUserMedia: !!navigator.mediaDevices?.getUserMedia,
      enumerateDevices: !!navigator.mediaDevices?.enumerateDevices,
      permissionsApi: !!navigator.permissions?.query,
    };
    console.log('  mediaDevices:', results.tests.browserSupport.mediaDevices ? '✅' : '❌');
    console.log('  getUserMedia:', results.tests.browserSupport.getUserMedia ? '✅' : '❌');
    console.log('  enumerateDevices:', results.tests.browserSupport.enumerateDevices ? '✅' : '❌');
    console.log('  Permissions API:', results.tests.browserSupport.permissionsApi ? '✅' : '❌');

    // Test 2: Permissions API Query
    console.log('\n📋 Test 2: Permissions API Query');
    try {
      if (navigator.permissions?.query) {
        const perm = await navigator.permissions.query({ name: 'microphone' });
        results.tests.permissionQuery = {
          success: true,
          state: perm.state,
          name: perm.name
        };
        console.log('  Permission state:', perm.state);
      } else {
        results.tests.permissionQuery = { success: false, reason: 'API not available' };
        console.log('  ⚠️ Permissions API not available');
      }
    } catch (e) {
      results.tests.permissionQuery = { success: false, error: e.message };
      console.log('  ❌ Query failed:', e.message);
    }

    // Test 3: Device Enumeration
    console.log('\n📋 Test 3: Device Enumeration');
    try {
      if (navigator.mediaDevices?.enumerateDevices) {
        const devices = await navigator.mediaDevices.enumerateDevices();
        const audioInputs = devices.filter(d => d.kind === 'audioinput');
        results.tests.deviceEnumeration = {
          success: true,
          totalDevices: devices.length,
          audioInputs: audioInputs.length,
          devices: audioInputs.map(d => ({
            deviceId: d.deviceId ? d.deviceId.substring(0, 16) + '...' : '(empty)',
            label: d.label || '(no label - permission needed)',
            kind: d.kind
          }))
        };
        console.log(`  Total devices: ${devices.length}`);
        console.log(`  Audio inputs: ${audioInputs.length}`);
        audioInputs.forEach((d, i) => {
          console.log(`    [${i}] ${d.label || '(no label)'} - ${d.deviceId?.substring(0, 16) || '(no id)'}...`);
        });
        if (audioInputs.length === 0) {
          console.log('  ⚠️ No audio inputs found - this is normal if permission not yet granted');
        }
      } else {
        results.tests.deviceEnumeration = { success: false, reason: 'API not available' };
      }
    } catch (e) {
      results.tests.deviceEnumeration = { success: false, error: e.message };
      console.log('  ❌ Enumeration failed:', e.message);
    }

    // Test 4: Direct getUserMedia Test
    console.log('\n📋 Test 4: Direct getUserMedia Test');
    console.log('  ⏳ Attempting getUserMedia (may show browser permission dialog)...');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const tracks = stream.getTracks();
      results.tests.getUserMedia = {
        success: true,
        streamId: stream.id,
        tracks: tracks.map(t => ({ kind: t.kind, label: t.label, enabled: t.enabled }))
      };
      console.log('  ✅ SUCCESS! Stream obtained:', stream.id);
      tracks.forEach(t => {
        console.log(`    Track: ${t.kind} - ${t.label} (enabled: ${t.enabled})`);
      });
      // Clean up
      tracks.forEach(t => t.stop());
      console.log('  🧹 Stream cleaned up');
    } catch (e) {
      results.tests.getUserMedia = {
        success: false,
        errorName: e.name,
        errorMessage: e.message
      };
      console.log(`  ❌ FAILED: ${e.name}`);
      console.log(`  Message: ${e.message}`);

      // Provide specific guidance
      if (e.name === 'NotAllowedError') {
        console.log('\n  💡 SOLUTION: You need to grant microphone permission');
        console.log('     - Check browser address bar for microphone icon');
        console.log('     - Check macOS System Preferences > Security & Privacy > Microphone');
        console.log('     - Ensure your browser has microphone access enabled');
      } else if (e.name === 'NotFoundError') {
        console.log('\n  💡 SOLUTION: No microphone detected');
        console.log('     - Check if microphone is connected');
        console.log('     - Check macOS Sound preferences for input devices');
        console.log('     - Try a different microphone');
      } else if (e.name === 'NotReadableError') {
        console.log('\n  💡 SOLUTION: Microphone is busy');
        console.log('     - Close other apps using the microphone');
        console.log('     - Try restarting your browser');
      }
    }

    // Test 5: Re-enumerate after permission
    console.log('\n📋 Test 5: Re-enumerate Devices (after permission attempt)');
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devices.filter(d => d.kind === 'audioinput');
      results.tests.postPermissionEnumeration = {
        success: true,
        audioInputs: audioInputs.length,
        hasLabels: audioInputs.some(d => d.label !== ''),
        devices: audioInputs.map(d => ({ label: d.label, deviceId: d.deviceId?.substring(0, 16) }))
      };
      console.log(`  Audio inputs: ${audioInputs.length}`);
      console.log(`  Has labels: ${results.tests.postPermissionEnumeration.hasLabels ? '✅ Yes' : '❌ No (permission not granted)'}`);
      audioInputs.forEach((d, i) => {
        console.log(`    [${i}] ${d.label || '(still no label)'}`);
      });
    } catch (e) {
      results.tests.postPermissionEnumeration = { success: false, error: e.message };
    }

    // Summary
    console.log('\n═══════════════════════════════════════════════════════════════');
    console.log('📊 SUMMARY');
    console.log('═══════════════════════════════════════════════════════════════');
    console.log('Browser:', `${this.browser.name} ${this.browser.version}`);
    console.log('Manager State:', JSON.stringify(this.state, null, 2));
    console.log('getUserMedia:', results.tests.getUserMedia?.success ? '✅ WORKING' : '❌ FAILED');

    if (!results.tests.getUserMedia?.success) {
      console.log('\n🔧 RECOMMENDED ACTIONS:');
      console.log('1. Open a new browser tab');
      console.log('2. Navigate to: chrome://settings/content/microphone (for Chrome)');
      console.log('3. Ensure this site is in the "Allow" list');
      console.log('4. Check macOS System Preferences > Security & Privacy > Privacy > Microphone');
      console.log('5. Ensure your browser app has a checkmark');
    }

    console.log('\n📋 Full results saved to: window.lastMicDiagnostics');
    window.lastMicDiagnostics = results;

    return results;
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
