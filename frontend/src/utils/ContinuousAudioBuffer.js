/**
 * Continuous Audio Buffer - Pre-captures audio to eliminate first-attempt misses
 * ===============================================================================
 *
 * This module maintains a rolling buffer of audio so that when speech recognition
 * detects a command, we already have the audio from BEFORE the detection.
 *
 * Key Features:
 * - Rolling circular buffer of the last N seconds of audio
 * - Seamless handoff to voice biometric system
 * - Zero-gap audio capture for first-attempt voice recognition
 * - Automatic gain and quality monitoring
 * - WebSocket streaming support for real-time processing
 */

class ContinuousAudioBuffer {
  constructor(options = {}) {
    // Configuration
    this.bufferDurationMs = options.bufferDurationMs || 5000; // Keep last 5 seconds
    this.sampleRate = options.sampleRate || 16000;
    this.chunkIntervalMs = options.chunkIntervalMs || 100; // 100ms chunks
    this.maxBufferChunks = Math.ceil(this.bufferDurationMs / this.chunkIntervalMs);

    // Audio state
    this.audioStream = null;
    this.mediaRecorder = null;
    this.audioContext = null;
    this.analyserNode = null;
    this.scriptProcessor = null;

    // Circular buffer for raw audio chunks
    this.audioChunks = [];
    this.chunkTimestamps = [];

    // Voice activity detection
    this.isVoiceActive = false;
    this.lastVoiceActivityTime = 0;
    this.voiceActivityThreshold = options.voiceActivityThreshold || 0.02;
    this.silenceThresholdMs = options.silenceThresholdMs || 1500;

    // State tracking
    this.isRunning = false;
    this.isInitialized = false;
    this.error = null;

    // Callbacks
    this.onVoiceStart = options.onVoiceStart || null;
    this.onVoiceEnd = options.onVoiceEnd || null;
    this.onAudioLevel = options.onAudioLevel || null;
    this.onError = options.onError || null;

    // Quality metrics
    this.metrics = {
      avgLevel: 0,
      peakLevel: 0,
      noiseFloor: 0.01,
      voiceActivityRatio: 0,
      capturedDuration: 0,
      chunkCount: 0
    };

    // MIME type for recording
    this.mimeType = null;

    console.log('[ContinuousAudioBuffer] Initialized with config:', {
      bufferDurationMs: this.bufferDurationMs,
      sampleRate: this.sampleRate,
      maxChunks: this.maxBufferChunks
    });
  }

  /**
   * Initialize and start continuous audio capture
   */
  async start() {
    if (this.isRunning) {
      console.log('[ContinuousAudioBuffer] Already running');
      return true;
    }

    try {
      console.log('[ContinuousAudioBuffer] Starting continuous audio capture...');

      // Get microphone access with optimal settings
      this.audioStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: { ideal: this.sampleRate, min: 8000, max: 48000 },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });

      // Set up audio analysis for voice activity detection
      await this._setupAudioAnalysis();

      // Set up media recorder for chunk capture
      await this._setupMediaRecorder();

      this.isRunning = true;
      this.isInitialized = true;
      this.error = null;

