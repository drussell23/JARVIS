/**
 * Frontend Configuration v2.0
 * ===========================
 * Centralized, dynamic configuration with:
 * - Zero hardcoded values (environment-aware defaults)
 * - Real-time URL updates from config service
 * - Reactive configuration for components
 * - Type-safe configuration access
 */

import configService, {
  getBackendState,
  onConfigReady,
  onBackendState
} from './services/DynamicConfigService';

// Environment detection utilities
const ENV = {
  isDev: () => process.env.NODE_ENV === 'development' ||
    (typeof window !== 'undefined' && window.location.hostname === 'localhost'),
  isProd: () => process.env.NODE_ENV === 'production',
  getEnvVar: (key, fallback = null) => {
    const value = typeof process !== 'undefined' ? process.env?.[key] : null;
    return value || fallback;
  }
};

// Dynamic URL generation based on environment
const inferUrls = () => {
  const hostname = typeof window !== 'undefined'
    ? window.location.hostname
    : 'localhost';

  const protocol = typeof window !== 'undefined'
    ? window.location.protocol.replace(':', '')
    : 'http';

  const wsProtocol = protocol === 'https' ? 'wss' : 'ws';

  // Check for environment variable first
  const envApiUrl = ENV.getEnvVar('REACT_APP_API_URL');
  if (envApiUrl) {
    return {
      API_BASE_URL: envApiUrl,
      WS_BASE_URL: envApiUrl.replace('http://', 'ws://').replace('https://', 'wss://')
    };
  }

  // Default port based on environment
  const port = ENV.isDev() ? 8000 : 8000;

  return {
    API_BASE_URL: `${protocol}://${hostname}:${port}`,
    WS_BASE_URL: `${wsProtocol}://${hostname}:${port}`
  };
};

// Initialize with inferred URLs
let { API_BASE_URL, WS_BASE_URL } = inferUrls();

// Update URLs when config service is ready
onConfigReady((config) => {
  if (config.API_BASE_URL) {
    API_BASE_URL = config.API_BASE_URL;
  }
  if (config.WS_BASE_URL) {
    WS_BASE_URL = config.WS_BASE_URL;
  }

  console.log('[Config] URLs updated from config service:', { API_BASE_URL, WS_BASE_URL });
});

// Also listen for config updates
configService.on('config-updated', (config) => {
  if (config.API_BASE_URL) {
    API_BASE_URL = config.API_BASE_URL;
  }
  if (config.WS_BASE_URL) {
    WS_BASE_URL = config.WS_BASE_URL;
  }
});

// Export dynamic URLs as getters for real-time values
export { API_BASE_URL, WS_BASE_URL };

// Configuration constants with sensible defaults
export const CONFIG = {
  // Speech recognition settings
  SPEECH_RECOGNITION_TIMEOUT: 15000,
  SPEECH_SYNTHESIS_RATE: 1.0,
  SPEECH_SYNTHESIS_PITCH: 0.95,
  SPEECH_SYNTHESIS_VOLUME: 1.0,

  // Audio settings
  AUDIO_SAMPLE_RATE: 44100,
  AUDIO_CHANNELS: 1,
  AUDIO_BIT_DEPTH: 16,

  // Vision system settings
  VISION_UPDATE_INTERVAL: 2000,
  VISION_RECONNECT_ATTEMPTS: 5,
  VISION_RECONNECT_DELAY: 2000,

  // WebSocket settings
  WS_RECONNECT_DELAY: 3000,
  WS_MAX_RECONNECT_ATTEMPTS: 10,
  WS_HEARTBEAT_INTERVAL: 30000,
  WS_CONNECTION_TIMEOUT: 10000,

  // ML Audio settings
  ML_AUDIO_ENABLED: true,
  ML_AUTO_RECOVERY: true,
  ML_MAX_RETRIES: 5,
  ML_RETRY_DELAYS: [100, 500, 1000, 2000, 5000],
  ML_ANOMALY_THRESHOLD: 0.8,
  ML_PREDICTION_THRESHOLD: 0.7,

  // Voice unlock settings
  VOICE_UNLOCK_THRESHOLD: 0.85,
  VOICE_UNLOCK_TIMEOUT: 10000,
  VOICE_ENROLLMENT_SAMPLES: 5,

  // Circuit breaker settings
  CIRCUIT_BREAKER_THRESHOLD: 5,
  CIRCUIT_BREAKER_RESET_TIMEOUT: 30000,
  CIRCUIT_BREAKER_SUCCESS_THRESHOLD: 2,

  // Performance settings
  MAX_CONCURRENT_REQUESTS: 5,
  REQUEST_TIMEOUT: 30000,
  CACHE_TTL: 300000, // 5 minutes

  // Feature flags (can be overridden by environment)
  FEATURES: {
    VOICE_UNLOCK: ENV.getEnvVar('REACT_APP_FEATURE_VOICE_UNLOCK', 'true') === 'true',
    VISION_SYSTEM: ENV.getEnvVar('REACT_APP_FEATURE_VISION', 'true') === 'true',
    ML_PROCESSING: ENV.getEnvVar('REACT_APP_FEATURE_ML', 'true') === 'true',
    CLOUD_FALLBACK: ENV.getEnvVar('REACT_APP_FEATURE_CLOUD_FALLBACK', 'true') === 'true',
    DEBUG_MODE: ENV.isDev()
  }
};

/**
 * Get current API URL with optional endpoint
 */
export const getApiUrl = (endpoint = '') => {
  // Try config service first for most up-to-date URL
  const serviceUrl = configService.getApiUrl(endpoint);
  if (serviceUrl) {
    return serviceUrl;
  }

  // Fallback to module-level URL
  const baseUrl = API_BASE_URL;
  return endpoint
    ? `${baseUrl}/${endpoint.replace(/^\//, '')}`
    : baseUrl;
};

/**
 * Get current WebSocket URL with optional endpoint
 */
export const getWebSocketUrl = (endpoint = '') => {
  // Try config service first for most up-to-date URL
  const serviceUrl = configService.getWebSocketUrl(endpoint);
  if (serviceUrl) {
    return serviceUrl;
  }

  // Fallback to module-level URL
  const baseUrl = WS_BASE_URL;
  return endpoint
    ? `${baseUrl}/${endpoint.replace(/^\//, '')}`
    : baseUrl;
};

/**
 * Get backend connection state
 */
export const getConnectionState = () => {
  const backendState = getBackendState();
  return {
    isConnected: backendState.status !== 'unknown',
    isReady: backendState.ready,
    mode: backendState.mode,
    status: backendState.status
  };
};

/**
 * Check if a feature is enabled
 */
export const isFeatureEnabled = (feature) => {
  return CONFIG.FEATURES[feature] === true;
};

/**
 * Get configuration value with optional default
 */
export const getConfigValue = (key, defaultValue = null) => {
  return CONFIG[key] !== undefined ? CONFIG[key] : defaultValue;
};

/**
 * Create a reactive config subscription
 */
export const subscribeToConfig = (callback) => {
  const handler = () => {
    callback({
      API_BASE_URL,
      WS_BASE_URL,
      ...CONFIG
    });
  };

  configService.on('config-updated', handler);
  configService.on('config-ready', handler);

  // Return unsubscribe function
  return () => {
    configService.off('config-updated', handler);
    configService.off('config-ready', handler);
  };
};

/**
 * Wait for configuration to be ready
 */
export const waitForConfig = async (timeout = 10000) => {
  return configService.waitForConfig(timeout);
};

// Export environment utilities
export { ENV };
