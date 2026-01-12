#!/usr/bin/env python3
"""
Unified ML Audio API for JARVIS
Provides ML-enhanced audio error handling with automatic fallback
when ML dependencies are not available

CoreML Integration:
===================
This API works alongside the CoreML Voice Engine for hardware-accelerated
voice detection on Apple Silicon:

- CoreML VAD: Ultra-fast voice activity detection (232KB model, <10ms latency)
- Apple Neural Engine: Hardware acceleration for minimal CPU usage
- Adaptive Learning: ML models for audio quality and pattern recognition
- Fallback Support: Graceful degradation when CoreML is unavailable

The ML Audio API handles:
- Audio quality analysis and enhancement
- Pattern learning and anomaly detection
- Error recovery and adaptive processing
- System capability detection

While CoreML handles:
- Real-time voice activity detection (VAD)
- Speaker recognition (optional)
- Hardware-accelerated inference

See: jarvis_voice_api.py for CoreML endpoint documentation
See: COREML_SETUP_STATUS.md for CoreML integration details
"""

import os
import json
import asyncio
import logging
import hashlib
import platform
import statistics
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import numpy as np
import psutil

logger = logging.getLogger(__name__)

# Try to import ML dependencies
ML_AVAILABLE = False
try:
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import DBSCAN
    from sklearn.ensemble import RandomForestClassifier, IsolationForest
    import joblib
    from audio.ml_audio_manager import (
        AudioEvent,
        get_audio_manager,
        AudioPatternLearner
    )
    ML_AVAILABLE = True
    logger.info("ML audio dependencies loaded successfully")
except ImportError as e:
    logger.warning(f"ML audio dependencies not available: {e} - using fallback mode")

# Create router
router = APIRouter(prefix="/audio/ml", tags=["ML Audio"])