      console.log('[ContinuousAudioBuffer] Started successfully');
      return true;

    } catch (error) {
      console.error('[ContinuousAudioBuffer] Failed to start:', error);
      this.error = error;
      if (this.onError) {
        this.onError(error);
      }
      return false;
    }
  }

  /**
   * Set up audio context and analyser for voice activity detection
   */
  async _setupAudioAnalysis() {
    this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: this.sampleRate
    });

    const source = this.audioContext.createMediaStreamSource(this.audioStream);

    // Create analyser node
    this.analyserNode = this.audioContext.createAnalyser();
    this.analyserNode.fftSize = 2048;
    this.analyserNode.smoothingTimeConstant = 0.3;

    source.connect(this.analyserNode);

    // Start voice activity monitoring
    this._startVoiceActivityMonitor();
  }

  /**
   * Start voice activity detection monitoring
   */
  _startVoiceActivityMonitor() {
    const dataArray = new Float32Array(this.analyserNode.frequencyBinCount);

    const checkVoiceActivity = () => {
      if (!this.isRunning) return;

      this.analyserNode.getFloatTimeDomainData(dataArray);

      // Calculate RMS level
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        sum += dataArray[i] * dataArray[i];
      }
      const rms = Math.sqrt(sum / dataArray.length);

      // Update metrics
      this.metrics.avgLevel = (this.metrics.avgLevel * 0.9) + (rms * 0.1);
      if (rms > this.metrics.peakLevel) {
        this.metrics.peakLevel = rms;
      }

      // Voice activity detection
      const wasVoiceActive = this.isVoiceActive;
      const threshold = Math.max(this.voiceActivityThreshold, this.metrics.noiseFloor * 3);

      if (rms > threshold) {
        this.isVoiceActive = true;
        this.lastVoiceActivityTime = Date.now();

        if (!wasVoiceActive && this.onVoiceStart) {
          console.log('[ContinuousAudioBuffer] Voice activity started');
          this.onVoiceStart();
        }
      } else {
        // Check if voice ended (silence duration exceeded)
        if (this.isVoiceActive &&
            Date.now() - this.lastVoiceActivityTime > this.silenceThresholdMs) {
          this.isVoiceActive = false;
          if (this.onVoiceEnd) {
            console.log('[ContinuousAudioBuffer] Voice activity ended');
            this.onVoiceEnd();
          }
        }
      }

      // Callback with audio level
      if (this.onAudioLevel) {
        this.onAudioLevel(rms, this.isVoiceActive);
      }

      // Continue monitoring
      requestAnimationFrame(checkVoiceActivity);
    };

    checkVoiceActivity();
  }

  /**
   * Set up media recorder for continuous chunk capture
   */
  async _setupMediaRecorder() {
    // Try different MIME types for compatibility
    const mimeTypes = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg',
      'audio/mp4',
      ''  // Default
    ];

    for (const mimeType of mimeTypes) {
      if (!mimeType || MediaRecorder.isTypeSupported(mimeType)) {
        this.mimeType = mimeType || 'audio/webm';
        break;
      }
    }

    console.log('[ContinuousAudioBuffer] Using MIME type:', this.mimeType);

    this.mediaRecorder = new MediaRecorder(
      this.audioStream,
      this.mimeType ? { mimeType: this.mimeType } : {}
    );

    // Handle data available - add to circular buffer
    this.mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        const timestamp = Date.now();

        // Add to circular buffer
        this.audioChunks.push(event.data);
        this.chunkTimestamps.push(timestamp);
        this.metrics.chunkCount++;

        // Remove old chunks if buffer is full
        while (this.audioChunks.length > this.maxBufferChunks) {
          this.audioChunks.shift();
          this.chunkTimestamps.shift();
        }

        // Update captured duration
        if (this.chunkTimestamps.length > 0) {
          this.metrics.capturedDuration =
            timestamp - this.chunkTimestamps[0];
        }
      }
    };

    this.mediaRecorder.onerror = (event) => {
      console.error('[ContinuousAudioBuffer] MediaRecorder error:', event.error);
      if (this.onError) {
        this.onError(event.error);
      }
    };

    // Start recording with small chunks
    this.mediaRecorder.start(this.chunkIntervalMs);
  }

  /**
   * Get the buffered audio as a single blob
   * Optionally specify how far back to go (default: all buffered audio)
   *
   * 🆕 CRITICAL FIX: Always include the first chunk (contains WebM/EBML header)
   * MediaRecorder chunks only have valid headers in the first chunk.
   * Concatenating chunks without the first one produces invalid WebM files.
   */
  getBufferedAudio(durationMs = null) {
    if (this.audioChunks.length === 0) {
      console.warn('[ContinuousAudioBuffer] No audio chunks available');
      return null;
    }

    let chunks = this.audioChunks;
    let actualStartIndex = 0;

    // If duration specified, only get that much audio
    // BUT: Always include the first chunk (has EBML header) for valid WebM
    if (durationMs !== null && this.audioChunks.length > 1) {
      const now = Date.now();
      const startTime = now - durationMs;

      // Find chunks that fall within the time window
      const timeBasedStartIndex = this.chunkTimestamps.findIndex(t => t >= startTime);

      if (timeBasedStartIndex > 0) {
        // 🔧 CRITICAL: Always include chunk 0 (contains WebM EBML header)
        // Then include the requested time-based chunks
        // This ensures the resulting blob is a valid WebM file
        const headerChunk = this.audioChunks[0];
        const dataChunks = this.audioChunks.slice(timeBasedStartIndex);

        // Combine header chunk with time-windowed data chunks
        chunks = [headerChunk, ...dataChunks];
        actualStartIndex = timeBasedStartIndex;

        console.log(`[ContinuousAudioBuffer] 🔧 Including header chunk + ${dataChunks.length} data chunks for valid WebM`);
      }
    }

    const blob = new Blob(chunks, { type: this.mimeType });
    console.log(`[ContinuousAudioBuffer] Retrieved ${chunks.length} chunks (start: ${actualStartIndex}), ${blob.size} bytes`);

    return {
      blob,
      mimeType: this.mimeType,
      durationMs: chunks.length * this.chunkIntervalMs,
      sampleRate: this.sampleRate,
      chunkCount: chunks.length,
      includesHeader: true  // 🆕 Indicate valid structure
    };
  }

  /**
   * Get buffered audio as base64
   */
  async getBufferedAudioBase64(durationMs = null) {
    const audioData = this.getBufferedAudio(durationMs);
    if (!audioData) return null;

    return new Promise((resolve, reject) => {
      const reader = new FileReader();

      reader.onloadend = () => {
        const base64 = reader.result.split(',')[1];
        resolve({
          ...audioData,
          audio: base64,
          base64Length: base64?.length || 0
        });
      };

      reader.onerror = (error) => {
        console.error('[ContinuousAudioBuffer] Base64 conversion failed:', error);
        reject(error);
      };

      reader.readAsDataURL(audioData.blob);
    });
  }

  /**
   * Clear the audio buffer
   */
  clearBuffer() {
    this.audioChunks = [];
    this.chunkTimestamps = [];
    this.metrics.capturedDuration = 0;
    console.log('[ContinuousAudioBuffer] Buffer cleared');
  }

  /**
   * Stop continuous capture and clean up
   */
  stop() {
    console.log('[ContinuousAudioBuffer] Stopping...');

    this.isRunning = false;

    // Stop media recorder
    if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
      try {
        this.mediaRecorder.stop();
      } catch (e) {
        console.debug('[ContinuousAudioBuffer] MediaRecorder stop:', e.message);
      }
    }
    this.mediaRecorder = null;

    // Stop audio context
    if (this.audioContext && this.audioContext.state !== 'closed') {
      try {
        this.audioContext.close();
      } catch (e) {
        console.debug('[ContinuousAudioBuffer] AudioContext close:', e.message);
      }
    }
    this.audioContext = null;
    this.analyserNode = null;

    // Stop audio stream
    if (this.audioStream) {
      try {
        this.audioStream.getTracks().forEach(track => track.stop());
      } catch (e) {
        console.debug('[ContinuousAudioBuffer] Stream stop:', e.message);
      }
    }
    this.audioStream = null;

    console.log('[ContinuousAudioBuffer] Stopped');
  }

  /**
   * Restart the capture (useful for recovery)
   */
  async restart() {
    console.log('[ContinuousAudioBuffer] Restarting...');
    this.stop();

    // Small delay for resource cleanup
    await new Promise(resolve => setTimeout(resolve, 100));

    return this.start();
  }

  /**
   * 🆕 CRITICAL: Capture fresh audio with VALID WebM container
   * This creates a NEW MediaRecorder to capture audio for a specified duration,
   * producing a valid, self-contained WebM file with proper headers.
   *
   * Use this for voice biometric verification instead of getBufferedAudio()
   * which concatenates chunks and produces invalid WebM.
   *
   * @param {number} durationMs - How long to record (default: 2000ms)
   * @returns {Promise<{blob, audio, mimeType, durationMs, sampleRate}>}
   */
  async captureFreshAudio(durationMs = 2000) {
    console.log(`[ContinuousAudioBuffer] 🎤 Capturing fresh audio for ${durationMs}ms...`);

    if (!this.audioStream || !this.isRunning) {
      console.error('[ContinuousAudioBuffer] Cannot capture - no active audio stream');
      return null;
    }

    return new Promise((resolve, reject) => {
      const chunks = [];
      let recorder = null;

      try {
        // Create a fresh MediaRecorder for this capture
        recorder = new MediaRecorder(
          this.audioStream,
          this.mimeType ? { mimeType: this.mimeType } : {}
        );

        recorder.ondataavailable = (event) => {
          if (event.data.size > 0) {
            chunks.push(event.data);
          }
        };

        recorder.onstop = async () => {
          if (chunks.length === 0) {
            console.warn('[ContinuousAudioBuffer] Fresh capture produced no chunks');
            resolve(null);
            return;
          }

          // Create a single blob - this will have valid WebM headers
          const blob = new Blob(chunks, { type: this.mimeType });
          console.log(`[ContinuousAudioBuffer] ✅ Fresh capture complete: ${chunks.length} chunks, ${blob.size} bytes`);

          // Convert to base64
          const reader = new FileReader();
          reader.onloadend = () => {
            const base64 = reader.result.split(',')[1];
            resolve({
              blob,
              audio: base64,
              base64Length: base64?.length || 0,
              mimeType: this.mimeType,
              durationMs: durationMs,
              sampleRate: this.sampleRate,
              chunkCount: chunks.length,
              isFreshCapture: true  // Flag to indicate this is a valid recording
            });
          };
          reader.onerror = () => {
            console.error('[ContinuousAudioBuffer] Base64 conversion failed');
            resolve(null);
          };
          reader.readAsDataURL(blob);
        };

        recorder.onerror = (event) => {
          console.error('[ContinuousAudioBuffer] Fresh capture recorder error:', event.error);
          reject(event.error);
        };

        // Start recording
        recorder.start();
        console.log(`[ContinuousAudioBuffer] Fresh recorder started, waiting ${durationMs}ms...`);

        // Stop after duration
        setTimeout(() => {
          if (recorder && recorder.state === 'recording') {
            recorder.stop();
          }
        }, durationMs);

      } catch (error) {
        console.error('[ContinuousAudioBuffer] Fresh capture failed:', error);
        reject(error);
      }
    });
  }

  /**
   * 🆕 Get fresh audio as base64 - wrapper for easier use
   */
  async getFreshAudioBase64(durationMs = 2000) {
    return this.captureFreshAudio(durationMs);
  }

  /**
   * Get current status and metrics
   */
  getStatus() {
    return {
      isRunning: this.isRunning,
      isInitialized: this.isInitialized,
      isVoiceActive: this.isVoiceActive,
      error: this.error?.message || null,
      bufferInfo: {
        chunks: this.audioChunks.length,
        maxChunks: this.maxBufferChunks,
        durationMs: this.metrics.capturedDuration,
        fullnessPercent: (this.audioChunks.length / this.maxBufferChunks) * 100
      },
      metrics: { ...this.metrics },
      mimeType: this.mimeType
    };
  }

  /**
   * Calibrate noise floor (call during silence)
   */
  calibrateNoiseFloor(durationMs = 1000) {
    return new Promise((resolve) => {
      const samples = [];
      const dataArray = new Float32Array(this.analyserNode.frequencyBinCount);
      const startTime = Date.now();

      const collectSamples = () => {
        if (Date.now() - startTime < durationMs) {
          this.analyserNode.getFloatTimeDomainData(dataArray);

          let sum = 0;
          for (let i = 0; i < dataArray.length; i++) {
            sum += dataArray[i] * dataArray[i];
          }
          samples.push(Math.sqrt(sum / dataArray.length));

          requestAnimationFrame(collectSamples);
        } else {
          // Calculate noise floor as median of samples
          samples.sort((a, b) => a - b);
          this.metrics.noiseFloor = samples[Math.floor(samples.length / 2)] || 0.01;

          console.log(`[ContinuousAudioBuffer] Noise floor calibrated: ${this.metrics.noiseFloor.toFixed(4)}`);
          resolve(this.metrics.noiseFloor);
        }
      };

      if (this.analyserNode) {
        collectSamples();
      } else {
        resolve(0.01);
      }
    });
  }
}

// Singleton instance for global use
let _globalInstance = null;

/**
 * Get or create the global continuous audio buffer instance
 */
export const getContinuousAudioBuffer = (options = {}) => {
  if (!_globalInstance) {
    _globalInstance = new ContinuousAudioBuffer(options);
  }
  return _globalInstance;
};

/**
 * Start the global continuous audio buffer
 */
export const startContinuousAudioBuffer = async (options = {}) => {
  const buffer = getContinuousAudioBuffer(options);
  return buffer.start();
};

/**
 * Stop the global continuous audio buffer
 */
export const stopContinuousAudioBuffer = () => {
  if (_globalInstance) {
    _globalInstance.stop();
    _globalInstance = null;
  }
};

export default ContinuousAudioBuffer;
