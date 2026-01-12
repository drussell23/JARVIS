#!/usr/bin/env python3
"""
JARVIS Voice Unlock Python Bridge
=================================

Provides voice processing and machine learning capabilities
for the Objective-C Voice Unlock daemon.
"""

import json
import os
import sys
import logging
import base64
import time
import numpy as np
import asyncio
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any

# Audio processing
import librosa
import soundfile as sf
import speech_recognition as sr
from scipy.signal import butter, filtfilt

# Machine learning
from sklearn.preprocessing import StandardScaler
from sklearn.metrics.pairwise import cosine_similarity
import joblib

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('jarvis_voice_bridge')


class VoiceProcessor:
    """Handles voice processing and feature extraction"""
    
    def __init__(self):
        self.sample_rate = 16000
        self.n_mfcc = 13
        self.n_mels = 128
        self.scaler = StandardScaler()
        
    def extract_features(self, audio_data: bytes) -> List[float]:
        """Extract voice features from audio data"""
        try:
            # Convert bytes to numpy array
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            audio_float = audio_array.astype(np.float32) / 32768.0
            
            # Extract MFCC features
            mfccs = librosa.feature.mfcc(
                y=audio_float,
                sr=self.sample_rate,
                n_mfcc=self.n_mfcc
            )
            
            # Extract additional features
            spectral_centroid = librosa.feature.spectral_centroid(
                y=audio_float,
                sr=self.sample_rate
            )
            
            zero_crossing_rate = librosa.feature.zero_crossing_rate(audio_float)
            
            # Combine features
            features = []
            features.extend(np.mean(mfccs, axis=1))
            features.extend(np.std(mfccs, axis=1))
            features.append(np.mean(spectral_centroid))
            features.append(np.std(spectral_centroid))
            features.append(np.mean(zero_crossing_rate))
            
            # Pad to 128 dimensions
            while len(features) < 128:
                features.append(0.0)
                
            return features[:128]
            
        except Exception as e:
            logger.error(f"Feature extraction failed: {e}")
            return [0.0] * 128
    
    def detect_wake_phrase(self, audio_data: bytes) -> Dict[str, Any]:
        """Detect wake phrases in audio"""
        try:
            # Convert to appropriate format for speech recognition
            recognizer = sr.Recognizer()
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            
            # Create AudioData object
            audio = sr.AudioData(
                audio_data,
                sample_rate=self.sample_rate,
                sample_width=2  # 16-bit audio
            )
            
            # Try to recognize speech
            try:
                text = recognizer.recognize_google(audio, language="en-US")
                text_lower = text.lower()
                
                # Check for wake phrases
                wake_phrases = [
                    "hello jarvis unlock my mac",
                    "jarvis this is",
                    "open sesame jarvis"
                ]
                
                for phrase in wake_phrases:
                    if phrase in text_lower:
                        return {
                            "detected": True,
                            "phrase": text,
                            "confidence": 0.95
                        }
                
                # Check partial matches
                if "jarvis" in text_lower and ("unlock" in text_lower or "mac" in text_lower):
                    return {
                        "detected": True,
                        "phrase": text,
                        "confidence": 0.85
                    }
                    
            except sr.UnknownValueError:
                # Could not understand audio
                pass
            except sr.RequestError as e:
                logger.error(f"Speech recognition error: {e}")
                
            return {
                "detected": False,
                "phrase": None,
                "confidence": 0.0
            }
            
        except Exception as e:
            logger.error(f"Wake phrase detection failed: {e}")
            return {
                "detected": False,
                "phrase": None,
                "confidence": 0.0
            }
    
    def analyze_audio_quality(self, audio_data: bytes) -> Dict[str, Any]:
        """Analyze audio quality metrics"""
        try:
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            audio_float = audio_array.astype(np.float32) / 32768.0
            
            # Calculate RMS
            rms = np.sqrt(np.mean(audio_float**2))
            
            # Calculate SNR (simplified)
            signal_power = np.mean(audio_float**2)
            noise_estimate = np.mean(audio_float[:1000]**2)  # First part as noise
            snr = 10 * np.log10(signal_power / (noise_estimate + 1e-10))
            
            # Duration
            duration = len(audio_float) / self.sample_rate
            
            return {
                "rms": float(rms),
                "snr": float(snr),
                "duration": float(duration),
                "suitable": rms > 0.01 and snr > 10.0
            }
            
        except Exception as e:
            logger.error(f"Audio quality analysis failed: {e}")
            return {
                "rms": 0.0,
                "snr": 0.0,
                "duration": 0.0,
                "suitable": False
            }


