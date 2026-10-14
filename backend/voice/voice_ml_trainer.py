"""
ML-based Voice Training System for JARVIS
Learns from user patterns and adapts using Anthropic's API
"""

import os
import sys
import json
import pickle
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
from collections import defaultdict
import asyncio
import logging

# ML libraries
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler
import joblib

# Audio processing
import librosa
import soundfile as sf
from scipy import signal
from scipy.stats import zscore

# Deep learning for voice embeddings
try:
    import torch
    import torchaudio
    from transformers import Wav2Vec2Processor, Wav2Vec2Model
    DEEP_LEARNING_AVAILABLE = True
except (ImportError, OSError, RuntimeError):
    DEEP_LEARNING_AVAILABLE = False
    from backend.core.optional_dependencies import note_missing
    note_missing("torch", "voice embedding training", install="torch transformers")

# Anthropic
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class VoicePattern:
    """Represents a learned voice pattern"""
    pattern_id: str
    user_text: str
    recognized_text: str
    confidence: float
    audio_features: Dict[str, float]
    timestamp: datetime
    corrected_text: Optional[str] = None
    success: bool = True
    context: Optional[str] = None

@dataclass
class UserVoiceProfile:
    """Complete voice profile for a user"""
    user_id: str
    created_at: datetime
    voice_patterns: List[VoicePattern]
    acoustic_profile: Dict[str, float]
    common_mistakes: Dict[str, str]  # recognized -> intended
    command_clusters: Dict[str, List[str]]
    accuracy_history: List[Tuple[datetime, float]]
    preferred_commands: Dict[str, int]  # command -> frequency

