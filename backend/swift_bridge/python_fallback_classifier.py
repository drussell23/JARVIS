#!/usr/bin/env python3
"""Python-based fallback classifier for when Swift is not available.

This module provides a comprehensive NLP-based command classification system
that serves as a fallback when Swift-based classification is unavailable.
It uses various NLP libraries (spaCy, NLTK) to intelligently route commands
between system operations and vision analysis tasks.

The classifier learns from user feedback and maintains patterns to improve
accuracy over time. It provides linguistic analysis, intent detection, and
confidence scoring for command routing decisions.

Example:
    >>> router = FallbackIntelligentCommandRouter()
    >>> handler_type, details = await router.route_command("close whatsapp")
    >>> print(handler_type)  # "system"
    >>> print(details["confidence"])  # 0.8
"""

import re
import json
import asyncio
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime
from pathlib import Path
import logging

# Optional NLP imports
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except Exception:
    SPACY_AVAILABLE = False

try:
    import nltk
    from nltk.tokenize import word_tokenize
    from nltk.tag import pos_tag
    NLTK_AVAILABLE = True
except Exception:
    NLTK_AVAILABLE = False

logger = logging.getLogger(__name__)

class PythonCommandClassifier:
    """Python-based intelligent command classifier using linguistic analysis.
    
    This class uses NLP techniques to analyze commands and determine whether
    they should be routed to system operations or vision analysis handlers.
    It maintains learned patterns and provides confidence scoring for decisions.
    
    Attributes:
        cache_file (Path): Path to the pattern cache file
        learned_patterns (Dict[str, Any]): Dictionary of learned command patterns
        action_verbs (set): Set of verbs that indicate system commands
        question_words (set): Set of words that indicate vision queries
        vision_verbs (set): Set of verbs related to vision operations
    """
    
    def __init__(self, cache_file: Optional[str] = None):
        """Initialize the command classifier.
        
        Args:
            cache_file: Optional path to cache file for learned patterns.
                       Defaults to ~/.jarvis/command_patterns.json
        """
        self.cache_file = cache_file or Path.home() / ".jarvis" / "command_patterns.json"
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load learned patterns
        self.learned_patterns = self._load_patterns()
        
        # Action verbs that typically indicate system commands
        self.action_verbs = {
            "close", "quit", "exit", "terminate", "kill", "stop",
            "open", "launch", "start", "run", "execute",
            "restart", "reload", "refresh",
            "minimize", "maximize", "hide", "show",
            "switch", "focus", "activate"
        }
        
        # Question words that typically indicate vision queries
        self.question_words = {
            "what", "where", "which", "who", "when", "how",
            "is", "are", "can", "could", "should", "would"
        }
        
        # Vision-related verbs
        self.vision_verbs = {
            "see", "look", "view", "check", "find", "search",
            "describe", "analyze", "examine", "inspect"
        }
    
    def _load_patterns(self) -> Dict[str, Any]:
        """Load learned patterns from cache file.
        
        Returns:
            Dictionary containing learned patterns, commands, and statistics.
            Returns default structure if cache file doesn't exist or is invalid.
        """
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load patterns cache: {e}")
        return {
            "commands": {},
            "patterns": {},
            "stats": {"total": 0, "correct": 0}
        }
    
    def _save_patterns(self):
        """Save learned patterns to cache file.
        
        Raises:
            Exception: If unable to write to cache file (logged as error)
        """
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self.learned_patterns, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save patterns: {e}")
    
    def _extract_linguistic_features(self, text: str) -> Dict[str, Any]:
        """Extract linguistic features from input text using available NLP libraries.
        
        Analyzes text using spaCy (preferred), NLTK, or basic pattern matching
        to extract features like verbs, entities, and sentence structure.
        
        Args:
            text: Input text to analyze
            
        Returns:
            Dictionary containing extracted features:
                - has_question (bool): Whether text contains question indicators
                - has_action_verb (bool): Whether text contains action verbs
                - has_vision_verb (bool): Whether text contains vision-related verbs
                - first_word (str): First word of the text
                - verb_count (int): Number of verbs detected
                - entities (List[str]): Named entities found in text
                - sentence_type (str): "question" or "statement"
        """
        features = {
            "has_question": False,
            "has_action_verb": False,
            "has_vision_verb": False,
            "first_word": "",
            "verb_count": 0,
            "entities": [],
            "sentence_type": "statement"
        }
        
        words = text.lower().split()
        if not words:
            return features
        
        features["first_word"] = words[0]
        
        # Check for question
        if any(word in self.question_words for word in words[:3]):
            features["has_question"] = True
            features["sentence_type"] = "question"
        
        if text.strip().endswith("?"):
            features["has_question"] = True
            features["sentence_type"] = "question"
        
        # Use NLP if available
        if SPACY_AVAILABLE:
            doc = nlp(text)
            verbs = [token.text.lower() for token in doc if token.pos_ == "VERB"]
            features["verb_count"] = len(verbs)
            
            # Check for action verbs
            for verb in verbs:
                if verb in self.action_verbs:
                    features["has_action_verb"] = True
                if verb in self.vision_verbs:
                    features["has_vision_verb"] = True
            
            # Extract entities
            features["entities"] = [ent.text for ent in doc.ents]
            
        elif NLTK_AVAILABLE:
            tokens = word_tokenize(text)
            tagged = pos_tag(tokens)
            verbs = [word.lower() for word, pos in tagged if pos.startswith('VB')]
            features["verb_count"] = len(verbs)
            
            for verb in verbs:
                if verb in self.action_verbs:
                    features["has_action_verb"] = True
                if verb in self.vision_verbs:
                    features["has_vision_verb"] = True
        
        else:
            # Basic fallback
            for word in words:
                if word in self.action_verbs:
                    features["has_action_verb"] = True
                if word in self.vision_verbs:
                    features["has_vision_verb"] = True
        
        return features
    
    def _determine_intent(self, text: str, features: Dict[str, Any]) -> str:
        """Determine the intent of the command based on linguistic features.
        
        Args:
            text: Original command text
            features: Extracted linguistic features
            
        Returns:
            Intent classification: "query", "action", "analysis", or "help_request"
        """
        # Questions about state or content -> vision
        if features["has_question"]:
            if "how" in text.lower() and any(verb in text.lower() for verb in ["close", "open", "quit"]):
                return "help_request"  # "how to close" is vision
            return "query"
        
        # Direct action commands -> system
        if features["has_action_verb"]:
            return "action"
        
        # Vision verbs -> vision
        if features["has_vision_verb"]:
            return "analysis"
        
        # Check learned patterns
        normalized = text.lower().strip()
        if normalized in self.learned_patterns["commands"]:
            pattern = self.learned_patterns["commands"][normalized]
            if pattern["confidence"] > 0.7:
                return pattern["intent"]
        
        # Default based on sentence structure
        if features["sentence_type"] == "question":
            return "query"
        
        return "action"  # Default to action for statements
    
    async def classify(self, text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Classify a command and return comprehensive routing information.
        
        Performs linguistic analysis to determine the appropriate handler type,
        intent, and confidence level for the given command.
        
        Args:
            text: Command text to classify
            context: Optional context information (currently unused)
            
        Returns:
            Classification result containing:
                - type (str): Handler type ("system" or "vision")
                - intent (str): Command intent classification
                - confidence (float): Confidence score (0.0-0.95)
                - features (Dict): Extracted linguistic features
                - reasoning (str): Human-readable explanation
                - entities (List[str]): Named entities found
                
        Example:
            >>> result = await classifier.classify("close whatsapp")
            >>> print(result["type"])  # "system"
            >>> print(result["confidence"])  # 0.8
        """
        # Extract features
        features = self._extract_linguistic_features(text)
        
        # Determine intent
        intent = self._determine_intent(text, features)
        
        # Determine handler type
        if intent in ["query", "analysis", "help_request"]:
            handler_type = "vision"
        else:
            handler_type = "system"
        
        # Check learned overrides
        normalized = text.lower().strip()
        if normalized in self.learned_patterns["commands"]:
            learned = self.learned_patterns["commands"][normalized]
            if learned["confidence"] > 0.8:
                handler_type = learned["type"]
        
        # Calculate confidence
        confidence = 0.5
        
        # Boost confidence for clear patterns
        if features["has_question"] and handler_type == "vision":
            confidence += 0.3
        elif features["has_action_verb"] and handler_type == "system":
            confidence += 0.3
        
        # Boost for learned patterns
        if normalized in self.learned_patterns["commands"]:
            confidence = max(confidence, self.learned_patterns["commands"][normalized]["confidence"])
        
        # Generate reasoning
        reasoning = self._generate_reasoning(text, features, intent, handler_type)
        
        result = {
            "type": handler_type,
            "intent": intent,
            "confidence": min(confidence, 0.95),
            "features": features,
            "reasoning": reasoning,
            "entities": features.get("entities", [])
        }
        
        # Update stats
        self.learned_patterns["stats"]["total"] += 1
        
        return result
    
    def _generate_reasoning(self, text: str, features: Dict[str, Any], intent: str, handler_type: str) -> str:
        """Generate human-readable reasoning for the classification decision.
        
        Args:
            text: Original command text
            features: Extracted linguistic features
            intent: Determined intent
            handler_type: Determined handler type
            
        Returns:
            Human-readable explanation of the classification decision
        """
        reasons = []
        
        if features["has_question"]:
            reasons.append("Question structure detected")
        
        if features["has_action_verb"]:
            reasons.append("Action verb detected")
        
        if features["has_vision_verb"]:
            reasons.append("Vision-related verb detected")
        
        if text.lower().strip() in self.learned_patterns["commands"]:
            reasons.append("Matched learned pattern")
        
        if not reasons:
            if handler_type == "system":
                reasons.append("Statement structure suggests action")
            else:
                reasons.append("Content suggests analysis needed")
        
        return ". ".join(reasons) + "."
    
    async def learn(self, text: str, correct_type: str, was_successful: bool):
        """Learn from user feedback to improve future classifications.
        
        Updates the learned patterns cache with feedback about whether
        a classification was correct and successful.
        
        Args:
            text: Original command text
            correct_type: The correct handler type ("system" or "vision")
            was_successful: Whether the command execution was successful
        """
        normalized = text.lower().strip()
        
        if normalized not in self.learned_patterns["commands"]:
            self.learned_patterns["commands"][normalized] = {
                "type": correct_type,
                "intent": "action" if correct_type == "system" else "query",
                "confidence": 0.6,
                "count": 0,
                "successful": 0
            }
        
        pattern = self.learned_patterns["commands"][normalized]
        pattern["count"] += 1
        
        if was_successful:
            pattern["successful"] += 1
            pattern["confidence"] = min(0.95, pattern["confidence"] + 0.1)
            self.learned_patterns["stats"]["correct"] += 1
        else:
            pattern["confidence"] = max(0.1, pattern["confidence"] - 0.05)
        
        pattern["type"] = correct_type
        pattern["last_updated"] = datetime.now().isoformat()
        
        self._save_patterns()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get classifier performance statistics.
        
        Returns:
            Dictionary containing:
                - total (int): Total number of classifications
                - correct (int): Number of correct classifications
                - learned_commands (int): Number of learned command patterns
                - accuracy (float): Overall accuracy ratio
        """
        stats = self.learned_patterns["stats"].copy()
        stats["learned_commands"] = len(self.learned_patterns["commands"])
        
        if stats["total"] > 0:
            stats["accuracy"] = stats["correct"] / stats["total"]
        else:
            stats["accuracy"] = 0
        
        return stats

class FallbackIntelligentCommandRouter:
    """Fallback intelligent command router using Python NLP.
    
    This class provides a drop-in replacement for Swift-based command routing
    when Swift is not available. It uses the PythonCommandClassifier to make
    intelligent routing decisions based on linguistic analysis.
    
    Attributes:
        classifier (PythonCommandClassifier): The underlying classification engine
    """
    
    def __init__(self):
        """Initialize the fallback command router.
        
        Creates a PythonCommandClassifier instance and logs that the fallback
        system is being used.
        """
        self.classifier = PythonCommandClassifier()
        logger.info("Using Python-based NLP classifier (Swift unavailable)")
    
    async def route_command(self, text: str, context: Optional[Dict] = None) -> Tuple[str, Dict[str, Any]]:
        """Route a command to the appropriate handler based on linguistic analysis.
        
        Args:
            text: Command text to route
            context: Optional context information for routing decisions
            
        Returns:
            Tuple containing:
                - handler_type (str): "system" or "vision"
                - classification_details (Dict): Detailed classification information
                
        Example:
            >>> router = FallbackIntelligentCommandRouter()
            >>> handler_type, details = await router.route_command("close safari")
            >>> print(handler_type)  # "system"
            >>> print(details["confidence"])  # 0.8
        """
        classification = await self.classifier.classify(text, context)
        return classification["type"], classification
    
    async def provide_feedback(self, command: str, correct_type: str, was_successful: bool):
        """Provide feedback to improve future classification accuracy.
        
        Args:
            command: The original command text
            correct_type: The correct handler type ("system" or "vision")
            was_successful: Whether the command execution was successful
        """
        await self.classifier.learn(command, correct_type, was_successful)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive routing and classification statistics.
        
        Returns:
            Dictionary containing performance metrics and learned patterns count
        """
        return self.classifier.get_stats()

# Export the fallback router
IntelligentCommandRouter = FallbackIntelligentCommandRouter

if __name__ == "__main__":
    # Test the classifier
    async def test():
        """Test function to demonstrate classifier capabilities.
        
        Creates a router instance and tests it with various command types
        to show classification results, confidence scores, and reasoning.
        """
        router = FallbackIntelligentCommandRouter()
        
        test_commands = [
            "close whatsapp",
            "what's on my screen",
            "open safari",
            "show me discord",
            "can you quit spotify",
            "where is terminal"
        ]
        
        print("Python Fallback Classifier Test")
        print("=" * 50)
        
        for cmd in test_commands:
            handler_type, details = await router.route_command(cmd)
            print(f"\nCommand: '{cmd}'")
            print(f"  Type: {handler_type}")
            print(f"  Confidence: {details['confidence']:.2f}")
            print(f"  Intent: {details['intent']}")
            print(f"  Reasoning: {details['reasoning']}")
    
    import asyncio
    asyncio.run(test())