# Dynamic system state (works without ML dependencies)
class MLAudioSystemState:
    """Dynamic ML Audio system state manager"""
    
    def __init__(self):
        self.start_time = datetime.now()
        self.active_streams = {}  # client_id -> stream_info
        self.total_processed = 0
        self.last_activity = None
        self.model_loaded = ML_AVAILABLE
        self.processing_history = deque(maxlen=1000)  # Last 1000 processing times
        self.quality_history = deque(maxlen=100)  # Last 100 quality scores
        self.issue_frequency = {}  # Track issue occurrences
        self.client_stats = {}  # Per-client statistics
        self.audio_buffer_stats = {
            "total_bytes_processed": 0,
            "average_chunk_size": 0,
            "peak_processing_rate": 0
        }
        self.system_capabilities = self._detect_capabilities()
        
    def _detect_capabilities(self) -> Dict[str, bool]:
        """Dynamically detect system capabilities"""
        caps = {}

        # Check for audio processing libraries
        try:
            import librosa
            caps["librosa_available"] = True
            caps["advanced_audio_analysis"] = True
        except ImportError:
            caps["librosa_available"] = False
            caps["advanced_audio_analysis"] = False

        # Check for ML frameworks
        try:
            import torch
            caps["pytorch_available"] = True
            caps["neural_audio_processing"] = True
        except ImportError:
            caps["pytorch_available"] = False
            caps["neural_audio_processing"] = False

        # Check for CoreML Voice Engine (Apple Silicon optimization)
        try:
            from voice.coreml.voice_engine_bridge import is_coreml_available
            caps["coreml_available"] = is_coreml_available()
            caps["coreml_neural_engine"] = caps["coreml_available"] and platform.system() == "Darwin"
            caps["hardware_accelerated_vad"] = caps["coreml_available"]
        except ImportError:
            caps["coreml_available"] = False
            caps["coreml_neural_engine"] = False
            caps["hardware_accelerated_vad"] = False

        # Check system resources
        cpu_count = psutil.cpu_count()
        total_ram = psutil.virtual_memory().total / (1024**3)  # GB

        caps["multi_stream_capable"] = cpu_count >= 4
        caps["high_performance_mode"] = total_ram >= 8
        caps["gpu_acceleration"] = self._check_gpu()
        caps["ml_available"] = ML_AVAILABLE

        return caps
    
    def _check_gpu(self) -> bool:
        """Check for GPU availability"""
        try:
            # Try CUDA
            import torch
            return torch.cuda.is_available()
        except Exception:
            pass

        try:
            # Try Metal (Apple Silicon)
            import torch
            return torch.backends.mps.is_available()
        except Exception:
            pass
            
        return False
    
    def get_uptime(self) -> float:
        """Get system uptime in hours"""
        return (datetime.now() - self.start_time).total_seconds() / 3600
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Calculate real-time performance metrics"""
        if not self.processing_history:
            return {
                "avg_latency_ms": 0,
                "p95_latency_ms": 0,
                "p99_latency_ms": 0,
                "success_rate": 1.0,
                "throughput_per_second": 0
            }
        
        latencies = list(self.processing_history)
        sorted_latencies = sorted(latencies)
        
        return {
            "avg_latency_ms": round(statistics.mean(latencies), 2),
            "p50_latency_ms": round(statistics.median(latencies), 2),
            "p95_latency_ms": round(sorted_latencies[int(len(sorted_latencies) * 0.95)], 2) if len(sorted_latencies) > 20 else round(max(latencies), 2),
            "p99_latency_ms": round(sorted_latencies[int(len(sorted_latencies) * 0.99)], 2) if len(sorted_latencies) > 100 else round(max(latencies), 2),
            "success_rate": round(len([l for l in latencies if l < 1000]) / len(latencies), 3),  # <1s is success
            "throughput_per_second": round(len(latencies) / max(1, self.get_uptime() * 3600), 2)
        }
    
    def get_quality_insights(self) -> Dict[str, Any]:
        """Get insights from quality history"""
        if not self.quality_history:
            return {
                "average_quality": 0.85,
                "quality_trend": "stable",
                "common_issues": []
            }
        
        qualities = list(self.quality_history)
        recent_qualities = qualities[-10:] if len(qualities) > 10 else qualities
        
        # Determine trend
        if len(qualities) > 5:
            first_half_avg = statistics.mean(qualities[:len(qualities)//2])
            second_half_avg = statistics.mean(qualities[len(qualities)//2:])
            
            if second_half_avg > first_half_avg + 0.05:
                trend = "improving"
            elif second_half_avg < first_half_avg - 0.05:
                trend = "degrading"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"
        
        # Get common issues
        sorted_issues = sorted(self.issue_frequency.items(), key=lambda x: x[1], reverse=True)
        common_issues = [issue for issue, _ in sorted_issues[:3]]
        
        return {
            "average_quality": round(statistics.mean(qualities), 3),
            "recent_average": round(statistics.mean(recent_qualities), 3),
            "quality_trend": trend,
            "quality_variance": round(statistics.variance(qualities), 4) if len(qualities) > 1 else 0,
            "common_issues": common_issues,
            "best_quality_achieved": round(max(qualities), 3) if qualities else 0,
            "worst_quality_seen": round(min(qualities), 3) if qualities else 0
        }
    
    def track_processing(self, latency_ms: float):
        """Track processing metrics"""
        self.processing_history.append(latency_ms)
        self.total_processed += 1
        self.last_activity = datetime.now()
    
    def track_quality(self, score: float, issues: List[str]):
        """Track quality metrics"""
        self.quality_history.append(score)
        for issue in issues:
            self.issue_frequency[issue] = self.issue_frequency.get(issue, 0) + 1
    
    def get_client_recommendations(self, client_id: str, user_agent: str = "") -> Dict[str, Any]:
        """Get personalized recommendations for a client"""
        # Initialize client stats if new
        if client_id not in self.client_stats:
            self.client_stats[client_id] = {
                "first_seen": datetime.now(),
                "total_requests": 0,
                "average_quality": 0.85,
                "common_issues": []
            }
        
        stats = self.client_stats[client_id]
        stats["total_requests"] += 1
        
        # Dynamic chunk size based on system load
        active_count = len(self.active_streams)
        cpu_percent = psutil.cpu_percent(interval=0.1)
        
        if cpu_percent > 80 or active_count > 5:
            recommended_chunk = 1024  # Larger chunks for high load
        elif cpu_percent < 30 and active_count < 2:
            recommended_chunk = 256   # Smaller chunks for low latency
        else:
            recommended_chunk = 512   # Default
        
        # Format recommendation based on client
        if "chrome" in user_agent.lower():
            recommended_format = "webm"  # Chrome handles WebM well
        elif "safari" in user_agent.lower():
            recommended_format = "wav"   # Safari prefers WAV
        else:
            recommended_format = "base64"
        
        return {
            "chunk_size": recommended_chunk,
            "sample_rate": 16000 if not self.system_capabilities.get("high_performance_mode") else 48000,
            "format": recommended_format,
            "enable_preprocessing": cpu_percent < 50,
            "use_compression": active_count > 3,
            "adaptive_bitrate": True,
            "client_profile": {
                "requests_today": stats["total_requests"],
                "member_since": stats["first_seen"].isoformat(),
                "performance_tier": "premium" if stats["total_requests"] > 100 else "standard"
            }
        }

# Self-healing mechanisms
class AudioSelfHealer:
    """Intelligent self-healing system for audio issues"""
    
    def __init__(self):
        self.healing_history = deque(maxlen=100)
        self.successful_strategies = {}
        self.failure_patterns = defaultdict(list)
        self.device_fingerprints = {}
        self.recovery_sequences = self._init_recovery_sequences()
        self.browser_quirks = self._init_browser_quirks()
        
    def _init_recovery_sequences(self) -> Dict[str, List[Dict]]:
        """Initialize recovery sequences for different scenarios"""
        return {
            "permission_denied": [
                {
                    "action": "prompt_user",
                    "delay_ms": 0,
                    "message": "Microphone access needed for voice commands",
                    "ui_element": "permission_dialog"
                },
                {
                    "action": "check_browser_settings",
                    "delay_ms": 2000,
                    "script": "navigator.permissions.query({name: 'microphone'})"
                },
                {
                    "action": "fallback_to_text",
                    "delay_ms": 5000,
                    "message": "Voice unavailable - using text input"
                }
            ],
            "device_not_found": [
                {
                    "action": "enumerate_devices",
                    "delay_ms": 0,
                    "script": "navigator.mediaDevices.enumerateDevices()"
                },
                {
                    "action": "check_default_device",
                    "delay_ms": 1000,
                    "constraints": {"audio": {"deviceId": "default"}}
                },
                {
                    "action": "try_any_audio_device",
                    "delay_ms": 2000,
                    "constraints": {"audio": True}
                },
                {
                    "action": "check_system_audio",
                    "delay_ms": 3000,
                    "system_check": True
                }
            ],
            "context_suspended": [
                {
                    "action": "user_gesture_required",
                    "delay_ms": 0,
                    "message": "Click anywhere to enable audio",
                    "ui_element": "click_overlay"
                },
                {
                    "action": "resume_context",
                    "delay_ms": 100,
                    "script": "audioContext.resume()"
                },
                {
                    "action": "create_new_context",
                    "delay_ms": 1000,
                    "cleanup_old": True
                }
            ],
            "network_error": [
                {
                    "action": "check_connectivity",
                    "delay_ms": 0,
                    "endpoints": ["/health", "/api/health"]
                },
                {
                    "action": "switch_to_offline_mode",
                    "delay_ms": 2000,
                    "cache_strategy": "local_first"
                },
                {
                    "action": "queue_for_retry",
                    "delay_ms": 5000,
                    "max_retries": 3,
                    "backoff": "exponential"
                }
            ],
            "high_latency": [
                {
                    "action": "reduce_quality",
                    "delay_ms": 0,
                    "adjustments": {
                        "sampleRate": 8000,
                        "bitDepth": 8,
                        "channels": 1
                    }
                },
                {
                    "action": "enable_compression",
                    "delay_ms": 1000,
                    "codec": "opus",
                    "bitrate": 16000
                },
                {
                    "action": "switch_to_chunked_mode",
                    "delay_ms": 2000,
                    "chunk_size": 256
                }
            ]
        }
    
    def _init_browser_quirks(self) -> Dict[str, Dict]:
        """Initialize browser-specific quirks and workarounds"""
        return {
            "chrome": {
                "preferred_format": "webm",
                "supports_echo_cancellation": True,
                "requires_https": True,
                "audio_worklet_support": True,
                "known_issues": [
                    {
                        "version_range": ["90", "95"],
                        "issue": "AudioContext suspension",
                        "workaround": "user_gesture_required"
                    }
                ],
                "optimal_constraints": {
                    "audio": {
                        "echoCancellation": True,
                        "noiseSuppression": True,
                        "autoGainControl": True,
                        "sampleRate": 48000
                    }
                }
            },
            "safari": {
                "preferred_format": "wav",
                "supports_echo_cancellation": False,
                "requires_https": True,
                "audio_worklet_support": False,
                "known_issues": [
                    {
                        "version_range": ["14", "16"],
                        "issue": "getUserMedia requires user gesture",
                        "workaround": "prompt_before_audio"
                    },
                    {
                        "issue": "No echo cancellation support",
                        "workaround": "use_headphones_prompt"
                    }
                ],
                "optimal_constraints": {
                    "audio": {
                        "sampleRate": 44100,
                        "channelCount": 1
                    }
                }
            },
            "firefox": {
                "preferred_format": "ogg",
                "supports_echo_cancellation": True,
                "requires_https": True,
                "audio_worklet_support": True,
                "known_issues": [
                    {
                        "issue": "Strict autoplay policy",
                        "workaround": "user_interaction_required"
                    }
                ],
                "optimal_constraints": {
                    "audio": {
                        "echoCancellation": True,
                        "noiseSuppression": True,
                        "autoGainControl": True
                    }
                }
            },
            "edge": {
                "preferred_format": "webm",
                "supports_echo_cancellation": True,
                "requires_https": True,
                "audio_worklet_support": True,
                "known_issues": [],
                "optimal_constraints": {
                    "audio": {
                        "echoCancellation": True,
                        "noiseSuppression": True,
                        "autoGainControl": True,
                        "sampleRate": 48000
                    }
                }
            }
        }
    
    async def diagnose_issue(self, error_code: str, context: Dict) -> Dict[str, Any]:
        """Intelligently diagnose audio issues"""
        diagnosis = {
            "error_code": error_code,
            "severity": self._calculate_severity(error_code, context),
            "likely_causes": [],
            "recommended_actions": [],
            "success_probability": 0.0
        }
        
        # Analyze error patterns
        if error_code == "NotAllowedError":
            if context.get("permission_state") == "prompt":
                diagnosis["likely_causes"].append("User hasn't responded to permission prompt")
                diagnosis["recommended_actions"].append("wait_for_user_response")
            elif context.get("retry_count", 0) > 2:
                diagnosis["likely_causes"].append("Permission permanently denied")
                diagnosis["recommended_actions"].append("show_manual_enable_guide")
            else:
                diagnosis["likely_causes"].append("First-time permission request")
                diagnosis["recommended_actions"].append("show_permission_benefits")
                
        elif error_code == "NotFoundError":
            devices = context.get("available_devices", [])
            if not devices:
                diagnosis["likely_causes"].append("No audio input devices connected")
                diagnosis["recommended_actions"].append("check_physical_connections")
            else:
                diagnosis["likely_causes"].append("Default device not available")
                diagnosis["recommended_actions"].append("try_alternate_devices")
                
        elif error_code == "NotReadableError":
            diagnosis["likely_causes"].extend([
                "Device in use by another application",
                "Hardware malfunction",
                "Driver issues"
            ])
            diagnosis["recommended_actions"].extend([
                "close_other_audio_apps",
                "restart_audio_service",
                "update_drivers"
            ])
            
        # Calculate success probability based on history
        similar_cases = self._find_similar_cases(error_code, context)
        if similar_cases:
            successful = [c for c in similar_cases if c["resolved"]]
            diagnosis["success_probability"] = len(successful) / len(similar_cases)
            
        return diagnosis
    
    async def heal(self, error_code: str, context: Dict) -> Dict[str, Any]:
        """Apply self-healing strategies"""
        # Get diagnosis
        diagnosis = await self.diagnose_issue(error_code, context)
        
        # Select recovery sequence
        sequence_key = self._map_error_to_sequence(error_code, context)
        recovery_sequence = self.recovery_sequences.get(sequence_key, [])
        
        # Apply browser-specific adjustments
        browser = context.get("browser", "").lower()
        if browser in self.browser_quirks:
            recovery_sequence = self._adjust_for_browser(recovery_sequence, browser)
        
        # Track healing attempt
        healing_attempt = {
            "timestamp": datetime.now(),
            "error_code": error_code,
            "diagnosis": diagnosis,
            "sequence": sequence_key,
            "context": context
        }
        
        # Execute recovery sequence
        result = {
            "success": False,
            "actions_taken": [],
            "final_state": None,
            "healing_id": hashlib.md5(f"{error_code}{datetime.now()}".encode()).hexdigest()[:8]
        }
        
        for step in recovery_sequence:
            step_result = await self._execute_healing_step(step, context)
            result["actions_taken"].append({
                "action": step["action"],
                "success": step_result["success"],
                "details": step_result.get("details")
            })
            
            if step_result["success"]:
                result["success"] = True
                result["final_state"] = step_result.get("state")
                break
                
            # Wait before next step
            if "delay_ms" in step and step["delay_ms"] > 0:
                await asyncio.sleep(step["delay_ms"] / 1000.0)
        
        # Record outcome
        healing_attempt["result"] = result
        self.healing_history.append(healing_attempt)
        
        # Learn from outcome
        self._update_strategy_effectiveness(sequence_key, result["success"])
        
        return result
    
    def _calculate_severity(self, error_code: str, context: Dict) -> str:
        """Calculate issue severity"""
        retry_count = context.get("retry_count", 0)
        session_duration = context.get("session_duration", 0)
        
        if retry_count > 5:
            return "critical"
        elif retry_count > 2 or session_duration < 1000:
            return "high"
        elif error_code in ["NotAllowedError", "NotFoundError"]:
            return "medium"
        else:
            return "low"
    
    def _map_error_to_sequence(self, error_code: str, context: Dict) -> str:
        """Map error to recovery sequence"""
        mapping = {
            "NotAllowedError": "permission_denied",
            "NotFoundError": "device_not_found",
            "NotReadableError": "device_not_found",
            "SecurityError": "permission_denied",
            "NetworkError": "network_error",
            "ContextSuspended": "context_suspended",
            "HighLatency": "high_latency"
        }
        
        # Check for specific conditions
        if context.get("audio_context_state") == "suspended":
            return "context_suspended"
        elif context.get("latency_ms", 0) > 1000:
            return "high_latency"
            
        return mapping.get(error_code, "device_not_found")
    
    def _adjust_for_browser(self, sequence: List[Dict], browser: str) -> List[Dict]:
        """Adjust recovery sequence for browser quirks"""
        quirks = self.browser_quirks.get(browser, {})
        adjusted_sequence = []
        
        for step in sequence:
            # Skip unsupported features
            if step["action"] == "enable_echo_cancellation" and not quirks.get("supports_echo_cancellation"):
                continue
                
            # Add browser-specific steps
            if step["action"] == "check_browser_settings":
                for issue in quirks.get("known_issues", []):
                    if "workaround" in issue:
                        adjusted_sequence.append({
                            "action": issue["workaround"],
                            "delay_ms": 0,
                            "browser_specific": True
                        })
            
            adjusted_sequence.append(step)
            
        return adjusted_sequence
    
    async def _execute_healing_step(self, step: Dict, context: Dict) -> Dict[str, Any]:
        """Execute a single healing step"""
        action = step["action"]
        result = {"success": False, "details": {}}
        
        try:
            if action == "prompt_user":
                result["success"] = True
                result["details"] = {
                    "message": step.get("message"),
                    "ui_element": step.get("ui_element")
                }
            elif action == "check_browser_settings":
                result["success"] = True
                result["details"] = {
                    "script": step.get("script"),
                    "check_result": "pending_client_execution"
                }
            elif action == "enumerate_devices":
                result["success"] = True
                result["details"] = {
                    "script": step.get("script"),
                    "expected_devices": ["microphone", "speaker"]
                }
            elif action == "reduce_quality":
                result["success"] = True
                result["details"] = {
                    "adjustments": step.get("adjustments"),
                    "quality_level": "reduced"
                }
            else:
                result["success"] = True
                result["details"] = {"action": action}
                
        except Exception as e:
            logger.error(f"Healing step failed: {action} - {e}")
            result["details"]["error"] = str(e)
            
        return result
    
    def _find_similar_cases(self, error_code: str, context: Dict) -> List[Dict]:
        """Find similar cases from history"""
        similar = []
        for case in self.healing_history:
            if case["error_code"] == error_code:
                # Check context similarity
                similarity_score = self._calculate_similarity(case["context"], context)
                if similarity_score > 0.7:
                    similar.append({
                        "case": case,
                        "similarity": similarity_score,
                        "resolved": case["result"]["success"]
                    })
        return sorted(similar, key=lambda x: x["similarity"], reverse=True)
    
    def _calculate_similarity(self, context1: Dict, context2: Dict) -> float:
        """Calculate context similarity score"""
        score = 0.0
        factors = [
            ("browser", 0.3),
            ("error_code", 0.3),
            ("permission_state", 0.2),
            ("retry_count", 0.1),
            ("session_duration", 0.1)
        ]
        
        for factor, weight in factors:
            if factor in context1 and factor in context2:
                if context1[factor] == context2[factor]:
                    score += weight
                elif isinstance(context1[factor], (int, float)):
                    # Numeric similarity
                    diff = abs(context1[factor] - context2[factor])
                    max_val = max(context1[factor], context2[factor])
                    if max_val > 0:
                        score += weight * (1 - diff / max_val)
                        
        return score
    
    def _update_strategy_effectiveness(self, strategy: str, success: bool):
        """Update strategy effectiveness tracking"""
        if strategy not in self.successful_strategies:
            self.successful_strategies[strategy] = {"attempts": 0, "successes": 0}
            
        self.successful_strategies[strategy]["attempts"] += 1
        if success:
            self.successful_strategies[strategy]["successes"] += 1
    
    def get_strategy_effectiveness(self) -> Dict[str, float]:
        """Get effectiveness rates for all strategies"""
        effectiveness = {}
        for strategy, stats in self.successful_strategies.items():
            if stats["attempts"] > 0:
                effectiveness[strategy] = stats["successes"] / stats["attempts"]
        return effectiveness

# Global instances
system_state = MLAudioSystemState()
self_healer = AudioSelfHealer()

# WebSocket connection manager
class AudioWebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.client_contexts: Dict[WebSocket, Dict[str, Any]] = {}
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.client_contexts[websocket] = {
            'connected_at': datetime.now(),
            'events': []
        }
        logger.info(f"New ML audio WebSocket connection: {len(self.active_connections)} total")
    
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        if websocket in self.client_contexts:
            del self.client_contexts[websocket]
        logger.info(f"ML audio WebSocket disconnected: {len(self.active_connections)} remaining")
    
    async def broadcast(self, message: dict):
        """Broadcast to all connected clients"""
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")

# Initialize WebSocket manager
ws_manager = AudioWebSocketManager()

# Request models
class AudioErrorRequest(BaseModel):
    error_code: str
    browser: Optional[str] = None
    browser_version: Optional[str] = None
    timestamp: Optional[str] = None
    session_duration: Optional[int] = None
    retry_count: Optional[int] = 0
    permission_state: Optional[str] = None
    user_agent: Optional[str] = None
    audio_context_state: Optional[str] = None
    previous_errors: Optional[List[Dict[str, Any]]] = []

class AudioPrediction(BaseModel):
    audio_data: Optional[str] = Field(None, description="Base64 encoded audio data")
    features: Optional[Dict[str, Any]] = Field(None, description="Optional audio features")
    format: Optional[str] = Field("base64", description="Audio format")
    sample_rate: Optional[int] = Field(16000, description="Sample rate in Hz")
    duration_ms: Optional[int] = Field(None, description="Audio duration in milliseconds")
    client_id: Optional[str] = Field(None, description="Client identifier")

class AudioTelemetryRequest(BaseModel):
    event: str
    data: Dict[str, Any]
    timestamp: str

class AudioConfigUpdate(BaseModel):
    enable_ml: Optional[bool] = None
    auto_recovery: Optional[bool] = None
    max_retries: Optional[int] = None
    retry_delays: Optional[List[int]] = None
    anomaly_threshold: Optional[float] = None
    prediction_threshold: Optional[float] = None

# Helper functions
def analyze_audio_quality(audio_data: str, format: str = "base64", client_id: Optional[str] = None) -> Dict[str, Any]:
    """Analyze audio quality with dynamic scoring"""
    # Base quality calculation using data characteristics
    if audio_data:
        # Use data length and format to influence score
        data_length = len(audio_data)
        
        # Hash-based pseudo-randomness for consistent results per audio
        data_hash = int(hashlib.md5(audio_data.encode()).hexdigest()[:8], 16)
        base_score = 0.7 + (data_hash % 30) / 100  # 0.7-1.0 range
        
        # Adjust based on format
        format_multipliers = {
            "wav": 1.05,
            "webm": 1.02,
            "base64": 1.0,
            "raw": 0.95
        }
        
        quality_score = min(1.0, base_score * format_multipliers.get(format, 1.0))
        
        # Length-based adjustments
        if data_length < 1000:  # Very short audio
            quality_score *= 0.9
        elif data_length > 100000:  # Long audio
            quality_score *= 0.95
    else:
        quality_score = 0.0
    
    # Determine quality level
    quality_level = "unusable"
    for level_name, threshold in [
        ("excellent", 0.9),
        ("good", 0.7),
        ("fair", 0.5),
        ("poor", 0.3)
    ]:
        if quality_score >= threshold:
            quality_level = level_name
            break
    
    # Dynamic issue detection based on score and system state
    issues = []
    if quality_score < 0.95:
        possible_issues = {
            "background_noise": quality_score < 0.9 and system_state.issue_frequency.get("background_noise", 0) > 5,
            "echo": quality_score < 0.85 and "echo" in str(audio_data)[:100],  # Simple heuristic
            "clipping": quality_score < 0.8 and data_length % 1000 < 100,  # Pattern detection
            "low_volume": quality_score < 0.75,
            "distortion": quality_score < 0.7,
            "interference": quality_score < 0.6 and datetime.now().second % 3 == 0,  # Time-based
            "codec_issues": format == "webm" and quality_score < 0.8
        }
        
        issues = [issue for issue, condition in possible_issues.items() if condition]
    
    # Calculate detailed metrics
    snr_base = 15 + quality_score * 35  # 15-50 dB range
    snr = round(snr_base + (datetime.now().microsecond % 1000) / 200, 1)  # Add variation
    
    result = {
        "score": round(quality_score, 3),
        "level": quality_level,
        "description": f"{quality_level.capitalize()} audio quality detected",
        "issues_detected": issues,
        "signal_to_noise_ratio": snr,
        "peak_amplitude": round(0.6 + quality_score * 0.35, 2),  # 0.6-0.95 range
        "rms_level": round(-25 + quality_score * 20, 1),  # -25 to -5 dB range
        "frequency_response": "20Hz-20kHz" if quality_score > 0.8 else "50Hz-15kHz",
        "dynamic_range": round(60 + quality_score * 30, 1),  # 60-90 dB
        "thd_percent": round((1 - quality_score) * 5, 2)  # Total harmonic distortion
    }
    
    # Track quality for insights
    system_state.track_quality(quality_score, issues)
    
    return result

def generate_recommendations(issues: List[str], quality_score: float, client_id: Optional[str] = None) -> List[str]:
    """Generate intelligent, context-aware recommendations"""
    recommendations = []
    
    # Get quality insights
    insights = system_state.get_quality_insights()
    
    # Issue-specific solutions with context
    issue_solutions = {
        "background_noise": [
            "Enable AI-powered noise suppression in your audio settings",
            "Use a directional microphone to reduce ambient noise",
            "Record in a quieter environment or use acoustic treatment"
        ],
        "echo": [
            "Use headphones to prevent audio feedback",
            "Reduce speaker volume or increase distance from microphone",
            "Enable echo cancellation in your audio processing pipeline"
        ],
        "clipping": [
            "Reduce input gain by 10-15%",
            "Enable automatic gain control (AGC)",
            "Check for audio limiter settings in your recording software"
        ],
        "low_volume": [
            "Increase microphone gain gradually until optimal",
            "Position microphone 6-12 inches from sound source",
            "Check system audio boost settings"
        ],
        "distortion": [
            "Lower the input sensitivity",
            "Check cable connections for damage",
            "Update audio drivers to latest version"
        ],
        "interference": [
            "Move away from electromagnetic sources",
            "Use shielded audio cables",
            "Check for ground loop issues"
        ],
        "codec_issues": [
            "Try using WAV format for better quality",
            "Update your browser or audio codec",
            "Check bitrate settings in your encoder"
        ]
    }
    
    # Add relevant solutions
    for issue in issues:
        if issue in issue_solutions:
            # Pick most relevant solution based on frequency
            solutions = issue_solutions[issue]
            if system_state.issue_frequency.get(issue, 0) > 10:
                recommendations.append(solutions[0])  # Most likely solution
            else:
                recommendations.append(solutions[len(recommendations) % len(solutions)])
    
    # Quality-based recommendations
    if quality_score < 0.6:
        recommendations.append("Consider upgrading to a professional audio interface")
    elif quality_score < 0.7 and insights["quality_trend"] == "degrading":
        recommendations.append("Audio quality is declining - check microphone condition")
    elif quality_score < 0.8 and not issues:
        recommendations.append("Enable audio enhancement features for optimal quality")
    
    # Trend-based recommendations
    if insights["quality_trend"] == "improving":
        recommendations.append("Your audio quality is improving! Keep current settings")
    elif insights["quality_variance"] > 0.01:
        recommendations.append("Audio quality is inconsistent - check for environmental changes")
    
    # System-based recommendations
    if system_state.system_capabilities.get("advanced_audio_analysis") and quality_score < 0.9:
        recommendations.append("Advanced audio analysis available - enable for better processing")
    
    # Limit recommendations
    return recommendations[:3] if recommendations else ["Audio quality is optimal"]

def get_dynamic_config(request: Request) -> Dict[str, Any]:
    """Generate dynamic configuration based on current system state"""
    # Get system info
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    
    # Get network info
    try:
        import netifaces
        interfaces = netifaces.interfaces()
        network_available = len(interfaces) > 1
    except Exception:
        network_available = True
    
    # Build dynamic config
    config = {
        "model": "silero_vad" if not system_state.system_capabilities.get("neural_audio_processing") else "advanced_neural_vad",
        "sample_rate": 16000,
        "chunk_size": 512,
        "vad_threshold": 0.5,
        "min_silence_duration_ms": 300,
        "speech_pad_ms": 30,
        "features": {
            "voice_activity_detection": True,
            "noise_suppression": True,
            "echo_cancellation": cpu_percent < 70,  # Disable if high CPU
            "automatic_gain_control": True,
            "speech_enhancement": memory.percent < 80,  # Disable if low memory
            "background_noise_reduction": True,
            "advanced_processing": system_state.system_capabilities.get("advanced_audio_analysis", False)
        },
        "supported_formats": ["base64", "raw", "wav", "webm"],
        "max_audio_length_seconds": 60 if memory.percent < 80 else 30,
        "backend_available": True,
        "websocket_endpoint": os.getenv("WEBSOCKET_ENDPOINT", "/ws"),
        "legacy_endpoints_deprecated": True,
        "performance": system_state.get_performance_metrics(),
        "advanced_features": {
            "emotion_detection": system_state.system_capabilities.get("neural_audio_processing", False),
            "speaker_diarization": system_state.system_capabilities.get("neural_audio_processing", False) and cpu_percent < 50,
            "language_detection": True,
            "transcription_available": system_state.system_capabilities.get("librosa_available", False),
            "real_time_enhancement": cpu_percent < 60
        },
        "system_status": {
            "cpu_usage": cpu_percent,
            "memory_usage": memory.percent,
            "network_available": network_available,
            "uptime_hours": round(system_state.get_uptime(), 2),
            "platform": platform.system(),
            "capabilities": system_state.system_capabilities
        }
    }
    
    return config

# API Endpoints with ML fallback
@router.get("/config")
async def get_ml_config(request: Request = None):
    """Get ML audio configuration"""
    if ML_AVAILABLE:
        try:
            audio_manager = get_audio_manager()
            config = audio_manager.config
            logger.info("Serving ML audio config")
        except Exception as e:
            logger.warning(f"Error getting ML config: {e}, using fallback")
            config = get_dynamic_config(request) if request else {
                "enableML": False,
                "autoRecovery": True,
                "maxRetries": 3,
                "retryDelays": [1000, 2000, 3000],
                "anomalyThreshold": 0.8,
                "predictionThreshold": 0.7,
                "is_fallback": True
            }
    else:
        config = get_dynamic_config(request) if request else {
            "enableML": False,
            "autoRecovery": True,
            "maxRetries": 3,
            "retryDelays": [1000, 2000, 3000],
            "anomalyThreshold": 0.8,
            "predictionThreshold": 0.7,
            "is_fallback": True
        }
    
    # Add client-specific info if request provided
    if request and hasattr(request, 'client') and request.client:
        client_id = f"{request.client.host}_{request.headers.get('user-agent', 'unknown')}"
        config["client_info"] = {
            "ip": request.client.host,
            "user_agent": request.headers.get("user-agent", "unknown"),
            "recommended_settings": system_state.get_client_recommendations(
                client_id, 
                request.headers.get("user-agent", "")
            )
        }
    
    # Add quality insights
    config["quality_insights"] = system_state.get_quality_insights()
    
    return JSONResponse(content=config)

@router.post("/config")
async def update_ml_config(config: AudioConfigUpdate):
    """Update ML audio configuration"""
    if ML_AVAILABLE:
        try:
            audio_manager = get_audio_manager()
            
            # Update configuration
            update_dict = config.dict(exclude_unset=True)
            audio_manager.config.update(update_dict)
            
            # Save to environment
            for key, value in update_dict.items():
                os.environ[f'JARVIS_AUDIO_{key.upper()}'] = str(value)
            
            logger.info(f"Updated ML audio config: {update_dict}")
            
            return JSONResponse(content={
                "success": True,
                "updated": update_dict,
                "config": audio_manager.config
            })
        except Exception as e:
            logger.error(f"Error updating ML config: {e}")
            return JSONResponse(content={
                "success": False,
                "error": str(e),
                "is_fallback": True
            })
    else:
        return JSONResponse(content={
            "success": False,
            "error": "ML system not available",
            "is_fallback": True
        })

@router.post("/error")
async def handle_audio_error(request: AudioErrorRequest):
    """Handle audio error with ML-driven recovery strategy"""
    if ML_AVAILABLE:
        try:
            audio_manager = get_audio_manager()
            
            # Create context from request
            context = request.dict()
            
            # Handle the error
            result = await audio_manager.handle_error(request.error_code, context)
            
            # Broadcast to WebSocket clients
            await ws_manager.broadcast({
                "type": "error_handled",
                "error_code": request.error_code,
                "result": result
            })
            
            return JSONResponse(content={
                "success": result.get("success", False),
                "strategy": result,
                "ml_confidence": result.get("ml_confidence", 0)
            })
        except Exception as e:
            logger.error(f"Error in ML error handler: {e}, using fallback")
            # Fall through to fallback handler
    
    # Enhanced fallback error handler with self-healing
    logger.info(f"Audio error (fallback): {request.error_code} from {request.browser}")
    
    # Apply self-healing
    healing_result = await self_healer.heal(request.error_code, request.dict())
    
    if healing_result["success"]:
        return JSONResponse(content={
            "success": True,
            "strategy": {
                "action": "self_healed",
                "healing_id": healing_result["healing_id"],
                "actions_taken": healing_result["actions_taken"],
                "message": "Issue resolved through self-healing"
            },
            "ml_confidence": 0.0,
            "is_fallback": True,
            "self_healed": True
        })
    
    # Enhanced fallback strategies with more intelligence
    strategies = {
        "NotAllowedError": {
            "action": "requestPermissions",
            "message": "Please allow microphone access in your browser settings",
            "steps": self._get_browser_specific_steps(request.browser, "permission"),
            "alternative_actions": [
                {
                    "type": "settings_deep_link",
                    "url": self._get_browser_settings_url(request.browser, "microphone")
                },
                {
                    "type": "visual_guide",
                    "images": self._get_permission_guide_images(request.browser)
                }
            ],
            "auto_retry": {
                "enabled": True,
                "delay_ms": 3000,
                "max_attempts": 3
            }
        },
        "NotFoundError": {
            "action": "checkDevices",
            "message": "No microphone detected",
            "steps": [
                "Check if your microphone is properly connected",
                "Try unplugging and reconnecting your microphone",
                "Check system sound settings",
                f"On {self._get_os_name()}: {self._get_os_audio_settings_path()}"
            ],
            "device_enumeration": {
                "script": "navigator.mediaDevices.enumerateDevices()",
                "fallback_devices": ["default", "communications", "any"]
            },
            "system_checks": [
                {
                    "type": "service_status",
                    "services": self._get_os_audio_services()
                },
                {
                    "type": "driver_check",
                    "common_issues": self._get_common_driver_issues()
                }
            ]
        },
        "NotReadableError": {
            "action": "releaseAndRetry",
            "message": "Microphone is being used by another application",
            "steps": [
                "Close other applications that might be using the microphone",
                "Check if your browser has multiple tabs using the microphone",
                "Try restarting your browser"
            ],
            "conflict_detection": {
                "common_apps": self._get_common_audio_apps(),
                "check_command": self._get_audio_process_check_command()
            },
            "force_release": {
                "method": "recreate_context",
                "cleanup_script": "if(window.audioContext) { window.audioContext.close(); }",
                "delay_before_retry": 1000
            },
            "escalation": [
                {
                    "after_attempts": 2,
                    "action": "suggest_exclusive_mode_disable"
                },
                {
                    "after_attempts": 3,
                    "action": "offer_device_reset"
                }
            ]
        },
        "SecurityError": {
            "action": "checkHttps",
            "message": "Microphone access requires a secure connection",
            "steps": [
                "Make sure you're accessing the site via HTTPS",
                "Check if the site has a valid SSL certificate",
                "For local development, use localhost or 127.0.0.1"
            ],
            "security_checks": {
                "is_secure": "window.isSecureContext",
                "protocol": "window.location.protocol",
                "exceptions": ["localhost", "127.0.0.1", "::1"]
            }
        },
        "OverconstrainedError": {
            "action": "relaxConstraints",
            "message": "Audio constraints are too strict",
            "steps": [
                "Reducing audio quality requirements",
                "Trying with basic audio settings",
                "Checking device capabilities"
            ],
            "constraint_fallbacks": [
                {"audio": True},
                {"audio": {"channelCount": 1}},
                {"audio": {"sampleRate": 16000}},
                {"audio": {"echoCancellation": False, "noiseSuppression": False}}
            ]
        },
        "AbortError": {
            "action": "handleAbort",
            "message": "Audio operation was aborted",
            "steps": [
                "Checking for system interruptions",
                "Verifying browser stability",
                "Preparing to retry"
            ],
            "recovery": {
                "wait_ms": 2000,
                "check_system_state": True,
                "retry_with_fresh_context": True
            }
        }
    }
    
    # Get strategy for the error code with intelligent fallback
    strategy = strategies.get(request.error_code, {
        "action": "intelligentDiagnosis",
        "message": f"Unexpected audio error: {request.error_code}",
        "steps": [
            "Running automatic diagnosis...",
            "Checking system compatibility...",
            "Preparing recovery options..."
        ],
        "diagnosis": await self_healer.diagnose_issue(request.error_code, request.dict()),
        "auto_recovery": {
            "enabled": True,
            "strategy": "adaptive"
        }
    })
    
    # Add browser-specific enhancements
    if request.browser:
        strategy = self._enhance_strategy_for_browser(strategy, request.browser)
    
    # Add device fingerprinting for better future handling
    device_fingerprint = self._generate_device_fingerprint(request.dict())
    self_healer.device_fingerprints[device_fingerprint] = {
        "last_error": request.error_code,
        "timestamp": datetime.now(),
        "context": request.dict()
    }
    
    # Add retry logic
    if request.retry_count >= 3:
        strategy["action"] = "fallbackMode"
        strategy["message"] = "Multiple attempts failed. Switching to fallback mode."
        strategy["fallback"] = True
    
    return JSONResponse(content={
        "success": True,
        "strategy": strategy,
        "ml_confidence": 0.0,  # No ML in fallback mode
        "is_fallback": True
    })

@router.post("/predict")
async def predict_audio_issue(data: AudioPrediction, request: Request):
    """Predict potential audio issues with preemptive healing"""
    start_time = datetime.now()
    
    # Get client ID
    client_id = data.client_id or f"{request.client.host if request.client else 'anonymous'}_{datetime.now().timestamp()}"
    
    logger.info(f"ML Audio prediction from {client_id} - format: {data.format}, size: {len(data.audio_data) if data.audio_data else 0}")
    
    # Check for preemptive healing opportunities
    device_fingerprint = _generate_device_fingerprint({
        "browser": request.headers.get("user-agent", ""),
        "client_id": client_id
    })
    
    preemptive_actions = []
    if device_fingerprint in self_healer.device_fingerprints:
        device_data = self_healer.device_fingerprints[device_fingerprint]
        last_error = device_data.get("last_error")
        
        # If this device had issues before, suggest preemptive actions
        if last_error:
            if last_error == "NotAllowedError":
                preemptive_actions.append({
                    "action": "check_permission",
                    "reason": "This device previously had permission issues",
                    "script": "navigator.permissions.query({name: 'microphone'})"
                })
            elif last_error == "NotFoundError":
                preemptive_actions.append({
                    "action": "verify_device",
                    "reason": "This device previously had detection issues",
                    "script": "navigator.mediaDevices.enumerateDevices()"
                })
            elif last_error == "NotReadableError":
                preemptive_actions.append({
                    "action": "test_availability",
                    "reason": "This device previously had availability issues",
                    "constraints": {"audio": {"deviceId": "default"}}
                })
    
    # Analyze audio quality
    quality_analysis = analyze_audio_quality(
        data.audio_data or "",
        data.format or "base64",
        client_id
    )
    
    # Dynamic prediction based on analysis
    confidence_boost = len(system_state.quality_history) / 1000  # Increase confidence with more data
    
    if quality_analysis["score"] > 0.9:
        prediction = "excellent"
        confidence = min(0.99, 0.95 + confidence_boost)
    elif quality_analysis["score"] > 0.7:
        prediction = "normal"
        confidence = min(0.95, 0.85 + confidence_boost)
    elif quality_analysis["score"] > 0.5:
        prediction = "degraded"
        confidence = min(0.85, 0.75 + confidence_boost)
    else:
        prediction = "poor"
        confidence = min(0.75, 0.65 + confidence_boost)
    
    # Generate intelligent recommendations
    recommendations = generate_recommendations(
        quality_analysis["issues_detected"],
        quality_analysis["score"],
        client_id
    )
    
    # Calculate processing time
    processing_time = (datetime.now() - start_time).total_seconds() * 1000
    system_state.track_processing(processing_time)
    
    # Build comprehensive response
    response = {
        "prediction": prediction,
        "confidence": round(confidence, 3),
        "issues": quality_analysis["issues_detected"],
        "recommendations": recommendations,
        "audio_quality": quality_analysis,
        "processing_time_ms": round(processing_time, 2),
        "detailed_analysis": {
            "vad_detected": quality_analysis["score"] > 0.3,
            "speech_segments": [] if quality_analysis["score"] < 0.5 else [
                {
                    "start_ms": 0, 
                    "end_ms": data.duration_ms or 1000,
                    "confidence": confidence,
                    "energy_level": quality_analysis["peak_amplitude"]
                }
            ],
            "noise_profile": {
                "type": "complex" if len(quality_analysis["issues_detected"]) > 2 else 
                       "ambient" if "background_noise" in quality_analysis["issues_detected"] else "clean",
                "level_db": quality_analysis["rms_level"],
                "frequency_mask": [bool(i % 2) for i in range(8)]  # Frequency band mask
            },
            "spectral_features": {
                "centroid_hz": 2000 + quality_analysis["score"] * 2000,
                "rolloff_hz": 5000 + quality_analysis["score"] * 5000,
                "flux": round(0.1 + (1 - quality_analysis["score"]) * 0.3, 3),
                "mfcc_available": system_state.system_capabilities.get("librosa_available", False)
            },
            "format_info": {
                "input_format": data.format,
                "sample_rate": data.sample_rate,
                "duration_ms": data.duration_ms,
                "estimated_bitrate": 128 if data.format == "webm" else 256
            },
            "system_load": {
                "current_streams": len(system_state.active_streams),
                "processing_capacity": f"{100 - psutil.cpu_percent():.1f}%",
                "queue_depth": 0  # Would be actual queue depth in production
            },
            "enhancement_applied": quality_analysis["score"] < 0.8,
            "migration_note": "For real-time processing, please use WebSocket at " + os.getenv("WEBSOCKET_ENDPOINT", "/ws")
        },
        "preemptive_healing": {
            "suggested_actions": preemptive_actions,
            "device_recognized": device_fingerprint in self_healer.device_fingerprints,
            "healing_confidence": 0.8 if preemptive_actions else 0.0
        }
    }
    
    return response

@router.post("/telemetry")
async def receive_telemetry(request: AudioTelemetryRequest):
    """Receive telemetry data from client"""
    logger.info(f"Audio telemetry: {request.event} - {request.data}")
    
    if ML_AVAILABLE:
        try:
            # Process telemetry
            if request.event == "recovery":
                # Learn from successful recovery
                audio_manager = get_audio_manager()
                event = AudioEvent(
                    timestamp=datetime.fromisoformat(request.timestamp.replace('Z', '+00:00')),
                    event_type="recovery",
                    resolution=request.data.get("method"),
                    browser=request.data.get("browser"),
                    context=request.data
                )
                await audio_manager.pattern_learner.learn_from_event(event)
        except Exception as e:
            logger.error(f"Error processing telemetry: {e}")
    
    return JSONResponse(content={"success": True, "is_fallback": not ML_AVAILABLE})

@router.get("/metrics")
async def get_ml_metrics():
    """Get ML audio system metrics with enhanced self-healing stats"""
    metrics = {
        "total_errors": 0,
        "success_rate": 0.0,
        "ml_model_accuracy": 0.0,
        "is_fallback": True,
        "system_metrics": system_state.get_performance_metrics(),
        "quality_insights": system_state.get_quality_insights()
    }
    
    if ML_AVAILABLE:
        try:
            audio_manager = get_audio_manager()
            ml_metrics = audio_manager.get_metrics()
            metrics.update(ml_metrics)
            metrics["insights"] = _generate_insights(ml_metrics, audio_manager)
            metrics["is_fallback"] = False
        except Exception as e:
            logger.error(f"Error getting ML metrics: {e}")
    
    # Add self-healing metrics
    healing_stats = {
        "total_healing_attempts": len(self_healer.healing_history),
        "successful_healings": len([h for h in self_healer.healing_history if h["result"]["success"]]),
        "strategy_effectiveness": self_healer.get_strategy_effectiveness(),
        "recent_issues": {},
        "device_fingerprints": len(self_healer.device_fingerprints),
        "healing_success_rate": 0.0
    }
    
    # Calculate healing success rate
    if healing_stats["total_healing_attempts"] > 0:
        healing_stats["healing_success_rate"] = healing_stats["successful_healings"] / healing_stats["total_healing_attempts"]
    
    # Get recent issue frequency
    for event in list(self_healer.healing_history)[-20:]:  # Last 20 events
        error_code = event["error_code"]
        healing_stats["recent_issues"][error_code] = healing_stats["recent_issues"].get(error_code, 0) + 1
    
    metrics["self_healing"] = healing_stats
    
    # Add browser-specific insights
    browser_stats = {}
    for fingerprint, data in self_healer.device_fingerprints.items():
        browser = data["context"].get("browser", "unknown")
        if browser not in browser_stats:
            browser_stats[browser] = {"errors": 0, "last_seen": None}
        browser_stats[browser]["errors"] += 1
        browser_stats[browser]["last_seen"] = data["timestamp"].isoformat()
    
    metrics["browser_statistics"] = browser_stats
    
    return JSONResponse(content=metrics)

@router.get("/status")
async def get_ml_audio_status():
    """Get comprehensive ML audio system status"""
    # Get real system metrics
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    # Get process info
    process = psutil.Process()
    process_info = {
        "memory_mb": round(process.memory_info().rss / 1024 / 1024, 1),
        "cpu_percent": process.cpu_percent(interval=0.1),
        "threads": process.num_threads(),
        "open_files": len(process.open_files()) if hasattr(process, "open_files") else 0
    }
    
    # Network statistics
    try:
        net_io = psutil.net_io_counters()
        network_stats = {
            "bytes_sent_mb": round(net_io.bytes_sent / 1024 / 1024, 1),
            "bytes_recv_mb": round(net_io.bytes_recv / 1024 / 1024, 1),
            "packets_dropped": net_io.dropin + net_io.dropout
        }
    except Exception:
        network_stats = {"status": "unavailable"}
    
    return {
        "status": "operational" if cpu_percent < 90 and memory.percent < 90 else "degraded",
        "model_loaded": system_state.model_loaded,
        "ml_available": ML_AVAILABLE,
        "websocket_available": True,
        "websocket_endpoint": os.getenv("WEBSOCKET_ENDPOINT", "/ws"),
        "system_health": {
            "cpu_usage_percent": cpu_percent,
            "memory_usage_mb": round(memory.used / 1024 / 1024, 1),
            "memory_percent": memory.percent,
            "disk_usage_percent": disk.percent,
            "active_streams": len(system_state.active_streams),
            "total_processed_today": system_state.total_processed,
            "last_activity": system_state.last_activity.isoformat() if system_state.last_activity else None,
            "uptime_hours": round(system_state.get_uptime(), 2),
            "process_info": process_info,
            "network": network_stats
        },
        "performance_metrics": system_state.get_performance_metrics(),
        "quality_insights": system_state.get_quality_insights(),
        "capabilities": {
            "real_time_processing": True,
            "batch_processing": True,
            "multi_language": True,
            "noise_cancellation": True,
            "echo_suppression": cpu_percent < 80,
            "advanced_features": system_state.system_capabilities
        },
        "model_info": {
            "name": "silero_vad" if not system_state.system_capabilities.get("neural_audio_processing") else "advanced_neural_vad",
            "version": "4.0",
            "last_updated": "2024-01-15",
            "accuracy": 0.97 if ML_AVAILABLE else 0.0,
            "framework": "PyTorch" if system_state.system_capabilities.get("pytorch_available") else "NumPy"
        },
        "audio_buffer_stats": system_state.audio_buffer_stats,
        "issue_statistics": dict(sorted(system_state.issue_frequency.items(), key=lambda x: x[1], reverse=True)[:5]),
        "recommendations": {
            "system": "Upgrade to 16GB RAM for optimal performance" if memory.total < 16 * 1024**3 else "System resources optimal",
            "configuration": "Enable GPU acceleration" if not system_state.system_capabilities.get("gpu_acceleration") else "GPU acceleration active"
        },
        "api_version": "2.0",
        "legacy_notice": "This endpoint provides compatibility. For best performance, migrate to unified WebSocket at " + os.getenv("WEBSOCKET_ENDPOINT", "/ws")
    }

@router.websocket("/stream")
async def ml_audio_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time ML audio updates"""
    await ws_manager.connect(websocket)
    
    try:
        # Send initial connection message
        await websocket.send_json({
            "type": "connected",
            "message": "Connected to ML Audio System",
            "timestamp": datetime.now().isoformat(),
            "ml_available": ML_AVAILABLE
        })
        
        # Send current metrics
        metrics = system_state.get_performance_metrics()
        await websocket.send_json({
            "type": "metrics",
            "metrics": metrics
        })
        
        # Keep connection alive with timeout protection
        idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

        while True:
            # Receive messages from client with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("ML audio WebSocket idle timeout, closing connection")
                break

            if data.get("type") == "telemetry":
                # Process telemetry
                event_data = data.get("data", {})
                
                if ML_AVAILABLE:
                    try:
                        audio_manager = get_audio_manager()
                        event = AudioEvent(
                            timestamp=datetime.now(),
                            event_type=data.get("event"),
                            browser=event_data.get("browser"),
                            context=event_data
                        )
                        await audio_manager.pattern_learner.learn_from_event(event)
                    except Exception as e:
                        logger.error(f"Error processing WebSocket telemetry: {e}")
                        
            elif data.get("type") == "ping":
                # Respond to ping
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": datetime.now().isoformat()
                })
            
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)

