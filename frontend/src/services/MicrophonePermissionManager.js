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

    // v3.0: Comprehensive debugging for microphone issues
    this._debug = true; // Enable debug logging
    this._log('MicrophonePermissionManager v3.0 initializing...');

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
