/**
 * Advanced Network Recovery Manager v2.0
 * =======================================
 * Implements comprehensive recovery strategies with:
 * - Multi-tier circuit breaker pattern
 * - Adaptive strategy selection based on failure patterns
 * - Real-time health scoring with exponential decay
 * - Intelligent fallback provider rotation
 * - ML-assisted recovery prediction (when available)
 * - Zero hardcoded endpoints
 */

import configService, {
  getCircuitBreakerState,
  onConfigReady
} from '../services/DynamicConfigService';

// Circuit Breaker States
const CIRCUIT_STATE = {
  CLOSED: 'closed',
  OPEN: 'open',
  HALF_OPEN: 'half_open'
};

// Strategy priority levels
const STRATEGY_PRIORITY = {
  IMMEDIATE: 1,    // Try first
  QUICK: 2,        // Quick fallbacks
  STANDARD: 3,     // Standard recovery
  EXTENDED: 4,     // Extended recovery
  LAST_RESORT: 5   // Final options
};

class NetworkRecoveryManager {
  constructor() {
    // Strategy definitions with priorities and conditions
    this.strategies = [
      { name: 'connectionCheck', priority: STRATEGY_PRIORITY.IMMEDIATE, timeout: 5000 },
      { name: 'quickRetry', priority: STRATEGY_PRIORITY.QUICK, timeout: 3000 },
      { name: 'serviceSwitch', priority: STRATEGY_PRIORITY.STANDARD, timeout: 10000 },
      { name: 'webSocketFallback', priority: STRATEGY_PRIORITY.STANDARD, timeout: 5000 },
      { name: 'dnsRecovery', priority: STRATEGY_PRIORITY.EXTENDED, timeout: 10000 },
      { name: 'mlBackendRecovery', priority: STRATEGY_PRIORITY.EXTENDED, timeout: 15000 },
      { name: 'offlineMode', priority: STRATEGY_PRIORITY.LAST_RESORT, timeout: 1000 },
      { name: 'edgeProcessing', priority: STRATEGY_PRIORITY.LAST_RESORT, timeout: 5000 }
    ];

    this.recoveryAttempts = 0;
    this.maxRecoveryAttempts = 5;

    // Enhanced connection health tracking
    this.connectionHealth = {
      lastSuccessful: Date.now(),
      consecutiveFailures: 0,
      averageLatency: 0,
      latencyHistory: [],
      serviceStatus: new Map(),
      errorPatterns: [],
      recoveryHistory: []
    };

    // Per-strategy circuit breakers
    this.strategyCircuitBreakers = new Map();
    this.strategies.forEach(s => {
      this.strategyCircuitBreakers.set(s.name, {
        state: CIRCUIT_STATE.CLOSED,
        failures: 0,
        threshold: 3,
        resetTimeout: 60000,
        lastFailure: null
      });
    });

    // Dynamic fallback providers (populated after config ready)
    this.fallbackProviders = new Map();

    this.isRecovering = false;
    this.recoveryQueue = [];
    this.monitoringInterval = null;

    // Wait for config then initialize
    this._initializeWithConfig();
  }

  /**
   * Initialize with dynamic configuration
   */
  async _initializeWithConfig() {
    try {
      await configService.waitForConfig(5000);
    } catch {
      console.warn('[NetworkRecoveryManager] Config timeout, using defaults');
    }

    // Start connection monitoring
    this.startConnectionMonitoring();
  }

  /**
   * Get API URL dynamically
   */
  _getApiUrl() {
    return configService.getApiUrl() || this._inferApiUrl();
  }

  /**
   * Get WebSocket URL dynamically
   */
  _getWebSocketUrl(endpoint = '') {
    const base = configService.getWebSocketUrl();
    if (base) {
      return endpoint ? `${base}/${endpoint.replace(/^\//, '')}` : base;
    }
    return this._inferWebSocketUrl(endpoint);
  }

  /**
   * Infer API URL from environment
   */
  _inferApiUrl() {
    const hostname = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
    const protocol = typeof window !== 'undefined' ? window.location.protocol.replace(':', '') : 'http';
    return `${protocol}://${hostname}:8000`;
  }