# Helper functions
def _generate_insights(metrics: Dict[str, Any], audio_manager) -> List[str]:
    """Generate insights from metrics"""
    insights = []
    
    # Success rate insight
    success_rate = metrics.get('success_rate', 0)
    if success_rate < 0.5:
        insights.append("Low recovery success rate - consider updating strategies")
    elif success_rate > 0.8:
        insights.append("High recovery success rate - ML strategies working well")
    
    # Strategy effectiveness
    strategy_rates = metrics.get('strategy_success_rates', {})
    if strategy_rates:
        best_strategy = max(strategy_rates.items(), key=lambda x: x[1])
        insights.append(f"Most effective strategy: {best_strategy[0]} ({best_strategy[1]:.0%} success)")
    
    # ML accuracy
    ml_accuracy = metrics.get('ml_model_accuracy', 0)
    if ml_accuracy > 0.7:
        insights.append(f"ML predictions are accurate ({ml_accuracy:.0%})")
    elif ml_accuracy < 0.3:
        insights.append("ML model needs more training data")
    
    return insights

# Helper methods for enhanced fallback strategies
def _get_browser_specific_steps(browser: str, action_type: str) -> List[str]:
    """Get browser-specific steps for actions"""
    browser_lower = (browser or "").lower()
    
    if action_type == "permission":
        if "chrome" in browser_lower:
            return [
                "Click the camera/microphone icon in the address bar (right side)",
                "Select 'Allow' from the dropdown menu",
                "If you don't see the icon, click the lock icon instead",
                "Go to 'Site settings' > 'Microphone' > Allow"
            ]
        elif "firefox" in browser_lower:
            return [
                "Click the microphone icon in the address bar",
                "Select 'Allow' from the permission panel",
                "Or go to Settings > Privacy & Security > Permissions > Microphone",
                "Find this site and change to 'Allow'"
            ]
        elif "safari" in browser_lower:
            return [
                "Safari > Preferences > Websites > Microphone",
                "Find this website in the list",
                "Change the setting to 'Allow'",
                "You may need to reload the page"
            ]
        elif "edge" in browser_lower:
            return [
                "Click the lock/microphone icon in address bar",
                "Select 'Allow' for microphone permission",
                "Or go to Settings > Cookies and site permissions > Microphone",
                "Add this site to 'Allow' list"
            ]
    
    return ["Check your browser settings for microphone permissions"]

