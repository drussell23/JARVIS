import React, { useState, useEffect, useRef } from 'react';
import './JarvisVoice.css';
import '../styles/JarvisVoiceError.css';
import MicrophonePermissionHelper from './MicrophonePermissionHelper';
import MicrophoneIndicator from './MicrophoneIndicator';
import WorkflowProgress from './WorkflowProgress'; // Workflow progress component
import mlAudioHandler from '../utils/MLAudioHandler'; // ML-enhanced audio handling
import { getNetworkRecoveryManager } from '../utils/NetworkRecoveryManager'; // Advanced network recovery
import WakeWordService from './WakeWordService'; // Wake word detection service
import configService, {
  getBackendState,
  onBackendState,
  onBackendReady,
  onStartupProgress,
  waitForConfig as waitForConfigService
} from '../services/DynamicConfigService'; // Dynamic configuration service
import adaptiveVoiceDetection from '../utils/AdaptiveVoiceDetection'; // Adaptive voice learning system
import HybridSTTClient from '../utils/HybridSTTClient'; // Hybrid STT client (replaces browser SpeechRecognition)
import VoiceStatsDisplay from './VoiceStatsDisplay'; // Adaptive voice stats display
import EnvironmentalStatsDisplay from './EnvironmentalStatsDisplay'; // Environmental stats display
import AudioQualityStatsDisplay from './AudioQualityStatsDisplay'; // Audio quality stats display
import CommandDetectionBanner from './CommandDetectionBanner'; // 🆕 Command detection banner for streaming safeguard
import { getJarvisConnectionService, ConnectionState, connectionStateToJarvisStatus } from '../services/JarvisConnectionService'; // 🆕 Unified connection service
import TrainingStatus from './TrainingStatus'; // 🆕 Real-time training progress display (Feedback Loop)
import { getContinuousAudioBuffer } from '../utils/ContinuousAudioBuffer'; // 🆕 Continuous audio pre-buffer for first-attempt recognition
import { initDynamicFavicon, setFaviconState } from '../utils/DynamicFavicon'; // 🆕 Dynamic JARVIS favicon

// Inline styles to ensure button visibility
const buttonVisibilityStyle = `
  .jarvis-button {
    display: inline-block !important;
    visibility: visible !important;
    opacity: 1 !important;
  }
  .voice-controls {
    display: block !important;
    visibility: visible !important;
    opacity: 1 !important;
  }
`;

// Dynamic API configuration - will be set after config service is ready
let API_URL = null;
let WS_URL = null;
let configReady = false;

// Backend state tracking for real-time synchronization
let backendStateListeners = [];
let backendReady = false;

/**
 * Get dynamically inferred URLs based on current environment
 * Zero hardcoding - derives from window.location
 */
const inferUrls = () => {
  const hostname = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
  const protocol = typeof window !== 'undefined' ? window.location.protocol.replace(':', '') : 'http';
  const wsProtocol = protocol === 'https' ? 'wss' : 'ws';
  // IMPORTANT: Default port must match backend's BACKEND_PORT (8010)
  const port = process.env.REACT_APP_BACKEND_PORT || 8010;

  return {
    API_BASE_URL: `${protocol}://${hostname}:${port}`,
    WS_BASE_URL: `${wsProtocol}://${hostname}:${port}`
  };
};

// Create promise to wait for config with enhanced robustness
const configPromise = new Promise(async (resolve) => {
  // Check if config is already ready
  const currentApiUrl = configService.getApiUrl();
  if (currentApiUrl) {
    API_URL = currentApiUrl;
    WS_URL = configService.getWebSocketUrl();
    configReady = true;
    console.log('[JarvisVoice] Config already ready', { API_URL, WS_URL });
    resolve({ API_URL, WS_URL });
    return;
  }

  // Initialize API URLs when config is ready
  const handleConfigReady = (config) => {
    if (configReady) return; // Prevent duplicate handling

    API_URL = config?.API_BASE_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
    WS_URL = config?.WS_BASE_URL || configService.getWebSocketUrl() || inferUrls().WS_BASE_URL;
    configReady = true;
    console.log('[JarvisVoice] Config ready', { API_URL, WS_URL });
    resolve({ API_URL, WS_URL });
  };

  // Subscribe to config ready event
  configService.once('config-ready', handleConfigReady);

  // Try the enhanced waitForConfig with timeout
  try {
    const config = await waitForConfigService(5000);
    if (!configReady) {
      handleConfigReady(config);
    }
  } catch (err) {
    console.log('[JarvisVoice] Config timeout, using inferred URLs');
    if (!configReady) {
      handleConfigReady(inferUrls());
    }
  }
});

// Subscribe to backend state changes for real-time status sync
onBackendState((state) => {
  console.log('[JarvisVoice] Backend state update:', state);
  backendReady = state.ready || false;

  // Notify all registered listeners
  backendStateListeners.forEach(listener => {
    try {
      listener(state);
    } catch (err) {
      console.error('[JarvisVoice] Backend state listener error:', err);
    }
  });
});

// Subscribe to backend ready event
onBackendReady((state) => {
  console.log('[JarvisVoice] Backend ready notification:', state);
  backendReady = true;
});

// Subscribe to startup progress for detailed tracking
onStartupProgress((progress) => {
  console.log(`[JarvisVoice] Startup progress: ${progress.progress}% - ${progress.message}`);
});

// Also listen for config updates
configService.on('config-updated', (config) => {
  const newApiUrl = config.API_BASE_URL || configService.getApiUrl();
  const newWsUrl = config.WS_BASE_URL || configService.getWebSocketUrl();

  if (newApiUrl !== API_URL || newWsUrl !== WS_URL) {
    API_URL = newApiUrl;
    WS_URL = newWsUrl;
    console.log('JarvisVoice: Config updated', { API_URL, WS_URL });
  }
});

// VisionConnection class for real-time workspace monitoring
class VisionConnection {
  constructor(onUpdate, onAction) {
    this.socket = null;
    this.isConnected = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 5;
    this.reconnectDelay = 2000;
    this.workspaceData = null;
    this.actionQueue = [];

    // Callbacks
    this.onWorkspaceUpdate = onUpdate || (() => { });
    this.onActionExecuted = onAction || (() => { });

    // Monitoring state
    this.monitoringActive = false;
    this.updateInterval = 2.0;
  }

  async connect() {
    try {
      console.log('🔌 Connecting to Vision WebSocket...');

      // Wait for config if not ready
      if (!WS_URL) {
        console.log('VisionConnection: Waiting for config...');
        await configPromise;
      }

      // Use main backend port for vision WebSocket - dynamic URL inference
      const wsBaseUrl = WS_URL || configService.getWebSocketUrl() || inferUrls().WS_BASE_URL;
      // Backend mounts vision WS at /vision/ws/vision (see backend/api/vision_ws_endpoint.py)
      const wsUrl = `${wsBaseUrl}/vision/ws/vision`;  // Use consistent WebSocket URL
      console.log('VisionConnection: Connecting to', wsUrl);
      this.socket = new WebSocket(wsUrl);

      this.socket.onopen = () => {
        console.log('✅ Vision WebSocket connected!');
        this.isConnected = true;
        this.reconnectAttempts = 0;
        this.monitoringActive = true;

        // Request initial analysis
        this.requestWorkspaceAnalysis();
      };

      this.socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.handleVisionMessage(data);
        } catch (error) {
          console.error('Error parsing vision message:', error);
        }
      };

      this.socket.onerror = (error) => {
        console.error('❌ Vision WebSocket error:', error);
      };

      this.socket.onclose = () => {
        console.log('🔌 Vision WebSocket disconnected');
        this.isConnected = false;
        this.monitoringActive = false;
        this.attemptReconnect();
      };

    } catch (error) {
      console.error('Failed to connect to Vision WebSocket:', error);
      this.attemptReconnect();
    }
  }

  handleVisionMessage(data) {
    console.log('👁️ Vision Update:', data.type);

    switch (data.type) {
      case 'initial_state':
        this.handleInitialState(data);
        break;

      case 'workspace_update':
        this.handleWorkspaceUpdate(data);
        break;

      case 'workspace_analysis':
        this.handleWorkspaceAnalysis(data);
        break;

      case 'action_result':
        this.handleActionResult(data);
        break;

      case 'config_updated':
        console.log('⚙️ Config updated:', data);
        this.updateInterval = data.update_interval;
        break;

      default:
        console.log('Unknown vision message type:', data.type);
    }
  }

  handleInitialState(data) {
    console.log('📊 Initial workspace state:', data.workspace);
    this.workspaceData = data.workspace;
    this.monitoringActive = data.monitoring_active;
    this.updateInterval = data.update_interval;

    this.onWorkspaceUpdate({
      type: 'initial',
      workspace: data.workspace,
      timestamp: data.timestamp
    });
  }

  handleWorkspaceUpdate(data) {
    // Check if workspace data exists
    if (!data || !data.workspace) {
      console.warn('Workspace update missing workspace data:', data);
      return;
    }

    console.log(`🔄 Workspace update: ${data.workspace.window_count || 0} windows`);

    this.workspaceData = data.workspace;

    // Process autonomous actions
    if (data.autonomous_actions && data.autonomous_actions.length > 0) {
      this.processAutonomousActions(data.autonomous_actions);
    }

    // Check for important notifications
    if (data.workspace.notification_details) {
      const details = data.workspace.notification_details;
      const totalNotifs = details.badges + details.messages + details.meetings + details.alerts;

      if (totalNotifs > 0) {
        console.log(`📬 Notifications: ${details.badges} badges, ${details.messages} messages, ${details.meetings} meetings, ${details.alerts} alerts`);
      }
    }

    // Notify UI
    this.onWorkspaceUpdate({
      type: 'update',
      workspace: data.workspace,
      autonomousActions: data.autonomous_actions,
      enhancedData: data.enhanced_data,
      queueStatus: data.queue_status,
      timestamp: data.timestamp
    });
  }

  handleWorkspaceAnalysis(data) {
    console.log('🔍 Workspace analysis received:', data.analysis);

    this.onWorkspaceUpdate({
      type: 'analysis',
      analysis: data.analysis,
      timestamp: data.timestamp
    });
  }

  handleActionResult(data) {
    console.log('⚡ Action result:', data);

    this.onActionExecuted({
      success: data.success,
      action: data.action,
      message: data.message
    });
  }

  processAutonomousActions(actions) {
    // Filter actions that don't require permission
    const autoActions = actions.filter(a => !a.requires_permission && a.confidence > 0.8);

    // Add to action queue
    this.actionQueue = [...this.actionQueue, ...autoActions];

    // Process queue
    this.processActionQueue();

    // Notify about actions requiring permission
    const permissionRequired = actions.filter(a => a.requires_permission);
    if (permissionRequired.length > 0) {
      console.log(`🔐 ${permissionRequired.length} actions require permission`);
      // Here you would show permission UI
    }
  }

  async processActionQueue() {
    if (this.actionQueue.length === 0) return;

    const action = this.actionQueue.shift();
    console.log(`🤖 Executing autonomous action: ${action.type}`);

    // Send action execution request
    this.executeAction(action);

    // Process next action after delay
    setTimeout(() => this.processActionQueue(), 1000);
  }

  requestWorkspaceAnalysis() {
    if (this.isConnected && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({
        type: 'request_analysis'
      }));
    }
  }

  setUpdateInterval(interval) {
    if (this.isConnected && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({
        type: 'set_interval',
        interval: interval
      }));
    }
  }

  executeAction(action) {
    if (this.isConnected && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({
        type: 'execute_action',
        action: action
      }));
    }
  }

  attemptReconnect() {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      console.log(`🔄 Attempting to reconnect (${this.reconnectAttempts}/${this.maxReconnectAttempts})...`);

      setTimeout(() => {
        this.connect();
      }, this.reconnectDelay * this.reconnectAttempts);
    } else {
      console.error('❌ Max reconnection attempts reached. Vision system offline.');
    }
  }

  disconnect() {
    if (this.socket) {
      this.socket.close();
      this.socket = null;
      this.isConnected = false;
      this.monitoringActive = false;
    }
  }

  getWorkspaceData() {
    return this.workspaceData;
  }

  isMonitoring() {
    return this.isConnected && this.monitoringActive;
  }

  async startMonitoring() {
    if (this.isConnected && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({
        type: 'start_monitoring'
      }));
      this.monitoringActive = true;
    }
  }

  async stopMonitoring() {
    if (this.isConnected && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({
        type: 'stop_monitoring'
      }));
      this.monitoringActive = false;
    }
  }
}

// Dynamic greeting generators
const getStartupGreeting = () => {
  const hour = new Date().getHours();
  const dayName = new Date().toLocaleDateString('en-US', { weekday: 'long' });

  // Determine time of day
  let timeContext = 'evening';
  if (hour >= 5 && hour < 12) timeContext = 'morning';
  else if (hour >= 12 && hour < 17) timeContext = 'afternoon';
  else if (hour >= 17 && hour < 22) timeContext = 'evening';
  else timeContext = 'night';

  const greetings = {
    morning: [
      "Good morning, Sir. JARVIS systems initialized and ready for your command.",
      "Morning, Sir. All systems operational. How may I assist you today?",
      "Systems online, Sir. Another beautiful " + dayName + " morning to be of service.",
      "Good morning. Neural networks calibrated. At your service.",
      "Rise and shine, Sir. JARVIS fully operational.",
      "Morning protocols complete. Ready to tackle today's challenges."
    ],
    afternoon: [
      "Good afternoon, Sir. JARVIS at your disposal.",
      "Welcome back. Systems online and ready to assist.",
      "Afternoon, Sir. All systems functioning at peak efficiency.",
      "System reactivation complete. How may I help you this " + dayName + " afternoon?",
      "JARVIS systems restored. Ready to continue where we left off."
    ],
    evening: [
      "Good evening, Sir. JARVIS at your service.",
      "Welcome back. Systems online for your evening session.",
      "Evening, Sir. All systems are operational.",
      "System activation complete. How may I be of service tonight?",
      "JARVIS online, Sir. I trust you've had a productive day?"
    ],
    night: [
      "Good evening, Sir. Working late again, I see.",
      "Welcome back. JARVIS systems online despite the late hour.",
      "System activation complete. Ready for your late-night commands.",
      "Late night session initiated. How may I assist you?",
      "Systems operational, Sir. Burning the midnight oil?"
    ]
  };

  // Add some variety with status messages
  const statusMessages = [
    "JARVIS initialization complete. All systems operational.",
    "System boot sequence finished. Ready to serve.",
    "Welcome back, Sir. What can I do for you today?",
    "JARVIS online. All systems nominal.",
    "AI neural pathways synchronized. Ready to proceed.",
    "Voice recognition calibrated. Standing by for your command."
  ];

  // Mix time-based and status-based greetings
  let greetingPool = [...greetings[timeContext]];
  if (Math.random() < 0.3) {
    greetingPool.push(...statusMessages);
  }

  // Weekend special
  if (dayName === 'Saturday' || dayName === 'Sunday') {
    if (Math.random() < 0.3) {
      greetingPool.push(
        "Happy " + dayName + ", Sir. JARVIS ready for your weekend commands.",
        "Weekend systems activated. How may I assist you this " + dayName + "?"
      );
    }
  }

  return greetingPool[Math.floor(Math.random() * greetingPool.length)];
};

/**
 * Dynamic Wake Word Response Generator
 * Context-aware, personality-driven, Phase 4 enhanced
 */
const getWakeWordResponse = (context = {}) => {
  const {
    timeOfDay = null,
    lastInteraction = null,
    proactiveMode = false,
    workspaceContext = null,
    userFocusLevel = 'casual'
  } = context;

  // Determine time context
  const hour = new Date().getHours();
  const timeContext = timeOfDay || (
    hour >= 5 && hour < 12 ? 'morning' :
    hour >= 12 && hour < 17 ? 'afternoon' :
    hour >= 17 && hour < 22 ? 'evening' : 'night'
  );

  // Check if this is a quick re-activation (within 2 minutes)
  const isQuickReturn = lastInteraction && (Date.now() - lastInteraction) < 120000;

  // Build response pools based on context
  const baseResponses = {
    morning: [
      "Yes, Sir. How may I assist you this morning?",
      "Good morning. I'm listening.",
      "At your service, Sir.",
      "Morning. What can I do for you?",
      "Ready and listening, Sir."
    ],
    afternoon: [
      "Yes, Sir. How can I help?",
      "At your service.",
      "I'm here. What do you need?",
      "Listening, Sir.",
      "Ready for your command."
    ],
    evening: [
      "Yes, Sir. How may I assist you this evening?",
      "Good evening. I'm listening.",
      "At your service, Sir.",
      "Evening. What can I do for you?",
      "Ready and standing by."
    ],
    night: [
      "Yes, Sir. Burning the midnight oil?",
      "I'm here. What do you need?",
      "Listening, Sir.",
      "Ready for your command, even at this late hour.",
      "At your service, Sir."
    ]
  };

  // Quick return responses (more casual, no time greeting)
  const quickReturnResponses = [
    "Yes?",
    "I'm here.",
    "Go ahead.",
    "Listening.",
    "Yes, Sir?",
    "What's next?",
    "Ready.",
    "I'm all ears."
  ];

  // Phase 4 Proactive Mode responses (more intelligent, aware)
  const proactiveResponses = [
    "Yes, Sir? I've been monitoring your workspace.",
    "I'm here. I have some suggestions when you're ready.",
    "At your service. I noticed a few patterns worth discussing.",
    "Listening. I've been keeping an eye on things.",
    "Yes? I'm tracking your workflow and ready to optimize."
  ];

  // Focus-aware responses
  const focusAwareResponses = {
    deep_work: [
      "Yes? I'll keep this brief.",
      "I'm here. What do you need?",
      "Listening.",
      "Go ahead - I know you're focused."
    ],
    focused: [
      "Yes, Sir?",
      "I'm listening.",
      "Ready.",
      "What can I do for you?"
    ],
    casual: [
      "Yes, Sir. How may I assist you?",
      "At your service.",
      "What's on your mind?",
      "How can I help?",
      "I'm all ears."
    ],
    idle: [
      "Finally! What can I do for you?",
      "Welcome back. What would you like to tackle?",
      "Yes, Sir. Ready for action.",
      "I'm here. Let's get productive."
    ]
  };

  // Workspace-aware responses
  const workspaceResponses = workspaceContext?.focused_app ? [
    `Yes, Sir? I see you're working in ${workspaceContext.focused_app}.`,
    `I'm here. ${workspaceContext.focused_app} still open?`,
    `At your service. Need help with ${workspaceContext.focused_app}?`
  ] : [];

  // Build final response pool
  let responsePool = [];

  // Priority 1: Quick return (most recent interaction)
  if (isQuickReturn && Math.random() < 0.7) {
    responsePool = quickReturnResponses;
  }
  // Priority 2: Proactive mode (Phase 4 active)
  else if (proactiveMode && Math.random() < 0.5) {
    responsePool = [...proactiveResponses];
  }
  // Priority 3: Focus-aware (deep work gets concise responses)
  else if (userFocusLevel === 'deep_work' || userFocusLevel === 'focused') {
    responsePool = [...focusAwareResponses[userFocusLevel]];
  }
  // Priority 4: Workspace context (if available)
  else if (workspaceResponses.length > 0 && Math.random() < 0.3) {
    responsePool = [...workspaceResponses, ...baseResponses[timeContext]];
  }
  // Priority 5: Standard context-aware responses
  else {
    responsePool = [
      ...baseResponses[timeContext],
      ...(focusAwareResponses[userFocusLevel] || focusAwareResponses.casual)
    ];
  }

  // Add personality variations (20% chance)
  if (Math.random() < 0.2) {
    const personalityVariations = [
      "Systems nominal. How may I help?",
      "Neural pathways ready. What's the task?",
      "All systems operational. Your command?",
      "Ready to optimize your workflow, Sir.",
      "Standing by for instructions."
    ];
    responsePool.push(...personalityVariations);
  }

  // Select and return random response
  return responsePool[Math.floor(Math.random() * responsePool.length)];
};

