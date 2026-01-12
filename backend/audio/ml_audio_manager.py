#!/usr/bin/env python3
"""
ML-Enhanced Audio Manager for JARVIS
Implements advanced machine learning algorithms for audio error mitigation
and predictive issue detection with self-learning capabilities
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import DBSCAN
from sklearn.ensemble import RandomForestClassifier, IsolationForest
import joblib
import aiofiles
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class AudioEvent:
    """Represents an audio system event"""
    timestamp: datetime
    event_type: str  # 'error', 'success', 'permission_request', 'recovery'
    error_code: Optional[str] = None
    browser: Optional[str] = None
    os_version: Optional[str] = None
    mic_device: Optional[str] = None
    resolution: Optional[str] = None
    duration_ms: Optional[int] = None
    context: Dict[str, Any] = None
    
    def to_features(self) -> np.ndarray:
        """Convert event to ML features"""
        features = []
        
        # Time-based features
        hour = self.timestamp.hour
        day_of_week = self.timestamp.weekday()
        features.extend([hour, day_of_week])
        
        # Error type encoding
        error_types = ['audio-capture', 'not-allowed', 'no-speech', 'network', 'aborted']
        error_vector = [1 if self.error_code == e else 0 for e in error_types]
        features.extend(error_vector)
        
        # Browser encoding
        browsers = ['chrome', 'safari', 'firefox', 'edge', 'other']
        browser_name = self._detect_browser(self.browser)
        browser_vector = [1 if browser_name == b else 0 for b in browsers]
        features.extend(browser_vector)
        
        # Context features
        if self.context:
            features.append(self.context.get('retry_count', 0))
            features.append(self.context.get('session_duration', 0))
            features.append(1 if self.context.get('first_time_user', False) else 0)
        else:
            features.extend([0, 0, 0])
        
        return np.array(features)
    
    def _detect_browser(self, user_agent: str) -> str:
        """Detect browser from user agent"""
        if not user_agent:
            return 'other'
        ua_lower = user_agent.lower()
        if 'chrome' in ua_lower and 'edge' not in ua_lower:
            return 'chrome'
        elif 'safari' in ua_lower and 'chrome' not in ua_lower:
            return 'safari'
        elif 'firefox' in ua_lower:
            return 'firefox'
        elif 'edge' in ua_lower:
            return 'edge'
        return 'other'

class AudioPatternLearner:
    """Learns patterns from audio events to predict and prevent issues"""
    
    def __init__(self, model_dir: str = None):
        self.model_dir = Path(model_dir or os.getenv('JARVIS_ML_MODELS', './ml_models'))
        self.model_dir.mkdir(exist_ok=True)
        
        # ML Models
        self.error_predictor = RandomForestClassifier(n_estimators=100, random_state=42)
        self.anomaly_detector = IsolationForest(contamination=0.1, random_state=42)
        self.pattern_clusterer = DBSCAN(eps=0.5, min_samples=5)
        self.scaler = StandardScaler()
        
        # Pattern storage
        self.event_history = deque(maxlen=10000)
        self.error_patterns = defaultdict(list)
        self.success_patterns = defaultdict(list)
        self.recovery_strategies = {}
        
        # Load existing models if available
        self._load_models()
    
    def _load_models(self):
        """Load pre-trained models if they exist"""
        try:
            model_path = self.model_dir / 'audio_models.pkl'
            if model_path.exists():
                models = joblib.load(model_path)
                self.error_predictor = models.get('error_predictor', self.error_predictor)
                self.anomaly_detector = models.get('anomaly_detector', self.anomaly_detector)
                self.scaler = models.get('scaler', self.scaler)
                logger.info("Loaded pre-trained audio ML models")
        except Exception as e:
            logger.warning(f"Could not load models: {e}")
    
    def _save_models(self):
        """Save trained models"""
        try:
            models = {
                'error_predictor': self.error_predictor,
                'anomaly_detector': self.anomaly_detector,
                'scaler': self.scaler
            }
            model_path = self.model_dir / 'audio_models.pkl'
            joblib.dump(models, model_path)
            logger.info("Saved audio ML models")
        except Exception as e:
            logger.error(f"Could not save models: {e}")
    
    async def learn_from_event(self, event: AudioEvent):
        """Learn from a new audio event"""
        self.event_history.append(event)
        
        # Categorize event
        if event.event_type == 'error':
            self.error_patterns[event.error_code].append(event)
        elif event.event_type == 'success':
            self.success_patterns[event.browser].append(event)
        
        # Retrain models periodically
        if len(self.event_history) % 100 == 0:
            await self._retrain_models()
    
    async def _retrain_models(self):
        """Retrain ML models with recent data"""
        if len(self.event_history) < 50:
            return
        
        try:
            # Prepare training data
            X = []
            y_error = []
            
            for event in self.event_history:
                features = event.to_features()
                X.append(features)
                y_error.append(1 if event.event_type == 'error' else 0)
            
            X = np.array(X)
            y_error = np.array(y_error)
            
            # Scale features
            X_scaled = self.scaler.fit_transform(X)
            
            # Train error predictor
            if len(np.unique(y_error)) > 1:
                self.error_predictor.fit(X_scaled, y_error)
            
            # Train anomaly detector
            self.anomaly_detector.fit(X_scaled)
            
            # Save models
            self._save_models()
            
            logger.info("Retrained audio ML models")
            
        except Exception as e:
            logger.error(f"Error retraining models: {e}")
    
    def predict_error_probability(self, context: Dict[str, Any]) -> float:
        """Predict probability of audio error occurring"""
        try:
            # Create synthetic event from context
            event = AudioEvent(
                timestamp=datetime.now(),
                event_type='prediction',
                browser=context.get('browser'),
                context=context
            )
            
            features = event.to_features().reshape(1, -1)
            features_scaled = self.scaler.transform(features)
            
            # Get probability of error
            if hasattr(self.error_predictor, 'predict_proba'):
                prob = self.error_predictor.predict_proba(features_scaled)[0][1]
                return float(prob)
            else:
                return 0.5  # Default if model not trained
                
        except Exception as e:
            logger.error(f"Error predicting: {e}")
            return 0.5
    
    def detect_anomaly(self, event: AudioEvent) -> bool:
        """Detect if event is anomalous"""
        try:
            features = event.to_features().reshape(1, -1)
            features_scaled = self.scaler.transform(features)
            prediction = self.anomaly_detector.predict(features_scaled)
            return prediction[0] == -1  # -1 indicates anomaly
        except Exception:
            return False
    
    def get_recovery_strategy(self, error_code: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Get optimal recovery strategy based on learned patterns"""
        strategies = []
        
        # Analyze successful recoveries for this error type
        error_events = self.error_patterns.get(error_code, [])
        success_after_error = []
        
        for i, event in enumerate(self.event_history):
            if event.event_type == 'error' and event.error_code == error_code:
                # Look for success within next 5 events
                for j in range(i+1, min(i+6, len(self.event_history))):
                    if self.event_history[j].event_type == 'success':
                        success_after_error.append({
                            'error': event,
                            'success': self.event_history[j],
                            'steps': j - i
                        })
                        break
        
        # Analyze successful patterns
        if success_after_error:
            # Group by resolution method
            resolution_methods = defaultdict(list)
            for item in success_after_error:
                if item['success'].resolution:
                    resolution_methods[item['success'].resolution].append(item)
            
            # Rank strategies by success rate and speed
            for method, items in resolution_methods.items():
                avg_steps = np.mean([item['steps'] for item in items])
                success_rate = len(items) / max(len(error_events), 1)
                
                strategies.append({
                    'method': method,
                    'success_rate': success_rate,
                    'avg_steps': avg_steps,
                    'confidence': min(len(items) / 10, 1.0)  # Confidence based on sample size
                })
        
        # Sort by success rate and speed
        strategies.sort(key=lambda x: (x['success_rate'], -x['avg_steps']), reverse=True)
        
        # Add default strategies if none found
        if not strategies:
            strategies = self._get_default_strategies(error_code)
        
        return {
            'primary': strategies[0] if strategies else None,
            'alternatives': strategies[1:3] if len(strategies) > 1 else [],
            'ml_confidence': self._calculate_strategy_confidence(strategies)
        }
    
    def _get_default_strategies(self, error_code: str) -> List[Dict[str, Any]]:
        """Get default recovery strategies"""
        defaults = {
            'audio-capture': [
                {
                    'method': 'request_permission',
                    'success_rate': 0.8,
                    'avg_steps': 2,
                    'confidence': 0.5
                },
                {
                    'method': 'browser_settings',
                    'success_rate': 0.6,
                    'avg_steps': 3,
                    'confidence': 0.3
                }
            ],
            'not-allowed': [
                {
                    'method': 'system_settings',
                    'success_rate': 0.7,
                    'avg_steps': 4,
                    'confidence': 0.4
                }
            ]
        }
        return defaults.get(error_code, [])
    
    def _calculate_strategy_confidence(self, strategies: List[Dict[str, Any]]) -> float:
        """Calculate overall confidence in strategies"""
        if not strategies:
            return 0.0
        
        # Weighted confidence based on success rates
        total_weight = sum(s['success_rate'] * s['confidence'] for s in strategies)
        total_confidence = sum(s['confidence'] for s in strategies)
        
        return min(total_weight / max(total_confidence, 1), 1.0)