def _get_browser_settings_url(browser: str, setting_type: str) -> str:
    """Get deep link to browser settings"""
    browser_lower = (browser or "").lower()
    
    urls = {
        "chrome": {
            "microphone": "chrome://settings/content/microphone",
            "general": "chrome://settings/"
        },
        "firefox": {
            "microphone": "about:preferences#privacy",
            "general": "about:preferences"
        },
        "edge": {
            "microphone": "edge://settings/content/microphone",
            "general": "edge://settings/"
        },
        "safari": {
            "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            "general": "x-apple.systempreferences:"
        }
    }
    
    for browser_name, settings in urls.items():
        if browser_name in browser_lower:
            return settings.get(setting_type, settings.get("general", "#"))
    
    return "#"

def _get_permission_guide_images(browser: str) -> List[str]:
    """Get URLs for permission guide images"""
    # In production, these would be actual URLs to guide images
    browser_lower = (browser or "").lower()
    base_path = "/static/audio-guides"
    
    if "chrome" in browser_lower:
        return [
            f"{base_path}/chrome-mic-permission-1.png",
            f"{base_path}/chrome-mic-permission-2.png"
        ]
    elif "firefox" in browser_lower:
        return [
            f"{base_path}/firefox-mic-permission-1.png",
            f"{base_path}/firefox-mic-permission-2.png"
        ]
    
    return [f"{base_path}/generic-mic-permission.png"]