const JarvisVoice = () => {
  const [isListening, setIsListening] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [jarvisStatus, setJarvisStatus] = useState('initializing'); // Start with 'initializing' instead of 'offline'
  const [systemMode, setSystemMode] = useState('unknown'); // 'minimal' or 'full'
  const [showUpgradeSuccess, setShowUpgradeSuccess] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [response, setResponse] = useState('');
  const [error, setError] = useState(null);
  const [textCommand, setTextCommand] = useState('');
  const [continuousListening, setContinuousListening] = useState(false);
  const [isWaitingForCommand, setIsWaitingForCommand] = useState(false);
  const [isJarvisSpeaking, setIsJarvisSpeaking] = useState(false);
  const [microphonePermission, setMicrophonePermission] = useState('checking');
  const [visionConnected, setVisionConnected] = useState(false);
  const [workspaceData, setWorkspaceData] = useState(null);
  const [autonomousMode, setAutonomousMode] = useState(false);
  const [micStatus, setMicStatus] = useState('unknown');
  const [networkRetries, setNetworkRetries] = useState(0);
  const [maxNetworkRetries] = useState(3);
  const [workflowProgress, setWorkflowProgress] = useState(null);

  // Phase 4: Proactive Intelligence state
  const [proactiveSuggestions, setProactiveSuggestions] = useState([]);
  const [proactiveIntelligenceActive, setProactiveIntelligenceActive] = useState(false);
  const [lastSuggestionTime, setLastSuggestionTime] = useState(null);

  // User interaction state
  const [isTyping, setIsTyping] = useState(false);
  const [lastUserInteraction, setLastUserInteraction] = useState(null);
  const [showSTTDetails, setShowSTTDetails] = useState(true); // Show hybrid STT engine details
  const [useHybridSTT, setUseHybridSTT] = useState(true); // Use hybrid STT instead of browser API
  const [detectedCommand, setDetectedCommand] = useState(null); // 🆕 Command detected by streaming safeguard

  // VBI (Voice Biometric Intelligence) Progress State - Real-time voice unlock tracking
  const [vbiProgress, setVbiProgress] = useState(null); // { stage, stage_name, progress, status, details }
  const [vbiStages, setVbiStages] = useState([]); // History of completed stages for display
  const vbiProgressTimeoutRef = useRef(null); // Auto-hide VBI progress after completion

  // ═══════════════════════════════════════════════════════════════════
  // v8.0: PROACTIVE UNLOCK PROGRESS STATE
  // ═══════════════════════════════════════════════════════════════════
  // Shows visual progress when JARVIS proactively unlocks screen
  // for a command like "search for dogs" while screen is locked
  // ═══════════════════════════════════════════════════════════════════
  const [proactiveUnlockProgress, setProactiveUnlockProgress] = useState(null);
  // { stage, message, originalCommand, continuationIntent, progress, status }
  const proactiveUnlockTimeoutRef = useRef(null);

  const typingTimeoutRef = useRef(null);

  const wsRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioWebSocketRef = useRef(null);
  const offlineModeRef = useRef(false);
  const commandQueueRef = useRef([]);
  const proxyEndpointRef = useRef(null);

  // 🆕 ROBUST COMMAND QUEUE SYSTEM - Never lose commands during WebSocket issues
  const pendingCommandsRef = useRef([]);  // Commands waiting for WebSocket
  const commandQueueProcessingRef = useRef(false);  // Prevent concurrent processing
  const maxQueuedCommands = 10;  // Prevent memory buildup
  const commandQueueTimeoutMs = 30000;  // Max time to wait before discarding

  // 🆕 INTELLIGENT SPEECH RECOVERY - Prevents loops while staying responsive
  const speechRecoveryStateRef = useRef({
    consecutiveAborts: 0,
    lastAbortTime: 0,
    inBackoffMode: false,
    backoffEndTime: 0,
    pendingRestart: false,
    lastSuccessfulRecognition: Date.now(),
    recognitionAttempts: 0,
    commandsDetectedDuringBackoff: []  // Don't lose commands during backoff
  });

  // 🆕 DYNAMIC CONFIGURATION - No hardcoding
  const voiceConfigRef = useRef({
    wakeWords: ['hey jarvis', 'jarvis', 'ok jarvis', 'hello jarvis', 'hey j'],
    wakeWordFuzzyThreshold: 0.7,  // For partial matches like "hey" -> "hey jarvis"
    criticalCommandConfidenceThreshold: 0.40,  // Lower than before for better detection
    normalCommandConfidenceThreshold: 0.65,
    maxAbortBackoffMs: 1500,  // Reduced from 3000ms
    minTimeBetweenRestarts: 100,  // Minimum ms between recognition restarts
    commandTimeoutAfterWakeWord: 45000,  // Longer timeout for command after wake word
    partialMatchPatterns: {
      unlock: ['unlock', 'unloc', 'unlo', 'un lock', 'on lock'],
      lock: ['lock', 'loc', 'log my', 'locking'],
      jarvis: ['jarvis', 'jarv', 'jarv is', 'jar vis', 'j.a.r.v.i.s']
    }
  });
  const recognitionRef = useRef(null);
  const hybridSTTClientRef = useRef(null); // Hybrid STT client (replaces browser SpeechRecognition)
  const visionConnectionRef = useRef(null);
  const lastSpeechTimeRef = useRef(0);
  const speechActiveRef = useRef(false); // Track active speech to prevent restart during recognition
  const wakeWordServiceRef = useRef(null);
  const continuousListeningRef = useRef(false);
  const isWaitingForCommandRef = useRef(false);

  // ============================================================
  // 🔇 SELF-VOICE SUPPRESSION SYSTEM v2.0 (REF-BASED)
  // ============================================================
  // Uses refs for synchronous access - no closure issues
  // Aggressive approach: PAUSE recognition while speaking
  const selfVoiceSuppressionRef = useRef({
    isSpeaking: false,                    // Currently speaking
    speakingEndTime: 0,                   // When speaking ended (for cooldown)
    cooldownMs: 1200,                     // Cooldown after speech (increased from 800)
    lastSpokenText: '',                   // Last spoken text for similarity check
    recentSpokenTexts: [],                // History of recent spoken texts
    maxRecentTexts: 10,                   // Keep more history
    recognitionPausedForSpeech: false,    // Whether we paused recognition for speech
    lastPauseTime: 0,                     // When we paused recognition
  });

  /**
   * 🔇 PAUSE recognition when JARVIS starts speaking
   * This is the most aggressive and reliable way to prevent self-voice feedback
   */
  const pauseRecognitionForSpeech = (textToSpeak) => {
    const suppression = selfVoiceSuppressionRef.current;
    const now = Date.now();

    console.log('🔇 [Self-Voice v2] PAUSING recognition - JARVIS speaking:', textToSpeak?.substring(0, 50));

    suppression.isSpeaking = true;
    suppression.lastSpokenText = textToSpeak?.toLowerCase().trim() || '';
    suppression.lastPauseTime = now;

    // Add to recent spoken texts history
    if (textToSpeak) {
      suppression.recentSpokenTexts.unshift({
        text: textToSpeak.toLowerCase().trim(),
        timestamp: now
      });
      // Keep only recent texts
      if (suppression.recentSpokenTexts.length > suppression.maxRecentTexts) {
        suppression.recentSpokenTexts = suppression.recentSpokenTexts.slice(0, suppression.maxRecentTexts);
      }
    }

    // Actually stop the recognition to prevent any input
    if (recognitionRef.current) {
      try {
        suppression.recognitionPausedForSpeech = true;
        recognitionRef.current.abort();
        console.log('🔇 [Self-Voice v2] Recognition ABORTED');
      } catch (e) {
        console.log('🔇 [Self-Voice v2] Recognition abort failed (may already be stopped):', e.message);
      }
    }
  };

  /**
   * 🔇 RESUME recognition after JARVIS finishes speaking
   * Includes a cooldown delay to handle audio echo/latency
   */
  const resumeRecognitionAfterSpeech = () => {
    const suppression = selfVoiceSuppressionRef.current;
    const now = Date.now();

    console.log('🔇 [Self-Voice v2] Speech ended, starting cooldown...');

    suppression.isSpeaking = false;
    suppression.speakingEndTime = now;

    // Resume recognition after cooldown
    setTimeout(() => {
      if (!suppression.isSpeaking) { // Only resume if not speaking again
        console.log(`🔇 [Self-Voice v2] Cooldown complete (${suppression.cooldownMs}ms), resuming recognition`);
        suppression.recognitionPausedForSpeech = false;

        // Restart recognition
        if (recognitionRef.current && continuousListeningRef.current) {
          try {
            recognitionRef.current.start();
            console.log('🔇 [Self-Voice v2] Recognition RESUMED');
          } catch (e) {
            console.log('🔇 [Self-Voice v2] Recognition restart failed:', e.message);
            // It might already be running, that's ok
          }
        }
      } else {
        console.log('🔇 [Self-Voice v2] Still speaking, skipping resume');
      }
    }, suppression.cooldownMs);
  };

  /**
   * 🔇 Check if recognized text should be blocked (REF-BASED for synchronous access)
   * v8.0: Now ALSO checks backend UnifiedSpeechStateManager via WebSocket
   * This is a backup check in case recognition wasn't paused properly
   */
  const shouldBlockSelfVoice = (recognizedText) => {
    const suppression = selfVoiceSuppressionRef.current;
    const now = Date.now();
    const text = recognizedText?.toLowerCase().trim() || '';

    // =========================================================================
    // v8.0: PRIORITY CHECK - Backend Unified Speech State
    // =========================================================================
    // This is the most reliable source of truth because the backend knows
    // EXACTLY when TTS is happening and manages a global cooldown.
    // =========================================================================
    try {
      const { getUnifiedWebSocketService } = require('../services/UnifiedWebSocketService');
      const wsService = getUnifiedWebSocketService();
      if (wsService && wsService.speechState) {
        const backendState = wsService.speechState;
        
        // Backend says speaking - trust it absolutely
        if (backendState.isSpeaking) {
          console.log('🔇 [Self-Voice v8] BLOCKED by backend: JARVIS speaking');
          return { block: true, reason: 'backend_speaking' };
        }
        
        // Backend says in cooldown - trust it
        if (backendState.inCooldown && backendState.cooldownRemainingMs > 0) {
          console.log(`🔇 [Self-Voice v8] BLOCKED by backend: cooldown ${backendState.cooldownRemainingMs}ms`);
          return { block: true, reason: `backend_cooldown_${backendState.cooldownRemainingMs}ms` };
        }
        
        // Check text similarity against backend's last spoken text
        if (backendState.lastSpokenText && text) {
          const similarity = calculateTextSimilarityQuick(text, backendState.lastSpokenText.toLowerCase());
          if (similarity > 0.5) {
            console.log(`🔇 [Self-Voice v8] BLOCKED by backend similarity: ${(similarity * 100).toFixed(0)}%`);
            return { block: true, reason: `backend_similarity_${(similarity * 100).toFixed(0)}%` };
          }
        }
      }
    } catch (e) {
      // WebSocket service not available - fall back to local checks
      console.debug('[Self-Voice v8] Backend check unavailable, using local checks');
    }

    // =========================================================================
    // LOCAL CHECKS (Fallback/Additional Layer)
    // =========================================================================
    
    // Check 1: Currently speaking (local state)
    if (suppression.isSpeaking) {
      return { block: true, reason: 'currently_speaking' };
    }

    // Check 2: Within cooldown period (local state)
    const timeSinceEnd = now - suppression.speakingEndTime;
    if (timeSinceEnd < suppression.cooldownMs && suppression.speakingEndTime > 0) {
      return { block: true, reason: `cooldown_${timeSinceEnd}ms` };
    }

    // Check 3: Text similarity with last spoken (local state)
    if (suppression.lastSpokenText && text) {
      const similarity = calculateTextSimilarityQuick(text, suppression.lastSpokenText);
      if (similarity > 0.5) {
        return { block: true, reason: `similarity_${(similarity * 100).toFixed(0)}%` };
      }
    }

    // Check 4: Match against recent spoken texts (local state)
    for (const recent of suppression.recentSpokenTexts) {
      const age = now - recent.timestamp;
      if (age < 15000) { // Only check texts from last 15 seconds
        const similarity = calculateTextSimilarityQuick(text, recent.text);
        if (similarity > 0.4) {
          return { block: true, reason: `recent_match_${age}ms_${(similarity * 100).toFixed(0)}%` };
        }
      }
    }

    return { block: false };
  };

  /**
   * Quick text similarity calculation (word-based)
   */
  const calculateTextSimilarityQuick = (text1, text2) => {
    if (!text1 || !text2) return 0;
    if (text1 === text2) return 1.0;

    // Check substring containment
    if (text1.includes(text2) || text2.includes(text1)) {
      return 0.85;
    }

    // Word-based similarity
    const words1 = text1.split(/\s+/).filter(w => w.length > 2);
    const words2 = text2.split(/\s+/).filter(w => w.length > 2);

    if (words1.length === 0 || words2.length === 0) return 0;

    const commonWords = words1.filter(w => words2.some(w2 => w2.includes(w) || w.includes(w2)));
    return (2 * commonWords.length) / (words1.length + words2.length);
  };

  // Circuit breaker for restart loop prevention
  // v3.0: Much more tolerant thresholds for continuous voice unlock flow
  const restartCircuitBreakerRef = useRef({
    count: 0,
    lastReset: Date.now(),
    threshold: 20,  // Increased from 10 - voice unlock with continuous buffer needs more headroom
    windowMs: 30000,  // Increased from 15s - give much more time for recovery
    consecutiveFailures: 0,
    lastSuccessTime: Date.now()
  });

  // Track if we should skip the next restart (e.g., due to aborted error)
  const skipNextRestartRef = useRef(false);

  // 🎤 Unified Voice Capture - Records audio while browser SpeechRecognition runs
  const voiceAudioStreamRef = useRef(null); // Audio stream for voice biometrics
  const voiceAudioRecorderRef = useRef(null); // MediaRecorder for continuous audio capture
  const voiceAudioChunksRef = useRef([]); // Audio chunks buffer
  const isRecordingVoiceRef = useRef(false); // Track recording state

  // 🆕 Continuous Audio Buffer - Pre-captures audio to eliminate first-attempt misses
  const continuousAudioBufferRef = useRef(null);

  // API URLs are defined globally at the top of the file
  // Ensure consistent WebSocket URL (fix port mismatch)
  const JARVIS_WS_URL = WS_URL;  // Use same base URL as API

  // 🆕 JarvisConnectionService integration - handles all connection state
  const jarvisConnectionServiceRef = useRef(null);

  useEffect(() => {
    // Initialize the unified connection service
    const connectionService = getJarvisConnectionService();
    jarvisConnectionServiceRef.current = connectionService;

    // Subscribe to connection state changes
    const unsubscribeState = connectionService.on('stateChange', ({ state: newState }) => {
      console.log('[JarvisVoice] Connection state changed:', newState);
      
      // Map connection state to jarvisStatus
      const newStatus = connectionStateToJarvisStatus(newState);
      setJarvisStatus(newStatus);
      
      // If we just came online, ensure WebSocket ref is updated
      if (newState === ConnectionState.ONLINE) {
        const ws = connectionService.getWebSocket();
        if (ws && (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)) {
          console.log('[JarvisVoice] Updating wsRef from connection service');
          wsRef.current = ws;
        }
        setError(null);
      }
    });

    const unsubscribeMode = connectionService.on('modeChange', ({ mode }) => {
      console.log('[JarvisVoice] Backend mode changed:', mode);
      setSystemMode(mode);
      
      if (mode === 'full') {
        // Show upgrade success banner
        setShowUpgradeSuccess(true);
        setTimeout(() => setShowUpgradeSuccess(false), 10000);
      }
    });

    // Subscribe to ALL messages via the connection service.
    // CRITICAL: Do NOT overwrite ws.onmessage on the shared socket (it is owned by DynamicWebSocketClient).
    // Overwriting it breaks heartbeat + routing and causes reconnect loops/offline state.
    const unsubscribeMessage = connectionService.on('message', ({ data }) => {
      handleWebSocketMessage(data);
    });

    // Get initial state
    const initialState = connectionService.getState();
    setJarvisStatus(connectionStateToJarvisStatus(initialState));
    setSystemMode(connectionService.getMode());

    // If already online, update wsRef
    if (initialState === ConnectionState.ONLINE) {
      const ws = connectionService.getWebSocket();
      if (ws) {
        wsRef.current = ws;
      }
    }

    return () => {
      unsubscribeState();
      unsubscribeMode();
      unsubscribeMessage();
    };
  }, []);

  // 🎨 Dynamic Favicon - Changes based on JARVIS state
  useEffect(() => {
    // Initialize the dynamic favicon system on mount
    initDynamicFavicon();
    console.log('🎨 [Favicon] Dynamic favicon system initialized');
  }, []);

  // Update favicon based on processing/listening state
  useEffect(() => {
    if (isProcessing) {
      setFaviconState('processing');
    } else if (isWaitingForCommand || isListening) {
      setFaviconState('listening');
    } else {
      setFaviconState('idle');
    }
  }, [isProcessing, isWaitingForCommand, isListening]);

  useEffect(() => {
    // Preload voices to ensure Daniel is available
    if ('speechSynthesis' in window) {
      // Force load voices
      window.speechSynthesis.getVoices();

      // Listen for voices to be loaded
      window.speechSynthesis.onvoiceschanged = () => {
        const voices = window.speechSynthesis.getVoices();
        const danielVoice = voices.find(v => v.name.includes('Daniel'));
        if (danielVoice) {
          console.log('✅ Daniel voice preloaded:', danielVoice.name);
        }
      };
    }

    // Auto-activate JARVIS on mount for seamless wake word experience
    const autoActivate = async () => {
      // Wait for config to be ready before making any API calls
      await configPromise;
      console.log('JarvisVoice: Config ready, initializing...');

      // CRITICAL: Connect WebSocket FIRST before doing anything else
      console.log('🔌 Connecting WebSocket on component mount...');
      connectWebSocket();

      await checkJarvisStatus();
      await checkMicrophonePermission();
      await initializeWakeWordService();
    };

    autoActivate();

    // Predict potential audio issues - disabled to prevent CORS errors
    // mlAudioHandler.predictAudioIssue();

    // Inject style to ensure button visibility
    const styleElement = document.createElement('style');
    styleElement.textContent = buttonVisibilityStyle;
    document.head.appendChild(styleElement);

    // Set up ML event listeners
    const handleAudioPrediction = (event) => {
      const { prediction, suggestedAction } = event.detail;
      if (prediction.probability > 0.7) {
        console.warn('High probability of audio issue:', prediction);
        // Take proactive action
        if (suggestedAction === 'preemptive_permission_check') {
          checkMicrophonePermission();
        }
      }
    };

    const handleAudioAnomaly = (event) => {
      console.warn('Audio anomaly detected:', event.detail);
      setError('Audio anomaly detected. System is adapting...');
    };

    const handleAudioMetrics = (event) => {
      console.log('Audio metrics update:', event.detail);
    };

    const handleTextFallback = (event) => {
      console.log('Enabling text fallback mode');
      // Focus on text input
      const textInput = document.querySelector('.voice-input input');
      if (textInput) {
        textInput.focus();
        textInput.placeholder = 'Voice unavailable - type your command here...';
      }
    };

    // Add ML event listeners
    window.addEventListener('audioIssuePredicted', handleAudioPrediction);
    window.addEventListener('audioAnomaly', handleAudioAnomaly);
    window.addEventListener('audioMetricsUpdate', handleAudioMetrics);
    window.addEventListener('enableTextFallback', handleTextFallback);

    // Add emergency activate listener
    const handleEmergencyActivate = () => {
      console.log('Emergency activate event received');
      activateJarvis();
    };
    window.addEventListener('jarvis-emergency-activate', handleEmergencyActivate);


    // Initialize Vision Connection
    if (!visionConnectionRef.current) {
      visionConnectionRef.current = new VisionConnection(
        // Workspace update callback
        (data) => {
          setWorkspaceData(data);
          setVisionConnected(true);

          // Process workspace updates
          if (data.type === 'update' && data.workspace) {
            // Check for important notifications - NO AUDIO FEEDBACK (removed to prevent feedback loops)
            if (data.workspace.notifications && data.workspace.notifications.length > 0) {
              const notification = data.workspace.notifications[0];
              console.log(`📢 Notification: ${notification}`);
            }

            // Handle autonomous actions - NO AUDIO FEEDBACK (removed to prevent feedback loops)
            if (data.autonomousActions && data.autonomousActions.length > 0 && autonomousMode) {
              const highPriorityActions = data.autonomousActions.filter(a =>
                a.priority === 'HIGH' || a.priority === 'CRITICAL'
              );

              if (highPriorityActions.length > 0) {
                const action = highPriorityActions[0];
                console.log(`🎯 Action: ${action.type.replace(/_/g, ' ')} for ${action.target}`);
              }
            }

            // Check queue status
            if (data.queueStatus && data.queueStatus.queue_length > 0) {
              console.log(`📋 Action Queue: ${data.queueStatus.queue_length} actions pending`);
            }
          }
        },
        // Action executed callback - NO AUDIO FEEDBACK (removed to prevent feedback loops)
        (result) => {
          console.log('Action executed:', result);
          if (!result.success) {
            console.error(`❌ Issue encountered: ${result.message}`);
          }
        }
      );
    }

    return () => {
      // Stop health monitoring
      stopHealthMonitoring();

      // Clean up WebSocket
      if (wsRef.current) {
        console.log('[WS-CLEANUP] Closing WebSocket connection');
        try {
          if (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING) {
            wsRef.current.close();
          }
        } catch (e) {
          console.log('[WS-CLEANUP] Error closing WebSocket:', e);
        }
        wsRef.current = null;
      }

      // Stop speech recognition
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
      // Disconnect vision
      if (visionConnectionRef.current) {
        visionConnectionRef.current.disconnect();
      }
      // Cleanup wake word service
      if (wakeWordServiceRef.current && typeof wakeWordServiceRef.current.disconnect === 'function') {
        wakeWordServiceRef.current.disconnect();
      }
      // 🆕 Cleanup continuous audio buffer
      if (continuousAudioBufferRef.current) {
        console.log('[ContinuousBuffer] Stopping on component unmount');
        continuousAudioBufferRef.current.stop();
        continuousAudioBufferRef.current = null;
      }
      // Remove ML event listeners
      window.removeEventListener('audioIssuePredicted', handleAudioPrediction);
      window.removeEventListener('audioAnomaly', handleAudioAnomaly);
      window.removeEventListener('audioMetricsUpdate', handleAudioMetrics);
      window.removeEventListener('enableTextFallback', handleTextFallback);

      // Remove emergency activate listener
      window.removeEventListener('jarvis-emergency-activate', handleEmergencyActivate);

      // Clear button checker interval
      // if (checkButtonsInterval) {
      //   clearInterval(checkButtonsInterval);
      // }

      // Remove injected style
      if (styleElement && styleElement.parentNode) {
        styleElement.parentNode.removeChild(styleElement);
      }
    };
  }, []);

  // Subscribe to backend state for real-time status synchronization
  useEffect(() => {
    const handleBackendState = (state) => {
      console.log('[JarvisVoice] Backend state change:', state);

      // Sync jarvisStatus with backend ready state
      if (state.ready && jarvisStatus !== 'online') {
        console.log('[JarvisVoice] Backend ready - setting status to online');
        setJarvisStatus('online');
      }
    };

    // Register listener for backend state changes
    backendStateListeners.push(handleBackendState);

    // Also subscribe to startup progress for transparency
    const unsubscribe = configService.on('startup-progress', (progress) => {
      console.log(`[JarvisVoice] Startup: ${progress.progress}% - ${progress.message}`);
    });

    // Listen for jarvis-startup-state custom event (from ConfigAwareStartup)
    const handleStartupState = (event) => {
      const state = event.detail;
      console.log('[JarvisVoice] Startup state event:', state);

      if (state.phase === 'ready' && jarvisStatus !== 'online') {
        setJarvisStatus('online');
      }
    };

    window.addEventListener('jarvis-startup-state', handleStartupState);

    return () => {
      // Remove from listeners
      const index = backendStateListeners.indexOf(handleBackendState);
      if (index > -1) backendStateListeners.splice(index, 1);

      if (typeof unsubscribe === 'function') unsubscribe();
      window.removeEventListener('jarvis-startup-state', handleStartupState);
    };
  }, [jarvisStatus]);

  // Separate effect for auto-activation and wake word setup
  useEffect(() => {
    // Don't auto-activate when offline/reconnecting - connection service handles that
    // Only enable wake word when actually online
    if (jarvisStatus === 'online' && !continuousListening) {
      // Enable wake word detection when JARVIS comes online
      console.log('JARVIS online, enabling wake word detection');
      // Small delay to ensure speech recognition is initialized
      const timer = setTimeout(() => {
        if (recognitionRef.current) {
          enableContinuousListening();
        } else {
          console.log('Speech recognition not initialized yet, retrying...');
          initializeSpeechRecognition();
          setTimeout(() => enableContinuousListening(), 1000);
        }
      }, 1000);
      return () => clearTimeout(timer);
    }
  }, [jarvisStatus, continuousListening]);

  const checkJarvisStatus = async () => {
    // Ensure config is ready
    if (!configReady || !API_URL) {
      console.log('JarvisVoice: Waiting for config before checking status...');
      await configPromise;
    }

    try {
      // Get API URL dynamically - no hardcoding
      const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
      console.log('[JarvisVoice] Checking JARVIS status at:', apiUrl);
      const response = await fetch(`${apiUrl}/voice/jarvis/status`);
      const data = await response.json();

      // Enhanced logging for mode detection
      const previousMode = systemMode;
      const previousStatus = jarvisStatus;

      // Auto-refresh when JARVIS becomes fully ready and startup announcement is complete
      // This ensures the UI is fully synced with backend state
      if (previousStatus !== 'online' &&
          (data.status === 'online' || data.status === 'ready' || data.status === 'available') &&
          data.startup_announced === true &&
          data.mode !== 'minimal') {

        console.log('🔄 JARVIS is fully ready with announcement complete! Auto-refreshing page...');

        // Show countdown notification before refresh
        setResponse('✅ JARVIS is fully operational! Refreshing page in 3...');

        setTimeout(() => {
          setResponse('Refreshing in 2...');
          setTimeout(() => {
            setResponse('Refreshing in 1...');
            setTimeout(() => {
              console.log('🔄 Hard refresh triggered');
              window.location.reload(true); // Hard refresh to ensure all components load properly
            }, 1000);
          }, 1000);
        }, 1000);

        return; // Exit early to prevent further processing during countdown
      }

      if (data.mode === 'minimal') {
        console.log('🔄 JARVIS Status: Running in MINIMAL MODE');
        console.log('  ⏳ This is temporary while full system initializes');
        console.log('  📊 Available features:', {
          voice: data.components?.voice || false,
          vision: data.components?.vision || false,
          memory: data.components?.memory || false,
          tools: data.components?.tools || false,
          rust: data.components?.rust || false
        });
        if (data.upgrader) {
          console.log('  🚀 Upgrade Progress:', {
            monitoring: data.upgrader.monitoring,
            attempts: `${data.upgrader.attempts}/${data.upgrader.max_attempts}`,
            status: 'Waiting for components to initialize...'
          });
        }
        console.log('  ✅ Basic voice commands are available');
        console.log('  ⚠️  Advanced features (wake word, ML audio) temporarily unavailable');
        setSystemMode('minimal');
      } else {
        // Full mode detected!
        if (previousMode === 'minimal') {
          console.log('🎉 JARVIS UPGRADED TO FULL MODE! 🎉');
          console.log('  ✅ All features now available:');
          console.log('    • Wake word detection ("Hey JARVIS")');
          console.log('    • ML-powered audio processing');
          console.log('    • Vision system active');
          console.log('    • Memory system online');
          console.log('    • Advanced tools enabled');
          console.log('  🚀 System running at full capacity!');

          // Show success message to user - NO AUDIO FEEDBACK (removed to prevent feedback loops)
          setResponse('System upgraded! All features are now available.');

          // Show upgrade success banner
          setShowUpgradeSuccess(true);
          setTimeout(() => setShowUpgradeSuccess(false), 10000); // Hide after 10 seconds
        } else {
          console.log('✅ JARVIS Status: Running in FULL MODE');
          console.log('  🚀 All systems operational');
        }
        setSystemMode('full');
      }

      // Map backend status to frontend status
      const status = data.status || 'offline';
      if (status === 'standby' || status === 'ready' || status === 'available') {
        setJarvisStatus('online'); // Show as online when in standby, ready, or available
      } else {
        setJarvisStatus(status);
      }

      // Connect WebSocket if JARVIS is available (including standby, ready, active, and available)
      if (data.status === 'online' || data.status === 'standby' || data.status === 'active' || data.status === 'ready' || data.status === 'available') {
        if (data.mode === 'minimal') {
          console.log('📡 Minimal mode: WebSocket features limited');
        } else {
          console.log('JARVIS is available, connecting WebSocket...');
        }
        setTimeout(() => {
          connectWebSocket();
        }, 500);
      } else {
        console.log('JARVIS is offline, not connecting WebSocket');
      }
    } catch (err) {
      console.error('Failed to check JARVIS status:', err);
      console.log('Setting status to offline due to error');
      setJarvisStatus('offline');
    }
  };

  const checkMicrophonePermission = async () => {
    try {
      // Check if browser supports speech recognition
      if (!('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)) {
        setMicrophonePermission('unsupported');
        setError('Speech recognition not supported in this browser');
        return;
      }

      // Try to get microphone access
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => track.stop());

      setMicrophonePermission('granted');
      setMicStatus('ready');
      // Initialize speech recognition after permission granted
      initializeSpeechRecognition();
    } catch (error) {
      console.error('Microphone permission error:', error);
      if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
        setMicrophonePermission('denied');
        setMicStatus('error');
        setError('Microphone access denied. Please grant permission to use JARVIS.');
      } else if (error.name === 'NotFoundError') {
        setMicrophonePermission('no-device');
        setMicStatus('error');
        setError('No microphone found. Please connect a microphone.');
      } else {
        setMicrophonePermission('error');
        setMicStatus('error');
        setError('Error accessing microphone: ' + error.message);
      }
    }
  };

  // Advanced WebSocket reconnection state
  const reconnectionStateRef = useRef({
    attempts: 0,
    maxAttempts: 10,
    baseDelay: 1000,
    maxDelay: 30000,
    backoffMultiplier: 1.5,
    pingInterval: null,
    healthCheckInterval: null,
    lastPingTime: null,
    latency: 0,
    connectionHealth: 100,
    reconnecting: false
  });

  const connectWebSocket = async () => {
    // ==========================================================================
    // SINGLE SOURCE OF TRUTH: JarvisConnectionService
    // ==========================================================================
    // CRITICAL: Do NOT create competing WebSocket connections here.
    // JarvisConnectionService (via DynamicWebSocketClient) is the ONLY WebSocket manager.
    // This function only delegates to the service - it never creates its own socket.
    // Messages are received via the 'message' event subscription in the useEffect above.
    // ==========================================================================
    
    if (!jarvisConnectionServiceRef.current) {
      console.log('[WS] Waiting for JarvisConnectionService to initialize...');
      setJarvisStatus('connecting');
      return;
    }
    
    const connectionService = jarvisConnectionServiceRef.current;
    const currentState = connectionService.getState();
    
    // If already connected, just update local state
    if (connectionService.isConnected()) {
      const ws = connectionService.getWebSocket();
      if (ws && ws.readyState === WebSocket.OPEN) {
        console.log('[WS] ✅ Using existing connection from JarvisConnectionService');
        wsRef.current = ws;
        setJarvisStatus('online');
        setError(null);
        
        // Initialize Hybrid STT Client if enabled and not already initialized
        if (useHybridSTT && !hybridSTTClientRef.current) {
          try {
            hybridSTTClientRef.current = new HybridSTTClient(ws, {
              strategy: 'balanced',
              speakerName: null,
              confidenceThreshold: 0.6,
              continuous: true,
              interimResults: true
            });
            console.log('🎤 [HybridSTT] Client initialized with shared WebSocket');
          } catch (error) {
            console.error('🎤 [HybridSTT] Failed to initialize:', error);
          }
        }
        
        // Process any queued commands
        if (pendingCommandsRef.current.length > 0) {
          console.log(`[WS] 📬 Processing ${pendingCommandsRef.current.length} queued command(s)...`);
          setTimeout(() => processCommandQueue(), 200);
        }
        return;
      }
    }
    
    // If service is offline or errored, trigger reconnect
    if (currentState === ConnectionState.OFFLINE || currentState === ConnectionState.ERROR) {
      console.log('[WS] Service offline/error - triggering reconnect via JarvisConnectionService');
      setJarvisStatus('connecting');
      connectionService.reconnect();
      return;
    }
    
    // If service is in any connecting state, just wait (it will emit stateChange when done)
    if (currentState === ConnectionState.CONNECTING || 
        currentState === ConnectionState.RECONNECTING ||
        currentState === ConnectionState.DISCOVERING ||
        currentState === ConnectionState.INITIALIZING) {
      console.log(`[WS] JarvisConnectionService is ${currentState}, waiting...`);
      setJarvisStatus('connecting');
      return;
    }
    
    // Fallback: if we get here with an unknown state, trigger reconnect
    console.log(`[WS] Unknown service state (${currentState}), triggering reconnect`);
    setJarvisStatus('connecting');
    connectionService.reconnect();
  };

  // NOTE: Health monitoring is now handled by JarvisConnectionService/DynamicWebSocketClient.
  // These functions are kept as no-ops for backwards compatibility.
  const startHealthMonitoring = () => {
    // Health monitoring delegated to JarvisConnectionService
  };

  const stopHealthMonitoring = () => {
    // Health monitoring delegated to JarvisConnectionService
  };

  const handleWebSocketMessage = (data) => {
    switch (data.type) {
      case 'connected':
        setJarvisStatus('online');
        break;
      case 'connection_established':
        // Handle advanced connection established with features
        console.log('[WS-ADVANCED] Connection established with features:', data.features);
        setJarvisStatus('online');
        break;
      case 'pong':
        // Handle pong response - calculate latency
        if (reconnectionStateRef.current.lastPingTime) {
          const latency = Date.now() - data.timestamp;
          reconnectionStateRef.current.latency = latency;
          console.log(`[WS-HEALTH] Pong received - latency: ${latency}ms`);

          // Improve health score based on latency
          if (latency < 100) {
            reconnectionStateRef.current.connectionHealth = Math.min(100, reconnectionStateRef.current.connectionHealth + 5);
          } else if (latency > 500) {
            reconnectionStateRef.current.connectionHealth = Math.max(0, reconnectionStateRef.current.connectionHealth - 5);
          }
        }
        break;
      case 'pong_ack':
        // Handle pong acknowledgment with latency from backend
        if (data.latency_ms !== undefined) {
          reconnectionStateRef.current.latency = data.latency_ms;
          console.log(`[WS-HEALTH] Backend latency: ${data.latency_ms}ms`);
        }
        break;
      case 'connection_health':
        // Handle connection health updates from backend
        console.log(`[WS-HEALTH] Backend health status: ${data.state}, score: ${data.health_score}`);
        if (data.state === 'degraded') {
          console.warn('[WS-HEALTH] ⚠️ Backend reports degraded connection');
          setError('Connection unstable - backend recovery in progress...');
        }
        break;
      case 'reconnection_advisory':
        // Backend is advising us to prepare for reconnection
        console.warn('[WS-HEALTH] 🔮 Backend predicts connection issues:', data.message);
        setError(data.message || 'Connection optimization in progress...');
        break;
      case 'connection_optimization':
        // Backend is requesting optimization (reduce load)
        console.log('[WS-HEALTH] Backend requesting optimization:', data.action);
        if (data.action === 'reduce_load') {
          // We could reduce message frequency here if needed
          console.log('[WS-HEALTH] Reducing message load as requested');
        }
        break;
      case 'system_status':
        // System-wide status updates (e.g., circuit breaker)
        console.log('[WS-ADVANCED] System status:', data.status, '-', data.message);
        if (data.status === 'degraded') {
          setError(`System: ${data.message}`);
        }
        break;
      case 'system_shutdown':
        // Backend is shutting down gracefully
        console.log('[WS-ADVANCED] 🛑 Backend shutdown notification received');
        setJarvisStatus('offline');
        setError('JARVIS backend is shutting down. System will reconnect automatically if backend restarts.');
        setResponse('Backend shutting down...');
        // Close current connection gracefully
        if (wsRef.current) {
          wsRef.current.close();
        }
        break;
      case 'display_detected':
        // Handle display monitor notifications
        console.log('🖥️ Display detected:', data);
        const displayMessage = data.message || `${data.display_name} is now available`;
        setResponse(displayMessage);
        // Speak the message using JARVIS voice
        if (data.message) {
          speakResponse(data.message, false);
        }
        break;
      case 'processing':
        setIsProcessing(true);
        // Don't cancel speech here - it might cancel the wake word response
        break;

      case 'vbi_progress':
        // Handle real-time VBI (Voice Biometric Intelligence) progress updates
        // Supports both Cloud-First format and legacy format
        const vbiStage = data.stage || data.stage_name || 'unknown';
        const vbiMessage = data.message || data.status || '';
        const vbiProgress = data.progress || 0;
        
        // Get stage icon based on stage name
        const getStageIcon = (stage) => {
          const icons = {
            'initializing': '⚡',
            'cache_check': '🔍',
            'cache_hit': '✨',
            'routing': '🌐',
            'parallel_dispatch': '🚀',
            'extracting': '🎤',
            'parallel_wait': '⏳',
            'results_processing': '🔄',
            'voice_complete': '✓',
            'early_exit': '⚡',
            'timeout_fallback': '⏱️',
            'fusion': '🔗',
            'complete': '✅',
            'error': '❌'
          };
          return icons[stage] || data.stage_icon || '🔧';
        };
        
        console.log('%c[VBI Progress]', 'color: #00bfff; font-weight: bold',
          `${vbiStage} (${vbiProgress}%) - ${vbiMessage}`);

        // Determine if verification was successful based on available data
        // Success indicators: explicit success flag, no error with 100% progress, stage is 'complete' with speaker
        const hasExplicitSuccess = data.success === true || data.status === 'success';
        const hasError = !!data.error || data.success === false || data.status === 'failed' || data.status === 'error';
        const isCompleteWithSpeaker = vbiStage === 'complete' && (data.speaker || data.confidence > 0);
        const isSuccessfulCompletion = vbiProgress >= 100 && !hasError;
        
        // Determine overall success status
        const isSuccess = hasExplicitSuccess || (isCompleteWithSpeaker && !hasError) || (isSuccessfulCompletion && (data.speaker || data.confidence));
        
        // Determine status for display (success/failed/in_progress)
        const displayStatus = hasError ? 'failed' : isSuccess ? 'success' : vbiProgress < 100 ? 'in_progress' : 'success';

        // Update VBI progress state with normalized data
        setVbiProgress({
          stage: vbiStage,
          stageName: data.stage_name || vbiStage.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
          stageIcon: getStageIcon(vbiStage),
          progress: vbiProgress,
          status: displayStatus,  // Use computed status ('success', 'failed', 'in_progress')
          statusMessage: vbiMessage,  // Keep the original message separately
          message: vbiMessage,
          details: data.details || {},
          error: data.error,
          traceId: data.trace_id,
          timestamp: data.timestamp,
          isSuccess: isSuccess,  // Explicit boolean for success state
          // Cloud-First specific fields
          confidence: data.confidence,
          speaker: data.speaker,
          cloudAvailable: data.cloud_available,
          cached: data.cached
        });

        // Replace "Processing..." with actual progress message
        if (vbiProgress > 0 && vbiProgress < 100) {
          setResponse(vbiMessage || `Verifying voice... ${vbiProgress}%`);
        }

        // Add to stages history when progress changes significantly
        const progressThresholds = [25, 50, 75, 100];
        const shouldAddStage = progressThresholds.includes(vbiProgress) || 
                               vbiStage === 'complete' || 
                               displayStatus === 'success' || 
                               displayStatus === 'failed';
        
        if (shouldAddStage) {
          setVbiStages(prevStages => {
            // Avoid duplicates
            if (prevStages.some(s => s.stage === vbiStage && s.progress === vbiProgress)) {
              return prevStages;
            }
            return [...prevStages, {
              stage: vbiStage,
              stageName: data.stage_name || vbiStage,
              stageIcon: getStageIcon(vbiStage),
              status: displayStatus,  // Use computed status for history too
              statusMessage: vbiMessage,
              progress: vbiProgress,
              details: data.details || {},
              error: data.error,
              confidence: data.confidence,
              speaker: data.speaker,
              isSuccess: isSuccess
            }];
          });
        }

        // Auto-clear progress after completion
        if (vbiStage === 'complete' || vbiProgress >= 100) {
          // Clear any existing timeout
          if (vbiProgressTimeoutRef.current) {
            clearTimeout(vbiProgressTimeoutRef.current);
          }
          // Auto-hide after 5 seconds
          vbiProgressTimeoutRef.current = setTimeout(() => {
            setVbiProgress(null);
            setVbiStages([]);
          }, 5000);
        }
        break;

      case 'voice_unlock':
        // Handle voice unlock responses
        console.log('Voice unlock response received:', data);

        // 🔍 VBI TRACE DISPLAY - Show detailed Voice Biometric Intelligence trace data
        if (data.vbi_trace) {
          const trace = data.vbi_trace;
          console.log('%c═══════════════════════════════════════════════════════════════════════════════', 'color: #00ff88; font-weight: bold');
          console.log('%c📊 VBI TRACE: ' + (trace.trace_id || 'unknown'), 'color: #00ff88; font-size: 14px; font-weight: bold');
          console.log('%c═══════════════════════════════════════════════════════════════════════════════', 'color: #00ff88; font-weight: bold');

          // Overall status
          console.log(`%cStatus: ${trace.status || 'unknown'} | Duration: ${(trace.total_duration_ms || 0).toFixed(1)}ms`,
            trace.status === 'success' ? 'color: #00ff88' : 'color: #ff6b6b');

          // Speaker info
          if (trace.speaker_name) {
            console.log(`%c👤 Speaker: ${trace.speaker_name} (Confidence: ${((trace.confidence || 0) * 100).toFixed(1)}%)`,
              'color: #00bfff; font-weight: bold');
          }

          // Step-by-step breakdown
          if (trace.steps && trace.steps.length > 0) {
            console.log('%c\n📋 PIPELINE STEPS:', 'color: #ffa500; font-weight: bold');
            trace.steps.forEach((step, idx) => {
              const statusIcon = step.status === 'success' ? '✅' : step.status === 'error' ? '❌' : step.status === 'skipped' ? '⏭️' : '⏳';
              const duration = step.duration_ms ? `${step.duration_ms.toFixed(1)}ms` : 'N/A';
              const color = step.status === 'success' ? 'color: #00ff88' : step.status === 'error' ? 'color: #ff6b6b' : 'color: #888';

              console.log(`%c  ${idx + 1}. ${statusIcon} ${step.step_name || 'unnamed'} [${duration}]`, color);

              // Show step details if present
              if (step.details) {
                Object.entries(step.details).forEach(([key, value]) => {
                  const displayValue = typeof value === 'object' ? JSON.stringify(value) : value;
                  console.log(`%c      └─ ${key}: ${displayValue}`, 'color: #aaa');
                });
              }

              // Show error message if present
              if (step.error) {
                console.log(`%c      └─ ERROR: ${step.error}`, 'color: #ff6b6b; font-weight: bold');
              }
            });
          }

          // Metadata summary
          if (trace.metadata) {
            console.log('%c\n📦 METADATA:', 'color: #da70d6; font-weight: bold');
            Object.entries(trace.metadata).slice(0, 10).forEach(([key, value]) => {
              const displayValue = typeof value === 'object' ? JSON.stringify(value).substring(0, 100) : value;
              console.log(`%c  • ${key}: ${displayValue}`, 'color: #ccc');
            });
          }

          console.log('%c═══════════════════════════════════════════════════════════════════════════════\n', 'color: #00ff88; font-weight: bold');
        } else if (data.trace_id) {
          // Minimal trace info if full trace not included
          console.log(`%c🔍 VBI Trace ID: ${data.trace_id} (full trace available in backend logs)`, 'color: #888');
        }

        const voiceUnlockText = data.message || data.text || 'Voice unlock command processed';
        setResponse(voiceUnlockText);
        setIsProcessing(false);

        // Update VBI progress with final result from voice_unlock message
        // This ensures the UI displays the correct success/failure status
        const unlockSuccess = data.success === true;
        const unlockSpeakerName = data.speaker_name || data.speaker || (data.vbi_trace?.speaker_name);
        const unlockConfidence = data.confidence || (data.vbi_trace?.confidence) || 0;
        
        setVbiProgress(prev => prev ? {
          ...prev,
          stage: 'complete',
          stageName: 'Complete',
          status: unlockSuccess ? 'success' : (data.success === false ? 'failed' : prev.status),
          isSuccess: unlockSuccess,
          speaker: unlockSpeakerName,
          confidence: unlockConfidence,
          error: unlockSuccess ? null : (data.error || prev.error),
          details: {
            ...prev.details,
            speaker: unlockSpeakerName,
            confidence: unlockConfidence
          }
        } : null);

        // NO AUDIO FEEDBACK for voice unlock (removed to prevent feedback loops)
        if ((data.message || data.text) && data.speak !== false) {
          console.log('[JARVIS Audio] Voice unlock response (silent):', voiceUnlockText);
        }

        // Reset waiting state after voice unlock command
        if (isWaitingForCommandRef.current) {
          setTimeout(() => {
            setIsWaitingForCommand(false);
            isWaitingForCommandRef.current = false;
            // Ensure continuous listening remains active
            if (!continuousListeningRef.current && jarvisStatus === 'online') {
              console.log('Re-enabling continuous listening after voice unlock command');
              enableContinuousListening();
            }
          }, 1000);
        }
        break;
      case 'transcription_started':
        // Handle hybrid STT transcription started
        console.log('🎤 [HybridSTT] Transcription started:', data);
        setResponse('🎤 Transcribing...');
        setIsProcessing(true);
        break;

      case 'transcription_result':
        // Handle hybrid STT transcription result
        console.log('🎤 [HybridSTT] Transcription result:', data);
        const { text: transcribedText, confidence, engine, model_name, latency_ms, speaker_identified } = data;

        // Display transcription with metadata
        setTranscript(transcribedText);
        setResponse(`✅ ${transcribedText}\n\n📊 Confidence: ${(confidence * 100).toFixed(1)}% | Engine: ${engine} (${model_name}) | Latency: ${latency_ms.toFixed(0)}ms${speaker_identified ? ` | Speaker: ${speaker_identified}` : ''}`);

        // Log STT details
        console.log(`🎤 [HybridSTT] Transcribed: "${transcribedText}" (confidence: ${(confidence * 100).toFixed(1)}%, engine: ${engine}, latency: ${latency_ms.toFixed(0)}ms)`);

        // Note: Command will be auto-processed by backend if confidence >= 0.6
        // We'll receive a command_response message next
        setIsProcessing(false);
        break;

      case 'transcription_error':
        // Handle hybrid STT transcription error
        console.error('🎤 [HybridSTT] Transcription error:', data.message);
        setResponse(`❌ Transcription failed: ${data.message}`);
        setIsProcessing(false);
        setError(data.message);
        break;

      case 'stream_stop':
        // 🆕 Handle stream_stop message from backend
        // Backend detected a command (e.g., "unlock") and wants to stop audio stream
        console.log('🛡️ [Stream Stop] Backend detected command:', data);

        if (hybridSTTClientRef.current) {
          hybridSTTClientRef.current.handleStreamStop(data);
        }

        // Update UI to show command detection
        if (data.command) {
          setDetectedCommand(data.command); // Show banner
          setResponse(`🛡️ Command detected: "${data.command}" - stopping audio stream`);
        }
        break;

      case 'screen_lock':
      case 'screen_unlock': {
        // Handle direct screen control events (some backends emit these instead of command_response/response)
        const msg = data.response || data.message || data.text || '';
        if (msg) {
          setResponse(msg);
          if (data.speak && msg) {
            speakResponse(msg);
          }
        }
        setIsProcessing(false);
        break;
      }
      
      case 'proactive_unlock_progress': {
        // ═══════════════════════════════════════════════════════════════════
        // v8.0: PROACTIVE UNLOCK PROGRESS - Real-time visual feedback
        // ═══════════════════════════════════════════════════════════════════
        // Shows a popup notification during the proactive unlock flow:
        // 1. Screen locked detected → 2. Verifying voice → 3. Unlocking → 
        // 4. Processing command → 5. Complete
        // ═══════════════════════════════════════════════════════════════════
        const stage = data.stage || 'initializing';
        const message = data.message || '';
        const progress = data.progress || 0;
        const originalCommand = data.original_command || '';
        const continuationIntent = data.continuation_intent || '';
        const status = data.status || 'in_progress';
        
        console.log('%c[PROACTIVE-UNLOCK]', 'color: #00bfff; font-weight: bold',
          `Stage: ${stage} (${progress}%) - ${message}`);
        
        // Stage icon mapping - v9.0: Added voice_verified as distinct stage
        const stageIcons = {
          'screen_detected': '🔒',
          'verifying_voice': '🎤',
          'voice_verified': '✅',  // SECURITY: Voice confirmed BEFORE unlock
          'unlocking': '🔐',       // Only after voice_verified
          'unlocked': '🔓',
          'executing': '🚀',
          'complete': '✨',
          'error': '❌',
        };
        
        // Update proactive unlock progress state
        setProactiveUnlockProgress({
          stage,
          stageIcon: stageIcons[stage] || '⚙️',
          message,
          originalCommand,
          continuationIntent,
          progress,
          status,
          timestamp: Date.now(),
        });
        
        // Auto-hide after completion
        if (status === 'complete' || status === 'error') {
          if (proactiveUnlockTimeoutRef.current) {
            clearTimeout(proactiveUnlockTimeoutRef.current);
          }
          proactiveUnlockTimeoutRef.current = setTimeout(() => {
            setProactiveUnlockProgress(null);
          }, status === 'complete' ? 3000 : 5000);
        }
        break;
      }
      
      case 'proactive_unlock_success': {
        // ═══════════════════════════════════════════════════════════════════
        // v8.0: PROACTIVE UNLOCK FLOW - Screen was locked, we unlocked it,
        // and a continuation command is scheduled in the background.
        // ═══════════════════════════════════════════════════════════════════
        // Example: User said "search for dogs" → JARVIS unlocked screen → 
        //          Now searching for dogs in background
        // ═══════════════════════════════════════════════════════════════════
        console.log('🔓 [PROACTIVE-UNLOCK] Screen unlocked, continuation scheduled:', data);
        
        const proactiveData = data.proactive_unlock || {};
        const continuationIntent = proactiveData.continuation_intent || '';
        const unlockLatency = proactiveData.unlock_latency_ms || 0;
        
        // Update the progress to show success
        setProactiveUnlockProgress({
          stage: 'unlocked',
          stageIcon: '🔓',
          message: `Screen unlocked. Now ${continuationIntent}...`,
          originalCommand: proactiveData.original_command || '',
          continuationIntent,
          progress: 80,
          status: 'success',
          unlockLatency,
          timestamp: Date.now(),
        });
        
        // Show the unlock response but indicate continuation is coming
        const msg = data.response || 'Screen unlocked, processing your request...';
        setResponse(msg);
        
        // Don't speak the response - JARVIS already spoke the acknowledgment before unlock
        // and will speak again after the continuation completes
        
        // Keep processing indicator on since continuation is coming
        // We'll clear it when the continuation response arrives
        // Actually, set to false now since the continuation will trigger new processing
        setIsProcessing(false);
        
        // Log the proactive unlock metrics
        if (unlockLatency > 0) {
          console.log(`🔓 [PROACTIVE-UNLOCK] Unlock completed in ${unlockLatency.toFixed(0)}ms`);
        }
        if (continuationIntent) {
          console.log(`🔄 [PROACTIVE-UNLOCK] Continuation scheduled: "${continuationIntent}"`);
        }
        
        // Auto-hide progress after delay
        if (proactiveUnlockTimeoutRef.current) {
          clearTimeout(proactiveUnlockTimeoutRef.current);
        }
        proactiveUnlockTimeoutRef.current = setTimeout(() => {
          setProactiveUnlockProgress(null);
        }, 5000);
        break;
      }

      case 'command_response':
        // Handle new async pipeline responses
        console.log('WebSocket command_response received:', data);

        // 🔍 VBI TRACE DISPLAY - Check for VBI trace in command responses too
        if (data.vbi_trace) {
          const trace = data.vbi_trace;
          console.log('%c═══════════════════════════════════════════════════════════════════════════════', 'color: #00ff88; font-weight: bold');
          console.log('%c📊 VBI TRACE (via command_response): ' + (trace.trace_id || 'unknown'), 'color: #00ff88; font-size: 14px; font-weight: bold');
          console.log('%c═══════════════════════════════════════════════════════════════════════════════', 'color: #00ff88; font-weight: bold');
          console.log(`%cStatus: ${trace.status || 'unknown'} | Duration: ${(trace.total_duration_ms || 0).toFixed(1)}ms`,
            trace.status === 'success' ? 'color: #00ff88' : 'color: #ff6b6b');
          if (trace.speaker_name) {
            console.log(`%c👤 Speaker: ${trace.speaker_name} (Confidence: ${((trace.confidence || 0) * 100).toFixed(1)}%)`,
              'color: #00bfff; font-weight: bold');
          }
          if (trace.steps && trace.steps.length > 0) {
            console.log('%c\n📋 PIPELINE STEPS:', 'color: #ffa500; font-weight: bold');
            trace.steps.forEach((step, idx) => {
              const statusIcon = step.status === 'success' ? '✅' : step.status === 'error' ? '❌' : '⏳';
              console.log(`%c  ${idx + 1}. ${statusIcon} ${step.step_name || 'unnamed'} [${step.duration_ms?.toFixed(1) || 'N/A'}ms]`,
                step.status === 'success' ? 'color: #00ff88' : 'color: #ff6b6b');
            });
          }
          console.log('%c═══════════════════════════════════════════════════════════════════════════════\n', 'color: #00ff88; font-weight: bold');
        }

        if (data.response) {
          // Update the UI with the response
          setResponse(data.response);
          setIsProcessing(false);

          // If speak flag is set, trigger text-to-speech
          if (data.speak && data.response) {
            speakResponse(data.response);
          }

          // Log successful command execution
          if (data.action) {
            console.log(`✅ Command executed: ${data.action}`);
          }

          // Log STT engine info if available (from hybrid STT)
          if (data.stt_engine) {
            console.log(`🎤 [HybridSTT] Command processed via ${data.stt_engine} (confidence: ${(data.confidence * 100).toFixed(1)}%)`);
          }
        }
        break;

      case 'processing':
        // Handle processing acknowledgment (vision commands take 2-8 seconds)
        console.log('WebSocket processing acknowledgment:', data);
        const processingMessage = data.message || 'Processing...';
        setResponse(processingMessage);
        setIsProcessing(true);

        // Optionally speak the acknowledgment
        if (data.speak !== false && processingMessage) {
          speakResponse(processingMessage, false);
        }
        break;

      case 'response':
        console.log('WebSocket response received:', data);

        // Check if this is an error response we should ignore
        const errorText = (data.text || data.response || '').toLowerCase();
        // Only mark as error if response explicitly indicates failure, not if it contains these words in a successful response
        const isError = (
          errorText.includes("don't have a handler") ||
          errorText.startsWith("error") ||
          errorText.startsWith("sorry") ||
          errorText.startsWith("command failed") ||
          errorText.startsWith("i encountered an error") ||
          (errorText.includes("error") && !errorText.includes("can see"))
        );

        if (errorText.includes("don't have a handler for query commands")) {
          console.log('Ignoring query handler error, continuing to listen...');
          setIsProcessing(false);
          // Don't reset waiting state - keep listening
          return;
        }

        // 🧠 Record command success/failure in adaptive system
        const lastCommand = data.metadata?.originalCommand || transcript;
        if (lastCommand && data.metadata) {
          const executionTime = Date.now() - (data.metadata.startTime || Date.now());
          adaptiveVoiceDetection.recordCommandExecution(lastCommand, {
            success: !isError,
            confidence: data.metadata.confidence || 0.85,
            executionTime,
            wasRetry: false,
          });
        }

        // Use the EXACT same text for both display and speech
        const responseText = data.text || data.message || 'Response received';
        console.log('[JARVIS Audio] Setting display text:', responseText);
        setResponse(responseText);
        setIsProcessing(false);

        // Use speech synthesis with Daniel voice
        if (responseText && data.speak !== false) {
          // Speak the EXACT same text that we just displayed
          console.log('[JARVIS Audio] Speaking exact text:', responseText);
          speakResponse(responseText, false);
        }

        // Reset waiting state after successful command
        if (isWaitingForCommandRef.current && responseText && !responseText.toLowerCase().includes('error')) {
          setTimeout(() => {
            setIsWaitingForCommand(false);
            isWaitingForCommandRef.current = false;
            // Ensure continuous listening remains active
            if (!continuousListeningRef.current && jarvisStatus === 'online') {
              console.log('Re-enabling continuous listening after command completion');
              enableContinuousListening();
            }
          }, 1000);
        }

        // Check for autonomy activation commands in response
        const responseTextLower = responseText.toLowerCase();
        if (data.command_type === 'autonomy_activation' ||
          responseTextLower.includes('autonomous mode activated') ||
          responseTextLower.includes('full autonomy enabled') ||
          responseTextLower.includes('all systems online')) {
          // Activate autonomous mode
          if (!autonomousMode) {
            setAutonomousMode(true);
            // Connect vision system
            if (visionConnectionRef.current && !visionConnectionRef.current.isConnected) {
              visionConnectionRef.current.connect();
            }
            // Enable continuous listening
            enableContinuousListening();
          }
        }
        break;
      case 'autonomy_status':
        // Handle autonomy status updates
        if (data.enabled) {
          setAutonomousMode(true);
          if (visionConnectionRef.current && !visionConnectionRef.current.isConnected) {
            visionConnectionRef.current.connect();
          }
        } else {
          setAutonomousMode(false);
        }
        break;
      case 'vision_status':
        // Handle vision connection status
        setVisionConnected(data.connected);
        break;
      case 'mode_changed':
        // Handle mode change confirmations
        if (data.mode === 'autonomous') {
          setAutonomousMode(true);
        } else {
          setAutonomousMode(false);
        }
        break;
      case 'error':
        setError(data.message);
        setIsProcessing(false);
        break;
      case 'debug_log':
        // Display debug logs in console with styling
        const logStyle = data.level === 'error'
          ? 'color: red; font-weight: bold;'
          : data.level === 'warning'
            ? 'color: orange;'
            : 'color: #4CAF50; font-weight: bold;';
        console.log(`%c[JARVIS DEBUG ${new Date(data.timestamp).toLocaleTimeString()}] ${data.message}`, logStyle);
        if (data.level === 'error') {
          console.error('Full error details:', data);
        }
        break;
      case 'workflow_analysis':
        // Workflow has been analyzed and is about to start
        console.log('🔄 Workflow analysis:', data);
        setWorkflowProgress({
          ...data.workflow,
          status: 'starting'
        });
        break;
      case 'workflow_started':
        // Workflow execution has started
        setWorkflowProgress(prev => ({
          ...prev,
          ...data,
          status: 'running'
        }));
        break;
      case 'action_started':
        // Individual action has started
        setWorkflowProgress(prev => {
          if (!prev || !prev.actions) return prev;
          const updatedActions = [...prev.actions];
          if (updatedActions[data.action_index]) {
            updatedActions[data.action_index].status = 'running';
          }
          return {
            ...prev,
            actions: updatedActions,
            currentAction: data.action_index
          };
        });
        break;
      case 'action_completed':
        // Individual action completed
        setWorkflowProgress(prev => {
          if (!prev || !prev.actions) return prev;
          const updatedActions = [...prev.actions];
          if (updatedActions[data.action_index]) {
            updatedActions[data.action_index].status = 'completed';
            updatedActions[data.action_index].duration = data.duration;
          }
          return {
            ...prev,
            actions: updatedActions
          };
        });
        break;
      case 'action_failed':
        // Individual action failed
        setWorkflowProgress(prev => {
          if (!prev || !prev.actions) return prev;
          const updatedActions = [...prev.actions];
          if (updatedActions[data.action_index]) {
            updatedActions[data.action_index].status = 'failed';
            updatedActions[data.action_index].error = data.error;
            updatedActions[data.action_index].duration = data.duration;
          }
          return {
            ...prev,
            actions: updatedActions
          };
        });
        break;
      case 'action_retry':
        // Action is being retried
        setWorkflowProgress(prev => {
          if (!prev || !prev.actions) return prev;
          const updatedActions = [...prev.actions];
          if (updatedActions[data.action_index]) {
            updatedActions[data.action_index].status = 'retry';
            updatedActions[data.action_index].retry_count = data.retry_count;
          }
          return {
            ...prev,
            actions: updatedActions
          };
        });
        break;
      case 'workflow_completed':
        // Workflow execution completed
        setWorkflowProgress(prev => ({
          ...prev,
          status: 'completed',
          total_duration: data.total_duration,
          success_rate: data.success_rate
        }));
        // Clear workflow progress after 10 seconds
        setTimeout(() => setWorkflowProgress(null), 10000);
        break;
      case 'proactive_suggestion':
        // Phase 4: Proactive intelligence suggestion
        console.log('💡 Proactive suggestion:', data);
        setProactiveSuggestions(prev => [...prev, data.suggestion]);
        setLastSuggestionTime(new Date());
        setProactiveIntelligenceActive(true);
        // Speak the suggestion if voice is enabled
        if (data.suggestion.voice_message) {
          speakResponse(data.suggestion.voice_message);
        }
        break;
      case 'proactive_intelligence_status':
        // Phase 4: Proactive intelligence status update
        console.log('🤖 Proactive Intelligence status:', data);
        setProactiveIntelligenceActive(data.active);
        break;
      case 'narration':
        // Handle document writing narration (display only, no voice)
        console.log('📝 Document narration:', data.message);
        if (data.message) {
          setResponse(data.message);
          // Don't speak narration messages to prevent overlap
        }
        break;
      case 'voice_narration':
        // Handle voice narration from document writer
        console.log('🎤 Voice narration received:', data.message);
        console.log('🎤 Speak flag:', data.speak);
        if (data.message && data.speak !== false) {
          // Store the exact message
          const narrationText = data.message;
          console.log('[JARVIS Audio] Narration text:', narrationText);

          // For narrations, WAIT to update display until voice actually starts
          // This ensures perfect synchronization
          speakResponseWithCallback(narrationText, () => {
            // Update display when voice actually starts
            setResponse(narrationText);
          });
        } else {
          console.log('🎤 Skipping narration - speak flag is false or no message');
        }
        break;
      default:
        break;
    }
  };


  // Function to setup recognition handlers
  const setupRecognitionHandlers = (recognition) => {
    if (!recognition) return;

    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    // Move all the existing handlers to be assigned here
    recognition.onresult = (event) => {
      // Existing onresult handler code will be here
      handleSpeechResult(event);
    };

    recognition.onerror = async (event) => {
      // Existing onerror handler code will be here
      handleSpeechError(event);
    };

    recognition.onend = () => {
      // Existing onend handler code will be here
      handleSpeechEnd();
    };

    recognition.onstart = () => {
      console.log('Speech recognition started');
      setError('');
      setMicStatus('ready');

      // ENABLE CONTINUOUS LISTENING IMMEDIATELY for wake word detection
      if (!continuousListeningRef.current) {
        console.log('🎯 Auto-enabling continuous listening for wake word detection');
        continuousListeningRef.current = true;
        setContinuousListening(true);
      }
    };

    return recognition;
  };

  const handleSpeechResult = (event) => {
    // This will contain the existing onresult handler logic
    // Moving it here for better organization
  };

  const handleSpeechError = async (event) => {
    // This will contain the existing onerror handler logic
    // Moving it here for better organization
  };

  const handleSpeechEnd = () => {
    // This will contain the existing onend handler logic
    // Moving it here for better organization
  };

  const initializeWakeWordService = async () => {
    // Ensure config is ready
    if (!configReady || !API_URL) {
      console.log('JarvisVoice: Waiting for config before wake word init...');
      await configPromise;
    }

    if (!wakeWordServiceRef.current) {
      // Create a simplified wake word handler object
      wakeWordServiceRef.current = {
        onWakeWordDetected: (data) => {
          console.log('🎤 Wake word activated!', data);

          // Clear any previous transcript
          setTranscript('');

          // Set listening state
          setIsWaitingForCommand(true);
          setIsListening(true);

          // NO AUDIO FEEDBACK for wake word (removed to prevent feedback loops)
          console.log('🎯 Wake word detected - listening for command (silent)');

          // Start timeout for command (30 seconds - more time to speak)
          setTimeout(() => {
            if (isWaitingForCommand && !isJarvisSpeaking) {
              setIsWaitingForCommand(false);
              console.log('⏱️ Command timeout - returning to wake word listening');
            }
          }, 30000);
        },
        isActive: false
      };

      // Try to connect to backend wake word service if available
      try {
        const wakeService = new WakeWordService();
        const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
        console.log('JarvisVoice: Initializing wake word service at:', apiUrl);
        const initialized = await wakeService.initialize(apiUrl);
        if (initialized) {
          console.log('✅ Backend wake word service connected');
          // Use backend service callbacks if available
          wakeService.setCallbacks({
            onWakeWordDetected: wakeWordServiceRef.current.onWakeWordDetected,
            onStatusChange: (status) => console.log('Wake word status:', status),
            onError: (error) => console.error('Wake word error:', error)
          });
        }
      } catch (e) {
        console.log('📢 Using frontend-only wake word detection');
      }

      wakeWordServiceRef.current.isActive = true;
    }
  };

  // ============================================================================
  // 🆕 ROBUST COMMAND QUEUE SYSTEM
  // Never lose commands during WebSocket disconnection
  // ============================================================================

  const queueCommand = (command, metadata = {}) => {
    const queuedCommand = {
      id: `cmd_${Date.now()}_${Math.random().toString(36).slice(2, 11)}`,
      command,
      metadata,
      timestamp: Date.now(),
      attempts: 0,
      maxAttempts: 3
    };

    // Prevent queue overflow
    if (pendingCommandsRef.current.length >= maxQueuedCommands) {
      console.warn('[CMD-QUEUE] Queue full, removing oldest command');
      pendingCommandsRef.current.shift();
    }

    pendingCommandsRef.current.push(queuedCommand);
    console.log(`[CMD-QUEUE] Command queued: "${command}" (queue size: ${pendingCommandsRef.current.length})`);

    // Try to process immediately
    processCommandQueue();

    return queuedCommand.id;
  };

  const processCommandQueue = async () => {
    // Prevent concurrent processing
    if (commandQueueProcessingRef.current) {
      return;
    }

    // Check if WebSocket is ready
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.log('[CMD-QUEUE] WebSocket not ready, will retry when connected');
      return;
    }

    commandQueueProcessingRef.current = true;

    try {
      while (pendingCommandsRef.current.length > 0) {
        const queuedCmd = pendingCommandsRef.current[0];

        // Check if command has expired
        if (Date.now() - queuedCmd.timestamp > commandQueueTimeoutMs) {
          console.warn(`[CMD-QUEUE] Command expired: "${queuedCmd.command}"`);
          pendingCommandsRef.current.shift();
          continue;
        }

        // Try to send
        try {
          queuedCmd.attempts++;

          const message = {
            type: 'command',
            text: queuedCmd.command,
            mode: autonomousMode ? 'autonomous' : 'manual',
            priority: queuedCmd.metadata.priority || 'normal',
            metadata: {
              ...queuedCmd.metadata,
              queuedAt: queuedCmd.timestamp,
              processedAt: Date.now(),
              queueAttempt: queuedCmd.attempts
            }
          };

          // Attach audio if available
          if (queuedCmd.metadata.audioData) {
            message.audio_data = queuedCmd.metadata.audioData.audio;
            message.sample_rate = queuedCmd.metadata.audioData.sampleRate;
            message.mime_type = queuedCmd.metadata.audioData.mimeType;
          }

          wsRef.current.send(JSON.stringify(message));
          console.log(`[CMD-QUEUE] ✅ Sent queued command: "${queuedCmd.command}"`);

          // Remove from queue on success
          pendingCommandsRef.current.shift();

          // Small delay between commands
          await new Promise(r => setTimeout(r, 100));

        } catch (sendError) {
          console.error('[CMD-QUEUE] Failed to send:', sendError);

          if (queuedCmd.attempts >= queuedCmd.maxAttempts) {
            console.error(`[CMD-QUEUE] Max attempts reached for: "${queuedCmd.command}"`);
            pendingCommandsRef.current.shift();
          } else {
            // Will retry next cycle
            break;
          }
        }
      }
    } finally {
      commandQueueProcessingRef.current = false;
    }
  };

  // ============================================================================
  // 🆕 FUZZY WAKE WORD MATCHING
  // Handles partial matches like "hey" for "hey jarvis"
  // ============================================================================

  const fuzzyMatchWakeWord = (transcript) => {
    const config = voiceConfigRef.current;
    const normalizedTranscript = transcript.toLowerCase().trim();

    // First, check exact matches
    for (const wakeWord of config.wakeWords) {
      if (normalizedTranscript.includes(wakeWord)) {
        return { matched: true, wakeWord, matchType: 'exact', confidence: 1.0 };
      }
    }

    // Check partial matches for jarvis variations
    const jarvisPatterns = config.partialMatchPatterns.jarvis;
    for (const pattern of jarvisPatterns) {
      if (normalizedTranscript.includes(pattern)) {
        return { matched: true, wakeWord: 'jarvis', matchType: 'partial', confidence: 0.9 };
      }
    }

    // Check if transcript starts with "hey" and might be incomplete
    if (normalizedTranscript.startsWith('hey') && normalizedTranscript.length < 12) {
      // Could be "hey jarvis" in progress
      return { matched: false, potential: true, wakeWord: null, matchType: 'potential', confidence: 0.5 };
    }

    // Check for "hey" followed by any word starting with 'j'
    const heyJMatch = normalizedTranscript.match(/hey\s+j\w*/);
    if (heyJMatch) {
      return { matched: true, wakeWord: 'hey jarvis', matchType: 'fuzzy', confidence: 0.8 };
    }

    return { matched: false, potential: false, wakeWord: null, matchType: 'none', confidence: 0 };
  };

  // ============================================================================
  // 🆕 FUZZY COMMAND MATCHING
  // Handles partial command matches like "unlock" variations
  // ============================================================================

  const fuzzyMatchCommand = (transcript) => {
    const config = voiceConfigRef.current;
    const normalizedTranscript = transcript.toLowerCase().trim();

    // Check for unlock command patterns
    for (const pattern of config.partialMatchPatterns.unlock) {
      if (normalizedTranscript.includes(pattern)) {
        // Extract the full command
        const fullCommand = normalizedTranscript.includes('screen')
          ? 'unlock my screen'
          : normalizedTranscript.includes('mac')
            ? 'unlock my mac'
            : 'unlock my screen';
        return { matched: true, command: fullCommand, commandType: 'unlock', confidence: 0.85 };
      }
    }

    // Check for lock command patterns (but not unlock)
    if (!normalizedTranscript.includes('unlock')) {
      for (const pattern of config.partialMatchPatterns.lock) {
        if (normalizedTranscript.includes(pattern)) {
          const fullCommand = normalizedTranscript.includes('screen')
            ? 'lock my screen'
            : normalizedTranscript.includes('mac')
              ? 'lock my mac'
              : 'lock my screen';
          return { matched: true, command: fullCommand, commandType: 'lock', confidence: 0.85 };
        }
      }
    }

    return { matched: false, command: null, commandType: null, confidence: 0 };
  };

  // ============================================================================
  // 🆕 INTELLIGENT SPEECH RECOVERY
  // Handles aborts without losing responsiveness
  // ============================================================================

  const handleIntelligentRecovery = () => {
    const recovery = speechRecoveryStateRef.current;
    const now = Date.now();

    // Process any commands detected during backoff
    if (recovery.commandsDetectedDuringBackoff.length > 0) {
      console.log(`[RECOVERY] Processing ${recovery.commandsDetectedDuringBackoff.length} commands from backoff period`);
      recovery.commandsDetectedDuringBackoff.forEach(cmd => {
        queueCommand(cmd.command, cmd.metadata);
      });
      recovery.commandsDetectedDuringBackoff = [];
    }

    // Check if we're in backoff and it's expired
    if (recovery.inBackoffMode && now >= recovery.backoffEndTime) {
      console.log('[RECOVERY] Backoff period ended, resuming normal operation');
      recovery.inBackoffMode = false;
      recovery.consecutiveAborts = 0;
    }

    // Try to restart recognition if continuous listening is enabled
    if (continuousListeningRef.current && !recovery.inBackoffMode) {
      try {
        if (recognitionRef.current) {
          recognitionRef.current.start();
          recovery.recognitionAttempts++;
          console.log('[RECOVERY] ✅ Recognition restarted');
        }
      } catch (e) {
        if (!e.message?.includes('already started')) {
          console.debug('[RECOVERY] Restart attempt failed:', e.message);
        }
      }
    }
  };

  const enterSmartBackoff = (reason = 'unknown') => {
    const recovery = speechRecoveryStateRef.current;
    const config = voiceConfigRef.current;

    recovery.consecutiveAborts++;
    recovery.lastAbortTime = Date.now();

    // Calculate adaptive backoff - shorter than before
    const baseBackoff = Math.min(
      config.maxAbortBackoffMs,
      200 * Math.pow(1.5, Math.min(recovery.consecutiveAborts - 1, 4))
    );

    recovery.backoffEndTime = Date.now() + baseBackoff;
    recovery.inBackoffMode = true;

    console.log(`[RECOVERY] Entering smart backoff: ${baseBackoff}ms (reason: ${reason}, consecutive: ${recovery.consecutiveAborts})`);

    // Schedule recovery
    setTimeout(() => {
      handleIntelligentRecovery();
    }, baseBackoff);
  };

  const initializeSpeechRecognition = async () => {
    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      recognitionRef.current = new SpeechRecognition();

      console.log('⚠️ Environmental audio processing DISABLED to prevent echo/feedback');

      // Track if JARVIS is speaking to avoid self-triggering
      let jarvisSpeaking = false;

      recognitionRef.current.continuous = true;
      recognitionRef.current.interimResults = true;
      recognitionRef.current.lang = 'en-US';
      recognitionRef.current.maxAlternatives = 3; // Get multiple alternatives for better accuracy

      // Optimize for faster, more responsive recognition
      // Note: These are non-standard but work in some browsers
      if ('speechTimeout' in recognitionRef.current) {
        recognitionRef.current.speechTimeout = 5000; // Faster timeout for quicker response
      }
      if ('noSpeechTimeout' in recognitionRef.current) {
        recognitionRef.current.noSpeechTimeout = 3000; // Shorter silence timeout
      }

      // Enable faster recognition (non-standard but supported by Chrome)
      if ('grammar' in recognitionRef.current) {
        // Add grammar hints for common commands to improve recognition speed
        const commandHints = [
          'lock my screen', 'unlock my screen', 'lock screen', 'unlock screen',
          'lock the screen', 'unlock the screen', 'hey jarvis', 'jarvis'
        ];
        recognitionRef.current.grammars = commandHints;
      }

      recognitionRef.current.onresult = (event) => {
        const last = event.results.length - 1;
        const result = event.results[last][0];
        const transcript = result.transcript.toLowerCase();
        const isFinal = event.results[last].isFinal;
        const confidence = result.confidence || 0;

        // IMMEDIATE DEBUG LOG - to see if we're even getting input
        console.log('🎤 RAW SPEECH:', transcript, `(final: ${isFinal}, conf: ${confidence})`);

        // ============================================================
        // 🔇 SELF-VOICE SUPPRESSION v2.0 - REF-BASED (No closure issues)
        // ============================================================
        // Check if this is JARVIS hearing its own voice using ref-based system
        const blockCheck = shouldBlockSelfVoice(transcript);
        if (blockCheck.block) {
          console.log(`🔇 [Self-Voice v2] BLOCKED: "${transcript.substring(0, 50)}..." - Reason: ${blockCheck.reason}`);
          return; // Completely ignore this speech input
        }

        // Also check the older system as backup
        const echoCheck = isSelfVoiceEcho(transcript);
        if (echoCheck.isEcho) {
          console.log(`🔇 [Self-Voice v1] BLOCKED: "${transcript.substring(0, 50)}..." - Reason: ${echoCheck.reason}`);
          return; // Completely ignore this speech input
        }

        // 🆕 ULTRA-FAST-PATH: Check for critical unlock/lock commands immediately
        // These commands get special priority with LOWER confidence threshold
        // Also use fuzzy matching for better detection
        const isUnlockCommand = /unlock\s*(my)?\s*(screen|mac|computer)?/i.test(transcript);
        const isLockCommand = /lock\s*(my)?\s*(screen|mac|computer)?/i.test(transcript) && !isUnlockCommand;
        const isCriticalCommand = isUnlockCommand || isLockCommand;

        // Get dynamic threshold from config (no hardcoding)
        const criticalThreshold = voiceConfigRef.current.criticalCommandConfidenceThreshold;

        // For critical commands, use a much lower confidence threshold from config
        // This ensures unlock commands are caught on the first attempt
        if (isCriticalCommand && confidence >= criticalThreshold) {
          console.log(`🔓 CRITICAL COMMAND ULTRA-FAST-PATH: "${transcript}" (conf: ${(confidence * 100).toFixed(1)}%)`);

          // For unlock commands, process immediately without waiting for final
          // Use dynamic threshold from config
          if (isFinal || confidence >= voiceConfigRef.current.normalCommandConfidenceThreshold) {
            console.log(`🔓 Processing ${isUnlockCommand ? 'unlock' : 'lock'} command IMMEDIATELY via priority path`);
            
            // 🚀 ULTRA-FAST: Send command directly without waiting for audio buffer
            // This shaves off 200-500ms of latency for critical commands
            sendPriorityCommand(result.transcript, {
              confidence: confidence,
              originalConfidence: confidence,
              isFinal,
              wasCriticalCommand: true,
              commandType: isUnlockCommand ? 'unlock' : 'lock',
            });
            return;
          }
        }

        // 🧠 ADAPTIVE VOICE DETECTION - Analyze with learning system
        const adaptiveDecision = adaptiveVoiceDetection.shouldProcessResult(result, {
          transcript,
          confidence,
          isFinal,
          isWaitingForCommand: isWaitingForCommandRef.current,
          timestamp: Date.now(),
        });

        const enhancedConfidence = adaptiveDecision.enhancedConfidence || confidence;
        const shouldProcess = adaptiveDecision.shouldProcess;

        // Debug logging with adaptive confidence score
        console.log(`🎙️ Speech detected: "${transcript}" (final: ${isFinal}, original: ${(confidence * 100).toFixed(1)}%, enhanced: ${(enhancedConfidence * 100).toFixed(1)}%) | Threshold: ${(adaptiveDecision.threshold * 100).toFixed(1)}% | Should Process: ${shouldProcess}`);
        console.log(`📊 Reason: ${adaptiveDecision.reason}`);

        // 🆕 Lower the high confidence threshold from 0.85 to 0.70 for better first-attempt recognition
        const isHighConfidence = enhancedConfidence >= 0.70;

        // Process based on adaptive decision
        if (!shouldProcess && !isWaitingForCommandRef.current) return;

        // 🆕 Check for wake words with FUZZY MATCHING when not waiting for command
        if (!isWaitingForCommandRef.current && continuousListeningRef.current) {
          // Use new fuzzy matching system
          const wakeWordMatch = fuzzyMatchWakeWord(transcript);
          const detectedWakeWord = wakeWordMatch.matched ? wakeWordMatch.wakeWord : null;

          console.log('🔍 Wake word check (fuzzy):', {
            transcript,
            isWaiting: isWaitingForCommandRef.current,
            continuousListening: continuousListeningRef.current,
            detectedWakeWord,
            matchType: wakeWordMatch.matchType,
            matchConfidence: wakeWordMatch.confidence,
            shouldProcess
          });

          // Also check for direct critical commands without wake word (e.g., "unlock my screen")
          const directCommandMatch = fuzzyMatchCommand(transcript);
          if (directCommandMatch.matched && (isFinal || confidence >= voiceConfigRef.current.criticalCommandConfidenceThreshold)) {
            console.log(`🎯 Direct ${directCommandMatch.commandType} command detected without wake word: "${transcript}"`);
            sendPriorityCommand(directCommandMatch.command, {
              confidence,
              originalConfidence: confidence,
              isFinal,
              wasCriticalCommand: true,
              commandType: directCommandMatch.commandType,
              matchType: 'direct_fuzzy'
            });
            return;
          }

          if (detectedWakeWord) {
            console.log('🎯 Wake word detected:', detectedWakeWord, `(confidence: ${(confidence * 100).toFixed(1)}%)`, '| Current state:', {
              isWaitingForCommand: isWaitingForCommandRef.current,
              continuousListening: continuousListeningRef.current,
              isListening,
              isFinal,
              isHighConfidence
            });

            // Check if there's a command after the wake word in the same sentence
            const fullTranscript = result.transcript;
            let commandAfterWakeWord = fullTranscript.toLowerCase();

            // Remove the wake word to get just the command
            const wakeWordIndex = commandAfterWakeWord.indexOf(detectedWakeWord);
            if (wakeWordIndex !== -1) {
              commandAfterWakeWord = commandAfterWakeWord.substring(wakeWordIndex + detectedWakeWord.length).trim();
            }

            // If there's a command after the wake word, process it directly (for final or high-confidence results)
            if (commandAfterWakeWord.length > 5 && (isFinal || isHighConfidence)) {
              console.log('🎯 Command found after wake word:', commandAfterWakeWord, `(${isFinal ? 'final' : 'high-confidence'})`);

              // Process the command immediately with confidence info
              handleVoiceCommand(commandAfterWakeWord, {
                confidence: enhancedConfidence,
                originalConfidence: confidence,
                isFinal,
                wasWakeWordCombo: true,
              });
              return;
            }

            // For wake word only (no command after), require final result to avoid false positives
            if (isFinal && commandAfterWakeWord.length <= 5) {
              setTranscript('');
              console.log('🚀 Triggering wake word handler for listening mode...');
              handleWakeWordDetected();
              return;
            }
          }
        }

        // When waiting for command after wake word, process any speech
        if (isWaitingForCommandRef.current && transcript.length > 0) {
          // Process final results OR high-confidence interim results for faster response
          if (!isFinal && !isHighConfidence) return;

          // For interim results, wait for a complete command (at least 5 chars)
          if (!isFinal && transcript.length < 5) return;

          // Log what we're processing
          console.log(`🎯 Processing ${isFinal ? 'final' : 'high-confidence interim'} command result`);

          // Filter out wake words from commands
          const wakeWords = ['hey jarvis', 'jarvis', 'ok jarvis', 'hello jarvis'];
          let commandText = result.transcript;

          // Remove wake word if it's at the beginning of the command
          wakeWords.forEach(word => {
            if (commandText.toLowerCase().startsWith(word)) {
              commandText = commandText.substring(word.length).trim();
            }
          });

          // Only process if there's actual command content
          if (commandText.length > 0) {
            console.log('📢 Processing command:', commandText);
            console.log('🚀 Sending command to backend via WebSocket');
            handleVoiceCommand(commandText, {
              confidence: enhancedConfidence,
              originalConfidence: confidence,
              isFinal,
              wasWaitingForCommand: true,
            });

            // Reset waiting state
            setIsWaitingForCommand(false);
            isWaitingForCommandRef.current = false;
          } else {
            console.log('⚠️ No command text after removing wake word, continuing to listen...');
          }
        }
      };

      // Enhanced: Faster recognition start on speech
      // CRITICAL: Track speech activity to prevent restart during active speech
      recognitionRef.current.onspeechstart = () => {
        console.log('🎤 Speech start detected - ready for immediate processing');
        // Mark that speech is active - prevents restart loop from interrupting
        speechActiveRef.current = true;
        lastSpeechTimeRef.current = Date.now();
      };

      recognitionRef.current.onspeechend = () => {
        console.log('🎤 Speech end detected');
        // Speech ended - allow restarts after a brief delay for processing
        setTimeout(() => {
          speechActiveRef.current = false;
        }, 500); // Give time for result processing
      };

      recognitionRef.current.onsoundstart = () => {
        console.log('🔊 Sound detected');
        // Sound detected - temporarily block aggressive restarts
        lastSpeechTimeRef.current = Date.now();
      };

      recognitionRef.current.onerror = async (event) => {
        // Handle "no-speech" errors quietly - they're expected
        if (event.error !== 'no-speech') {
          console.error('Speech recognition error:', event.error, event);
        }

        // Use ML-enhanced error handling
        let mlResult = null;
        try {
          mlResult = await mlAudioHandler.handleAudioError(event, recognitionRef.current);
        } catch (error) {
          console.warn('ML audio handler error:', error);
        }

        // =============================================================
        // CRITICAL: Handle permission denied separately to prevent loops
        // =============================================================
        if (mlResult && mlResult.permissionDenied) {
          console.log('🚫 Permission denied detected - stopping all retry attempts');
          
          // CRITICAL: Stop continuous listening to prevent infinite loop
          continuousListeningRef.current = false;
          setContinuousListening(false);
          skipNextRestartRef.current = true;
          
          // Show appropriate error message
          setError('🎤 Microphone permission denied. Click the lock icon in the address bar to enable.');
          setMicStatus('permission_denied');
          setMicrophonePermission('denied');
          
          return;  // Don't process further
        }

        if (mlResult && mlResult.success) {
          // Only log recovery for non-no-speech errors
          if (event.error !== 'no-speech' && event.error !== 'aborted') {
            console.log('ML audio recovery successful:', mlResult);
          }
          setError('');
          setMicStatus('ready');

          // Restart recognition if needed (but NOT for aborted errors with skipRestart)
          if (mlResult.skipRestart) {
            console.log('Skipping restart per ML handler instruction');
            skipNextRestartRef.current = true;  // Set flag to prevent onend from restarting
            return;  // Don't restart
          }

          if (mlResult && (mlResult.newContext || (mlResult.message && mlResult.message.includes('granted')))) {
            startListening();
          }
        } else if (mlResult && mlResult.skipRestart) {
          // ML handler says to skip restart even though recovery wasn't successful
          console.log('⛔ ML handler instructed to skip restart');
          skipNextRestartRef.current = true;
          
          // Still show appropriate error
          if (event.error === 'not-allowed' || event.error === 'audio-capture') {
            setError('🎤 Microphone access issue. Check browser permissions.');
            setMicStatus('error');
            continuousListeningRef.current = false;
            setContinuousListening(false);
          }
          return;
        } else {
          // Fallback to basic error handling
          switch (event.error) {
            case 'audio-capture':
              setError('🎤 Microphone access denied. ML recovery failed.');
              setMicStatus('error');
              // Stop continuous listening to prevent retry loop
              continuousListeningRef.current = false;
              setContinuousListening(false);
              skipNextRestartRef.current = true;  // CRITICAL: Prevent onend restart
              console.warn('⛔ Stopping continuous listening due to audio capture error');
              break;

            case 'not-allowed':
              setError('🚫 Microphone permission denied. Please enable in browser settings.');
              setMicStatus('error');
              // Stop continuous listening to prevent retry loop
              continuousListeningRef.current = false;
              setContinuousListening(false);
              skipNextRestartRef.current = true;  // CRITICAL: Prevent onend restart
              console.warn('⛔ Stopping continuous listening due to permission denial');
              break;

            case 'no-speech':
              // Enhanced indefinite listening - ALWAYS restart
              console.log('No speech detected, enforcing indefinite listening...');
              if (continuousListening) {
                // Don't show error to user for expected silence
                setError('');

                // Immediately restart without delay
                try {
                  recognitionRef.current.stop();
                  // Restart immediately
                  setTimeout(() => {
                    try {
                      recognitionRef.current.start();
                      console.log('✅ Microphone restarted successfully after silence');
                    } catch (e) {
                      // If already started, that's fine
                      if (e.message && !e.message.includes('already started')) {
                        console.log('Restart attempt:', e.message);
                      }
                    }
                  }, 50); // Minimal delay
                } catch (e) {
                  console.log('Stopping for restart:', e);
                }
              }
              break;

            case 'network':
              console.log('🌐 Network error detected, initiating advanced recovery...');
              setError('🌐 Network error. Initiating advanced recovery...');

              // Use advanced network recovery manager
              const networkRecoveryManager = getNetworkRecoveryManager();

              (async () => {
                try {
                  const recoveryResult = await networkRecoveryManager.recoverFromNetworkError(
                    event,
                    recognitionRef.current,
                    {
                      continuousListening,
                      isListening,
                      mlAudioHandler,
                      jarvisStatus,
                      wsRef: wsRef.current
                    }
                  );

                  if (recoveryResult.success) {
                    console.log('✅ Network recovery successful:', recoveryResult);
                    setError('');
                    setNetworkRetries(0);

                    // Handle different recovery types
                    if (recoveryResult.newRecognition) {
                      // Service switched, update reference
                      recognitionRef.current = recoveryResult.newRecognition;
                      setupRecognitionHandlers(recoveryResult.newRecognition);
                    } else if (recoveryResult.useWebSocket) {
                      // Switch to WebSocket mode
                      setError('📡 Switched to WebSocket audio streaming');
                      // Store WebSocket reference for audio streaming
                      audioWebSocketRef.current = recoveryResult.websocket;
                      setTimeout(() => setError(''), 3000);
                    } else if (recoveryResult.offlineMode) {
                      // Enable offline mode
                      setError('📴 Offline mode active - commands will sync when online');
                      offlineModeRef.current = true;
                      commandQueueRef.current = recoveryResult.commandQueue;
                    } else if (recoveryResult.useProxy) {
                      // Use ML backend proxy
                      setError('🤖 Using ML backend for speech processing');
                      proxyEndpointRef.current = recoveryResult.proxyEndpoint;
                      setTimeout(() => setError(''), 3000);
                    }
                  } else {
                    // All strategies failed
                    setError('🌐 Network recovery failed. Manual intervention required.');
                    console.error('All network recovery strategies exhausted');

                    // Show recovery tips
                    setTimeout(() => {
                      setError(
                        '💡 Try:\n' +
                        '1. Check internet connection\n' +
                        '2. Disable VPN/Proxy\n' +
                        '3. Clear browser cache\n' +
                        '4. Restart browser'
                      );
                    }, 3000);
                  }
                } catch (recoveryError) {
                  console.error('Recovery manager error:', recoveryError);
                  setError('🌐 Network recovery system error');
                }
              })();
              break;

            case 'aborted':
              // ================================================================
              // 🆕 INTELLIGENT ABORT HANDLING - Uses smart backoff system
              // ================================================================
              {
                const recovery = speechRecoveryStateRef.current;
                const abortNow = Date.now();

                // Reset if enough time has passed since last abort
                if (abortNow - recovery.lastAbortTime > 5000) {
                  recovery.consecutiveAborts = 0;
                }

                recovery.consecutiveAborts++;
                recovery.lastAbortTime = abortNow;

                // Only log occasionally to reduce spam
                if (recovery.consecutiveAborts === 1 || recovery.consecutiveAborts % 5 === 0) {
                  console.log(`[ABORT] Recognition aborted (consecutive: ${recovery.consecutiveAborts})`);
                }

                // If too many aborts, use smart backoff (much shorter than before)
                if (recovery.consecutiveAborts > 3) {
                  enterSmartBackoff('too_many_aborts');
                  break;
                }

                // If speech was recently active, short delay before restart
                const abortTimeSinceSpeech = abortNow - lastSpeechTimeRef.current;
                if (speechActiveRef.current || abortTimeSinceSpeech < 1000) {
                  console.debug(`[ABORT] During active speech (${abortTimeSinceSpeech}ms ago) - short delay`);
                  setTimeout(() => {
                    if (continuousListeningRef.current && !speechActiveRef.current) {
                      recovery.consecutiveAborts = 0;
                      try {
                        recognitionRef.current.start();
                      } catch (e) {
                        if (!e.message?.includes('already started')) {
                          console.debug('[ABORT] Delayed restart failed:', e.message);
                      }
                    }
                  }
                }, 500);  // Reduced from 1000ms
                } else {
                  // Normal abort - let recovery system handle restart quickly
                  recovery.consecutiveAborts = 0;
                }
              }
              break;

            default:
              setError(`Speech recognition error: ${event.error}`);
          }
        }
      };

      recognitionRef.current.onend = () => {
        // Only log occasionally to reduce console spam
        const breaker = restartCircuitBreakerRef.current;
        if (breaker.count % 10 === 0 || breaker.count < 3) {
          console.log('Speech recognition ended - enforcing indefinite listening');
        }

        // Check if we should skip this restart (e.g., due to aborted error)
        if (skipNextRestartRef.current) {
          console.debug('⏭️ Skipping restart due to skipNextRestart flag');
          skipNextRestartRef.current = false;  // Reset flag
          return;  // Don't restart
        }

        // CRITICAL: Don't restart if speech was recently active - wait for result processing
        const now = Date.now();
        const timeSinceLastSpeech = now - lastSpeechTimeRef.current;
        
        if (speechActiveRef.current || timeSinceLastSpeech < 1000) {
          console.debug(`🔇 Speech was active ${timeSinceLastSpeech}ms ago - delaying restart`);
          // Delay restart to allow result processing
          setTimeout(() => {
            if (!speechActiveRef.current && continuousListeningRef.current) {
              try {
                recognitionRef.current.start();
                // Reset recovery state on successful start
                speechRecoveryStateRef.current.consecutiveAborts = 0;
                speechRecoveryStateRef.current.lastSuccessfulRecognition = Date.now();
              } catch (e) {
                if (!e.message?.includes('already started')) {
                  console.debug('Delayed restart failed:', e.message);
                }
              }
            }
          }, 500);
          return;
        }

        // ALWAYS restart if continuous listening is enabled (check ref for most current state)
        if (continuousListeningRef.current || continuousListening) {
          // Check circuit breaker to prevent infinite restart loops

          // Reset counter if window has passed
          if (now - breaker.lastReset > breaker.windowMs) {
            breaker.count = 0;
            breaker.lastReset = now;
            breaker.consecutiveFailures = 0;  // Also reset consecutive failures
          }

          // Check if we've exceeded threshold
          breaker.count++;
          if (breaker.count > breaker.threshold) {
            // Only trip if we have actual consecutive failures
            if (breaker.consecutiveFailures > 3) {
              console.warn(`🚨 Circuit breaker tripped: ${breaker.count} restarts, ${breaker.consecutiveFailures} consecutive failures`);
              console.warn('⛔ Stopping continuous listening to prevent infinite loop');
              continuousListeningRef.current = false;
              setContinuousListening(false);
              setError('⚠️ Microphone issues detected - click the mic button to restart');
              setMicStatus('error');
              skipNextRestartRef.current = false;
              return;
            }
            // Many restarts but working - continue silently
          }

          // Only log every 10th restart to reduce spam
          if (breaker.count % 10 === 0 || breaker.count < 3) {
            console.log(`♾️ Indefinite listening - restart ${breaker.count}`);
          }

          // Track restart attempts
          let restartAttempt = 0;
          const maxAttempts = 10;

          const attemptRestart = () => {
            restartAttempt++;

            try {
              recognitionRef.current.start();
              console.log(`✅ Microphone restarted successfully (attempt ${restartAttempt})`);
              setError(''); // Clear any errors
              setMicStatus('ready');

              // Reset speech timestamp and track success
              lastSpeechTimeRef.current = Date.now();
              restartCircuitBreakerRef.current.consecutiveFailures = 0;
              restartCircuitBreakerRef.current.lastSuccessTime = Date.now();
            } catch (e) {
              if (e.message && e.message.includes('already started')) {
                console.log('Microphone already active');
                restartCircuitBreakerRef.current.consecutiveFailures = 0; // Not a failure
                return;
              }

              console.log(`Restart attempt ${restartAttempt}/${maxAttempts} failed:`, e.message);

              // Keep trying with exponential backoff
              if (restartAttempt < maxAttempts && continuousListening) {
                const delay = Math.min(50 * Math.pow(2, restartAttempt), 2000);
                console.log(`Retrying in ${delay}ms...`);
                setTimeout(attemptRestart, delay);
              } else {
                restartCircuitBreakerRef.current.consecutiveFailures++;
                console.error(`Failed to restart microphone after ${maxAttempts} attempts (consecutive failures: ${restartCircuitBreakerRef.current.consecutiveFailures})`);
                
                // Only show error if we have multiple consecutive failures
                if (restartCircuitBreakerRef.current.consecutiveFailures >= 3) {
                  setError('Microphone restart failed - click to retry');
                  setMicStatus('error');
                } else {
                  // Temporary issue, will retry on next speech end
                  console.log('Single restart failure - will retry on next cycle');
                }
              }
            }
          };

          // Start restart attempts immediately
          setTimeout(attemptRestart, 50);
        } else {
          setIsListening(false);
          setIsWaitingForCommand(false);
          console.log('Continuous listening disabled - microphone stopped');
        }
      };
    } else {
      setError('Speech recognition not supported in this browser');
    }
  };

  const handleWakeWordDetected = () => {
    console.log('🎯 handleWakeWordDetected called!');

    setIsWaitingForCommand(true);
    isWaitingForCommandRef.current = true;
    setIsListening(true);

    // 🎤 Start audio capture for voice biometrics
    startVoiceAudioCapture();

    // Wake word detected - NO AUDIO FEEDBACK (removed to prevent feedback loops)

    // Don't send anything to backend - we're handling the wake word response locally
    // Just ensure WebSocket is connected for subsequent commands
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.log('WebSocket not connected, attempting to connect...');
      connectWebSocket();
    }

    // Timeout for command after 30 seconds (longer for conversation)
    setTimeout(() => {
      setIsWaitingForCommand((currentWaiting) => {
        if (currentWaiting) {
          console.log('⏱️ Command timeout - stopping listening and audio capture');
          setIsListening(false);
          isWaitingForCommandRef.current = false;
          stopVoiceAudioCapture(); // Clean up audio recording
          return false;
        }
        return currentWaiting;
      });
    }, 30000);
  };

  // 🚀 ULTRA-FAST PRIORITY COMMAND PATH
  // This function bypasses audio buffer waiting for critical commands like unlock/lock
  // Shaves 200-500ms latency by sending immediately while audio is captured in background
  const sendPriorityCommand = async (command, confidenceInfo = {}) => {
    console.log('🚀 sendPriorityCommand called with:', command);
    console.log('📡 WebSocket state:', wsRef.current ? wsRef.current.readyState : 'No WebSocket');

    const commandStartTime = Date.now();
    setTranscript(command);

    // Infer command type from either caller hint or tokenized text
    // Token-based to avoid substring collisions (e.g., "unlock" contains "lock")
    const inferPriorityCommandType = (text) => {
      try {
        const tokens = (text || '').toLowerCase().match(/[a-z']+/g) || [];
        const tokenSet = new Set(tokens);
        if (tokenSet.has('unlock')) return 'unlock';
        if (tokenSet.has('lock')) return 'lock';
        return null;
      } catch {
        return null;
      }
    };

    const inferredType = confidenceInfo.commandType || inferPriorityCommandType(command);
    const effectiveCommandType = inferredType || 'priority';

    // Check WebSocket - but don't wait long for reconnect on priority commands
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.warn('⚠️ WebSocket not connected for priority command! Quick reconnect attempt...');
      connectWebSocket();
      
      // Only wait 1 second max for priority commands (vs 5s for normal)
      for (let i = 0; i < 2; i++) {
        await new Promise(resolve => setTimeout(resolve, 500));
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          console.log('✅ WebSocket reconnected for priority command');
          break;
        }
      }
      
      // If still not connected, handle based on command type
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        // =========================================================================
        // 🔒 LOCK COMMANDS: Context-intelligent HTTP execution with dynamic timing
        // Same architecture as WebSocket path - optimistic UI + speech callback
        // =========================================================================
        if (effectiveCommandType === 'lock') {
          console.log('🔒🧠 WebSocket unavailable - context-intelligent HTTP lock');
          const lockStartTime = Date.now();

          // 1. Show response immediately (optimistic UI)
          setResponse('🔒 Locking your screen now. See you soon!');
          setIsProcessing(false);

          // 2. Build context
          const lockContext = {
            timestamp: new Date().toISOString(),
            session_duration_ms: lockStartTime - (window.sessionStartTime || lockStartTime),
            last_activity_ms: Date.now() - (window.lastActivityTime || Date.now()),
          };

          // 3. Lock execution function
          const executeLock = async () => {
            try {
              const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
              console.log('🔒🧠 Executing context-intelligent lock (HTTP fallback)');
              fetch(`${apiUrl}/lock-with-context`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ context: lockContext }),
              }).catch(() => {
                fetch(`${apiUrl}/lock-now`, { method: 'GET' }).catch(() => {});
              });
            } catch (e) {
              console.error('🔒 Lock execution error:', e);
            }
          };

          // 4. Speak with callback - execute lock when audio starts
          let lockExecuted = false;
          const onSpeechStart = () => {
            if (!lockExecuted) {
              lockExecuted = true;
              console.log(`🔒⚡ Speech started - executing lock (${Date.now() - lockStartTime}ms)`);
              executeLock();
            }
          };

          speakResponseWithCallback('Locking your screen now. See you soon!', onSpeechStart);

          // 5. Safety fallback
          setTimeout(() => {
            if (!lockExecuted) {
              console.log('🔒⏱️ Safety fallback - executing lock');
              lockExecuted = true;
              executeLock();
            }
          }, 1000);

          return; // Don't queue lock commands
        }

        // For non-lock commands, queue for later delivery
        console.warn('❌ Quick reconnect failed - queuing command for later delivery');

        // Get audio while we can for later use
        let audioForQueue = null;
        if (continuousAudioBufferRef.current && continuousAudioBufferRef.current.isRunning) {
          try {
            const bufferedAudio = await continuousAudioBufferRef.current.getBufferedAudioBase64(2000);
            if (bufferedAudio && bufferedAudio.audio) {
              audioForQueue = {
                audio: bufferedAudio.audio,
                sampleRate: bufferedAudio.sampleRate,
                mimeType: bufferedAudio.mimeType
              };
            }
          } catch (e) {
            console.log('[Priority] Could not capture audio for queue');
          }
        }

        // Queue the command with priority
        queueCommand(command, {
          ...confidenceInfo,
          priority: 'critical',
          isPriorityCommand: true,
          commandType: effectiveCommandType,
          audioData: audioForQueue
        });

        setResponse('⏳ Command queued - connecting...');
        return;
      }
    }

    // 🚀 Send command IMMEDIATELY - don't wait for audio buffer
    // Audio will be attached by the backend from its own buffer if needed
    const message = {
      type: 'command',
      text: command,
      mode: autonomousMode ? 'autonomous' : 'manual',
      priority: 'critical',  // Flag this as a priority/critical command
      metadata: {
        ...confidenceInfo,
        startTime: commandStartTime,
        isPriorityCommand: true,
        commandType: effectiveCommandType,
      }
    };

    // 🆕 CRITICAL FIX: Use fresh audio capture for voice biometric verification
    // Lock commands can bypass verification (anyone can lock), but unlock commands
    // REQUIRE audio for voice biometric verification (VBI security).
    // The old getBufferedAudioBase64() concatenates MediaRecorder chunks which produces
    // invalid WebM files (broken headers). Fresh capture creates a valid WebM container.

    // Skip audio capture for lock commands - they don't need voice verification
    // Only unlock commands require audio for voice biometric verification (VBI)
    const skipAudioForCommand = (effectiveCommandType === 'lock'); 

    if (!skipAudioForCommand && continuousAudioBufferRef.current && continuousAudioBufferRef.current.isRunning) {
      try {
        console.log('🎤 [Priority] Starting fresh audio capture for voice biometrics...');

        // Use getFreshAudioBase64 which creates a NEW MediaRecorder for valid WebM
        // 2 seconds is enough for voice biometric verification
        const freshAudio = await Promise.race([
          continuousAudioBufferRef.current.getFreshAudioBase64(2000),
          new Promise((_, reject) => setTimeout(() => reject(new Error('Fresh capture timeout')), 2500))
        ]);

        if (freshAudio && freshAudio.audio && freshAudio.audio.length > 500) {
          message.audio_data = freshAudio.audio;
          message.sample_rate = freshAudio.sampleRate;
          message.mime_type = freshAudio.mimeType;
          message.audio_source = 'fresh_capture';  // Flag to indicate valid WebM
          console.log(`🎤 [Priority] ✅ Got fresh audio: ${freshAudio.base64Length} chars (valid WebM)`);
        }
      } catch (e) {
        // Fresh capture timeout - fall back to buffered audio (may have issues)
        console.log('🎤 [Priority] Fresh capture timeout, trying buffered audio...');
        try {
          const bufferedAudio = await continuousAudioBufferRef.current.getBufferedAudioBase64(2000);
          if (bufferedAudio && bufferedAudio.audio && bufferedAudio.audio.length > 500) {
            message.audio_data = bufferedAudio.audio;
            message.sample_rate = bufferedAudio.sampleRate;
            message.mime_type = bufferedAudio.mimeType;
            message.audio_source = 'continuous_buffer_fallback';
            console.log(`🎤 [Priority] Got fallback buffered audio: ${bufferedAudio.base64Length} chars`);
          }
        } catch (e2) {
          console.log('🎤 [Priority] All audio capture failed');
        }
      }
    } else if (skipAudioForCommand) {
      console.log(`⚡ [Priority] Skipping audio capture for ${effectiveCommandType} command (lock doesn't need verification)`);
    }

    try {
      // =========================================================================
      // 🔒 LOCK COMMANDS: Context-intelligent execution with dynamic speech timing
      //
      // Architecture (NOT a workaround - this is the correct pattern):
      // - Screen lock is a "terminal operation" - no response can arrive after lock
      // - Browser suspends when screen locks, breaking WebSocket
      // - Therefore: Show/speak response BEFORE lock, execute lock when speech starts
      //
      // Flow:
      // 1. Show response immediately (optimistic UI)
      // 2. Start speech synthesis
      // 3. When audio ACTUALLY starts playing (onplay event) → execute lock
      // 4. Lock executes while user hears "Locking your screen..."
      // =========================================================================
      if (effectiveCommandType === 'lock') {
        console.log('🔒🧠 Lock command - context-intelligent execution with dynamic timing');
        const lockStartTime = Date.now();

        // 1. Show final response IMMEDIATELY (optimistic UI - correct pattern for terminal ops)
        const lockMessage = '🔒 Locking your screen now. See you soon!';
        setResponse(lockMessage);
        setIsProcessing(false);

        // 2. Build context for intelligent lock endpoint
        const lockContext = {
          timestamp: new Date().toISOString(),
          session_duration_ms: lockStartTime - (window.sessionStartTime || lockStartTime),
          last_activity_ms: Date.now() - (window.lastActivityTime || Date.now()),
        };

        // 3. Execute lock function (called when speech starts OR as fallback)
        const executeLock = async () => {
          try {
            const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;

            // Use context-intelligent endpoint
            console.log('🔒🧠 Executing context-intelligent lock');
            fetch(`${apiUrl}/lock-with-context`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ context: lockContext }),
            }).catch(() => {
              // Fallback to simple lock if context endpoint fails
              console.log('🔒 Context lock failed, using simple lock');
              fetch(`${apiUrl}/lock-now`, { method: 'GET' }).catch(() => {});
            });
          } catch (e) {
            console.error('🔒 Lock execution error:', e);
          }
        };

        // 4. Speak with callback - execute lock when audio STARTS (dynamic timing)
        // This replaces the hardcoded 300ms delay with actual audio start detection
        let lockExecuted = false;
        const onSpeechStart = () => {
          if (!lockExecuted) {
            lockExecuted = true;
            console.log(`🔒⚡ Speech started - executing lock (${Date.now() - lockStartTime}ms since command)`);
            executeLock();
          }
        };

        // Use the callback version of speakResponse
        speakResponseWithCallback('Locking your screen now. See you soon!', onSpeechStart);

        // 5. Safety fallback: If speech doesn't start within 1 second, execute lock anyway
        // This handles cases where audio fails to load or play
        setTimeout(() => {
          if (!lockExecuted) {
            console.log('🔒⏱️ Safety fallback - speech did not start in 1s, executing lock');
            lockExecuted = true;
            executeLock();
          }
        }, 1000);

        return; // Don't continue to normal WebSocket flow
      }

      wsRef.current.send(JSON.stringify(message));
      console.log(`🚀 Priority command sent in ${Date.now() - commandStartTime}ms`);
      if (effectiveCommandType === 'unlock') {
        setResponse('🔓 Unlocking...');
        setIsProcessing(true);
      } else {
        setResponse('⚡ Processing...');
        setIsProcessing(true);
      }
    } catch (sendError) {
      console.error('[WS] Failed to send priority command:', sendError);
      sendTextCommand(command);
    }
  };

  const handleVoiceCommand = async (command, confidenceInfo = {}) => {
    console.log('🎯 handleVoiceCommand called with:', command);
    console.log('📡 WebSocket state:', wsRef.current ? wsRef.current.readyState : 'No WebSocket');

    // CRITICAL: Check WebSocket connection before proceeding
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.warn('⚠️ WebSocket not connected! Attempting to reconnect...');
      connectWebSocket();
      setError('Reconnecting to JARVIS...');

      // Wait for connection with timeout
      for (let i = 0; i < 10; i++) {
        await new Promise(resolve => setTimeout(resolve, 500));
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          console.log('✅ WebSocket reconnected successfully');
          setError('');
          break;
        }
      }

      // If still not connected after 5 seconds, queue the command
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        console.warn('❌ Failed to reconnect WebSocket - queuing command');

        // Try to get audio for the queued command
        let audioForQueue = null;
        if (continuousAudioBufferRef.current && continuousAudioBufferRef.current.isRunning) {
          try {
            const bufferedAudio = await continuousAudioBufferRef.current.getBufferedAudioBase64(3000);
            if (bufferedAudio && bufferedAudio.audio) {
              audioForQueue = {
                audio: bufferedAudio.audio,
                sampleRate: bufferedAudio.sampleRate,
                mimeType: bufferedAudio.mimeType
              };
            }
          } catch (e) {
            console.log('[Voice] Could not capture audio for queue');
          }
        }

        // Queue the command
        queueCommand(command, {
          ...confidenceInfo,
          audioData: audioForQueue
        });

        setError('⏳ Command queued - will send when connected');
        setResponse('⏳ Queued: ' + command);
        return;
      }
    }

    setTranscript(command);

    // Track command start time for adaptive learning
    const commandStartTime = Date.now();

    // 🆕 PRIORITY: Get audio from continuous buffer FIRST (always has audio, never misses)
    let audioData = null;
    let audioSource = 'none';

    // Try continuous buffer first - it's always recording and never misses
    if (continuousAudioBufferRef.current && continuousAudioBufferRef.current.isRunning) {
      try {
        console.log('🎤 [ContinuousBuffer] Getting pre-buffered audio (last 3 seconds)...');
        const bufferedAudio = await continuousAudioBufferRef.current.getBufferedAudioBase64(3000);
        if (bufferedAudio && bufferedAudio.audio && bufferedAudio.audio.length > 1000) {
          audioData = {
            audio: bufferedAudio.audio,
            sampleRate: bufferedAudio.sampleRate,
            mimeType: bufferedAudio.mimeType
          };
          audioSource = 'continuous_buffer';
          console.log(`🎤 [ContinuousBuffer] ✅ Got ${bufferedAudio.base64Length} chars of pre-buffered audio (${bufferedAudio.durationMs}ms)`);
        }
      } catch (e) {
        console.warn('🎤 [ContinuousBuffer] Failed to get buffered audio:', e);
      }
    }

    // Fallback to legacy audio capture if continuous buffer doesn't have good data
    if (!audioData || audioData.audio.length < 1000) {
      console.log(`🎤 [VoiceCapture] Trying legacy capture (isRecording: ${isRecordingVoiceRef.current})`);
      const legacyAudioData = await stopVoiceAudioCapture();
      if (legacyAudioData && legacyAudioData.audio && legacyAudioData.audio.length > 1000) {
        audioData = legacyAudioData;
        audioSource = 'legacy_capture';
        console.log(`🎤 [VoiceCapture] ✅ Legacy audio captured: ${audioData.audio?.length || 0} chars, ${audioData.sampleRate}Hz`);
      }
    }

    // Log final audio status
    if (audioData) {
      console.log(`🎤 [Voice] ✅ Audio ready from ${audioSource}: ${audioData.audio?.length || 0} chars`);
    } else {
      // Even without captured audio, we still send the command - it just won't have biometric verification
      console.warn('🎤 [Voice] ⚠️ No audio data captured - command will be sent without voice biometric verification');
    }

    // 🎤 Restart legacy audio capture if continuous listening is still active
    if (continuousListeningRef.current && !isRecordingVoiceRef.current) {
      console.log('🎤 [VoiceCapture] Restarting legacy audio capture for next command');
      startVoiceAudioCapture();
    }

    // Check for autonomy activation commands
    const lowerCommand = command.toLowerCase();
    if (lowerCommand.includes('activate full autonomy') ||
      lowerCommand.includes('enable autonomous mode') ||
      lowerCommand.includes('activate autonomy') ||
      lowerCommand.includes('iron man mode') ||
      lowerCommand.includes('activate all systems')) {
      // Direct autonomy activation
      toggleAutonomousMode();

      // Record successful command execution in adaptive system
      adaptiveVoiceDetection.recordCommandExecution(command, {
        success: true,
        confidence: confidenceInfo.confidence || 0.9,
        executionTime: Date.now() - commandStartTime,
        wasRetry: false,
      });

      return;
    }

    // ═══════════════════════════════════════════════════════════════════
    // v8.0: SELF-VOICE CHECK BEFORE SENDING COMMAND
    // ═══════════════════════════════════════════════════════════════════
    // Check if JARVIS is speaking or in cooldown before sending.
    // This prevents sending commands that might include JARVIS's own voice.
    // ═══════════════════════════════════════════════════════════════════
    try {
      const { getUnifiedWebSocketService } = require('../services/UnifiedWebSocketService');
      const wsService = getUnifiedWebSocketService();
      if (wsService && wsService.speechState) {
        const { isSpeaking, inCooldown, cooldownRemainingMs } = wsService.speechState;
        if (isSpeaking || inCooldown) {
          console.log(`🔇 [Self-Voice v8] Command blocked - JARVIS is ${isSpeaking ? 'speaking' : `in cooldown (${cooldownRemainingMs}ms remaining)`}`);
          console.log(`🔇 [Self-Voice v8] Blocked command: "${command.substring(0, 50)}..."`);
          // Don't send the command - it's likely JARVIS's own voice
          return;
        }
      }
    } catch (e) {
      // Service not available - continue with command
      console.debug('[Self-Voice v8] Speech state check unavailable');
    }
    
    // Send via WebSocket with audio_data for voice biometrics
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      const message = {
        type: 'command',
        text: command,
        mode: autonomousMode ? 'autonomous' : 'manual',
        metadata: {
          ...confidenceInfo,
          startTime: commandStartTime,
        }
      };

      // Include audio_data if available for voice biometric verification
      if (audioData) {
        message.audio_data = audioData.audio; // Send base64 audio
        message.sample_rate = audioData.sampleRate; // Send actual sample rate from browser
        message.mime_type = audioData.mimeType; // Send MIME type for decoding
        message.audio_source = audioSource; // Track where audio came from for debugging
        console.log(`🎤 Sending command with audio data for voice verification (source: ${audioSource}, ${audioData.sampleRate}Hz, ${audioData.mimeType})`);
      } else {
        console.warn('🎤 Sending command WITHOUT audio data - voice biometric verification will not be possible');
      }

      try {
        wsRef.current.send(JSON.stringify(message));
        setResponse('⚙️ Processing...');
        
        // Set a timeout to detect if backend doesn't respond (zombie WebSocket detection)
        setTimeout(() => {
          // If still showing Processing after 10 seconds, WebSocket might be dead
          if (response === '⚙️ Processing...' || isProcessing) {
            console.warn('[WS] No response after 10s - WebSocket might be dead, forcing reconnect...');
            if (wsRef.current) {
              wsRef.current.close(); // Force close to trigger reconnect
            }
            // Fallback to REST API
            sendTextCommand(command);
          }
        }, 10000);
      } catch (sendError) {
        console.error('[WS] Failed to send command via WebSocket:', sendError);
        // Fallback to REST API
        sendTextCommand(command);
      }
    } else {
      // Fallback to REST API if WebSocket not connected
      sendTextCommand(command);
    }

    // Don't immediately reset waiting state - let the response handler do it
    // This ensures we don't miss the response
    console.log('Command sent, waiting for response...');
  };

  // Handle text command submission
  const handleTextCommandSubmit = (e) => {
    e.preventDefault();

    if (!textCommand.trim()) return;

    console.log('[TEXT-CMD] Submitting typed command:', textCommand);

    // Set transcript to show what was typed
    setTranscript(textCommand);

    // Send via WebSocket
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({
          type: 'command',
          text: textCommand,
          mode: autonomousMode ? 'autonomous' : 'manual',
          metadata: {
            source: 'text_input',
            timestamp: Date.now()
          }
        }));
        setResponse('⚙️ Processing...');
        setIsProcessing(true);
        
        // Set a timeout to detect zombie WebSocket (no response after 10s)
        setTimeout(() => {
          if (isProcessing) {
            console.warn('[WS] No response after 10s for text command - reconnecting...');
            if (wsRef.current) {
              wsRef.current.close(); // Force reconnect
            }
            setResponse('❌ Connection lost - please try again');
            setIsProcessing(false);
          }
        }, 10000);
      } catch (sendError) {
        console.error('[WS] Failed to send text command:', sendError);
        setResponse('❌ Failed to send command');
        setIsProcessing(false);
      }
    } else {
      setResponse('❌ Not connected to JARVIS');
    }

    // Clear the input
    setTextCommand('');
  };

  const activateJarvis = async () => {
    // Ensure config is ready
    if (!configReady || !API_URL) {
      console.log('JarvisVoice: Waiting for config before activating...');
      await configPromise;
    }

    try {
      const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
      console.log('JarvisVoice: Activating JARVIS at:', apiUrl);
      const response = await fetch(`${apiUrl}/voice/jarvis/activate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({})
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      console.log('JarvisVoice: Activation response:', data);
      setJarvisStatus('activating');
      setTimeout(async () => {
        setJarvisStatus('online');

        // CRITICAL: Ensure WebSocket is connected before proceeding
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
          console.log('🔌 WebSocket not connected after activation, connecting now...');
          connectWebSocket();
          // Wait a bit for connection to establish
          await new Promise(resolve => setTimeout(resolve, 1000));
        } else {
          console.log('✅ WebSocket already connected');
        }

        // Initialize wake word service if not already done
        if (!wakeWordServiceRef.current) {
          await initializeWakeWordService();
        }

        // NO STARTUP GREETING - removed to prevent feedback loops

        // Enable continuous listening for wake word detection
        console.log('🎙️ Enabling continuous listening for wake word...');
        enableContinuousListening();
      }, 2000);
    } catch (err) {
      console.error('Failed to activate JARVIS:', err);
      setError('Failed to activate JARVIS');
    }
  };

  const toggleAutonomousMode = async () => {
    const newMode = !autonomousMode;
    setAutonomousMode(newMode);

    if (newMode) {
      // Enable autonomous mode - NO AUDIO FEEDBACK (removed to prevent feedback loops)

      // Connect vision system
      if (visionConnectionRef.current) {
        console.log('Connecting vision system...');
        await visionConnectionRef.current.connect();
        // Start monitoring immediately
        if (visionConnectionRef.current.isConnected) {
          visionConnectionRef.current.startMonitoring();
        }
      }

      // Enable continuous listening
      enableContinuousListening();

      // Notify backend about autonomy mode
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'set_mode',
          mode: 'autonomous'
        }));
      }
    } else {
      // Disable autonomous mode - NO AUDIO FEEDBACK (removed to prevent feedback loops)

      // Stop vision monitoring
      if (visionConnectionRef.current && visionConnectionRef.current.isConnected) {
        visionConnectionRef.current.stopMonitoring();
        visionConnectionRef.current.disconnect();
      }
      setVisionConnected(false);

      // Notify backend
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'set_mode',
          mode: 'manual'
        }));
      }

      // Keep listening if user wants
    }
  };

  // ═══════════════════════════════════════════════════════════════
  // 🎤 UNIFIED VOICE CAPTURE - Audio Recording for Voice Biometrics
  // ═══════════════════════════════════════════════════════════════

  const startVoiceAudioCapture = async () => {
    if (isRecordingVoiceRef.current) {
      console.log('🎤 [VoiceCapture] Already recording');
      return;
    }

    // CRITICAL: Set flag IMMEDIATELY to prevent race conditions
    // This ensures stopVoiceAudioCapture waits for recording to actually start
    isRecordingVoiceRef.current = true;
    console.log('🎤 [VoiceCapture] Recording flag set - starting audio capture for voice biometrics...');

    // Retry configuration
    const maxRetries = 3;
    const retryDelays = [100, 300, 1000]; // Exponential-ish backoff
    
    for (let attempt = 0; attempt < maxRetries; attempt++) {
      try {
        // Clean up any existing stream first to prevent resource conflicts
        if (voiceAudioStreamRef.current) {
          console.log('🎤 [VoiceCapture] Cleaning up previous stream...');
          try {
            voiceAudioStreamRef.current.getTracks().forEach(track => {
              track.stop();
            });
          } catch (e) {
            console.debug('🎤 [VoiceCapture] Previous stream cleanup:', e.message);
          }
          voiceAudioStreamRef.current = null;
        }
        
        // Clean up any existing recorder
        if (voiceAudioRecorderRef.current) {
          try {
            if (voiceAudioRecorderRef.current.state !== 'inactive') {
              voiceAudioRecorderRef.current.stop();
            }
          } catch (e) {
            console.debug('🎤 [VoiceCapture] Previous recorder cleanup:', e.message);
          }
          voiceAudioRecorderRef.current = null;
        }
        
        // Small delay to ensure resources are fully released
        if (attempt > 0) {
          await new Promise(resolve => setTimeout(resolve, retryDelays[attempt - 1]));
          console.log(`🎤 [VoiceCapture] Retry attempt ${attempt + 1}/${maxRetries}...`);
        }
        
        // Get microphone access with robust constraints
        voiceAudioStreamRef.current = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,  // Mono
            sampleRate: { ideal: 16000, min: 8000, max: 48000 }, // Flexible sample rate
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
          }
        });

        // Try different MIME types for compatibility
        const mimeTypes = [
          'audio/webm;codecs=opus',
          'audio/webm',
          'audio/ogg;codecs=opus',
          'audio/wav'
        ];

        let selectedMimeType = mimeTypes.find(type => MediaRecorder.isTypeSupported(type));
        if (!selectedMimeType) {
          console.warn('🎤 [VoiceCapture] No supported MIME type, using default');
          selectedMimeType = '';
        }

        voiceAudioRecorderRef.current = new MediaRecorder(
          voiceAudioStreamRef.current,
          selectedMimeType ? { mimeType: selectedMimeType } : {}
        );

        // Collect audio chunks
        voiceAudioChunksRef.current = [];
        voiceAudioRecorderRef.current.ondataavailable = (event) => {
          if (event.data.size > 0) {
            voiceAudioChunksRef.current.push(event.data);
          }
        };
        
        // Handle recorder errors gracefully
        voiceAudioRecorderRef.current.onerror = (event) => {
          console.error('🎤 [VoiceCapture] MediaRecorder error:', event.error);
          // Don't set mic error status - the recorder is separate from speech recognition
        };

        // Start recording with 100ms chunks for continuous capture
        voiceAudioRecorderRef.current.start(100);

        console.log(`🎤 [VoiceCapture] Recording started (${selectedMimeType || 'default'})`);
        return; // Success - exit retry loop
        
      } catch (error) {
        console.warn(`🎤 [VoiceCapture] Attempt ${attempt + 1} failed:`, error.message);
        
        // Check if it's a permission error (don't retry)
        if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
          console.error('🎤 [VoiceCapture] Microphone permission denied - not retrying');
          isRecordingVoiceRef.current = false;
          // Don't set micStatus to error - speech recognition might still work
          return;
        }
        
        // Check if it's a device error (don't retry)
        if (error.name === 'NotFoundError') {
          console.error('🎤 [VoiceCapture] No microphone found - not retrying');
          isRecordingVoiceRef.current = false;
          return;
        }
        
        // For other errors, continue to next retry attempt
        if (attempt === maxRetries - 1) {
          console.error('🎤 [VoiceCapture] Failed after all retries:', error);
          isRecordingVoiceRef.current = false;
          // Don't set mic error - voice commands still work, just without biometric audio
          console.warn('🎤 [VoiceCapture] Voice biometric capture unavailable - commands will still work');
        }
      }
    }
  };

  const stopVoiceAudioCapture = async () => {
    if (!isRecordingVoiceRef.current) {
      console.log('🎤 [VoiceCapture] Not recording - no audio to capture');
      return null;
    }

    console.log('🎤 [VoiceCapture] Stopping audio capture...');

    // Wait for recorder to be initialized if it's still starting
    let waitAttempts = 0;
    const maxWaitAttempts = 20;
    while (!voiceAudioRecorderRef.current && waitAttempts < maxWaitAttempts) {
      console.log(`🎤 [VoiceCapture] Waiting for recorder to initialize (${waitAttempts + 1}/${maxWaitAttempts})...`);
      await new Promise(resolve => setTimeout(resolve, 50));
      waitAttempts++;
    }

    if (!voiceAudioRecorderRef.current) {
      console.warn('🎤 [VoiceCapture] Recorder never initialized, no audio captured');
      isRecordingVoiceRef.current = false;
      return null;
    }

    return new Promise((resolve) => {
      // Set a timeout to prevent hanging forever
      const timeoutId = setTimeout(() => {
        console.warn('🎤 [VoiceCapture] Stop operation timed out, cleaning up...');
        cleanupAndResolve(null);
      }, 5000); // 5 second timeout
      
      const cleanupAndResolve = (result) => {
        clearTimeout(timeoutId);
        
        // Stop audio stream AFTER getting the data
        if (voiceAudioStreamRef.current) {
          try {
            voiceAudioStreamRef.current.getTracks().forEach(track => {
              track.stop();
            });
          } catch (e) {
            console.debug('🎤 [VoiceCapture] Stream cleanup:', e.message);
          }
          voiceAudioStreamRef.current = null;
        }
        
        // Clear recorder reference
        voiceAudioRecorderRef.current = null;
        
        // Reset recording flag
        isRecordingVoiceRef.current = false;
        
        resolve(result);
      };
      
      try {
        if (voiceAudioRecorderRef.current && voiceAudioRecorderRef.current.state !== 'inactive') {
          // Store reference to mimeType before clearing
          const mimeType = voiceAudioRecorderRef.current.mimeType || 'audio/webm';
          
          voiceAudioRecorderRef.current.onstop = async () => {
            console.log(`🎤 [VoiceCapture] Recording stopped, captured ${voiceAudioChunksRef.current.length} chunks`);

            if (voiceAudioChunksRef.current.length === 0) {
              cleanupAndResolve(null);
              return;
            }

            try {
              // Create audio blob
              const audioBlob = new Blob(voiceAudioChunksRef.current, { type: mimeType });
              console.log(`🎤 [VoiceCapture] Audio blob created: ${audioBlob.size} bytes`);

              // Get actual sample rate from the audio stream (browser may override requested rate)
              const actualSampleRate = voiceAudioStreamRef.current?.getAudioTracks()[0]?.getSettings()?.sampleRate || 16000;
              console.log(`🎤 [VoiceCapture] Actual sample rate: ${actualSampleRate}Hz`);

              // Convert to base64
              const reader = new FileReader();
              reader.onloadend = () => {
                try {
                  const base64Audio = reader.result.split(',')[1]; // Remove data:audio/...;base64, prefix
                  console.log(`🎤 [VoiceCapture] Converted to base64: ${base64Audio?.length || 0} chars`);
                  
                  // Clear audio chunks
                  voiceAudioChunksRef.current = [];
                  
                  // Return object with audio data and metadata
                  cleanupAndResolve({
                    audio: base64Audio,
                    sampleRate: actualSampleRate,
                    mimeType: mimeType
                  });
                } catch (e) {
                  console.error('🎤 [VoiceCapture] Error processing audio:', e);
                  cleanupAndResolve(null);
                }
              };
              reader.onerror = () => {
                console.error('🎤 [VoiceCapture] Failed to convert to base64');
                cleanupAndResolve(null);
              };
              reader.readAsDataURL(audioBlob);
            } catch (e) {
              console.error('🎤 [VoiceCapture] Error creating audio blob:', e);
              cleanupAndResolve(null);
            }
          };

          voiceAudioRecorderRef.current.stop();
        } else {
          console.log('🎤 [VoiceCapture] Recorder already inactive');
          cleanupAndResolve(null);
        }
      } catch (e) {
        console.error('🎤 [VoiceCapture] Error stopping recorder:', e);
        cleanupAndResolve(null);
      }
    });
  };

  const enableContinuousListening = async () => {
    if (recognitionRef.current) {
      setContinuousListening(true);
      continuousListeningRef.current = true;
      setIsListening(true);

      // 🆕 Start continuous audio pre-buffer FIRST (for first-attempt voice recognition)
      if (!continuousAudioBufferRef.current) {
        console.log('🎤 [ContinuousBuffer] Initializing continuous audio pre-buffer for zero-gap voice capture');
        continuousAudioBufferRef.current = getContinuousAudioBuffer({
          bufferDurationMs: 5000, // Keep last 5 seconds of audio
          sampleRate: 16000,
          chunkIntervalMs: 100,
          voiceActivityThreshold: 0.015, // Lower threshold for better sensitivity
          silenceThresholdMs: 1500,
          onVoiceStart: () => {
            console.log('🎤 [ContinuousBuffer] Voice activity detected - buffer ready');
          },
          onVoiceEnd: () => {
            console.log('🎤 [ContinuousBuffer] Voice ended - buffer preserved');
          },
          onError: (error) => {
            console.error('🎤 [ContinuousBuffer] Error:', error);
          }
        });

        try {
          await continuousAudioBufferRef.current.start();
          console.log('🎤 [ContinuousBuffer] Started successfully - audio is now being pre-captured');

          // Calibrate noise floor after 1 second
          setTimeout(() => {
            if (continuousAudioBufferRef.current) {
              continuousAudioBufferRef.current.calibrateNoiseFloor(1000);
            }
          }, 1000);
        } catch (e) {
          console.error('🎤 [ContinuousBuffer] Failed to start:', e);
          // Fall back to legacy audio capture
        }
      }

      // 🎤 Start legacy audio capture for voice biometrics as backup
      if (!isRecordingVoiceRef.current) {
        console.log('🎤 Starting legacy audio capture for voice biometrics');
        startVoiceAudioCapture();
      }

      // Configure for INDEFINITE continuous listening
      recognitionRef.current.continuous = true;
      recognitionRef.current.interimResults = true;

      // Override any browser timeouts
      if ('speechTimeout' in recognitionRef.current) {
        recognitionRef.current.speechTimeout = 999999999;
      }
      if ('noSpeechTimeout' in recognitionRef.current) {
        recognitionRef.current.noSpeechTimeout = 999999999;
      }

      try {
        recognitionRef.current.start();
        console.log('♾️ INDEFINITE listening enabled - microphone will NEVER turn off automatically');

        // Set up keep-alive mechanism
        const keepAliveInterval = setInterval(() => {
          if (!continuousListening) {
            clearInterval(keepAliveInterval);
            return;
          }

          // Check if recognition is still active
          console.log('🔄 Keep-alive check - ensuring microphone stays active');

          // If no speech for a while, send a dummy event to keep it alive
          const timeSinceLastSpeech = Date.now() - lastSpeechTimeRef.current;
          if (timeSinceLastSpeech > 30000) { // 30 seconds
            console.log('⚡ Triggering keep-alive pulse');
            lastSpeechTimeRef.current = Date.now();

            // Force a restart if needed
            if (!isListening) {
              console.log('🔄 Keep-alive: Restarting stopped recognition');
              try {
                recognitionRef.current.stop();
                setTimeout(() => {
                  recognitionRef.current.start();
                  setIsListening(true);
                }, 100);
              } catch (e) {
                console.log('Keep-alive restart:', e.message);
              }
            }
          }
        }, 5000); // Check every 5 seconds

        // Store interval reference for cleanup
        recognitionRef.current._keepAliveInterval = keepAliveInterval;

        // Enhanced notification
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification('JARVIS Microphone Active ♾️', {
            body: 'Microphone will stay on indefinitely. Say "Hey JARVIS" anytime.',
            icon: '/favicon.ico',
            requireInteraction: true // Keep notification visible
          });
        }

        // Visual indicator in console
        console.log('%c🎤 MICROPHONE STATUS: INDEFINITE MODE ACTIVE',
          'color: #00ff00; font-size: 16px; font-weight: bold; background: #000; padding: 10px;');

        // Set initial timestamp
        lastSpeechTimeRef.current = Date.now();

      } catch (e) {
        if (e.message && e.message.includes('already started')) {
          console.log('Recognition already active - good!');
        } else {
          console.error('Failed to start indefinite listening:', e);
          setError('Failed to start microphone - retrying...');

          // Retry after a moment
          setTimeout(() => enableContinuousListening(), 1000);
        }
      }
    }
  };

  const disableContinuousListening = () => {
    setContinuousListening(false);
    continuousListeningRef.current = false;
    setIsListening(false);
    setIsWaitingForCommand(false);
    isWaitingForCommandRef.current = false;

    if (recognitionRef.current) {
      // Clear keep-alive interval
      if (recognitionRef.current._keepAliveInterval) {
        clearInterval(recognitionRef.current._keepAliveInterval);
        recognitionRef.current._keepAliveInterval = null;
        console.log('🛑 Keep-alive mechanism stopped');
      }

      // Stop recognition
      try {
        recognitionRef.current.stop();
        console.log('🔴 Microphone stopped - indefinite listening disabled');
      } catch (e) {
        console.log('Stop recognition:', e.message);
      }
    }

    // Visual indicator in console
    console.log('%c🎤 MICROPHONE STATUS: STOPPED',
      'color: #ff0000; font-size: 16px; font-weight: bold; background: #000; padding: 10px;');
  };

  const startListening = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      mediaRecorderRef.current = new MediaRecorder(stream);
      audioChunksRef.current = [];

      mediaRecorderRef.current.ondataavailable = (event) => {
        audioChunksRef.current.push(event.data);
      };

      mediaRecorderRef.current.onstop = async () => {
        const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/wav' });
        await sendAudioToJarvis(audioBlob);
      };

      mediaRecorderRef.current.start();
      setIsListening(true);

      // Auto-stop after 5 seconds
      setTimeout(() => {
        if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
          stopListening();
        }
      }, 5000);
    } catch (err) {
      console.error('Failed to start recording:', err);
      setError('Microphone access denied');
    }
  };

  const stopListening = () => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
      mediaRecorderRef.current.stop();
      setIsListening(false);

      // Stop all tracks
      mediaRecorderRef.current.stream.getTracks().forEach(track => track.stop());
    }
  };

  const sendAudioToJarvis = async (audioBlob) => {
    setIsProcessing(true);

    // Convert blob to base64
    const reader = new FileReader();
    reader.readAsDataURL(audioBlob);
    reader.onloadend = () => {
      const base64Audio = reader.result.split(',')[1];

      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'audio',
          data: base64Audio
        }));
      }
    };
  };

  const sendTextCommand = async (text) => {
    if (!text.trim()) return;

    console.log('[TEXT COMMAND] Sending typed command:', text);

    // Clear typing state and update interaction timestamp
    setIsTyping(false);
    setLastUserInteraction(Date.now());

    setTranscript(text);
    console.log('[TEXT COMMAND] Set transcript:', text);
    setIsProcessing(true);
    setResponse('');  // Clear previous response

    // Use WebSocket if connected
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      console.log('[TEXT COMMAND] Sending via WebSocket');
      wsRef.current.send(JSON.stringify({
        type: 'command',
        text: text,
        mode: autonomousMode ? 'autonomous' : 'manual'
      }));
      // Response will come through WebSocket message handler
    } else {
      // WebSocket not connected
      console.log('[TEXT COMMAND] WebSocket not connected');
      setError('Not connected to JARVIS. Please refresh the page.');
      setIsProcessing(false);
    }
  };

  const playAudioResponse_UNUSED = async (text) => {
    console.log('Playing audio response:', text.substring(0, 100) + '...');

    try {
      // For long text, always use POST method to avoid URL length limits
      // GET requests have a limit of ~2000 characters in the URL
      const usePost = text.length > 500 || text.includes('\n');

      if (!usePost) {
        // Short text: Use GET method with URL (simpler and more reliable)
        const audioUrl = `${API_URL}/audio/speak/${encodeURIComponent(text)}`;
        const audio = new Audio(audioUrl);
        audio.volume = 1.0;

        setIsJarvisSpeaking(true);

        audio.onended = () => {
          console.log('Audio playback completed');
          setIsJarvisSpeaking(false);
        };

        audio.onerror = async (e) => {
          console.warn('GET method failed, trying POST with blob...');

          // Fallback to POST
          await playAudioUsingPost(text);
        };

        await audio.play();
      } else {
        // Long text: Use POST method directly
        await playAudioUsingPost(text);
      }
    } catch (error) {
      console.error('Audio playback failed:', error);
      setIsJarvisSpeaking(false);
    }
  };

  const playAudioUsingPost = async (text, onStartCallback = null) => {
    console.log('[JARVIS Audio] POST: Attempting to play audio via POST');
    try {
      const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
      const response = await fetch(`${apiUrl}/audio/speak`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ text })
      });

      console.log('[JARVIS Audio] POST: Response status:', response.status);
      console.log('[JARVIS Audio] POST: Content-Type:', response.headers.get('content-type'));

      if (response.ok) {
        // Get audio data as blob
        const blob = await response.blob();
        console.log('[JARVIS Audio] POST: Received audio blob:', blob.size, 'bytes');

        const audioUrl = URL.createObjectURL(blob);

        const audio2 = new Audio(audioUrl);
        audio2.volume = 1.0;

        setIsJarvisSpeaking(true);
        isSpeakingRef.current = true;
        // 🔇 SELF-VOICE SUPPRESSION v2: PAUSE recognition (POST method)
        pauseRecognitionForSpeech(text);
        recordSpokenText(text);

        audio2.onplay = () => {
          console.log('[JARVIS Audio] POST: Playback started');
          // Call the callback when audio ACTUALLY starts playing (perfect sync!)
          if (onStartCallback && typeof onStartCallback === 'function') {
            console.log('[JARVIS Audio] Calling start callback - text will appear NOW');
            onStartCallback();
          }
        };

        audio2.onended = () => {
          console.log('[JARVIS Audio] POST: Playback completed');
          setIsJarvisSpeaking(false);
          isSpeakingRef.current = false;
          // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended
          recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
          URL.revokeObjectURL(audioUrl); // Clean up
          // Process next in queue after a small delay
          setTimeout(() => processNextInSpeechQueue(), 200);
        };

        audio2.onerror = (e) => {
          console.error('[JARVIS Audio] POST: Playback error:', e);
          if (audio2.error) {
            console.error('[JARVIS Audio] POST: Error details:', {
              code: audio2.error.code,
              message: audio2.error.message
            });
          }
          setIsJarvisSpeaking(false);
          isSpeakingRef.current = false;
          // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended (even on error)
          recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
          URL.revokeObjectURL(audioUrl); // Clean up
          // Process next in queue even after error
          setTimeout(() => processNextInSpeechQueue(), 200);
        };

        await audio2.play();
        console.log('[JARVIS Audio] POST: Playing audio successfully');
      } else {
        throw new Error(`Audio generation failed: ${response.status}`);
      }
    } catch (postError) {
      console.error('[JARVIS Audio] POST: Failed:', postError);
      setIsJarvisSpeaking(false);
      isSpeakingRef.current = false;
      // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended (even on error)
      recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
      // Process next in queue even after error
      setTimeout(() => processNextInSpeechQueue(), 200);
    }
  };



  // Speech queue to prevent overlapping
  const speechQueueRef = useRef([]);
  const isSpeakingRef = useRef(false);

  // ============================================================
  // 🔇 SELF-VOICE SUPPRESSION SYSTEM
  // ============================================================
  // Prevents JARVIS from hearing and responding to its own voice
  // Uses multiple strategies for robust echo cancellation:
  // 1. Speaking state check - ignore all input while speaking
  // 2. Cooldown period - ignore input for 500ms after speech ends
  // 3. Text similarity - detect and filter echoes of what JARVIS just said
  // 4. Audio fingerprinting - match acoustic patterns (future)
  const lastSpokenTextRef = useRef('');           // What JARVIS just said
  const lastSpokenTimeRef = useRef(0);            // When speaking started
  const speakingEndTimeRef = useRef(0);           // When speaking ended
  const selfVoiceCooldownMs = 800;                // Cooldown after speech ends (ms)
  const recentSpokenTextsRef = useRef([]);        // History of recent spoken texts
  const maxRecentSpokenTexts = 5;                 // Keep last 5 spoken texts for matching

  /**
   * Calculate text similarity using Levenshtein distance ratio
   * Returns a value between 0 (completely different) and 1 (identical)
   */
  const calculateTextSimilarity = (text1, text2) => {
    if (!text1 || !text2) return 0;

    const s1 = text1.toLowerCase().trim();
    const s2 = text2.toLowerCase().trim();

    if (s1 === s2) return 1.0;
    if (s1.length === 0 || s2.length === 0) return 0;

    // Check if one text is contained in the other (partial match)
    if (s1.includes(s2) || s2.includes(s1)) {
      return 0.85; // High similarity for substring matches
    }

    // Quick word-based similarity check
    const words1 = s1.split(/\s+/).filter(w => w.length > 2);
    const words2 = s2.split(/\s+/).filter(w => w.length > 2);

    if (words1.length === 0 || words2.length === 0) return 0;

    const commonWords = words1.filter(w => words2.includes(w));
    const wordSimilarity = (2 * commonWords.length) / (words1.length + words2.length);

    return wordSimilarity;
  };

  /**
   * Check if the recognized text is an echo of JARVIS's speech
   * Uses multiple strategies for robust detection
   */
  const isSelfVoiceEcho = (recognizedText) => {
    const now = Date.now();
    const text = recognizedText.toLowerCase().trim();

    // Strategy 1: Currently speaking - definitely our voice
    if (isSpeakingRef.current) {
      console.log('🔇 [Self-Voice] Suppressed: JARVIS is currently speaking');
      return { isEcho: true, reason: 'currently_speaking', confidence: 1.0 };
    }

    // Strategy 2: Within cooldown period after speech ended
    const timeSinceSpeechEnd = now - speakingEndTimeRef.current;
    if (timeSinceSpeechEnd < selfVoiceCooldownMs && speakingEndTimeRef.current > 0) {
      console.log(`🔇 [Self-Voice] Suppressed: Within cooldown (${timeSinceSpeechEnd}ms since speech ended)`);
      return { isEcho: true, reason: 'cooldown_period', confidence: 0.9, timeSinceSpeechEnd };
    }

    // Strategy 3: Text similarity with last spoken text
    if (lastSpokenTextRef.current) {
      const similarity = calculateTextSimilarity(text, lastSpokenTextRef.current);
      if (similarity > 0.6) {
        console.log(`🔇 [Self-Voice] Suppressed: Text similarity ${(similarity * 100).toFixed(1)}% with last spoken`);
        return { isEcho: true, reason: 'text_similarity', confidence: similarity, matchedText: lastSpokenTextRef.current };
      }
    }

    // Strategy 4: Check against recent spoken texts history
    for (const recentText of recentSpokenTextsRef.current) {
      const similarity = calculateTextSimilarity(text, recentText.text);
      const age = now - recentText.timestamp;

      // More lenient threshold for recent texts (within 10 seconds)
      if (similarity > 0.5 && age < 10000) {
        console.log(`🔇 [Self-Voice] Suppressed: Matches recent spoken text (${(similarity * 100).toFixed(1)}% similar, ${age}ms ago)`);
        return { isEcho: true, reason: 'recent_text_match', confidence: similarity, matchedText: recentText.text, age };
      }
    }

    return { isEcho: false, reason: 'passed_all_checks' };
  };

  /**
   * Record that JARVIS just spoke something (for echo detection)
   */
  const recordSpokenText = (text) => {
    const now = Date.now();
    lastSpokenTextRef.current = text;
    lastSpokenTimeRef.current = now;

    // Add to recent spoken texts history
    recentSpokenTextsRef.current.unshift({ text: text.toLowerCase().trim(), timestamp: now });

    // Keep only the most recent texts
    if (recentSpokenTextsRef.current.length > maxRecentSpokenTexts) {
      recentSpokenTextsRef.current = recentSpokenTextsRef.current.slice(0, maxRecentSpokenTexts);
    }

    console.log(`🔇 [Self-Voice] Recorded spoken text: "${text.substring(0, 50)}..." (${recentSpokenTextsRef.current.length} in history)`);
  };

  /**
   * Record that JARVIS finished speaking (for cooldown)
   */
  const recordSpeakingEnded = () => {
    speakingEndTimeRef.current = Date.now();
    console.log('🔇 [Self-Voice] Speaking ended, cooldown started');
  };

  const processNextInSpeechQueue = async () => {
    if (speechQueueRef.current.length === 0 || isSpeakingRef.current) {
      return;
    }

    const nextItem = speechQueueRef.current.shift();
    // Handle both old format (string) and new format (object with text and callback)
    const text = typeof nextItem === 'string' ? nextItem : nextItem.text;
    const callback = typeof nextItem === 'object' ? nextItem.callback : null;

    await speakResponseInternal(text, callback);
  };

  const stopAllSpeech = () => {
    console.log('[JARVIS Audio] Stopping all speech and clearing queue');
    // Clear the queue
    speechQueueRef.current = [];
    // Stop browser synthesis if active
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
    }
    // Reset speaking states
    setIsJarvisSpeaking(false);
    isSpeakingRef.current = false;
  };

  const speakResponse = async (text, updateDisplay = true) => {
    // Only set the response in state if explicitly requested (default true for backward compatibility)
    if (updateDisplay) {
      setResponse(text);
    }

    console.log('[JARVIS Audio] Speaking response:', text.substring(0, 100) + '...');
    console.log('[JARVIS Audio] Current speaking state:', isJarvisSpeaking);

    // Add to speech queue instead of skipping
    speechQueueRef.current.push({ text, callback: null });

    // Process queue if not already speaking
    if (!isSpeakingRef.current) {
      await processNextInSpeechQueue();
    }
  };

  const speakResponseWithCallback = async (text, onStartCallback) => {
    console.log('[JARVIS Audio] Speaking response with callback:', text.substring(0, 100) + '...');

    // Add to speech queue with callback
    speechQueueRef.current.push({ text, callback: onStartCallback });

    // Process queue if not already speaking
    if (!isSpeakingRef.current) {
      await processNextInSpeechQueue();
    }
  };

  const speakResponseInternal = async (text, onStartCallback = null) => {
    // Prevent overlapping speech
    if (isSpeakingRef.current) {
      console.log('[JARVIS Audio] Already speaking, adding to queue');
      return;
    }

    try {
      console.log('[JARVIS Audio] Setting speaking state to true');
      console.log('[JARVIS Audio] Text to speak (exact):', text);
      setIsJarvisSpeaking(true);
      isSpeakingRef.current = true;

      // 🔇 SELF-VOICE SUPPRESSION v2: PAUSE recognition and record text
      pauseRecognitionForSpeech(text);
      recordSpokenText(text);

      // Use backend TTS endpoint for consistent voice quality
      // Use POST for any text with special characters or newlines to avoid URL encoding issues
      const hasSpecialChars = /[^\w\s.,!?-]/.test(text);
      const usePost = text.length > 500 || text.includes('\n') || hasSpecialChars;
      console.log('[JARVIS Audio] Text length:', text.length);
      console.log('[JARVIS Audio] Using POST method:', usePost);

      if (!usePost) {
        // Short text: Use GET method with URL
        const apiUrl = API_URL || configService.getApiUrl() || inferUrls().API_BASE_URL;
        const audioUrl = `${apiUrl}/audio/speak/${encodeURIComponent(text)}`;
        console.log('[JARVIS Audio] Using GET method:', audioUrl);

        const audio = new Audio();

        // Set up all event handlers before setting src
        audio.onloadstart = () => {
          console.log('[JARVIS Audio] Loading started');
        };

        audio.oncanplaythrough = () => {
          console.log('[JARVIS Audio] Can play through');
        };

        audio.onplay = () => {
          console.log('[JARVIS Audio] GET method playback started');
          // Call the callback when audio ACTUALLY starts playing (perfect sync!)
          if (onStartCallback && typeof onStartCallback === 'function') {
            console.log('[JARVIS Audio] Calling start callback - text will appear NOW');
            onStartCallback();
          }
        };

        audio.onended = () => {
          console.log('[JARVIS Audio] GET method playback completed');
          setIsJarvisSpeaking(false);
          isSpeakingRef.current = false;
          // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended
          recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
          // Process next in queue after a small delay
          setTimeout(() => processNextInSpeechQueue(), 200);
        };

        audio.onerror = async (e) => {
          console.error('[JARVIS Audio] GET audio error:', e);
          if (audio.error) {
            console.error('[JARVIS Audio] Error details:', {
              code: audio.error.code,
              message: audio.error.message
            });
          }
          console.log('[JARVIS Audio] Falling back to POST method');
          // Fallback to POST method with callback
          await playAudioUsingPost(text, onStartCallback);
        };

        // Set source and properties
        audio.src = audioUrl;
        audio.volume = 1.0;
        audio.crossOrigin = 'anonymous';  // Enable CORS

        // Try to play
        console.log('[JARVIS Audio] Attempting to play audio...');
        await audio.play();
        console.log('[JARVIS Audio] Play promise resolved');
      } else {
        // Long text: Use POST method directly with callback
        console.log('[JARVIS Audio] Using POST method for long text');
        await playAudioUsingPost(text, onStartCallback);
      }
    } catch (error) {
      console.error('[JARVIS Audio] Playback failed:', error);
      console.error('[JARVIS Audio] Error details:', {
        name: error.name,
        message: error.message,
        stack: error.stack
      });
      setIsJarvisSpeaking(false);
      isSpeakingRef.current = false;
      // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended (even on error)
      recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
      // Process next in queue even after error
      setTimeout(() => processNextInSpeechQueue(), 200);

      // Fallback to browser speech synthesis if backend TTS fails
      console.log('[JARVIS Audio] Falling back to browser speech synthesis...');
      if ('speechSynthesis' in window) {
        // Cancel any ongoing speech first
        window.speechSynthesis.cancel();

        const utterance = new SpeechSynthesisUtterance(text);
        utterance.rate = 0.7;   // Even slower rate for smooth, non-rushed speech
        utterance.pitch = 0.95; // Slightly lower pitch for more authoritative tone
        utterance.volume = 0.9; // Slightly lower volume for more natural sound

        // Try to find Daniel or other British male voice
        const voices = window.speechSynthesis.getVoices();
        console.log(`[JARVIS Audio] Available voices: ${voices.length}`);

        // First try to find Daniel
        let selectedVoice = voices.find(voice => voice.name.includes('Daniel'));

        if (!selectedVoice) {
          // Try other British male voices
          selectedVoice = voices.find(voice =>
            (voice.lang.includes('en-GB') || voice.lang.includes('en_GB')) &&
            (voice.name.includes('Oliver') || voice.name.includes('James') ||
              voice.name.toLowerCase().includes('male'))
          );
        }

        if (!selectedVoice) {
          // Try any British voice
          selectedVoice = voices.find(voice =>
            voice.lang.includes('en-GB') || voice.lang.includes('en_GB')
          );
        }

        if (selectedVoice) {
          utterance.voice = selectedVoice;
          console.log(`[JARVIS Audio] Using voice: ${selectedVoice.name}`);
        } else {
          console.log('[JARVIS Audio] No British voice found, using default');
        }

        utterance.onstart = () => {
          console.log('[JARVIS Audio] Browser speech synthesis started');
          setIsJarvisSpeaking(true);
          isSpeakingRef.current = true;
          // 🔇 SELF-VOICE SUPPRESSION v2: PAUSE recognition (browser synthesis)
          pauseRecognitionForSpeech(text);
          recordSpokenText(text);
          // Call the callback when browser speech ACTUALLY starts playing (perfect sync!)
          if (onStartCallback && typeof onStartCallback === 'function') {
            console.log('[JARVIS Audio] Calling start callback - text will appear NOW');
            onStartCallback();
          }
        };
        utterance.onend = () => {
          console.log('[JARVIS Audio] Browser speech completed');
          setIsJarvisSpeaking(false);
          isSpeakingRef.current = false;
          // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended
          recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
          // Process next in queue after a small delay
          setTimeout(() => processNextInSpeechQueue(), 200);
        };
        utterance.onerror = (e) => {
          console.error('[JARVIS Audio] Browser speech error:', e);
          setIsJarvisSpeaking(false);
          isSpeakingRef.current = false;
          // 🔇 SELF-VOICE SUPPRESSION: Record when speaking ended (even on error)
          recordSpeakingEnded();
          resumeRecognitionAfterSpeech();
          // Process next in queue even after error
          setTimeout(() => processNextInSpeechQueue(), 200);
        };

        window.speechSynthesis.speak(utterance);
      }
    }
  };

  return (
    <div className="jarvis-voice-container">
      {/* JARVIS Header */}
      <div className="jarvis-header">
        <h1 className="jarvis-title">
          <span className="jarvis-logo">J.A.R.V.I.S.</span>
          <span className="jarvis-subtitle">Just A Rather Very Intelligent System</span>
        </h1>
      </div>

      {/* Orange microphone indicator when listening */}
      <MicrophoneIndicator isListening={isListening && continuousListening} />

      {microphonePermission !== 'granted' && (
        <MicrophonePermissionHelper
          onPermissionGranted={() => {
            setMicrophonePermission('granted');
            setMicStatus('ready');
            initializeSpeechRecognition();
          }}
        />
      )}

      <div className={`arc-reactor ${isListening ? 'listening' : ''} ${isProcessing ? 'processing' : ''} ${continuousListening ? 'continuous' : ''} ${isWaitingForCommand ? 'waiting' : ''}`}>
        <div className="core"></div>
        <div className="ring ring-1"></div>
        <div className="ring ring-2"></div>
        <div className="ring ring-3"></div>
      </div>

      <div className="jarvis-status">
        <div className={`status-indicator ${jarvisStatus || 'offline'}`}></div>
        <span className="status-text">
          {jarvisStatus === 'online' || jarvisStatus === 'active' ? (
            <>
              SYSTEM READY
              {systemMode === 'minimal' && <span className="mode-badge minimal">[MINIMAL MODE]</span>}
              {proactiveIntelligenceActive && <span className="mode-badge phase4">[PHASE 4: PROACTIVE]</span>}
            </>
          ) : jarvisStatus === 'activating' || jarvisStatus === 'initializing' ? (
            <>INITIALIZING...</>
          ) : jarvisStatus === 'connecting' ? (
            <>CONNECTING...</>
          ) : jarvisStatus === 'reconnecting' ? (
            <>RECONNECTING...</>
          ) : jarvisStatus === 'offline' ? (
            <>OFFLINE - SEARCHING FOR BACKEND...</>
          ) : (
            <>SYSTEM {(jarvisStatus || 'offline').toUpperCase()}</>
          )}
        </span>
        {micStatus === 'error' && (
          <span className="mic-status error">
            <span className="error-dot"></span> MIC ERROR
          </span>
        )}
      </div>

      {/* Mode Information Banner */}
      {systemMode === 'minimal' && jarvisStatus === 'online' && (
        <div className="minimal-mode-banner">
          <div className="mode-info">
            <span className="mode-icon">⚡</span>
            <span className="mode-text">Running in Minimal Mode - Full features loading...</span>
            <span className="mode-spinner">🔄</span>
          </div>
        </div>
      )}

      {/* Upgrade Success Banner */}
      {showUpgradeSuccess && (
        <div className="upgrade-success-banner">
          <div className="success-info">
            <span className="success-icon">🎉</span>
            <span className="success-text">System Upgraded to Full Mode!</span>
            <span className="success-check">✅</span>
          </div>
          <div className="success-features">
            All advanced features now available: Wake word • ML Audio • Vision • Memory • Tools
          </div>
        </div>
      )}

      {/* 🆕 Command Detection Banner - Shows when streaming safeguard detects a command */}
      {detectedCommand && (
        <CommandDetectionBanner
          command={detectedCommand}
          onDismiss={() => setDetectedCommand(null)}
          autoDismiss={3000}
        />
      )}

      {/* ═══════════════════════════════════════════════════════════════════ */}
      {/* PROACTIVE UNLOCK PROGRESS - Visual feedback for autonomous unlock flow */}
      {/* Shows when JARVIS detects locked screen and unlocks for a command     */}
      {/* ═══════════════════════════════════════════════════════════════════ */}
      {proactiveUnlockProgress && (
        <div className="proactive-unlock-container">
          <div className="proactive-unlock-header">
            <span className="proactive-unlock-icon">
              {proactiveUnlockProgress.stageIcon || '🔐'}
            </span>
            <div className="proactive-unlock-title-section">
              <span className="proactive-unlock-title">Autonomous Screen Unlock</span>
              <span className="proactive-unlock-subtitle">
                {proactiveUnlockProgress.originalCommand 
                  ? `"${proactiveUnlockProgress.originalCommand}"` 
                  : 'Processing your request...'}
              </span>
            </div>
            <span className="proactive-unlock-percentage">{proactiveUnlockProgress.progress}%</span>
          </div>

          {/* Progress Bar */}
          <div className="proactive-unlock-progress-bar-container">
            <div
              className={`proactive-unlock-progress-bar ${proactiveUnlockProgress.status}`}
              style={{ width: `${proactiveUnlockProgress.progress}%` }}
            >
              <div className="proactive-unlock-progress-glow"></div>
            </div>
          </div>

          {/* Current Stage Message */}
          <div className={`proactive-unlock-message ${proactiveUnlockProgress.status}`}>
            <span className="proactive-unlock-stage-icon">
              {proactiveUnlockProgress.status === 'complete' ? '✅' 
                : proactiveUnlockProgress.status === 'error' ? '❌' 
                : '⏳'}
            </span>
            <span className="proactive-unlock-stage-text">
              {proactiveUnlockProgress.message || 'Processing...'}
            </span>
          </div>

          {/* Flow Steps Visualization - v9.0: Added voice_verified before unlocking */}
          <div className="proactive-unlock-steps">
            {[
              { key: 'screen_detected', label: 'Detected', icon: '🔒' },
              { key: 'verifying_voice', label: 'Verify', icon: '🎤' },
              { key: 'voice_verified', label: 'Verified', icon: '✅' },
              { key: 'unlocking', label: 'Unlock', icon: '🔐' },
              { key: 'executing', label: 'Execute', icon: '🚀' },
              { key: 'complete', label: 'Done', icon: '✨' },
            ].map((step, idx) => {
              // v9.0: SECURITY - voice_verified MUST come before unlocking
              const currentStageOrder = {
                'screen_detected': 0,
                'verifying_voice': 1,
                'voice_verified': 2,  // CRITICAL: Voice confirmed
                'unlocking': 3,       // Only after verified
                'unlocked': 4,
                'executing': 4,
                'complete': 5,
                'error': -1,
              };
              const currentOrder = currentStageOrder[proactiveUnlockProgress.stage] ?? 0;
              const stepOrder = idx;
              const isActive = stepOrder === currentOrder;
              const isComplete = stepOrder < currentOrder || proactiveUnlockProgress.stage === 'complete';
              const isFuture = stepOrder > currentOrder;
              const isError = proactiveUnlockProgress.status === 'error';
              
              return (
                <div 
                  key={step.key} 
                  className={`proactive-unlock-step ${isActive ? 'active' : ''} ${isComplete ? 'complete' : ''} ${isFuture ? 'future' : ''} ${isError && isActive ? 'error' : ''}`}
                >
                  <span className="step-icon">
                    {isError && isActive ? '❌' : isComplete && step.key !== 'complete' ? '✓' : step.icon}
                  </span>
                  <span className="step-label">{step.label}</span>
                  {idx < 5 && <span className="step-connector"></span>}
                </div>
              );
            })}
          </div>

          {/* Continuation Intent */}
          {proactiveUnlockProgress.continuationIntent && proactiveUnlockProgress.status !== 'error' && (
            <div className="proactive-unlock-continuation">
              <span className="continuation-label">Next:</span>
              <span className="continuation-action">{proactiveUnlockProgress.continuationIntent}</span>
            </div>
          )}

          {/* Error State */}
          {proactiveUnlockProgress.status === 'error' && (
            <div className="proactive-unlock-error">
              <span className="error-icon">⚠️</span>
              <span className="error-text">{proactiveUnlockProgress.message || 'Unlock failed'}</span>
            </div>
          )}
        </div>
      )}

      {/* VBI (Voice Biometric Intelligence) Progress Display - Real-time voice unlock visualization */}
      {vbiProgress && (
        <div className="vbi-progress-container">
          <div className="vbi-progress-header">
            <span className="vbi-icon">
              {vbiProgress.stage === 'complete'
                ? (vbiProgress.status === 'success' ? '🔓' : '🔒')
                : '🔐'}
            </span>
            <span className="vbi-title">Voice Biometric Verification</span>
            <span className="vbi-percentage">{vbiProgress.progress}%</span>
          </div>

          {/* Progress Bar */}
          <div className="vbi-progress-bar-container">
            <div
              className={`vbi-progress-bar ${vbiProgress.status === 'failed' ? 'error' : ''}`}
              style={{ width: `${vbiProgress.progress}%` }}
            >
              <div className="vbi-progress-glow"></div>
            </div>
          </div>

          {/* Current Stage Display */}
          <div className={`vbi-current-stage ${vbiProgress.status}`}>
            <span className="vbi-stage-icon">
              {vbiProgress.status === 'in_progress' ? '⏳' : vbiProgress.status === 'success' ? '✅' : '❌'}
            </span>
            <span className="vbi-stage-name">{vbiProgress.stageName}</span>
            {vbiProgress.details?.message && (
              <span className="vbi-stage-detail">{vbiProgress.details.message}</span>
            )}
          </div>

          {/* Completed Stages Timeline */}
          {vbiStages.length > 0 && (
            <div className="vbi-stages-timeline">
              {vbiStages.map((stage, idx) => (
                <div key={idx} className={`vbi-timeline-stage ${stage.status}`}>
                  <span className="vbi-timeline-dot"></span>
                  <span className="vbi-timeline-name">{stage.stageName}</span>
                  {stage.status === 'success' && stage.details?.confidence && (
                    <span className="vbi-timeline-confidence">
                      {(stage.details.confidence * 100).toFixed(1)}%
                    </span>
                  )}
                  {stage.status === 'success' && stage.details?.speaker && (
                    <span className="vbi-timeline-speaker">{stage.details.speaker}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Result Display */}
          {vbiProgress.stage === 'complete' && (
            <div className={`vbi-result ${vbiProgress.status}`}>
              {vbiProgress.isSuccess || vbiProgress.status === 'success' ? (
                <span className="vbi-result-text">
                  ✅ Welcome, {vbiProgress.speaker || vbiProgress.details?.speaker || 'User'} 
                  {(vbiProgress.confidence || vbiProgress.details?.confidence) > 0 && 
                    ` (${((vbiProgress.confidence || vbiProgress.details?.confidence || 0) * 100).toFixed(1)}% confidence)`
                  }
                </span>
              ) : vbiProgress.error ? (
                <span className="vbi-result-text">
                  ❌ Verification Failed: {vbiProgress.error}
                </span>
              ) : (
                <span className="vbi-result-text">
                  ⏳ Verification Complete
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Simplified Status Indicator */}
      <div className="status-indicator-bar">
        {continuousListening && !isWaitingForCommand && (
          <div className="status-item wake-active">
            <span className="status-dot"></span>
            <span className="status-text">Say "Hey JARVIS"</span>
          </div>
        )}
        {isWaitingForCommand && (
          <div className="status-item listening">
            <span className="status-dot active"></span>
            <span className="status-text">Listening...</span>
          </div>
        )}
      </div>

      {/* Error Display */}
      {error && (
        <div className={`jarvis-error ${error.includes('Network') ? 'network-error' : ''}`}>
          <div className="error-icon">{error.includes('Network') ? '🌐' : '⚠️'}</div>
          <div className="error-text" style={{ whiteSpace: 'pre-line' }}>
            {error}
            {error.includes('Network') && networkRetries > 0 && networkRetries < maxNetworkRetries && (
              <span className="retry-status"> (Auto-retrying...)</span>
            )}
          </div>
          {error.includes('Microphone') && (
            <button
              onClick={checkMicrophonePermission}
              className="jarvis-button retry-button"
            >
              🎤 Retry Microphone Access
            </button>
          )}
        </div>
      )}

      {/* Transcript Display - Always visible when there's activity */}
      {(transcript || response || isProcessing || isJarvisSpeaking) && (
        <div className="jarvis-transcript">
          {/* User Message */}
          {transcript && (
            <div className="user-message">
              <span className="message-label">You:</span>
              <span className="message-text">{transcript}</span>
            </div>
          )}

          {/* JARVIS Response - Always show when processing, speaking, or has response */}
          {(isProcessing || isJarvisSpeaking || response) && (
            <div className="jarvis-message">
              <span className="message-label">JARVIS:</span>
              <span className="message-text" style={{ whiteSpace: 'pre-wrap' }}>
                {/* Priority 1: Processing state (no response yet) */}
                {isProcessing && !response ? (
                  <span className="processing-indicator">⚙️ Processing your request...</span>
                ) : isJarvisSpeaking && response ? (
                  /* Priority 2: Speaking with response */
                  <>
                    {response}
                    <span className="speaking-indicator"> 🎤</span>
                  </>
                ) : (
                  /* Priority 3: Just the response */
                  response || ''
                )}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Text Command Input - Always visible */}
      <div className="text-command-container">
        <form onSubmit={handleTextCommandSubmit} className="text-command-form">
          <input
            type="text"
            value={textCommand}
            onChange={(e) => setTextCommand(e.target.value)}
            placeholder="Type a command to JARVIS..."
            className="text-command-input"
            disabled={jarvisStatus !== 'online'}
          />
          <button
            type="submit"
            className="text-command-submit"
            disabled={!textCommand.trim() || jarvisStatus !== 'online'}
          >
            Send
          </button>
        </form>
      </div>

      {/* Phase 4: Proactive Suggestions */}
      {proactiveSuggestions.length > 0 && (
        <div className="proactive-suggestions-container">
          {proactiveSuggestions.map((suggestion) => (
            <div key={suggestion.id} className="proactive-suggestion">
              <div className="suggestion-content">
                <p>{suggestion.message || suggestion.voice_message}</p>
              </div>
              <div className="suggestion-actions">
                <button
                  onClick={() => {
                    // Send response to backend
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({
                        type: 'proactive_suggestion_response',
                        suggestion_id: suggestion.id,
                        response: 'accept'
                      }));
                    }
                    // Remove suggestion from UI
                    setProactiveSuggestions(prev =>
                      prev.filter(s => s.id !== suggestion.id)
                    );
                  }}
                >
                  Accept
                </button>
                <button
                  onClick={() => {
                    // Remove suggestion from UI
                    setProactiveSuggestions(prev =>
                      prev.filter(s => s.id !== suggestion.id)
                    );
                  }}
                >
                  Dismiss
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Workflow Progress */}
      {workflowProgress && (
        <WorkflowProgress
          workflow={workflowProgress}
          currentAction={workflowProgress.currentAction}
          onCancel={() => {
            // Send cancel request to backend
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({
                type: 'cancel_workflow',
                workflow_id: workflowProgress.workflow_id
              }));
            }
            setWorkflowProgress(null);
          }}
        />
      )}

      {/* Training Status - Real-time training progress from Reactor-Core (Feedback Loop) */}
      <TrainingStatus />

      {/* Vision Status - REMOVED */}

      {/* Simplified Control - Only show when needed */}
      {jarvisStatus === 'activating' && (
        <div className="jarvis-controls">
          <div className="initializing-message">Initializing JARVIS systems...</div>
        </div>
      )}

      {/* Command Input Section */}
      <div className="jarvis-input-section">
        <div className="jarvis-input-container">
          <input
            type="text"
            className="jarvis-input"
            placeholder={
              isJarvisSpeaking
                ? "🎤 JARVIS is speaking..."
                : isProcessing
                ? "⚙️ Processing..."
                : isTyping
                ? "✍️ Type your command..."
                : proactiveSuggestions.length > 0
                ? "💡 Proactive suggestion available..."
                : jarvisStatus === 'online'
                ? "Say 'Hey JARVIS' or type a command..."
                : "Initializing..."
            }
            onChange={(e) => {
              // Track typing state
              setIsTyping(e.target.value.length > 0);
              setLastUserInteraction(Date.now());

              // Clear existing timeout
              if (typingTimeoutRef.current) {
                clearTimeout(typingTimeoutRef.current);
              }

              // Set typing to false after 2 seconds of inactivity
              if (e.target.value.length > 0) {
                typingTimeoutRef.current = setTimeout(() => {
                  setIsTyping(false);
                }, 2000);
              }
            }}
            onKeyPress={(e) => {
              if (e.key === 'Enter') {
                setIsTyping(false);
                setLastUserInteraction(Date.now());
                sendTextCommand(e.target.value);
                e.target.value = '';
              }
            }}
            disabled={!jarvisStatus || jarvisStatus === 'offline' || jarvisStatus === 'error'}
          />
          <button
            className="jarvis-send-button"
            onClick={() => {
              const input = document.querySelector('.jarvis-input');
              if (input.value) {
                sendTextCommand(input.value);
                input.value = '';
              }
            }}
            disabled={!jarvisStatus || jarvisStatus === 'offline' || jarvisStatus === 'error'}
          >
            <span className="send-icon">→</span>
          </button>
        </div>
      </div>

      {/* Adaptive Voice Learning Stats Display */}
      <VoiceStatsDisplay show={jarvisStatus === 'online' && continuousListening} />

      {/* Environmental Adaptation Stats Display */}
      <EnvironmentalStatsDisplay show={jarvisStatus === 'online' && continuousListening} />

      {/* Audio Quality Stats Display */}
      <AudioQualityStatsDisplay show={jarvisStatus === 'online' && continuousListening} />

    </div>
  );
};

export default JarvisVoice;