class AdaptiveAudioErrorHandler:
    """Handles audio errors with ML-driven adaptive strategies"""
    
    def __init__(self, config_path: Optional[str] = None):
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Initialize ML components
        self.pattern_learner = AudioPatternLearner()
        
        # Strategy execution history
        self.strategy_history = deque(maxlen=1000)
        self.active_strategies = {}
        
        # Performance metrics
        self.metrics = {
            'total_errors': 0,
            'resolved_errors': 0,
            'avg_resolution_time': 0,
            'strategy_success_rates': defaultdict(float)
        }
    
    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Load configuration from file or environment"""
        config = {
            'retry_delays': [100, 500, 1000, 2000, 5000],
            'max_retries': 5,
            'ml_threshold': 0.7,
            'anomaly_detection': True,
            'auto_recovery': True,
            'logging_level': 'INFO'
        }
        
        # Load from file if provided
        if config_path and os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config.update(json.load(f))
        
        # Override with environment variables
        for key in config:
            env_key = f'JARVIS_AUDIO_{key.upper()}'
            if env_key in os.environ:
                config[key] = os.environ[env_key]
        
        return config
    
    async def handle_error(self, error_code: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Handle audio error with ML-driven strategy"""
        self.metrics['total_errors'] += 1
        
        # Create event
        event = AudioEvent(
            timestamp=datetime.now(),
            event_type='error',
            error_code=error_code,
            browser=context.get('browser'),
            context=context
        )
        
        # Learn from event
        await self.pattern_learner.learn_from_event(event)
        
        # Check if anomaly
        is_anomaly = self.pattern_learner.detect_anomaly(event)
        if is_anomaly:
            logger.warning(f"Anomalous audio error detected: {error_code}")
        
        # Get recovery strategy
        strategy = self.pattern_learner.get_recovery_strategy(error_code, context)
        
        # Execute strategy
        result = await self._execute_strategy(strategy, event)
        
        # Record outcome
        if result['success']:
            self.metrics['resolved_errors'] += 1
            success_event = AudioEvent(
                timestamp=datetime.now(),
                event_type='success',
                browser=context.get('browser'),
                resolution=result['method'],
                duration_ms=result.get('duration_ms'),
                context=context
            )
            await self.pattern_learner.learn_from_event(success_event)
        
        return result
    
    async def _execute_strategy(self, strategy: Dict[str, Any], event: AudioEvent) -> Dict[str, Any]:
        """Execute recovery strategy"""
        start_time = datetime.now()
        
        if not strategy['primary']:
            return {
                'success': False,
                'message': 'No recovery strategy available',
                'ml_confidence': 0
            }
        
        primary = strategy['primary']
        method = primary['method']
        
        # Execute based on method
        result = await self._execute_recovery_method(method, event)
        
        # If primary fails, try alternatives
        if not result['success'] and strategy['alternatives']:
            for alt in strategy['alternatives']:
                result = await self._execute_recovery_method(alt['method'], event)
                if result['success']:
                    break
        
        # Calculate duration
        duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
        result['duration_ms'] = duration_ms
        
        # Update metrics
        self.strategy_history.append({
            'strategy': method,
            'success': result['success'],
            'duration_ms': duration_ms,
            'timestamp': datetime.now()
        })
        
        return result
    
    async def _execute_recovery_method(self, method: str, event: AudioEvent) -> Dict[str, Any]:
        """Execute specific recovery method"""
        recovery_methods = {
            'request_permission': self._request_permission_recovery,
            'browser_settings': self._browser_settings_recovery,
            'system_settings': self._system_settings_recovery,
            'restart_audio': self._restart_audio_recovery,
            'fallback_mode': self._fallback_mode_recovery
        }
        
        handler = recovery_methods.get(method, self._default_recovery)
        return await handler(event)
    
    async def _request_permission_recovery(self, event: AudioEvent) -> Dict[str, Any]:
        """Recovery by requesting permission again"""
        return {
            'success': True,
            'method': 'request_permission',
            'message': 'Requesting microphone permission',
            'action': {
                'type': 'request_media_permission',
                'params': {
                    'audio': True,
                    'retryDelays': self.config['retry_delays']
                }
            }
        }
    
    async def _browser_settings_recovery(self, event: AudioEvent) -> Dict[str, Any]:
        """Recovery through browser settings guidance"""
        browser = event.browser or 'chrome'
        instructions = self._get_browser_instructions(browser)
        
        return {
            'success': True,
            'method': 'browser_settings',
            'message': 'Browser settings guidance',
            'action': {
                'type': 'show_instructions',
                'params': {
                    'instructions': instructions,
                    'browser': browser
                }
            }
        }
    
    async def _system_settings_recovery(self, event: AudioEvent) -> Dict[str, Any]:
        """Recovery through system settings"""
        return {
            'success': True,
            'method': 'system_settings',
            'message': 'System settings guidance',
            'action': {
                'type': 'show_system_settings',
                'params': {
                    'os': 'macos',
                    'setting': 'microphone_permissions'
                }
            }
        }
    
    async def _restart_audio_recovery(self, event: AudioEvent) -> Dict[str, Any]:
        """Recovery by restarting audio subsystem"""
        return {
            'success': True,
            'method': 'restart_audio',
            'message': 'Restarting audio system',
            'action': {
                'type': 'restart_audio_context',
                'params': {}
            }
        }
    
    async def _fallback_mode_recovery(self, event: AudioEvent) -> Dict[str, Any]:
        """Recovery using fallback input mode"""
        return {
            'success': True,
            'method': 'fallback_mode',
            'message': 'Switching to text input mode',
            'action': {
                'type': 'enable_text_fallback',
                'params': {
                    'showKeyboard': True
                }
            }
        }
    
    async def _default_recovery(self, event: AudioEvent) -> Dict[str, Any]:
        """Default recovery method"""
        return {
            'success': False,
            'method': 'default',
            'message': 'No specific recovery available'
        }
    
    def _get_browser_instructions(self, browser: str) -> Dict[str, List[str]]:
        """Get browser-specific instructions"""
        instructions = {
            'chrome': [
                "Click the lock icon 🔒 in the address bar",
                "Set Microphone to 'Allow'",
                "Reload the page",
                "Alternative: Go to chrome://settings/content/microphone"
            ],
            'safari': [
                "Safari → Preferences → Websites → Microphone",
                "Set localhost to 'Allow'",
                "Reload the page"
            ],
            'firefox': [
                "Click the lock icon 🔒 in the address bar",
                "Click '>' next to 'Connection Secure'",
                "Set Microphone to 'Allow'",
                "Reload the page"
            ]
        }
        return instructions.get(browser, instructions['chrome'])
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        success_rate = self.metrics['resolved_errors'] / max(self.metrics['total_errors'], 1)
        
        # Calculate strategy success rates
        strategy_stats = defaultdict(lambda: {'total': 0, 'success': 0})
        for entry in self.strategy_history:
            strategy = entry['strategy']
            strategy_stats[strategy]['total'] += 1
            if entry['success']:
                strategy_stats[strategy]['success'] += 1
        
        strategy_success_rates = {
            strategy: stats['success'] / max(stats['total'], 1)
            for strategy, stats in strategy_stats.items()
        }
        
        return {
            'total_errors': self.metrics['total_errors'],
            'resolved_errors': self.metrics['resolved_errors'],
            'success_rate': success_rate,
            'strategy_success_rates': strategy_success_rates,
            'ml_model_accuracy': self._calculate_model_accuracy()
        }
    
    def _calculate_model_accuracy(self) -> float:
        """Calculate ML model accuracy"""
        if len(self.strategy_history) < 10:
            return 0.0
        
        # Compare predictions with actual outcomes
        correct_predictions = 0
        total_predictions = 0
        
        for i in range(len(self.strategy_history) - 1):
            current = self.strategy_history[i]
            # Simple accuracy: did we predict success correctly?
            if current.get('predicted_success') is not None:
                total_predictions += 1
                if current['predicted_success'] == current['success']:
                    correct_predictions += 1
        
        return correct_predictions / max(total_predictions, 1)

# Singleton instance
_audio_manager = None

def get_audio_manager() -> AdaptiveAudioErrorHandler:
    """Get singleton audio manager instance"""
    global _audio_manager
    if _audio_manager is None:
        _audio_manager = AdaptiveAudioErrorHandler()
    return _audio_manager

# Export main classes
__all__ = [
    'AudioEvent',
    'AudioPatternLearner',
    'AdaptiveAudioErrorHandler',
    'get_audio_manager'
]