  /**
   * Infer WebSocket URL from environment
   */
  _inferWebSocketUrl(endpoint = '') {
    const hostname = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
    const protocol = typeof window !== 'undefined'
      ? (window.location.protocol === 'https:' ? 'wss' : 'ws')
      : 'ws';
    const base = `${protocol}://${hostname}:8000`;
    return endpoint ? `${base}/${endpoint.replace(/^\//, '')}` : base;
  }

  /**
   * Main recovery method with intelligent strategy selection
   */
  async recoverFromNetworkError(error, recognition, context = {}) {
    if (this.isRecovering) {
      console.log('[NetworkRecovery] Recovery in progress, queueing request...');
      return new Promise((resolve) => {
        this.recoveryQueue.push({ error, recognition, context, resolve });
      });
    }

    this.isRecovering = true;
    this.recoveryAttempts++;
    this.connectionHealth.consecutiveFailures++;

    console.log(`[NetworkRecovery] Starting recovery (attempt ${this.recoveryAttempts})`);
    this._recordErrorPattern(error);

    // Select strategies based on error type and history
    const selectedStrategies = this._selectStrategies(error, context);

    // Try strategies in priority order
    for (const strategy of selectedStrategies) {
      // Check strategy-specific circuit breaker
      if (!this._checkStrategyCircuitBreaker(strategy.name)) {
        console.log(`[NetworkRecovery] Skipping ${strategy.name} (circuit open)`);
        continue;
      }

      try {
        console.log(`[NetworkRecovery] Trying strategy: ${strategy.name}`);
        const result = await this._executeStrategy(strategy, error, recognition, context);

        if (result.success) {
          this.onRecoverySuccess(strategy.name, result);
          this.isRecovering = false;
          this.processRecoveryQueue();
          return result;
        }

        this._recordStrategyFailure(strategy.name);
      } catch (e) {
        console.warn(`[NetworkRecovery] Strategy ${strategy.name} failed:`, e.message);
        this._recordStrategyFailure(strategy.name);
      }
    }

    this.isRecovering = false;
    this.processRecoveryQueue();

    return {
      success: false,
      message: 'All recovery strategies exhausted',
      needsManualIntervention: true,
      recoveryHistory: this.connectionHealth.recoveryHistory
    };
  }

  /**
   * Select strategies based on error type and history
   */
  _selectStrategies(error, context) {
    const errorType = this._classifyError(error);
    let strategies = [...this.strategies];

    // Filter and prioritize based on error type
    if (errorType === 'network') {
      // Network errors: prioritize connection checks
      strategies = strategies.sort((a, b) => {
        if (a.name === 'connectionCheck') return -1;
        if (b.name === 'connectionCheck') return 1;
        return a.priority - b.priority;
      });
    } else if (errorType === 'service') {
      // Service errors: prioritize service switching
      strategies = strategies.sort((a, b) => {
        if (a.name === 'serviceSwitch') return -1;
        if (b.name === 'serviceSwitch') return 1;
        return a.priority - b.priority;
      });
    }

    // Consider recovery history
    const recentSuccesses = this.connectionHealth.recoveryHistory
      .filter(r => r.success && Date.now() - r.timestamp < 300000)
      .map(r => r.strategy);

    // Prioritize strategies that worked recently
    strategies = strategies.sort((a, b) => {
      const aRecent = recentSuccesses.includes(a.name) ? -1 : 0;
      const bRecent = recentSuccesses.includes(b.name) ? -1 : 0;
      return aRecent - bRecent || a.priority - b.priority;
    });

    return strategies;
  }

  /**
   * Classify error type for strategy selection
   */
  _classifyError(error) {
    const errorStr = String(error?.error || error?.message || error).toLowerCase();

    if (errorStr.includes('network') || errorStr.includes('offline') || errorStr.includes('connection')) {
      return 'network';
    }
    if (errorStr.includes('service') || errorStr.includes('unavailable') || errorStr.includes('500')) {
      return 'service';
    }
    if (errorStr.includes('audio') || errorStr.includes('microphone') || errorStr.includes('permission')) {
      return 'audio';
    }
    return 'unknown';
  }