class VoiceMLTrainer:
    """Machine Learning trainer for voice recognition improvement"""
    
    def __init__(self, anthropic_api_key: str, model_dir: str = "models/voice_ml"):
        self.anthropic = Anthropic(api_key=anthropic_api_key)
        self.model_dir = model_dir
        os.makedirs(model_dir, exist_ok=True)
        
        # ML Models
        self.mistake_corrector = None  # Learns common recognition mistakes
        self.command_classifier = None  # Classifies command intent
        self.anomaly_detector = None  # Detects unusual patterns
        self.similarity_model = None  # Finds similar commands
        
        # Feature extractors
        self.tfidf_vectorizer = TfidfVectorizer(max_features=500, ngram_range=(1, 3))
        self.audio_scaler = StandardScaler()
        
        # User profiles
        self.user_profiles: Dict[str, UserVoiceProfile] = {}
        self.current_user = "default"
        
        # Load existing models
        self._load_models()
        
        # Voice embeddings model (lazy loading)
        self.wav2vec_processor = None
        self.wav2vec_model = None
        self._deep_models_initialized = False
        
    def _init_deep_models(self):
        """Initialize deep learning models for voice embeddings"""
        if self._deep_models_initialized:
            return
            
        if not DEEP_LEARNING_AVAILABLE:
            logger.warning("Deep learning not available - skipping Wav2Vec2 initialization")
            return
            
        try:
            logger.info("Skipping Wav2Vec2 initialization - using shared model from ml_enhanced_voice_system")
            # REMOVED DUPLICATE: Wav2Vec2 models are already loaded in ml_enhanced_voice_system.py
            # This prevents loading the same 360MB model twice
            # self.wav2vec_processor = Wav2Vec2Processor.from_pretrained("facebook/wav2vec2-base")
            # self.wav2vec_model = Wav2Vec2Model.from_pretrained("facebook/wav2vec2-base")
            # self.wav2vec_model.eval()
            self._deep_models_initialized = False  # Set to False since we're not loading here
            logger.info("Will use shared Wav2Vec2 models to save memory")
        except Exception as e:
            logger.error(f"Failed to initialize deep models: {e}")
            self.wav2vec_processor = None
            self.wav2vec_model = None
            self._deep_models_initialized = False
    
    def _load_models(self):
        """Load existing ML models"""
        try:
            # Load mistake corrector
            corrector_path = os.path.join(self.model_dir, "mistake_corrector.pkl")
            if os.path.exists(corrector_path):
                self.mistake_corrector = joblib.load(corrector_path)
                logger.info("Loaded mistake corrector model")
            
            # Load command classifier
            classifier_path = os.path.join(self.model_dir, "command_classifier.pkl")
            if os.path.exists(classifier_path):
                self.command_classifier = joblib.load(classifier_path)
                self.tfidf_vectorizer = joblib.load(os.path.join(self.model_dir, "tfidf_vectorizer.pkl"))
                logger.info("Loaded command classifier model")
            
            # Load user profiles
            profiles_path = os.path.join(self.model_dir, "user_profiles.json")
            if os.path.exists(profiles_path):
                with open(profiles_path, 'r') as f:
                    profiles_data = json.load(f)
                    for user_id, profile_data in profiles_data.items():
                        # Reconstruct profile
                        self.user_profiles[user_id] = self._reconstruct_profile(profile_data)
                logger.info(f"Loaded {len(self.user_profiles)} user profiles")
                
        except Exception as e:
            logger.error(f"Error loading models: {e}")
    
    def _save_models(self):
        """Save ML models to disk"""
        try:
            # Save mistake corrector
            if self.mistake_corrector:
                joblib.dump(self.mistake_corrector, os.path.join(self.model_dir, "mistake_corrector.pkl"))
            
            # Save command classifier
            if self.command_classifier:
                joblib.dump(self.command_classifier, os.path.join(self.model_dir, "command_classifier.pkl"))
                joblib.dump(self.tfidf_vectorizer, os.path.join(self.model_dir, "tfidf_vectorizer.pkl"))
            
            # Save user profiles
            profiles_data = {}
            for user_id, profile in self.user_profiles.items():
                profiles_data[user_id] = self._serialize_profile(profile)
            
            with open(os.path.join(self.model_dir, "user_profiles.json"), 'w') as f:
                json.dump(profiles_data, f, indent=2)
                
            logger.info("Models and profiles saved successfully")
            
        except Exception as e:
            logger.error(f"Error saving models: {e}")
    
    def extract_audio_features(self, audio_data: np.ndarray, sample_rate: int = 16000) -> Dict[str, float]:
        """Extract comprehensive audio features for ML"""
        features = {}
        
        try:
            # Basic features
            features['duration'] = len(audio_data) / sample_rate
            features['energy'] = np.mean(np.abs(audio_data))
            features['energy_std'] = np.std(np.abs(audio_data))
            
            # Spectral features
            spectral_centroids = librosa.feature.spectral_centroid(y=audio_data, sr=sample_rate)[0]
            features['spectral_centroid_mean'] = np.mean(spectral_centroids)
            features['spectral_centroid_std'] = np.std(spectral_centroids)
            
            # Zero crossing rate
            zcr = librosa.feature.zero_crossing_rate(audio_data)[0]
            features['zcr_mean'] = np.mean(zcr)
            features['zcr_std'] = np.std(zcr)
            
            # MFCC features
            mfccs = librosa.feature.mfcc(y=audio_data, sr=sample_rate, n_mfcc=13)
            for i in range(13):
                features[f'mfcc_{i}_mean'] = np.mean(mfccs[i])
                features[f'mfcc_{i}_std'] = np.std(mfccs[i])
            
            # Pitch features
            pitches, magnitudes = librosa.piptrack(y=audio_data, sr=sample_rate)
            pitch_values = []
            for t in range(pitches.shape[1]):
                index = magnitudes[:, t].argmax()
                pitch = pitches[index, t]
                if pitch > 0:
                    pitch_values.append(pitch)
            
            if pitch_values:
                features['pitch_mean'] = np.mean(pitch_values)
                features['pitch_std'] = np.std(pitch_values)
            else:
                features['pitch_mean'] = 0
                features['pitch_std'] = 0
            
            # Speech rate estimation (syllables per second)
            onset_frames = librosa.onset.onset_detect(y=audio_data, sr=sample_rate)
            features['speech_rate'] = len(onset_frames) / features['duration'] if features['duration'] > 0 else 0
            
            # Signal-to-noise ratio estimation
            noise_floor = np.percentile(np.abs(audio_data), 10)
            signal_peak = np.percentile(np.abs(audio_data), 90)
            features['snr_estimate'] = 20 * np.log10(signal_peak / noise_floor) if noise_floor > 0 else 0
            
        except Exception as e:
            logger.error(f"Error extracting audio features: {e}")
        
        return features
    
    def get_voice_embedding(self, audio_data: np.ndarray, sample_rate: int = 16000) -> Optional[np.ndarray]:
        """Get deep voice embedding using Wav2Vec2"""
        if not DEEP_LEARNING_AVAILABLE:
            return None
            
        # Lazy initialize deep models
        if not self._deep_models_initialized:
            self._init_deep_models()
            
        if not self.wav2vec_processor:
            return None
        
        try:
            # Preprocess audio
            inputs = self.wav2vec_processor(audio_data, sampling_rate=sample_rate, return_tensors="pt")
            
            # Get embeddings
            with torch.no_grad():
                outputs = self.wav2vec_model(**inputs)
                # CRITICAL: Use .copy() to avoid memory corruption when tensor is GC'd
                embeddings = outputs.last_hidden_state.mean(dim=1).cpu().numpy().copy()

            return embeddings[0]
            
        except Exception as e:
            logger.error(f"Error getting voice embedding: {e}")
            return None
    
    async def learn_from_interaction(self, 
                                   recognized_text: str,
                                   confidence: float,
                                   audio_data: Optional[np.ndarray],
                                   corrected_text: Optional[str],
                                   success: bool,
                                   context: Optional[str] = None):
        """Learn from a voice interaction"""
        # Create voice pattern
        pattern = VoicePattern(
            pattern_id=f"{self.current_user}_{datetime.now().timestamp()}",
            user_text=corrected_text or recognized_text,
            recognized_text=recognized_text,
            confidence=confidence,
            audio_features=self.extract_audio_features(audio_data) if audio_data is not None else {},
            timestamp=datetime.now(),
            corrected_text=corrected_text,
            success=success,
            context=context
        )
        
        # Get or create user profile
        if self.current_user not in self.user_profiles:
            self.user_profiles[self.current_user] = UserVoiceProfile(
                user_id=self.current_user,
                created_at=datetime.now(),
                voice_patterns=[],
                acoustic_profile={},
                common_mistakes={},
                command_clusters={},
                accuracy_history=[],
                preferred_commands={}
            )
        
        profile = self.user_profiles[self.current_user]
        profile.voice_patterns.append(pattern)
        
        # Update common mistakes
        if corrected_text and corrected_text != recognized_text:
            profile.common_mistakes[recognized_text] = corrected_text
        
        # Update preferred commands
        command = corrected_text or recognized_text
        profile.preferred_commands[command] = profile.preferred_commands.get(command, 0) + 1
        
        # Update accuracy history
        current_accuracy = self._calculate_recent_accuracy(profile)
        profile.accuracy_history.append((datetime.now(), current_accuracy))
        
        # Retrain models if we have enough data
        if len(profile.voice_patterns) >= 20:
            await self._retrain_models()
        
        # Save periodically
        if len(profile.voice_patterns) % 10 == 0:
            self._save_models()
    
    def _calculate_recent_accuracy(self, profile: UserVoiceProfile, window_size: int = 20) -> float:
        """Calculate recent accuracy from patterns"""
        recent_patterns = profile.voice_patterns[-window_size:]
        if not recent_patterns:
            return 0.0
        
        successful = sum(1 for p in recent_patterns if p.success and p.confidence > 0.7)
        return successful / len(recent_patterns)
    
    async def _retrain_models(self):
        """Retrain ML models with accumulated data"""
        logger.info("Retraining ML models with new data...")
        
        all_patterns = []
        for profile in self.user_profiles.values():
            all_patterns.extend(profile.voice_patterns)
        
        if len(all_patterns) < 50:
            return  # Not enough data
        
        # Train mistake corrector
        await self._train_mistake_corrector(all_patterns)
        
        # Train command classifier
        await self._train_command_classifier(all_patterns)
        
        # Update command clusters
        await self._update_command_clusters()
        
        # Train anomaly detector
        await self._train_anomaly_detector(all_patterns)
        
        logger.info("Model retraining complete")
    
    async def _train_mistake_corrector(self, patterns: List[VoicePattern]):
        """Train model to correct common mistakes"""
        mistake_pairs = []
        
        for pattern in patterns:
            if pattern.corrected_text and pattern.corrected_text != pattern.recognized_text:
                mistake_pairs.append({
                    'recognized': pattern.recognized_text,
                    'corrected': pattern.corrected_text,
                    'confidence': pattern.confidence,
                    'features': pattern.audio_features
                })
        
        if len(mistake_pairs) < 10:
            return
        
        # Use Anthropic to analyze mistake patterns
        prompt = f"""Analyze these voice recognition mistakes and identify patterns:

{json.dumps(mistake_pairs[:20], indent=2)}

Identify:
1. Common recognition errors
2. Phonetic confusion patterns
3. Context-dependent mistakes
4. Suggestions for improvement

Provide a structured analysis."""

        message = await asyncio.to_thread(
            self.anthropic.messages.create,
            model="claude-3-haiku-20240307",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        
        analysis = message.content[0].text
        
        # Store analysis
        with open(os.path.join(self.model_dir, "mistake_analysis.txt"), 'w') as f:
            f.write(analysis)
        
        logger.info("Mistake patterns analyzed and stored")
    
    async def _train_command_classifier(self, patterns: List[VoicePattern]):
        """Train command intent classifier"""
        commands = []
        intents = []
        
        for pattern in patterns:
            command = pattern.corrected_text or pattern.recognized_text
            commands.append(command)
            
            # Simple intent detection (can be enhanced)
            if any(word in command.lower() for word in ['play', 'open', 'start', 'launch']):
                intents.append('action')
            elif any(word in command.lower() for word in ['what', 'when', 'where', 'who', 'why', 'how']):
                intents.append('question')
            elif any(word in command.lower() for word in ['tell', 'show', 'find', 'search']):
                intents.append('information')
            else:
                intents.append('general')
        
        if len(set(intents)) < 2:
            return  # Not enough variety
        
        # Train classifier
        X = self.tfidf_vectorizer.fit_transform(commands)
        self.command_classifier = RandomForestClassifier(n_estimators=100, random_state=42)
        self.command_classifier.fit(X, intents)
        
        logger.info("Command classifier trained")
    
    async def _update_command_clusters(self):
        """Cluster similar commands together"""
        for user_id, profile in self.user_profiles.items():
            commands = list(profile.preferred_commands.keys())
            if len(commands) < 5:
                continue
            
            # Vectorize commands
            X = self.tfidf_vectorizer.transform(commands)
            
            # Cluster using DBSCAN
            clustering = DBSCAN(eps=0.3, min_samples=2, metric='cosine')
            clusters = clustering.fit_predict(X)
            
            # Group commands by cluster
            profile.command_clusters = defaultdict(list)
            for cmd, cluster in zip(commands, clusters):
                if cluster != -1:  # Not noise
                    profile.command_clusters[f"cluster_{cluster}"].append(cmd)
            
            logger.info(f"Updated command clusters for user {user_id}")
    
    async def _train_anomaly_detector(self, patterns: List[VoicePattern]):
        """Train anomaly detector for unusual patterns"""
        if len(patterns) < 50:
            return
        
        # Extract features
        features = []
        for pattern in patterns:
            if pattern.audio_features:
                feature_vector = [
                    pattern.confidence,
                    pattern.audio_features.get('energy', 0),
                    pattern.audio_features.get('pitch_mean', 0),
                    pattern.audio_features.get('speech_rate', 0),
                    pattern.audio_features.get('snr_estimate', 0)
                ]
                features.append(feature_vector)
        
        if len(features) < 20:
            return
        
        # Scale features
        features_scaled = self.audio_scaler.fit_transform(features)
        
        # Train isolation forest
        self.anomaly_detector = IsolationForest(contamination='auto', random_state=42)
        self.anomaly_detector.fit(features_scaled)
        
        logger.info("Anomaly detector trained")
    
    def predict_correction(self, recognized_text: str, confidence: float, 
                          audio_features: Optional[Dict] = None) -> Optional[str]:
        """Predict the correct text based on learned patterns"""
        profile = self.user_profiles.get(self.current_user)
        if not profile:
            return None
        
        # Check common mistakes first
        if recognized_text in profile.common_mistakes:
            return profile.common_mistakes[recognized_text]
        
        # Find similar commands
        similar_commands = self._find_similar_commands(recognized_text, profile)
        if similar_commands and similar_commands[0][1] > 0.8:  # High similarity
            return similar_commands[0][0]
        
        return None
    
    def _find_similar_commands(self, command: str, profile: UserVoiceProfile, top_k: int = 3) -> List[Tuple[str, float]]:
        """Find similar commands from user history"""
        if not profile.preferred_commands:
            return []
        
        try:
            # Vectorize the command
            command_vec = self.tfidf_vectorizer.transform([command])
            
            # Vectorize historical commands
            historical_commands = list(profile.preferred_commands.keys())
            historical_vecs = self.tfidf_vectorizer.transform(historical_commands)
            
            # Calculate similarities
            similarities = cosine_similarity(command_vec, historical_vecs)[0]
            
            # Get top similar commands
            top_indices = np.argsort(similarities)[-top_k:][::-1]
            
            results = []
            for idx in top_indices:
                if similarities[idx] > 0.5:  # Minimum similarity threshold
                    results.append((historical_commands[idx], similarities[idx]))
            
            return results
            
        except Exception as e:
            logger.error(f"Error finding similar commands: {e}")
            return []
    
    def is_anomaly(self, confidence: float, audio_features: Dict) -> bool:
        """Check if the current pattern is anomalous"""
        if not self.anomaly_detector or not audio_features:
            return False
        
        try:
            feature_vector = [[
                confidence,
                audio_features.get('energy', 0),
                audio_features.get('pitch_mean', 0),
                audio_features.get('speech_rate', 0),
                audio_features.get('snr_estimate', 0)
            ]]
            
            features_scaled = self.audio_scaler.transform(feature_vector)
            prediction = self.anomaly_detector.predict(features_scaled)
            
            return prediction[0] == -1  # -1 indicates anomaly
            
        except Exception as e:
            logger.error(f"Error detecting anomaly: {e}")
            return False
    
    def get_user_insights(self, user_id: Optional[str] = None) -> Dict:
        """Get insights about user's voice patterns"""
        user_id = user_id or self.current_user
        profile = self.user_profiles.get(user_id)
        
        if not profile:
            return {"error": "No profile found"}
        
        # Calculate insights
        total_interactions = len(profile.voice_patterns)
        recent_accuracy = self._calculate_recent_accuracy(profile)
        
        # Most common commands
        top_commands = sorted(
            profile.preferred_commands.items(),
            key=lambda x: x[1],
            reverse=True
        )[:10]
        
        # Common mistakes
        mistake_patterns = list(profile.common_mistakes.items())[:10]
        
        # Accuracy trend
        accuracy_trend = []
        if profile.accuracy_history:
            recent_history = profile.accuracy_history[-30:]
            for timestamp, accuracy in recent_history:
                accuracy_trend.append({
                    'date': timestamp.isoformat(),
                    'accuracy': accuracy
                })
        
        # Voice characteristics
        avg_features = {}
        if profile.voice_patterns:
            recent_patterns = profile.voice_patterns[-50:]
            for pattern in recent_patterns:
                for feature, value in pattern.audio_features.items():
                    if feature not in avg_features:
                        avg_features[feature] = []
                    avg_features[feature].append(value)
            
            # Calculate averages
            for feature in avg_features:
                avg_features[feature] = np.mean(avg_features[feature])
        
        return {
            'user_id': user_id,
            'total_interactions': total_interactions,
            'recent_accuracy': recent_accuracy,
            'top_commands': top_commands,
            'common_mistakes': mistake_patterns,
            'accuracy_trend': accuracy_trend,
            'voice_characteristics': avg_features,
            'command_clusters': dict(profile.command_clusters)
        }
    
    async def generate_personalized_tips(self, user_id: Optional[str] = None) -> str:
        """Generate personalized tips using Anthropic"""
        insights = self.get_user_insights(user_id)
        
        if 'error' in insights:
            return "No data available for personalized tips."
        
        # Create prompt for Anthropic
        prompt = f"""Based on this user's voice interaction data, provide personalized tips to improve their experience:

User Insights:
- Total interactions: {insights['total_interactions']}
- Recent accuracy: {insights['recent_accuracy']:.2%}
- Most used commands: {insights['top_commands'][:5]}
- Common recognition errors: {insights['common_mistakes'][:5]}
- Voice characteristics: 
  - Average pitch: {insights['voice_characteristics'].get('pitch_mean', 'N/A')}
  - Speech rate: {insights['voice_characteristics'].get('speech_rate', 'N/A')}
  - SNR estimate: {insights['voice_characteristics'].get('snr_estimate', 'N/A')}

Provide 3-5 specific, actionable tips to help this user improve their voice interaction accuracy."""

        message = await asyncio.to_thread(
            self.anthropic.messages.create,
            model="claude-3-haiku-20240307",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return message.content[0].text
    
    def export_user_model(self, user_id: Optional[str] = None) -> str:
        """Export user's voice model for backup or sharing"""
        user_id = user_id or self.current_user
        profile = self.user_profiles.get(user_id)
        
        if not profile:
            return None
        
        export_path = os.path.join(self.model_dir, f"user_model_{user_id}.pkl")
        
        export_data = {
            'profile': profile,
            'common_mistakes': profile.common_mistakes,
            'command_clusters': profile.command_clusters,
            'acoustic_profile': profile.acoustic_profile
        }
        
        with open(export_path, 'wb') as f:
            pickle.dump(export_data, f)
        
        return export_path
    
    def import_user_model(self, model_path: str) -> bool:
        """Import a user's voice model"""
        try:
            with open(model_path, 'rb') as f:
                export_data = pickle.load(f)
            
            profile = export_data['profile']
            self.user_profiles[profile.user_id] = profile
            
            logger.info(f"Imported model for user {profile.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error importing model: {e}")
            return False
    
    def _serialize_profile(self, profile: UserVoiceProfile) -> Dict:
        """Serialize profile for JSON storage"""
        return {
            'user_id': profile.user_id,
            'created_at': profile.created_at.isoformat(),
            'voice_patterns': [
                {
                    'pattern_id': p.pattern_id,
                    'user_text': p.user_text,
                    'recognized_text': p.recognized_text,
                    'confidence': p.confidence,
                    'audio_features': p.audio_features,
                    'timestamp': p.timestamp.isoformat(),
                    'corrected_text': p.corrected_text,
                    'success': p.success,
                    'context': p.context
                }
                for p in profile.voice_patterns[-100:]  # Keep last 100
            ],
            'acoustic_profile': profile.acoustic_profile,
            'common_mistakes': profile.common_mistakes,
            'command_clusters': dict(profile.command_clusters),
            'accuracy_history': [
                (ts.isoformat(), acc) for ts, acc in profile.accuracy_history[-50:]
            ],
            'preferred_commands': dict(sorted(
                profile.preferred_commands.items(),
                key=lambda x: x[1],
                reverse=True
            )[:50])  # Keep top 50
        }
    
    def _reconstruct_profile(self, data: Dict) -> UserVoiceProfile:
        """Reconstruct profile from JSON data"""
        profile = UserVoiceProfile(
            user_id=data['user_id'],
            created_at=datetime.fromisoformat(data['created_at']),
            voice_patterns=[],
            acoustic_profile=data.get('acoustic_profile', {}),
            common_mistakes=data.get('common_mistakes', {}),
            command_clusters=data.get('command_clusters', {}),
            accuracy_history=[],
            preferred_commands=data.get('preferred_commands', {})
        )
        
        # Reconstruct patterns
        for pattern_data in data.get('voice_patterns', []):
            pattern = VoicePattern(
                pattern_id=pattern_data['pattern_id'],
                user_text=pattern_data['user_text'],
                recognized_text=pattern_data['recognized_text'],
                confidence=pattern_data['confidence'],
                audio_features=pattern_data['audio_features'],
                timestamp=datetime.fromisoformat(pattern_data['timestamp']),
                corrected_text=pattern_data.get('corrected_text'),
                success=pattern_data.get('success', True),
                context=pattern_data.get('context')
            )
            profile.voice_patterns.append(pattern)
        
        # Reconstruct accuracy history
        for ts_str, acc in data.get('accuracy_history', []):
            profile.accuracy_history.append((datetime.fromisoformat(ts_str), acc))
        
        return profile

# Integration example
async def demo_ml_training():
    """Demonstrate ML training system"""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return
    trainer = VoiceMLTrainer(api_key)
    
    # Simulate some interactions
    interactions = [
        ("play some music", "play sum muzik", 0.6, True, "play some music"),
        ("what's the weather", "what's the weather", 0.9, True, None),
        ("open spotify", "open spot if i", 0.5, True, "open spotify"),
        ("set a timer for 5 minutes", "set a timer for 5 minutes", 0.95, True, None),
        ("turn on the lights", "turn on the lights", 0.85, True, None)
    ]
    
    print("Training ML system with sample interactions...")
    
    for intended, recognized, confidence, success, corrected in interactions:
        await trainer.learn_from_interaction(
            recognized_text=recognized,
            confidence=confidence,
            audio_data=np.random.randn(16000),  # Dummy audio
            corrected_text=corrected,
            success=success
        )
    
    # Get insights
    insights = trainer.get_user_insights()
    print("\nUser Insights:")
    print(f"Total interactions: {insights['total_interactions']}")
    print(f"Recent accuracy: {insights['recent_accuracy']:.2%}")
    print(f"Top commands: {insights['top_commands']}")
    
    # Generate tips
    tips = await trainer.generate_personalized_tips()
    print("\nPersonalized Tips:")
    print(tips)
    
    # Test prediction
    test_command = "play sum musik"
    predicted = trainer.predict_correction(test_command, 0.6)
    print(f"\nPrediction for '{test_command}': {predicted}")

if __name__ == "__main__":
    asyncio.run(demo_ml_training())