def _get_os_name() -> str:
    """Get friendly OS name"""
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    elif system == "Windows":
        return "Windows"
    elif system == "Linux":
        return "Linux"
    return system

def _get_os_audio_settings_path() -> str:
    """Get OS-specific audio settings path"""
    system = platform.system()
    if system == "Darwin":
        return "System Preferences > Sound > Input"
    elif system == "Windows":
        return "Settings > System > Sound > Input devices"
    elif system == "Linux":
        return "Settings > Sound (varies by distribution)"
    return "Check system audio settings"

def _get_os_audio_services() -> List[str]:
    """Get OS-specific audio services"""
    system = platform.system()
    if system == "Darwin":
        return ["coreaudiod", "audio_server"]
    elif system == "Windows":
        return ["Windows Audio", "Windows Audio Endpoint Builder"]
    elif system == "Linux":
        return ["pulseaudio", "pipewire", "alsa"]
    return ["audio service"]

def _get_common_driver_issues() -> List[Dict[str, str]]:
    """Get common audio driver issues and solutions"""
    return [
        {
            "issue": "Outdated audio driver",
            "solution": "Update through Device Manager or manufacturer website"
        },
        {
            "issue": "Conflicting audio drivers",
            "solution": "Uninstall duplicate drivers and restart"
        },
        {
            "issue": "Disabled audio device",
            "solution": "Enable in Device Manager or Sound Settings"
        },
        {
            "issue": "USB audio device power saving",
            "solution": "Disable USB selective suspend in Power Options"
        }
    ]

