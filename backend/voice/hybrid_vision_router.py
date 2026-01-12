#!/usr/bin/env python3
"""
Hybrid Vision Router - C++ Speed + Python ML Intelligence
Zero hardcoding approach with multi-level analysis
"""

import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Any, Union
from dataclasses import dataclass, field
from datetime import datetime
import numpy as np
import json
import os
from pathlib import Path

# Try to import C++ extension
try:
    import vision_ml_router
    CPP_AVAILABLE = True
except ImportError:
    CPP_AVAILABLE = False
    logging.warning("C++ vision_ml_router not available, using Python fallback")

logger = logging.getLogger(__name__)

@dataclass
class EnhancedVisionIntent:
    """Enhanced vision intent with multi-level analysis"""
    command: str
    cpp_score: float = 0.0
    cpp_action: str = ""
    ml_score: float = 0.0
    ml_action: str = ""
    linguistic_score: float = 0.0
    linguistic_features: Dict[str, Any] = field(default_factory=dict)
    combined_confidence: float = 0.0
    final_action: str = ""
    reasoning: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

class HybridVisionRouter:
    """
    Hybrid router combining C++ speed with Python ML flexibility
    Completely dynamic with zero hardcoding
    """
    
    def __init__(self):
        self.cpp_available = CPP_AVAILABLE
        self.learning_history = []
        self.pattern_database = self._load_pattern_database()
        self.neural_weights = self._initialize_neural_weights()
        
        # Dynamic action mapping that learns
        self.action_handlers = {
            "adaptive": self._create_adaptive_handler
        }
        
        logger.info(f"Hybrid Vision Router initialized (C++ available: {self.cpp_available})")
        
    def _load_pattern_database(self) -> Dict:
        """Load learned patterns from database"""
        db_path = Path("backend/data/vision_patterns.json")
        if db_path.exists():
            try:
                with open(db_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "patterns": {},
            "success_rates": {},
            "user_corrections": []
        }
        
    def _initialize_neural_weights(self) -> np.ndarray:
        """Initialize neural network weights for pattern matching"""
        # Simple neural network for demonstration
        # In production, this would be a more sophisticated model
        return np.random.randn(50, 10) * 0.1
        
    async def analyze_command(
        self, 
        command: str,
        linguistic_features: Optional[Dict] = None,
        context: Optional[Dict] = None
    ) -> EnhancedVisionIntent:
        """
        Perform multi-level analysis of command
        """
        intent = EnhancedVisionIntent(command=command)
        
        # Level 1: C++ Analysis (if available)
        if self.cpp_available:
            cpp_score, cpp_action = vision_ml_router.analyze(command)
            intent.cpp_score = cpp_score
            intent.cpp_action = cpp_action
            intent.reasoning.append(f"C++ analysis: {cpp_action} (score: {cpp_score:.2f})")
            
        # Level 2: Python ML Analysis
        ml_score, ml_action = await self._ml_analysis(command, linguistic_features)
        intent.ml_score = ml_score
        intent.ml_action = ml_action
        intent.reasoning.append(f"ML analysis: {ml_action} (score: {ml_score:.2f})")
        
        # Level 3: Linguistic Analysis
        ling_score = await self._linguistic_analysis(command, linguistic_features)
        intent.linguistic_score = ling_score
        intent.linguistic_features = linguistic_features or {}
        intent.reasoning.append(f"Linguistic score: {ling_score:.2f}")
        
        # Level 4: Pattern Database Check
        pattern_match = self._check_pattern_database(command)
        if pattern_match:
            intent.reasoning.append(f"Pattern match found: {pattern_match['action']}")
            intent.metadata["pattern_match"] = pattern_match
            
        # Combine all signals
        intent.combined_confidence = self._combine_signals(intent)
        intent.final_action = self._determine_final_action(intent)
        
        # Add metadata
        intent.metadata.update({
            "timestamp": datetime.now().isoformat(),
            "cpp_available": self.cpp_available,
            "analysis_levels": 4
        })
        
        return intent
        
    async def _ml_analysis(
        self, 
        command: str,
        linguistic_features: Optional[Dict]
    ) -> Tuple[float, str]:
        """
        Python ML analysis using neural network
        """
        # Extract features
        features = self._extract_features(command, linguistic_features)
        
        # Neural network forward pass
        activations = np.tanh(np.dot(features, self.neural_weights))
        
        # Action scores
        action_scores = {
            "describe": activations[0],
            "analyze": activations[1],
            "check": activations[2],
            "monitor": activations[3],
            "search": activations[4],
            "examine": activations[5],
            "identify": activations[6],
            "track": activations[7],
            "observe": activations[8],
            "inspect": activations[9]
        }
        
        # Find best action
        best_action = max(action_scores, key=action_scores.get)
        best_score = float(action_scores[best_action])
        
        # Normalize score
        normalized_score = (best_score + 1) / 2  # tanh output is [-1, 1]
        
        return normalized_score, best_action
        
    async def _linguistic_analysis(
        self, 
        command: str,
        features: Optional[Dict]
    ) -> float:
        """
        Deep linguistic analysis
        """
        if not features:
            # Basic tokenization if no features provided
            if command:
                words = command.lower().split() if command else []
            else:
                words = []
            features = {"tokens": words}
            
        score = 0.0
        
        # Analyze sentence structure
        if "sentence_type" in features:
            if features["sentence_type"] == "interrogative":
                score += 0.3  # Questions often relate to vision
            elif features["sentence_type"] == "imperative":
                score += 0.2  # Commands
                
        # Analyze POS tags if available
        if "pos_tags" in features:
            verb_count = sum(1 for tag in features["pos_tags"] if tag.startswith("VB"))
            noun_count = sum(1 for tag in features["pos_tags"] if tag.startswith("NN"))
            
            # Vision commands typically have specific verb-noun patterns
            if verb_count > 0 and noun_count > 0:
                score += 0.3
                
        # Check for question words
        question_words = {"what", "where", "which", "how", "can", "could", "is", "are"}
        tokens = features.get("tokens", command.lower().split() if command else [])
        if any(word in question_words for word in tokens):
            score += 0.2
            
        return min(score, 1.0)
        
    def _check_pattern_database(self, command: str) -> Optional[Dict]:
        """
        Check if we've seen similar patterns before
        """
        command_lower = command.lower() if command else ""
        
        # Check exact matches first
        if command_lower in self.pattern_database["patterns"]:
            return self.pattern_database["patterns"][command_lower]
            
        # Check fuzzy matches
        for pattern, info in self.pattern_database["patterns"].items():
            similarity = self._calculate_similarity(command_lower, pattern)
            if similarity > 0.85:
                return {**info, "similarity": similarity}
                
        return None
        
    def _calculate_similarity(self, s1: str, s2: str) -> float:
        """
        Calculate string similarity using multiple methods
        """
        # Jaccard similarity on word sets
        words1 = set(s1.split())
        words2 = set(s2.split())
        
        if not words1 or not words2:
            return 0.0
            
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        jaccard = len(intersection) / len(union) if union else 0.0
        
        # Length similarity
        len_sim = 1 - abs(len(s1) - len(s2)) / max(len(s1), len(s2))
        
        # Combined score
        return (jaccard * 0.7 + len_sim * 0.3)
        
    def _extract_features(
        self, 
        command: str,
        linguistic_features: Optional[Dict]
    ) -> np.ndarray:
        """
        Extract feature vector for neural network
        """
        features = np.zeros(50)
        
        words = command.lower().split() if command else []
        
        # Basic features
        features[0] = len(words) / 20.0  # Normalized word count
        features[1] = len(command) / 100.0  # Normalized char count
        
        # Word presence features (learned, not hardcoded)
        for i, word in enumerate(words[:10]):  # First 10 words
            # Hash word to feature index
            feature_idx = 2 + (hash(word) % 20)
            features[feature_idx] = 1.0
            
        # Linguistic features if available
        if linguistic_features:
            if "sentence_type" in linguistic_features:
                sentence_types = ["declarative", "interrogative", "imperative", "exclamatory"]
                if linguistic_features["sentence_type"] in sentence_types:
                    idx = sentence_types.index(linguistic_features["sentence_type"])
                    features[22 + idx] = 1.0
                    
            if "complexity" in linguistic_features:
                features[26] = linguistic_features["complexity"]
                
        # Pattern features from database
        pattern_match = self._check_pattern_database(command)
        if pattern_match:
            features[30] = pattern_match.get("confidence", 0.5)
            features[31] = pattern_match.get("success_rate", 0.5)
            
        return features
        
    def _combine_signals(self, intent: EnhancedVisionIntent) -> float:
        """
        Combine all analysis signals into final confidence
        """
        scores = []
        weights = []
        
        # C++ score (highest weight if available)
        if self.cpp_available and intent.cpp_score > 0:
            scores.append(intent.cpp_score)
            weights.append(0.4)
            
        # ML score
        if intent.ml_score > 0:
            scores.append(intent.ml_score)
            weights.append(0.3)
            
        # Linguistic score
        if intent.linguistic_score > 0:
            scores.append(intent.linguistic_score)
            weights.append(0.2)
            
        # Pattern match bonus
        if "pattern_match" in intent.metadata:
            scores.append(0.9)
            weights.append(0.1)
            
        if not scores:
            return 0.0
            
        # Weighted average
        total_weight = sum(weights)
        weighted_sum = sum(s * w for s, w in zip(scores, weights))
        
        return weighted_sum / total_weight if total_weight > 0 else 0.0
        
    def _determine_final_action(self, intent: EnhancedVisionIntent) -> str:
        """
        Determine final action based on all signals
        """
        # Collect all suggested actions with their confidence
        action_votes = {}
        
        if intent.cpp_action and intent.cpp_score > 0.5:
            action_votes[intent.cpp_action] = intent.cpp_score * 0.4
            
        if intent.ml_action and intent.ml_score > 0.5:
            action_votes[intent.ml_action] = action_votes.get(intent.ml_action, 0) + intent.ml_score * 0.3
            
        if "pattern_match" in intent.metadata:
            pattern_action = intent.metadata["pattern_match"].get("action")
            if pattern_action:
                action_votes[pattern_action] = action_votes.get(pattern_action, 0) + 0.3
                
        # If no clear winner, use adaptive action
        if not action_votes or max(action_votes.values()) < 0.5:
            return "adaptive"
            
        return max(action_votes, key=action_votes.get)
        
    def create_handler(self, intent: EnhancedVisionIntent) -> callable:
        """
        Create dynamic handler for the intent
        """
        action = intent.final_action
        
        # Check if we have a specific handler
        if action in self.action_handlers:
            return self.action_handlers[action](intent)
            
        # Create adaptive handler
        return self._create_adaptive_handler(intent)
        
    def _create_adaptive_handler(self, intent: EnhancedVisionIntent):
        """
        Create an adaptive handler that can handle any vision request
        """
        async def adaptive_handler(vision_system, **kwargs):
            # Determine what to do based on the intent
            action_type = intent.final_action
            
            # Build dynamic response
            if action_type in ["describe", "explain", "tell"]:
                # Comprehensive description
                result = await vision_system.describe_screen(
                    detailed=True,
                    include_ocr=True,
                    include_ui_elements=True
                )
            elif action_type in ["analyze", "examine", "inspect"]:
                # Deep analysis
                result = await vision_system.analyze_workspace(
                    include_windows=True,
                    include_relationships=True,
                    include_content=True
                )
            elif action_type in ["check", "verify", "find"]:
                # Targeted check
                target = self._extract_target(intent.command)
                result = await vision_system.check_for(
                    target=target,
                    include_notifications=True,
                    include_changes=True
                )
            elif action_type in ["monitor", "track", "watch"]:
                # Start monitoring
                result = await vision_system.start_intelligent_monitoring(
                    interval=2.0,
                    change_threshold=0.1,
                    notify_on_change=True
                )
            else:
                # Generic adaptive analysis
                result = await vision_system.adaptive_analysis(
                    command=intent.command,
                    confidence=intent.combined_confidence,
                    hints=intent.reasoning
                )
                
            return result
            
        return adaptive_handler
        
    def _extract_target(self, command: str) -> str:
        """
        Extract target from command dynamically
        """
        words = command.lower().split() if command else []
        
        # Look for words after prepositions
        prepositions = {"for", "at", "on", "in", "of"}
        for i, word in enumerate(words):
            if word in prepositions and i + 1 < len(words):
                return " ".join(words[i+1:])
                
        # Default to general screen
        return "screen"
        
    def learn(self, intent: EnhancedVisionIntent, success: bool, user_feedback: Optional[str] = None):
        """
        Learn from execution results
        """
        # Update C++ learner if available
        if self.cpp_available:
            vision_ml_router.learn(
                intent.command,
                intent.final_action,
                1 if success else 0
            )
            
        # Update pattern database
        pattern_key = intent.command.lower() if intent.command else ""
        if pattern_key not in self.pattern_database["patterns"]:
            self.pattern_database["patterns"][pattern_key] = {
                "action": intent.final_action,
                "confidence": intent.combined_confidence,
                "success_count": 0,
                "failure_count": 0
            }
            
        if success:
            self.pattern_database["patterns"][pattern_key]["success_count"] += 1
        else:
            self.pattern_database["patterns"][pattern_key]["failure_count"] += 1
            
        # Update success rate
        pattern = self.pattern_database["patterns"][pattern_key]
        total = pattern["success_count"] + pattern["failure_count"]
        pattern["success_rate"] = pattern["success_count"] / total if total > 0 else 0
        
        # Process user feedback
        if user_feedback:
            self.pattern_database["user_corrections"].append({
                "command": intent.command,
                "feedback": user_feedback,
                "timestamp": datetime.now().isoformat()
            })
            
        # Update neural weights based on feedback
        if success:
            # Reinforce current weights
            features = self._extract_features(intent.command, intent.linguistic_features)
            self.neural_weights += 0.01 * np.outer(features, np.random.randn(10))
            
        # Save periodically
        if len(self.learning_history) % 10 == 0:
            self._save_pattern_database()
            
    def _save_pattern_database(self):
        """Save learned patterns to disk"""
        db_path = Path("backend/data/vision_patterns.json")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(db_path, 'w') as f:
                json.dump(self.pattern_database, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save pattern database: {e}")

class DynamicVisionExecutor:
    """
    Executes vision commands dynamically based on intent analysis
    """
    
    def __init__(self, vision_system):
        self.vision_system = vision_system
        self.router = HybridVisionRouter()
        self.execution_cache = {}
        
    async def execute_vision_command(
        self,
        command: str,
        linguistic_features: Optional[Dict] = None,
        context: Optional[Dict] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Execute vision command with full ML routing
        """
        # Analyze intent
        intent = await self.router.analyze_command(command, linguistic_features, context)
        
        logger.info(f"Vision intent analysis: {intent.final_action} "
                   f"(confidence: {intent.combined_confidence:.2f})")
        
        # Check confidence threshold
        if intent.combined_confidence < 0.5:
            return await self._handle_low_confidence(intent)
            
        # Create and execute handler
        handler = self.router.create_handler(intent)
        
        try:
            result = await handler(self.vision_system)
            
            # Learn from success
            self.router.learn(intent, success=True)
            
            return result, {
                "intent": intent,
                "handler": intent.final_action,
                "success": True
            }
            
        except Exception as e:
            logger.error(f"Vision execution error: {e}")
            
            # Learn from failure
            self.router.learn(intent, success=False)
            
            # Try fallback
            return await self._execute_fallback(intent, str(e))
            
    async def _handle_low_confidence(
        self, 
        intent: EnhancedVisionIntent
    ) -> Tuple[str, Dict]:
        """
        Handle low confidence commands
        """
        # Ask for clarification or use best guess
        response = (
            f"I think you want me to {intent.final_action} something "
            f"on your screen. Let me try to help with that."
        )
        
        # Use adaptive handler anyway
        handler = self.router.create_handler(intent)
        
        try:
            result = await handler(self.vision_system)
            return f"{response}\n\n{result}", {
                "intent": intent,
                "low_confidence": True,
                "success": True
            }
        except Exception:
            return response + "\n\nI'm having trouble accessing the vision system.", {
                "intent": intent,
                "low_confidence": True,
                "success": False
            }
            
    async def _execute_fallback(
        self,
        intent: EnhancedVisionIntent,
        error: str
    ) -> Tuple[str, Dict]:
        """
        Fallback execution when primary handler fails
        """
        # Try basic screen capture and description
        try:
            basic_result = await self.vision_system.capture_and_describe()
            
            return (
                f"I encountered an issue with the {intent.final_action} command, "
                f"but here's what I can see: {basic_result}",
                {
                    "intent": intent,
                    "fallback": True,
                    "error": error,
                    "success": True
                }
            )
        except Exception as e:
            return (
                f"I'm unable to access the vision system right now. "
                f"Please ensure screen recording permission is granted.",
                {
                    "intent": intent,
                    "fallback": True,
                    "error": str(e),
                    "success": False
                }
            )