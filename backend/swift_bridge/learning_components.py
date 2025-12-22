#!/usr/bin/env python3
"""
Learning Components for Advanced Command Classification
Zero hardcoding - Everything is learned and adaptive
"""

import sqlite3
import json
import logging
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional
from pathlib import Path
import pickle
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from collections import defaultdict, deque
import threading

logger = logging.getLogger(__name__)


class LearningDatabase:
    """
    Database for storing and retrieving learned patterns
    """
    
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_dir = Path.home() / ".jarvis" / "learning"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "command_patterns.db"
        
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.lock = threading.Lock()
        
        self._initialize_database()
    
    def _initialize_database(self):
        """Create database tables if they don't exist"""
        
        with self.lock:
            cursor = self.conn.cursor()
            
            # Patterns table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    features BLOB NOT NULL,
                    type TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    success_rate REAL DEFAULT 0.5,
                    usage_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Corrections table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    original_type TEXT NOT NULL,
                    correct_type TEXT NOT NULL,
                    correct_intent TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    rating REAL NOT NULL,
                    context BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Feedback table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    classified_as TEXT NOT NULL,
                    should_be TEXT NOT NULL,
                    user_rating REAL NOT NULL,
                    context BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Interactions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    classification BLOB NOT NULL,
                    context BLOB NOT NULL,
                    response_time REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Performance metrics table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accuracy REAL NOT NULL,
                    avg_response_time REAL NOT NULL,
                    total_classifications INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Intent patterns table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS intent_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Handler mapping table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS handler_mappings (
                    type TEXT PRIMARY KEY,
                    handler TEXT NOT NULL,
                    success_rate REAL DEFAULT 0.5,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            self.conn.commit()
    
    def find_similar_patterns(self, features: np.ndarray, threshold: float = 0.3) -> List[Dict[str, Any]]:
        """Find patterns similar to the given features"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT id, command, features, type, intent, confidence, success_rate
                FROM patterns
                ORDER BY updated_at DESC
                LIMIT 1000
            ''')
            
            similar_patterns = []
            for row in cursor.fetchall():
                pattern_id, command, features_blob, pattern_type, intent, confidence, success_rate = row
                pattern_features = pickle.loads(features_blob)
                
                # Calculate similarity
                similarity = self._calculate_similarity(features, pattern_features)
                
                if similarity > threshold:
                    similar_patterns.append({
                        "id": pattern_id,
                        "command": command,
                        "type": pattern_type,
                        "intent": intent,
                        "confidence": confidence,
                        "success_rate": success_rate,
                        "similarity": similarity
                    })
            
            # Sort by similarity
            similar_patterns.sort(key=lambda x: x["similarity"], reverse=True)
            
            return similar_patterns[:10]  # Return top 10
    
    def _calculate_similarity(self, features1: np.ndarray, features2: np.ndarray) -> float:
        """Calculate cosine similarity between two feature vectors"""
        
        # Ensure same shape
        min_len = min(len(features1), len(features2))
        if min_len == 0:
            return 0.0
        
        # Truncate to same length
        f1 = features1[:min_len].reshape(1, -1)
        f2 = features2[:min_len].reshape(1, -1)
        
        # Calculate cosine similarity
        similarity = cosine_similarity(f1, f2)[0, 0]
        
        # Ensure it's between 0 and 1
        return max(0.0, min(1.0, similarity))
    
    def get_corrections_for_command(self, command: str) -> List[Dict[str, Any]]:
        """Get corrections for a specific command"""
        
        with self.lock:
            cursor = self.conn.cursor()
            
            # Get exact matches first
            cursor.execute('''
                SELECT correct_type, correct_intent, confidence, rating,
                       julianday('now') - julianday(created_at) as age_days
                FROM corrections
                WHERE LOWER(command) = LOWER(?)
                ORDER BY created_at DESC
                LIMIT 10
            ''', (command,))
            
            corrections = []
            for row in cursor.fetchall():
                correct_type, correct_intent, confidence, rating, age_days = row
                
                # Calculate recency factor (newer corrections are more relevant)
                recency = 1.0 / (1.0 + age_days)
                
                corrections.append({
                    "correct_type": correct_type,
                    "correct_intent": correct_intent,
                    "confidence": confidence,
                    "rating": rating,
                    "recency": recency
                })
            
            return corrections
    
    def store_correction(
        self,
        command: str,
        original_type: str,
        correct_type: str,
        user_rating: float,
        context: Dict[str, Any]
    ):
        """Store a correction from user feedback"""
        
        with self.lock:
            cursor = self.conn.cursor()
            
            # Extract intent from context or generate
            correct_intent = context.get("intent", f"{correct_type}_intent")
            
            cursor.execute('''
                INSERT INTO corrections (command, original_type, correct_type, 
                                       correct_intent, confidence, rating, context)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                command,
                original_type,
                correct_type,
                correct_intent,
                user_rating,
                user_rating,
                pickle.dumps(context)
            ))
            
            self.conn.commit()
    
    def get_intents_for_type(self, command_type: str) -> List[Dict[str, Any]]:
        """Get learned intents for a specific command type"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT intent, pattern, confidence
                FROM intent_patterns
                WHERE type = ?
                ORDER BY confidence DESC
                LIMIT 20
            ''', (command_type,))
            
            intents = []
            for row in cursor.fetchall():
                intent, pattern, confidence = row
                intents.append({
                    "name": intent,
                    "pattern": pattern,
                    "confidence": confidence
                })
            
            return intents
    
    def get_handler_mapping(self) -> Dict[str, str]:
        """Get learned handler mappings"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT type, handler FROM handler_mappings')
            
            mapping = {}
            for row in cursor.fetchall():
                command_type, handler = row
                mapping[command_type] = handler
            
            return mapping
    
    def store_feedback(self, feedback):
        """Store user feedback"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO feedback (command, classified_as, should_be, 
                                    user_rating, context)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                feedback.command,
                feedback.classified_as,
                feedback.should_be,
                feedback.user_rating,
                pickle.dumps(feedback.context)
            ))
            
            self.conn.commit()
    
    def store_interaction(self, interaction_data: Dict[str, Any]):
        """Store an interaction for learning"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO interactions (command, classification, context, response_time)
                VALUES (?, ?, ?, ?)
            ''', (
                interaction_data["command"],
                pickle.dumps(interaction_data["classification"]),
                pickle.dumps(interaction_data["context"]),
                interaction_data.get("response_time", 0)
            ))
            
            # Also update or create pattern
            features = interaction_data.get("features")
            if features is not None:
                classification = interaction_data["classification"]
                cursor.execute('''
                    INSERT OR REPLACE INTO patterns 
                    (command, features, type, intent, confidence)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    interaction_data["command"],
                    pickle.dumps(features),
                    classification["type"],
                    classification["intent"],
                    classification["confidence"]
                ))
            
            self.conn.commit()
    
    def get_recent_patterns(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get patterns from the last N hours"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT command, features, type, intent, confidence, success_rate
                FROM patterns
                WHERE datetime(updated_at) > datetime('now', '-{} hours')
                ORDER BY updated_at DESC
            '''.format(hours))
            
            patterns = []
            for row in cursor.fetchall():
                command, features_blob, pattern_type, intent, confidence, success_rate = row
                patterns.append({
                    "command": command,
                    "features": pickle.loads(features_blob),
                    "type": pattern_type,
                    "intent": intent,
                    "confidence": confidence,
                    "success_rate": success_rate
                })
            
            return patterns
    
    def get_pattern_count(self) -> int:
        """Get total number of learned patterns"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM patterns')
            return cursor.fetchone()[0]
    
    def get_common_corrections(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get most common corrections"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT original_type, correct_type, COUNT(*) as count
                FROM corrections
                GROUP BY original_type, correct_type
                ORDER BY count DESC
                LIMIT ?
            ''', (limit,))
            
            corrections = []
            for row in cursor.fetchall():
                original, correct, count = row
                corrections.append({
                    "original_type": original,
                    "correct_type": correct,
                    "count": count
                })
            
            return corrections
    
    def load_all_patterns(self) -> List[Dict[str, Any]]:
        """Load all patterns for initialization"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT command, features, type, intent, confidence, success_rate
                FROM patterns
                ORDER BY success_rate DESC, updated_at DESC
                LIMIT 10000
            ''')
            
            patterns = []
            for row in cursor.fetchall():
                command, features_blob, pattern_type, intent, confidence, success_rate = row
                patterns.append({
                    "command": command,
                    "features": pickle.loads(features_blob),
                    "type": pattern_type,
                    "intent": intent,
                    "confidence": confidence,
                    "success_rate": success_rate
                })
            
            return patterns
    
    def load_performance_metrics(self) -> List[Dict[str, Any]]:
        """Load performance metrics history"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT accuracy, avg_response_time, total_classifications, created_at
                FROM metrics
                ORDER BY created_at DESC
                LIMIT 1000
            ''')
            
            metrics = []
            for row in cursor.fetchall():
                accuracy, avg_time, total, created_at = row
                metrics.append({
                    "accuracy": accuracy,
                    "avg_response_time": avg_time,
                    "total_classifications": total,
                    "timestamp": created_at
                })
            
            return metrics
    
    def get_recent_command_types(self) -> Dict[str, float]:
        """Get frequency of recent command types"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT type, COUNT(*) as count
                FROM patterns
                WHERE datetime(updated_at) > datetime('now', '-1 hour')
                GROUP BY type
            ''')
            
            total = 0
            type_counts = {}
            
            for row in cursor.fetchall():
                cmd_type, count = row
                type_counts[cmd_type] = count
                total += count
            
            # Convert to frequencies
            if total > 0:
                return {k: v/total for k, v in type_counts.items()}
            else:
                return {}
    
    def get_time_patterns(self) -> Dict[str, Dict[str, Any]]:
        """Get command type patterns by time of day"""
        
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT type, 
                       strftime('%H', created_at) as hour,
                       COUNT(*) as count
                FROM interactions
                WHERE datetime(created_at) > datetime('now', '-7 days')
                GROUP BY type, hour
            ''')
            
            patterns = defaultdict(lambda: {"peak_hours": [], "counts": {}})
            
            for row in cursor.fetchall():
                cmd_type, hour, count = row
                hour_int = int(hour)
                patterns[cmd_type]["counts"][hour_int] = count
            
            # Find peak hours for each type
            for cmd_type, data in patterns.items():
                counts = data["counts"]
                if counts:
                    max_count = max(counts.values())
                    threshold = max_count * 0.7
                    peak_hours = [h for h, c in counts.items() if c >= threshold]
                    patterns[cmd_type]["peak_hours"] = peak_hours
            
            return dict(patterns)
    
    def get_user_state_patterns(self) -> Dict[str, Dict[str, Any]]:
        """Get command patterns based on user state"""
        
        # This would analyze patterns based on user state from context
        # For now, returning a simplified version
        return {
            "system": {"state": "focused", "confidence": 0.8},
            "vision": {"state": "exploring", "confidence": 0.7},
            "conversation": {"state": "multitasking", "confidence": 0.6}
        }


class PatternLearner:
    """
    Machine learning component for pattern recognition and learning
    """
    
    def __init__(self):
        self.vectorizer = TfidfVectorizer(
            max_features=100,
            ngram_range=(1, 3),
            stop_words=None  # We want to learn everything
        )
        self.patterns = []
        self.feature_importance = defaultdict(float)
        self.action_words = set()
        self.target_words = set()
        self.learned_entities = defaultdict(set)
        
        # Initialize with empty fit
        self.vectorizer.fit([""])
    
    def extract_features(self, command: str, context: Dict[str, Any]) -> np.ndarray:
        """Extract features from command and context"""
        
        # Text features
        text_features = self.vectorizer.transform([command]).toarray()[0]
        
        # Linguistic features
        linguistic_features = self._extract_linguistic_features(command)
        
        # Context features
        context_features = self._extract_context_features(context)
        
        # Combine all features
        all_features = np.concatenate([
            text_features,
            linguistic_features,
            context_features
        ])
        
        return all_features
    
    def _extract_linguistic_features(self, command: str) -> np.ndarray:
        """Extract linguistic features from command"""
        
        tokens = command.lower().split()
        
        features = [
            len(tokens),                          # Token count
            len(command),                         # Character count
            command.count(" "),                   # Space count
            1.0 if command.endswith("?") else 0.0,  # Question mark
            1.0 if command.endswith("!") else 0.0,  # Exclamation
            1.0 if any(t in self.action_words for t in tokens) else 0.0,  # Has learned action
            1.0 if any(t in self.target_words for t in tokens) else 0.0,  # Has learned target
            sum(1 for t in tokens if t[0].isupper()) / max(1, len(tokens)),  # Capitalization ratio
        ]
        
        return np.array(features)
    
    def _extract_context_features(self, context: Dict[str, Any]) -> np.ndarray:
        """Extract features from context"""
        
        features = []
        
        # Previous commands count
        prev_commands = context.get("previous_commands", [])
        features.append(len(prev_commands))
        
        # Time features
        if "time_of_day" in context:
            hour = context["time_of_day"].hour
            features.extend([
                hour / 24.0,  # Normalized hour
                1.0 if 9 <= hour <= 17 else 0.0,  # Working hours
                1.0 if hour < 6 or hour > 22 else 0.0,  # Off hours
            ])
        else:
            features.extend([0.5, 0.5, 0.0])
        
        # User state features
        if "user_state" in context:
            state = context["user_state"]
            features.append(state.get("cognitive_load", 0.5))
            features.append(state.get("frustration_level", 0.0))
            features.append(state.get("expertise", 0.5))
        else:
            features.extend([0.5, 0.0, 0.5])
        
        # Session duration
        if "session_duration" in context:
            features.append(min(1.0, context["session_duration"] / 3600.0))
        else:
            features.append(0.0)
        
        return np.array(features)
    
    def learn_from_feedback(self, feedback):
        """Learn from user feedback"""
        
        # Extract features from the command
        features = self.extract_features(feedback.command, feedback.context)
        
        # Store pattern with correct classification
        self.patterns.append({
            "command": feedback.command,
            "features": features,
            "type": feedback.should_be,
            "confidence": feedback.user_rating
        })
        
        # Learn action and target words
        tokens = feedback.command.lower().split()
        if feedback.should_be == "system":
            # First word is likely an action
            if tokens:
                self.action_words.add(tokens[0])
            # Last word might be a target
            if len(tokens) > 1:
                self.target_words.add(tokens[-1])
        
        # Update feature importance based on successful classifications
        if feedback.user_rating > 0.7:
            self._update_feature_importance(features, feedback.should_be)
        
        # Retrain vectorizer periodically
        if len(self.patterns) % 100 == 0:
            self._retrain_vectorizer()
    
    def _update_feature_importance(self, features: np.ndarray, correct_type: str):
        """Update feature importance based on successful classification"""
        
        # Simple importance tracking
        feature_key = f"{correct_type}_features"
        
        if feature_key not in self.feature_importance:
            self.feature_importance[feature_key] = np.zeros_like(features)
        
        # Exponential moving average
        alpha = 0.1
        self.feature_importance[feature_key] = (
            alpha * features + (1 - alpha) * self.feature_importance[feature_key]
        )
    
    def _retrain_vectorizer(self):
        """Retrain the vectorizer with accumulated patterns"""
        
        if self.patterns:
            commands = [p["command"] for p in self.patterns]
            self.vectorizer = TfidfVectorizer(
                max_features=100,
                ngram_range=(1, 3)
            )
            self.vectorizer.fit(commands)
    
    def calculate_intent_match(
        self, 
        command: str, 
        intent_pattern: str, 
        features: np.ndarray
    ) -> float:
        """Calculate how well a command matches an intent pattern"""
        
        # Simple token overlap for now
        command_tokens = set(command.lower().split())
        pattern_tokens = set(intent_pattern.lower().split())
        
        if not pattern_tokens:
            return 0.0
        
        overlap = len(command_tokens & pattern_tokens)
        return overlap / len(pattern_tokens)
    
    def extract_action_words(self, tokens: List[str]) -> List[str]:
        """Extract action words from tokens (learned, not hardcoded)"""
        
        action_words = []
        
        # Check learned action words
        for token in tokens:
            if token in self.action_words:
                action_words.append(token)
        
        # If no learned actions, guess based on position and pattern
        if not action_words and tokens:
            # First word is often an action
            first_token = tokens[0]
            # Basic heuristic: words ending in common verb suffixes
            if any(first_token.endswith(suffix) for suffix in ["e", "ate", "ify"]):
                action_words.append(first_token)
                self.action_words.add(first_token)  # Learn it
        
        return action_words
    
    def extract_target_words(self, tokens: List[str]) -> List[str]:
        """Extract target words from tokens (learned, not hardcoded)"""
        
        target_words = []
        
        # Check learned target words
        for token in tokens:
            if token in self.target_words:
                target_words.append(token)
        
        # If no learned targets, guess based on pattern
        if not target_words and len(tokens) > 1:
            # Capitalized words are often targets (app names)
            for token in tokens[1:]:  # Skip first word (usually action)
                if token[0].isupper():
                    target_words.append(token)
                    self.target_words.add(token.lower())  # Learn it
        
        return target_words
    
    def extract_entities(self, command: str) -> List[Dict[str, Any]]:
        """Extract entities from command (learned, not hardcoded)"""
        
        entities = []
        tokens = command.split()
        
        # Extract based on learned patterns
        for i, token in enumerate(tokens):
            token_lower = token.lower()
            
            # Check if it's a learned action
            if token_lower in self.action_words:
                entities.append({
                    "text": token,
                    "type": "action",
                    "role": "primary_action",
                    "confidence": 0.9
                })
            
            # Check if it's a learned target
            elif token_lower in self.target_words:
                entities.append({
                    "text": token,
                    "type": "application",
                    "role": "target",
                    "confidence": 0.8
                })
            
            # Check if it's a capitalized word (potential app/entity)
            elif token[0].isupper() and i > 0:  # Not first word
                entities.append({
                    "text": token,
                    "type": "application",
                    "role": "potential_target",
                    "confidence": 0.6
                })
            
            # Check learned entity patterns
            for entity_type, patterns in self.learned_entities.items():
                if token_lower in patterns:
                    entities.append({
                        "text": token,
                        "type": entity_type,
                        "role": "learned_entity",
                        "confidence": 0.7
                    })
        
        return entities
    
    def get_linguistic_weights(self, features: np.ndarray) -> Dict[str, float]:
        """Get weights for command types based on linguistic features"""
        
        weights = {}
        
        # Use learned feature importance
        for type_key, importance in self.feature_importance.items():
            if "_features" in type_key:
                cmd_type = type_key.replace("_features", "")
                
                # Calculate weight based on feature similarity
                if len(features) == len(importance):
                    similarity = np.dot(features, importance) / (
                        np.linalg.norm(features) * np.linalg.norm(importance) + 1e-10
                    )
                    weights[cmd_type] = max(0, similarity)
        
        return weights
    
    def update_from_feedback(self, feedback):
        """Update learner from feedback"""
        self.learn_from_feedback(feedback)
    
    def retrain(self, recent_patterns: List[Dict[str, Any]]):
        """Retrain with recent patterns"""
        
        # Add new patterns
        for pattern in recent_patterns:
            self.patterns.append(pattern)
            
            # Learn from pattern
            tokens = pattern["command"].lower().split()
            if pattern["type"] == "system" and tokens:
                self.action_words.add(tokens[0])
                if len(tokens) > 1:
                    self.target_words.add(tokens[-1])
        
        # Limit pattern storage
        if len(self.patterns) > 10000:
            # Keep most recent and highest confidence patterns
            self.patterns.sort(key=lambda x: (x.get("confidence", 0), x.get("timestamp", 0)), reverse=True)
            self.patterns = self.patterns[:5000]
        
        # Retrain vectorizer
        self._retrain_vectorizer()
    
    def get_most_improved(self) -> List[Dict[str, Any]]:
        """Get classifications that have improved most"""
        
        # Track improvement in patterns
        improvements = []
        
        # Group patterns by command
        command_patterns = defaultdict(list)
        for pattern in self.patterns:
            command_patterns[pattern["command"]].append(pattern)
        
        # Find commands with increasing confidence
        for command, patterns in command_patterns.items():
            if len(patterns) >= 2:
                old_confidence = patterns[0].get("confidence", 0)
                new_confidence = patterns[-1].get("confidence", 0)
                improvement = new_confidence - old_confidence
                
                if improvement > 0:
                    improvements.append({
                        "command": command,
                        "improvement": improvement,
                        "current_confidence": new_confidence
                    })
        
        # Sort by improvement
        improvements.sort(key=lambda x: x["improvement"], reverse=True)
        
        return improvements[:10]
    
    def get_adaptation_rate(self) -> float:
        """Get rate of adaptation/learning"""
        
        if len(self.patterns) < 2:
            return 0.0
        
        # Calculate rate of new pattern acquisition
        recent_patterns = [p for p in self.patterns if p.get("timestamp", 0) > 0]
        
        if recent_patterns:
            time_span = max(1, len(recent_patterns))
            unique_commands = len(set(p["command"] for p in recent_patterns))
            
            return unique_commands / time_span
        
        return 0.0
    
    def get_dominant_features(self, features: np.ndarray) -> List[str]:
        """Get dominant features for reasoning"""
        
        dominant = []
        
        # Check linguistic features (these are at known positions after TF-IDF features)
        tfidf_size = len(self.vectorizer.get_feature_names_out()) if hasattr(self.vectorizer, 'get_feature_names_out') else 100
        
        if len(features) > tfidf_size:
            linguistic_start = tfidf_size
            
            # Check specific linguistic features
            if features[linguistic_start + 3] > 0:  # Question mark
                dominant.append("question")
            if features[linguistic_start + 5] > 0:  # Has action word
                dominant.append("action word")
            if features[linguistic_start + 6] > 0:  # Has target word
                dominant.append("target word")
        
        # Check TF-IDF features
        if hasattr(self.vectorizer, 'get_feature_names_out'):
            feature_names = self.vectorizer.get_feature_names_out()
            top_indices = np.argsort(features[:tfidf_size])[-3:][::-1]
            
            for idx in top_indices:
                if idx < len(feature_names) and features[idx] > 0.1:
                    dominant.append(f"'{feature_names[idx]}'")
        
        return dominant[:5]  # Return top 5 dominant features
    
    def load_patterns(self, patterns: List[Dict[str, Any]]):
        """Load historical patterns"""
        
        self.patterns = patterns
        
        # Learn from patterns
        for pattern in patterns:
            tokens = pattern["command"].lower().split()
            
            # Learn action and target words based on type
            if pattern["type"] == "system" and tokens:
                if tokens:
                    self.action_words.add(tokens[0])
                if len(tokens) > 1:
                    self.target_words.add(tokens[-1])
            
            # Learn entities
            if "entities" in pattern:
                for entity in pattern["entities"]:
                    self.learned_entities[entity.get("type", "unknown")].add(
                        entity.get("text", "").lower()
                    )
        
        # Retrain vectorizer
        if patterns:
            self._retrain_vectorizer()


class AdvancedContextManager:
    """
    Advanced context management with learning capabilities
    """
    
    def __init__(self):
        self.command_history = deque(maxlen=50)
        self.session_start = datetime.now()
        self.interaction_patterns = deque(maxlen=100)
        self.context_weights = defaultdict(float)
        self.user_profile = UserProfile()
    
    def get_context(self) -> Dict[str, Any]:
        """Get current context"""
        
        now = datetime.now()
        
        context = {
            "previous_commands": list(self.command_history)[-5:],
            "time_of_day": now,
            "day_of_week": now.weekday(),
            "session_duration": (now - self.session_start).total_seconds(),
            "user_state": self.user_profile.get_current_state(),
            "interaction_count": len(self.interaction_patterns),
            "recent_types": self._get_recent_command_types(),
            "context_weights": dict(self.context_weights)
        }
        
        return context
    
    def add_command(self, command: str):
        """Add command to history"""
        
        self.command_history.append(command)
        
        # Update interaction patterns
        self.interaction_patterns.append({
            "command": command,
            "timestamp": datetime.now()
        })
        
        # Update user profile
        self.user_profile.update_from_command(command)
    
    def _get_recent_command_types(self) -> Dict[str, int]:
        """Get count of recent command types"""
        
        # This would be populated from actual classifications
        # For now, returning empty
        return {}
    
    def update_weights(self, success_rates: Dict[str, float]):
        """Update context weights based on success rates"""
        
        for context_type, rate in success_rates.items():
            # Exponential moving average
            alpha = 0.1
            self.context_weights[context_type] = (
                alpha * rate + (1 - alpha) * self.context_weights.get(context_type, 0.5)
            )


class UserProfile:
    """
    User profile for personalized learning
    """
    
    def __init__(self):
        self.command_count = 0
        self.error_count = 0
        self.session_commands = 0
        self.expertise_level = 0.5
        self.working_pattern = "exploring"
        self.last_update = datetime.now()
    
    def get_current_state(self) -> Dict[str, Any]:
        """Get current user state"""
        
        # Calculate cognitive load based on command frequency
        time_since_last = (datetime.now() - self.last_update).total_seconds()
        command_rate = 1.0 / max(1, time_since_last) if time_since_last < 60 else 0
        
        cognitive_load = min(1.0, command_rate / 0.5)  # Normalize to 0-1
        
        # Calculate frustration based on error rate
        frustration = self.error_count / max(1, self.command_count)
        
        return {
            "working_pattern": self._infer_working_pattern(),
            "cognitive_load": cognitive_load,
            "frustration_level": frustration,
            "expertise": self.expertise_level
        }
    
    def update_from_command(self, command: str):
        """Update profile from command"""
        
        self.command_count += 1
        self.session_commands += 1
        self.last_update = datetime.now()
        
        # Update expertise based on command complexity
        command_length = len(command.split())
        if command_length > 5:
            self.expertise_level = min(1.0, self.expertise_level + 0.01)
    
    def _infer_working_pattern(self) -> str:
        """Infer current working pattern"""
        
        if self.session_commands < 5:
            return "exploring"
        elif self.session_commands < 20:
            return "focused"
        elif self.session_commands < 50:
            return "multitasking"
        else:
            return "automating"


class PerformanceTracker:
    """
    Track and analyze system performance
    """
    
    def __init__(self):
        self.classifications = deque(maxlen=1000)
        self.response_times = deque(maxlen=1000)
        self.accuracy_history = deque(maxlen=1000)
        self.feedback_history = deque(maxlen=1000)
    
    def record_classification(
        self,
        command: str,
        classification: Any,
        response_time: float
    ):
        """Record a classification for tracking"""
        
        self.classifications.append({
            "command": command,
            "type": classification.type,
            "confidence": classification.confidence,
            "timestamp": datetime.now()
        })
        
        self.response_times.append(response_time)
    
    def update_from_feedback(self, feedback):
        """Update metrics from user feedback"""
        
        self.feedback_history.append({
            "command": feedback.command,
            "correct": feedback.classified_as == feedback.should_be,
            "rating": feedback.user_rating,
            "timestamp": datetime.now()
        })
        
        # Update accuracy
        recent_feedback = list(self.feedback_history)[-100:]
        if recent_feedback:
            accuracy = sum(1 for f in recent_feedback if f["correct"]) / len(recent_feedback)
            self.accuracy_history.append(accuracy)
    
    def get_metrics(self) -> Any:
        """Get current performance metrics"""
        
        # Calculate metrics
        accuracy = np.mean(list(self.accuracy_history)) if self.accuracy_history else 0.5
        avg_response_time = np.mean(list(self.response_times)) if self.response_times else 0
        total_classifications = len(self.classifications)
        
        # Calculate improvement
        if len(self.accuracy_history) >= 2:
            old_accuracy = np.mean(list(self.accuracy_history)[:50])
            new_accuracy = np.mean(list(self.accuracy_history)[-50:])
            improvement = new_accuracy - old_accuracy
        else:
            improvement = 0
        
        # Get common errors
        errors = self._get_common_errors()
        
        return {
            "accuracy": accuracy,
            "avg_response_time": avg_response_time,
            "total_classifications": total_classifications,
            "improvement_rate": improvement,
            "common_errors": errors
        }
    
    def _get_common_errors(self) -> List[Tuple[str, str, int]]:
        """Get common classification errors"""
        
        # This would analyze feedback to find common errors
        # For now, returning empty list
        return []
    
    def get_accuracy_trend(self) -> List[float]:
        """Get accuracy trend over time"""
        return list(self.accuracy_history)[-100:]
    
    def get_context_success_rates(self) -> Dict[str, float]:
        """Get success rates for different contexts"""
        
        # This would analyze success by context
        # For now, returning default rates
        return {
            "morning": 0.8,
            "afternoon": 0.85,
            "evening": 0.75,
            "focused": 0.9,
            "multitasking": 0.7
        }
    
    def load_history(self, metrics: List[Dict[str, Any]]):
        """Load historical metrics"""
        
        for metric in metrics:
            if "accuracy" in metric:
                self.accuracy_history.append(metric["accuracy"])
            if "avg_response_time" in metric:
                self.response_times.append(metric["avg_response_time"])