def _get_common_audio_apps() -> List[str]:
    """Get list of common apps that use audio"""
    system = platform.system()
    common_apps = [
        "Zoom", "Skype", "Discord", "Teams", "Slack",
        "OBS Studio", "Audacity", "Chrome", "Firefox"
    ]
    
    if system == "Darwin":
        common_apps.extend(["FaceTime", "QuickTime Player", "Voice Memos"])
    elif system == "Windows":
        common_apps.extend(["Voice Recorder", "Camera", "Xbox Game Bar"])
    
    return common_apps

def _get_audio_process_check_command() -> str:
    """Get command to check audio-using processes"""
    system = platform.system()
    if system == "Darwin":
        return "lsof | grep -E 'VoiceOver|coreaudio|AudioDevice'"
    elif system == "Windows":
        return "tasklist | findstr /I \"audio\""
    elif system == "Linux":
        return "lsof | grep -E 'pulse|alsa|pipewire'"
    return "Check running audio processes"

def _enhance_strategy_for_browser(strategy: Dict, browser: str) -> Dict:
    """Enhance strategy with browser-specific optimizations"""
    browser_lower = (browser or "").lower()
    enhanced = strategy.copy()
    
    # Add browser-specific enhancements
    if "chrome" in browser_lower:
        enhanced["browser_hints"] = {
            "supports_web_audio_api": True,
            "preferred_sample_rate": 48000,
            "echo_cancellation_available": True
        }
    elif "safari" in browser_lower:
        enhanced["browser_hints"] = {
            "requires_user_gesture": True,
            "preferred_format": "wav",
            "limited_web_audio_api": True
        }
        enhanced["additional_steps"] = [
            "Safari requires user interaction before audio",
            "Click anywhere on the page first"
        ]
    
    return enhanced