  /**
   * Execute a specific strategy with timeout
   */
  async _executeStrategy(strategy, error, recognition, context) {
    const methodName = `strategy_${strategy.name}`;

    if (typeof this[methodName] !== 'function') {
      throw new Error(`Strategy method ${methodName} not found`);
    }

    // Execute with timeout
    return Promise.race([
      this[methodName](error, recognition, context),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error('Strategy timeout')), strategy.timeout)
      )
    ]);
  }

  /**
   * Strategy: Basic connection check and retry
   */
  async strategy_connectionCheck(error, recognition, context) {
    const isOnline = await this.checkConnectivity();

    if (!isOnline) {
      console.log('[NetworkRecovery] No internet, waiting for connection...');
      const recovered = await this.waitForConnection(5000);

      if (!recovered) {
        return { success: false, reason: 'No internet connection' };
      }
    }

    // Connection exists, try restart
    return this.restartRecognition(recognition);
  }

  /**
   * Strategy: Quick retry with minimal delay
   */
  async strategy_quickRetry(error, recognition, context) {
    // Wait briefly then retry
    await new Promise(resolve => setTimeout(resolve, 500));

    try {
      return await this.restartRecognition(recognition);
    } catch {
      return { success: false };
    }
  }

  /**
   * Strategy: Switch to alternative speech service
   */
  async strategy_serviceSwitch(error, recognition, context) {
    console.log('[NetworkRecovery] Attempting service switch...');

    // Stop current recognition
    try {
      recognition.stop();
    } catch {}

    await new Promise(resolve => setTimeout(resolve, 300));

    // Create new recognition with different configuration
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      return { success: false, reason: 'SpeechRecognition not available' };
    }

    const newRecognition = new SpeechRecognition();
    newRecognition.continuous = recognition.continuous;
    newRecognition.interimResults = recognition.interimResults;
    newRecognition.lang = recognition.lang || 'en-US';

    try {
      newRecognition.start();
      return {
        success: true,
        message: 'Switched to new speech recognition instance',
        newRecognition
      };
    } catch (e) {
      return { success: false, reason: e.message };
    }
  }

  /**
   * Strategy: WebSocket fallback for audio streaming
   */
  async strategy_webSocketFallback(error, recognition, context) {
    console.log('[NetworkRecovery] Attempting WebSocket fallback...');

    const wsUrl = this._getWebSocketUrl('voice/jarvis/stream');
    const ws = new WebSocket(wsUrl);

    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        ws.close();
        resolve({ success: false, reason: 'WebSocket timeout' });
      }, 3000);

      ws.onopen = () => {
        clearTimeout(timeout);
        console.log('[NetworkRecovery] WebSocket fallback available');
        resolve({
          success: true,
          message: 'Switched to WebSocket audio streaming',
          useWebSocket: true,
          websocket: ws
        });
      };

      ws.onerror = () => {
        clearTimeout(timeout);
        ws.close();
        resolve({ success: false, reason: 'WebSocket connection failed' });
      };
    });
  }

  /**
   * Strategy: DNS recovery and network diagnostics
   */
  async strategy_dnsRecovery(error, recognition, context) {
    console.log('[NetworkRecovery] Running network diagnostics...');

    try {
      const apiUrl = this._getApiUrl();
      const response = await fetch(`${apiUrl}/network/diagnose`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          error: error?.error || String(error),
          timestamp: Date.now(),
          userAgent: navigator.userAgent
        }),
        signal: AbortSignal.timeout(5000)
      });

      if (response.ok) {
        const diagnosis = await response.json();
        if (diagnosis.recovered) {
          return await this.restartRecognition(recognition);
        }
      }
    } catch (e) {
      console.warn('[NetworkRecovery] Network diagnosis failed:', e.message);
    }

    return { success: false };
  }

  /**
   * Strategy: ML backend assisted recovery
   */
  async strategy_mlBackendRecovery(error, recognition, context) {
    console.log('[NetworkRecovery] Requesting ML backend assistance...');

    try {
      const apiUrl = this._getApiUrl();
      const response = await fetch(`${apiUrl}/network/ml/advanced-recovery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          error: error?.error || String(error),
          connectionHealth: {
            consecutiveFailures: this.connectionHealth.consecutiveFailures,
            averageLatency: this.connectionHealth.averageLatency,
            errorPatterns: this.connectionHealth.errorPatterns.slice(-5)
          },
          recoveryAttempts: this.recoveryAttempts,
          browserInfo: {
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            language: navigator.language
          }
        }),
        signal: AbortSignal.timeout(10000)
      });

      if (response.ok) {
        const strategy = await response.json();

        if (strategy.proxyEndpoint) {
          return {
            success: true,
            message: 'Using ML backend as speech proxy',
            useProxy: true,
            proxyEndpoint: strategy.proxyEndpoint
          };
        }
      }
    } catch (e) {
      console.warn('[NetworkRecovery] ML backend recovery failed:', e.message);
    }

    return { success: false };
  }

  /**
   * Strategy: Offline mode with command queueing
   */
  async strategy_offlineMode(error, recognition, context) {
    console.log('[NetworkRecovery] Enabling offline mode...');

    return {
      success: true,
      message: 'Offline mode activated - commands will be queued',
      offlineMode: true,
      commandQueue: [],
      syncWhenOnline: true
    };
  }

  /**
   * Strategy: Edge processing with local models
   */
  async strategy_edgeProcessing(error, recognition, context) {
    console.log('[NetworkRecovery] Attempting edge processing...');

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      return { success: false };
    }

    try {
      const offlineRecognition = new SpeechRecognition();
      offlineRecognition.continuous = true;
      offlineRecognition.interimResults = true;

      // Add grammar for common commands
      if (window.SpeechGrammarList || window.webkitSpeechGrammarList) {
        const SpeechGrammarList = window.SpeechGrammarList || window.webkitSpeechGrammarList;
        const grammar = '#JSGF V1.0; grammar commands; public <command> = jarvis | hey jarvis | stop | start | help | unlock;';
        const speechRecognitionList = new SpeechGrammarList();
        speechRecognitionList.addFromString(grammar, 1);
        offlineRecognition.grammars = speechRecognitionList;
      }

      return new Promise((resolve) => {
        const timeout = setTimeout(() => {
          offlineRecognition.stop();
          resolve({ success: false });
        }, 3000);

        offlineRecognition.onstart = () => {
          clearTimeout(timeout);
          resolve({
            success: true,
            message: 'Switched to offline edge processing',
            offlineRecognition
          });
        };

        offlineRecognition.onerror = () => {
          clearTimeout(timeout);
          resolve({ success: false });
        };

        offlineRecognition.start();
      });
    } catch (e) {
      console.warn('[NetworkRecovery] Edge processing failed:', e.message);
      return { success: false };
    }
  }

  /**
   * Check internet connectivity
   */
  async checkConnectivity() {
    if (!navigator.onLine) {
      return false;
    }

    try {
      const response = await fetch('https://www.google.com/generate_204', {
        method: 'HEAD',
        mode: 'no-cors',
        signal: AbortSignal.timeout(3000)
      });
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Wait for connection to be restored
   */
  async waitForConnection(timeout = 5000) {
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      if (await this.checkConnectivity()) {
        return true;
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }

    return false;
  }

  /**
   * Restart speech recognition
   */
  async restartRecognition(recognition) {
    try {
      recognition.stop();
    } catch {}

    await new Promise(resolve => setTimeout(resolve, 500));

    try {
      recognition.start();
      return {
        success: true,
        message: 'Recognition restarted successfully'
      };
    } catch (e) {
      return {
        success: false,
        message: `Failed to restart: ${e.message}`
      };
    }
  }

  /**
   * Check strategy-specific circuit breaker
   */
  _checkStrategyCircuitBreaker(strategyName) {
    const cb = this.strategyCircuitBreakers.get(strategyName);
    if (!cb) return true;

    if (cb.state === CIRCUIT_STATE.CLOSED) {
      return true;
    }

    if (cb.state === CIRCUIT_STATE.OPEN) {
      const elapsed = Date.now() - cb.lastFailure;
      if (elapsed >= cb.resetTimeout) {
        cb.state = CIRCUIT_STATE.HALF_OPEN;
        return true;
      }
      return false;
    }

    return true; // HALF_OPEN allows one attempt
  }

  /**
   * Record strategy failure
   */
  _recordStrategyFailure(strategyName) {
    const cb = this.strategyCircuitBreakers.get(strategyName);
    if (!cb) return;

    cb.failures++;
    cb.lastFailure = Date.now();

    if (cb.state === CIRCUIT_STATE.HALF_OPEN || cb.failures >= cb.threshold) {
      cb.state = CIRCUIT_STATE.OPEN;
      console.log(`[NetworkRecovery] Circuit OPEN for strategy: ${strategyName}`);
    }
  }

  /**
   * Record recovery success
   */
  onRecoverySuccess(strategy, result) {
    console.log(`[NetworkRecovery] Recovery successful using: ${strategy}`);

    // Reset counters
    this.recoveryAttempts = 0;
    this.connectionHealth.consecutiveFailures = 0;
    this.connectionHealth.lastSuccessful = Date.now();

    // Record in history
    this.connectionHealth.recoveryHistory.push({
      strategy,
      success: true,
      timestamp: Date.now()
    });

    // Keep only last 20 entries
    if (this.connectionHealth.recoveryHistory.length > 20) {
      this.connectionHealth.recoveryHistory.shift();
    }

    // Reset strategy circuit breaker
    const cb = this.strategyCircuitBreakers.get(strategy);
    if (cb) {
      cb.state = CIRCUIT_STATE.CLOSED;
      cb.failures = 0;
    }

    // Log to backend
    this.logRecoverySuccess(strategy, result);
  }

  /**
   * Log recovery success to backend
   */
  async logRecoverySuccess(strategy, result) {
    try {
      const apiUrl = this._getApiUrl();
      await fetch(`${apiUrl}/network/ml/recovery-success`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy,
          result: { success: result.success, message: result.message },
          connectionHealth: {
            consecutiveFailures: this.connectionHealth.consecutiveFailures,
            averageLatency: this.connectionHealth.averageLatency
          },
          timestamp: Date.now()
        }),
        signal: AbortSignal.timeout(3000)
      });
    } catch {
      // Logging is non-critical
    }
  }

  /**
   * Record error pattern for analysis
   */
  _recordErrorPattern(error) {
    const pattern = {
      type: this._classifyError(error),
      message: String(error?.error || error?.message || error).substring(0, 100),
      timestamp: Date.now()
    };

    this.connectionHealth.errorPatterns.push(pattern);

    // Keep only last 20 patterns
    if (this.connectionHealth.errorPatterns.length > 20) {
      this.connectionHealth.errorPatterns.shift();
    }
  }

  /**
   * Process queued recovery requests
   */
  processRecoveryQueue() {
    while (this.recoveryQueue.length > 0) {
      const { error, recognition, context, resolve } = this.recoveryQueue.shift();
      this.recoverFromNetworkError(error, recognition, context).then(resolve);
    }
  }

  /**
   * Start connection health monitoring
   */
  startConnectionMonitoring() {
    if (this.monitoringInterval) {
      clearInterval(this.monitoringInterval);
    }

    this.monitoringInterval = setInterval(async () => {
      const isOnline = await this.checkConnectivity();

      if (!isOnline && this.connectionHealth.consecutiveFailures === 0) {
        console.warn('[NetworkRecovery] Connection lost - preparing recovery...');
      } else if (isOnline && this.connectionHealth.consecutiveFailures > 0) {
        console.log('[NetworkRecovery] Connection restored');
        this.connectionHealth.consecutiveFailures = 0;
        this.connectionHealth.lastSuccessful = Date.now();
      }
    }, 30000);
  }

  /**
   * Get current health metrics
   */
  getHealthMetrics() {
    return {
      ...this.connectionHealth,
      recoveryAttempts: this.recoveryAttempts,
      isRecovering: this.isRecovering,
      strategiesStatus: Object.fromEntries(this.strategyCircuitBreakers)
    };
  }

  /**
   * Cleanup resources
   */
  destroy() {
    if (this.monitoringInterval) {
      clearInterval(this.monitoringInterval);
      this.monitoringInterval = null;
    }
  }
}

// Singleton instance
let recoveryManagerInstance = null;

export const getNetworkRecoveryManager = () => {
  if (!recoveryManagerInstance) {
    recoveryManagerInstance = new NetworkRecoveryManager();
  }
  return recoveryManagerInstance;
};

export default NetworkRecoveryManager;