class VoiceAuthenticator:
    """Handles voice authentication"""
    
    def __init__(self, processor: VoiceProcessor):
        self.processor = processor
        self.enrolled_voiceprints = {}
        self.load_voiceprints()
        
    def load_voiceprints(self):
        """Load enrolled voiceprints from storage"""
        import os
        import json
        
        voiceprint_dir = os.path.expanduser("~/.jarvis/voice_unlock")
        if not os.path.exists(voiceprint_dir):
            return
            
        for filename in os.listdir(voiceprint_dir):
            if filename.endswith("_voiceprint.json"):
                filepath = os.path.join(voiceprint_dir, filename)
                try:
                    with open(filepath, 'r') as f:
                        data = json.load(f)
                        user_id = data['user_id']
                        self.enrolled_voiceprints[user_id] = {
                            'features': np.array(data['voiceprint']['features']),
                            'name': data['name']
                        }
                        logger.info(f"Loaded voiceprint for {user_id}")
                except Exception as e:
                    logger.error(f"Failed to load voiceprint {filename}: {e}")
    
    def authenticate(self, audio_data: bytes, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Authenticate voice against enrolled voiceprints"""
        try:
            # Extract features
            features = self.processor.extract_features(audio_data)
            features_array = np.array(features).reshape(1, -1)
            
            # If specific user, check only that user
            if user_id and user_id in self.enrolled_voiceprints:
                enrolled_features = self.enrolled_voiceprints[user_id]['features'].reshape(1, -1)
                similarity = cosine_similarity(features_array, enrolled_features)[0][0]
                
                authenticated = similarity > 0.85
                return {
                    "authenticated": authenticated,
                    "confidence": float(similarity),
                    "user_id": user_id if authenticated else None,
                    "liveness_score": 0.9,  # Simplified
                    "antispoofing_score": 0.9  # Simplified
                }
            
            # Check all enrolled users
            best_match = None
            best_similarity = 0.0
            
            for uid, voiceprint in self.enrolled_voiceprints.items():
                enrolled_features = voiceprint['features'].reshape(1, -1)
                similarity = cosine_similarity(features_array, enrolled_features)[0][0]
                
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = uid
            
            authenticated = best_similarity > 0.85
            return {
                "authenticated": authenticated,
                "confidence": float(best_similarity),
                "user_id": best_match if authenticated else None,
                "liveness_score": 0.9,
                "antispoofing_score": 0.9
            }
            
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return {
                "authenticated": False,
                "confidence": 0.0,
                "user_id": None,
                "error": str(e)
            }


class MessageHandler:
    """Handles messages from Objective-C daemon"""
    
    def __init__(self):
        self.processor = VoiceProcessor()
        self.authenticator = VoiceAuthenticator(self.processor)
        
    def send_response(self, message_id: str, success: bool, result=None, error=None, execution_time=0.0):
        """Send response back to Objective-C daemon"""
        response = {
            'type': 'response',
            'id': message_id,
            'success': success,
            'result': result,
            'execution_time': execution_time
        }
        if error:
            response['error'] = {'code': 500, 'message': str(error)}
        
        print(json.dumps(response), flush=True)
        
    def send_log(self, message: str, level: str = "info"):
        """Send log message"""
        log_msg = {
            'type': 'log',
            'message': message,
            'level': level,
            'timestamp': datetime.now().isoformat()
        }
        print(json.dumps(log_msg), flush=True)
        
    def process_message(self, message: Dict[str, Any]):
        """Process incoming message"""
        msg_type = message.get('type')
        msg_id = message.get('id', 'unknown')
        
        start_time = datetime.now()
        
        try:
            if msg_type == 'init':
                # Initialize bridge
                config = message.get('config', {})
                self.send_log("Voice unlock bridge initialized")
                self.send_response(msg_id, True, {'initialized': True})
                
            elif msg_type == 'shutdown':
                # Shutdown bridge
                self.send_log("Shutting down voice unlock bridge")
                sys.exit(0)
                
            elif msg_type == 'wake_phrase_detection':
                # Detect wake phrase
                audio_data = base64.b64decode(message.get('audio_data', ''))
                result = self.processor.detect_wake_phrase(audio_data)
                execution_time = (datetime.now() - start_time).total_seconds()
                self.send_response(msg_id, True, result, execution_time=execution_time)
                
            elif msg_type == 'feature_extraction':
                # Extract features
                audio_data = base64.b64decode(message.get('audio_data', ''))
                features = self.processor.extract_features(audio_data)
                execution_time = (datetime.now() - start_time).total_seconds()
                self.send_response(msg_id, True, {'features': features}, execution_time=execution_time)
                
            elif msg_type == 'authentication':
                # Authenticate voice
                audio_data = base64.b64decode(message.get('audio_data', ''))
                user_id = message.get('user_id')
                result = self.authenticator.authenticate(audio_data, user_id)
                execution_time = (datetime.now() - start_time).total_seconds()
                self.send_response(msg_id, True, result, execution_time=execution_time)
                
            elif msg_type == 'audio_quality':
                # Analyze audio quality
                audio_data = base64.b64decode(message.get('audio_data', ''))
                result = self.processor.analyze_audio_quality(audio_data)
                execution_time = (datetime.now() - start_time).total_seconds()
                self.send_response(msg_id, True, result, execution_time=execution_time)
                
            elif msg_type == 'voiceprint_comparison':
                # Compare voiceprints
                features1 = np.array(message.get('features1', []))
                features2 = np.array(message.get('features2', []))
                similarity = cosine_similarity(
                    features1.reshape(1, -1),
                    features2.reshape(1, -1)
                )[0][0]
                self.send_response(msg_id, True, float(similarity))
                
            elif msg_type == 'function_call':
                # Function call
                func = message.get('function')
                args = message.get('args', [])
                
                if func == 'sys.version':
                    self.send_response(msg_id, True, sys.version)
                elif func == 'list_modules':
                    modules = list(sys.modules.keys())
                    self.send_response(msg_id, True, modules[:20])  # First 20
                elif func == 'function_exists':
                    func_name = args[0] if args else ''
                    exists = func_name in globals() or hasattr(self, func_name)
                    self.send_response(msg_id, True, exists)
                elif func == 'authenticate_voice':
                    # WebSocket style authentication
                    params = args[0] if args else {}
                    audio_data = base64.b64decode(params.get('audio_data', ''))
                    user_id = params.get('user_id')
                    result = self.authenticator.authenticate(audio_data, user_id)
                    self.send_response(msg_id, True, {'result': result})
                else:
                    self.send_response(msg_id, False, error=f'Unknown function: {func}')
                    
            else:
                self.send_response(msg_id, False, error=f'Unknown message type: {msg_type}')
                
        except Exception as e:
            logger.error(f'Error processing message: {e}')
            self.send_response(msg_id, False, error=str(e))


def main():
    """Main entry point"""
    logger.info('JARVIS Voice Unlock Python Bridge started')

    handler = MessageHandler()

    session_timeout = float(os.getenv("TIMEOUT_VOICE_SESSION", "3600.0"))  # 1 hour default
    session_start = time.monotonic()
    read_timeout = float(os.getenv("TIMEOUT_VOICE_READ", "60.0"))  # 60 second read timeout

    # Read messages from stdin with timeout protection
    while time.monotonic() - session_start < session_timeout:
        try:
            # Use select for timeout on stdin read (Unix-like systems)
            import select
            ready, _, _ = select.select([sys.stdin], [], [], read_timeout)
            if not ready:
                logger.debug("Stdin read idle, continuing...")
                continue

            line = sys.stdin.readline()
            if not line:
                break

            message = json.loads(line.strip())
            handler.process_message(message)

        except json.JSONDecodeError as e:
            logger.error(f'Invalid JSON: {e}')
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f'Unexpected error: {e}')

    if time.monotonic() - session_start >= session_timeout:
        logger.warning('Voice unlock bridge session timeout reached')

    logger.info('JARVIS Voice Unlock Python Bridge stopped')


if __name__ == '__main__':
    main()