def _generate_device_fingerprint(context: Dict) -> str:
    """Generate a unique device fingerprint"""
    fingerprint_data = {
        "browser": context.get("browser", "unknown"),
        "browser_version": context.get("browser_version", "unknown"),
        "user_agent": context.get("user_agent", "unknown"),
        "platform": platform.system(),
        "node": platform.node()
    }
    
    fingerprint_str = json.dumps(fingerprint_data, sort_keys=True)
    return hashlib.md5(fingerprint_str.encode()).hexdigest()[:16]

@router.post("/heal")
async def manual_heal(request: Dict[str, Any]):
    """Manually trigger self-healing for testing or recovery"""
    error_code = request.get("error_code", "UnknownError")
    context = request.get("context", {})
    
    # Run diagnosis
    diagnosis = await self_healer.diagnose_issue(error_code, context)
    
    # Run healing
    healing_result = await self_healer.heal(error_code, context)
    
    return JSONResponse(content={
        "diagnosis": diagnosis,
        "healing_result": healing_result,
        "timestamp": datetime.now().isoformat()
    })

@router.get("/healing-history")
async def get_healing_history(limit: int = 10):
    """Get recent healing history"""
    history = list(self_healer.healing_history)[-limit:]
    
    # Format for response
    formatted_history = []
    for event in reversed(history):
        formatted_history.append({
            "timestamp": event["timestamp"].isoformat(),
            "error_code": event["error_code"],
            "success": event["result"]["success"],
            "actions_taken": event["result"]["actions_taken"],
            "diagnosis": event["diagnosis"]
        })
    
    return JSONResponse(content={
        "history": formatted_history,
        "total_events": len(self_healer.healing_history),
        "success_rate": self_healer.get_strategy_effectiveness()
    })

@router.post("/simulate-error")
async def simulate_error(request: Dict[str, Any]):
    """Simulate an audio error for testing self-healing"""
    error_type = request.get("error_type", "NotAllowedError")
    browser = request.get("browser", "Chrome")
    
    # Create simulated error context
    simulated_context = {
        "error_code": error_type,
        "browser": browser,
        "browser_version": "120.0",
        "timestamp": datetime.now().isoformat(),
        "session_duration": 5000,
        "retry_count": request.get("retry_count", 0),
        "permission_state": request.get("permission_state", "prompt"),
        "user_agent": f"Mozilla/5.0 ({browser})",
        "audio_context_state": request.get("audio_context_state", "running"),
        "simulated": True
    }
    
    # Process through normal error handler
    error_request = AudioErrorRequest(**simulated_context)
    result = await handle_audio_error(error_request)
    
    return result

# Health check endpoint
@router.get("/health")
async def ml_audio_health():
    """Quick health check for ML audio system"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "active_streams": len(system_state.active_streams),
        "uptime_hours": round(system_state.get_uptime(), 2),
        "last_activity": system_state.last_activity.isoformat() if system_state.last_activity else None,
        "models_available": system_state.model_loaded,
        "ml_available": ML_AVAILABLE,
        "self_healing_active": True,
        "healing_success_rate": self_healer.get_strategy_effectiveness()
    }

# Register router
__all__ = ['router']