"""
JARVIS Voice Command Handler for Voice Unlock
============================================

Fully async, dynamic command handler with:
- Fuzzy matching and phonetic similarity detection for robust STT error handling
- Self-learning STT error corrections
- Dynamic configuration loading (no hardcoding)
- Async processing for all operations
"""

import re
import os
import json
import asyncio
import logging
import aiofiles
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Set, Callable, Awaitable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from abc import ABC, abstractmethod
from enum import Enum, auto
from collections import defaultdict
import hashlib

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration System
# =============================================================================

class CommandConfig:
    """
    Dynamic configuration loader for command handling.
    Loads from environment variables, config files, and defaults.
    """

    # Config file paths (searched in order)
    CONFIG_PATHS = [
        Path.home() / ".jarvis" / "command_config.json",
        Path("/etc/jarvis/command_config.json"),
        Path(__file__).parent / "config" / "command_config.json",
    ]

    # Default configuration (used if no config file found)
    DEFAULTS = {
        "fuzzy_match_threshold": 0.45,
        "phonetic_match_weight": 0.3,
        "keyword_match_weight": 0.5,
        "ngram_match_weight": 0.15,
        "phrase_match_weight": 0.4,
        "command_history_limit": 100,
        "learning_enabled": True,
        "learning_threshold": 3,  # Times a correction must be confirmed
        "max_learned_mappings": 500,
        "confidence_boost_polite": 0.05,  # Boost for "please"
        "min_confidence_regex": 0.9,
        "min_confidence_fuzzy": 0.45,
    }

    _instance = None
    _config: Dict[str, Any] = {}
    _loaded = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    async def load(cls) -> "CommandConfig":
        """Load configuration from files and environment"""
        instance = cls()
        if not cls._loaded:
            await instance._load_config()
            cls._loaded = True
        return instance

    async def _load_config(self):
        """Load configuration from available sources"""
        # Start with defaults
        self._config = dict(self.DEFAULTS)

        # Try to load from config files
        for config_path in self.CONFIG_PATHS:
            if config_path.exists():
                try:
                    async with aiofiles.open(config_path, 'r') as f:
                        content = await f.read()
                        file_config = json.loads(content)
                        self._config.update(file_config)
                        logger.info(f"Loaded command config from {config_path}")
                        break
                except Exception as e:
                    logger.warning(f"Failed to load config from {config_path}: {e}")

        # Override with environment variables
        env_mappings = {
            "JARVIS_FUZZY_THRESHOLD": ("fuzzy_match_threshold", float),
            "JARVIS_LEARNING_ENABLED": ("learning_enabled", lambda x: x.lower() == "true"),
            "JARVIS_COMMAND_HISTORY_LIMIT": ("command_history_limit", int),
        }

        for env_key, (config_key, converter) in env_mappings.items():
            env_value = os.environ.get(env_key)
            if env_value:
                try:
                    self._config[config_key] = converter(env_value)
                except Exception as e:
                    logger.warning(f"Invalid env var {env_key}={env_value}: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value"""
        return self._config.get(key, default or self.DEFAULTS.get(key))

    def __getitem__(self, key: str) -> Any:
        return self.get(key)


# =============================================================================
# Command Types
# =============================================================================

class CommandType(Enum):
    """Enumeration of supported command types"""
    UNLOCK = auto()
    LOCK = auto()
    STATUS = auto()
    ENROLL = auto()
    SECURITY_TEST = auto()
    HELP = auto()
    CANCEL = auto()
    UNKNOWN = auto()


class MatchType(Enum):
    """How the command was matched"""
    REGEX = auto()
    FUZZY = auto()
    LEARNED = auto()
    PHONETIC = auto()


@dataclass
class VoiceCommand:
    """Parsed voice command with full metadata"""
    command_type: CommandType
    user_name: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_text: str = ""
    normalized_text: str = ""
    match_type: MatchType = MatchType.REGEX
    match_details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    trace_id: str = ""

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = f"cmd_{hashlib.md5(f'{self.raw_text}{self.timestamp}'.encode()).hexdigest()[:12]}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "command_type": self.command_type.name,
            "user_name": self.user_name,
            "parameters": self.parameters,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "match_type": self.match_type.name,
            "match_details": self.match_details,
            "timestamp": self.timestamp.isoformat(),
            "trace_id": self.trace_id,
        }


# =============================================================================
# Phonetic Matching (Double Metaphone Algorithm)
# =============================================================================

class PhoneticEncoder:
    """
    Double Metaphone algorithm for phonetic matching.
    Handles common speech-to-text errors by comparing how words sound.
    """

    VOWELS = frozenset('AEIOU')

    @classmethod
    def encode(cls, word: str) -> Tuple[str, str]:
        """
        Encode a word into its primary and secondary metaphone representations.
        Returns (primary, secondary) codes.
        """
        if not word:
            return ('', '')

        word = word.upper()
        word = re.sub(r'[^A-Z]', '', word)

        if not word:
            return ('', '')

        primary: List[str] = []
        secondary: List[str] = []
        current = 0
        length = len(word)
        last = length - 1

        # Skip initial silent letters
        if length >= 2 and word[:2] in ('GN', 'KN', 'PN', 'WR', 'PS'):
            current += 1

        # Initial X -> S
        if word[0] == 'X':
            primary.append('S')
            secondary.append('S')
            current += 1

        while current < length:
            char = word[current]

            if char in cls.VOWELS:
                if current == 0:
                    primary.append('A')
                    secondary.append('A')
                current += 1

            elif char == 'B':
                primary.append('P')
                secondary.append('P')
                current += 2 if current < last and word[current + 1] == 'B' else 1

            elif char == 'C':
                if current > 0 and word[current - 1:current + 2] == 'SCH':
                    primary.append('K')
                    secondary.append('K')
                    current += 1
                elif word[current:current + 2] == 'CH':
                    primary.append('X')
                    secondary.append('X')
                    current += 2
                elif word[current:current + 2] in ('CI', 'CE', 'CY'):
                    primary.append('S')
                    secondary.append('S')
                    current += 1
                else:
                    primary.append('K')
                    secondary.append('K')
                    current += 2 if word[current:current + 2] == 'CK' else 1

            elif char == 'D':
                if word[current:current + 2] == 'DG':
                    if current + 2 < length and word[current + 2] in ('I', 'E', 'Y'):
                        primary.append('J')
                        secondary.append('J')
                        current += 3
                    else:
                        primary.append('TK')
                        secondary.append('TK')
                        current += 2
                else:
                    primary.append('T')
                    secondary.append('T')
                    current += 2 if word[current:current + 2] == 'DT' else 1

            elif char == 'F':
                primary.append('F')
                secondary.append('F')
                current += 2 if current < last and word[current + 1] == 'F' else 1

            elif char == 'G':
                if current < last and word[current + 1] == 'H':
                    if current > 0 and word[current - 1] not in cls.VOWELS:
                        current += 2
                    else:
                        primary.append('K')
                        secondary.append('K')
                        current += 2
                elif word[current:current + 2] == 'GN':
                    primary.append('N')
                    secondary.append('KN')
                    current += 2
                elif current < last and word[current + 1] in ('I', 'E', 'Y'):
                    primary.append('J')
                    secondary.append('K')
                    current += 2
                else:
                    primary.append('K')
                    secondary.append('K')
                    current += 2 if current < last and word[current + 1] == 'G' else 1

            elif char == 'H':
                if current < last and word[current + 1] in cls.VOWELS:
                    if current == 0 or word[current - 1] in cls.VOWELS:
                        primary.append('H')
                        secondary.append('H')
                current += 1

            elif char == 'J':
                primary.append('J')
                secondary.append('J')
                current += 2 if current < last and word[current + 1] == 'J' else 1

            elif char == 'K':
                primary.append('K')
                secondary.append('K')
                current += 2 if current > 0 and word[current - 1] == 'C' else 1

            elif char == 'L':
                primary.append('L')
                secondary.append('L')
                current += 2 if current < last and word[current + 1] == 'L' else 1

            elif char == 'M':
                primary.append('M')
                secondary.append('M')
                current += 2 if current < last and word[current + 1] == 'M' else 1

            elif char == 'N':
                primary.append('N')
                secondary.append('N')
                current += 2 if current < last and word[current + 1] == 'N' else 1

            elif char == 'P':
                if current < last and word[current + 1] == 'H':
                    primary.append('F')
                    secondary.append('F')
                    current += 2
                else:
                    primary.append('P')
                    secondary.append('P')
                    current += 2 if word[current:current + 2] in ('PP', 'PB') else 1

            elif char == 'Q':
                primary.append('K')
                secondary.append('K')
                current += 2 if current < last and word[current + 1] == 'Q' else 1

            elif char == 'R':
                primary.append('R')
                secondary.append('R')
                current += 2 if current < last and word[current + 1] == 'R' else 1

            elif char == 'S':
                if word[current:current + 2] == 'SH':
                    primary.append('X')
                    secondary.append('X')
                    current += 2
                elif word[current:current + 3] in ('SIO', 'SIA'):
                    primary.append('X')
                    secondary.append('S')
                    current += 3
                else:
                    primary.append('S')
                    secondary.append('S')
                    current += 2 if word[current:current + 2] in ('SS', 'SC') else 1

            elif char == 'T':
                if word[current:current + 2] == 'TH':
                    primary.append('0')  # theta
                    secondary.append('T')
                    current += 2
                elif word[current:current + 3] == 'TIO':
                    primary.append('X')
                    secondary.append('X')
                    current += 3
                else:
                    primary.append('T')
                    secondary.append('T')
                    current += 2 if word[current:current + 2] == 'TT' else 1

            elif char == 'V':
                primary.append('F')
                secondary.append('F')
                current += 2 if current < last and word[current + 1] == 'V' else 1

            elif char == 'W':
                if current < last and word[current + 1] in cls.VOWELS:
                    primary.append('W')
                    secondary.append('W')
                current += 1

            elif char == 'X':
                primary.append('KS')
                secondary.append('KS')
                current += 2 if current < last and word[current + 1] == 'X' else 1

            elif char == 'Y':
                if current < last and word[current + 1] in cls.VOWELS:
                    primary.append('Y')
                    secondary.append('Y')
                current += 1

            elif char == 'Z':
                primary.append('S')
                secondary.append('S')
                current += 2 if current < last and word[current + 1] == 'Z' else 1

            else:
                current += 1

        return (''.join(primary)[:4], ''.join(secondary)[:4])


# =============================================================================
# Self-Learning STT Error Correction
# =============================================================================

class STTLearningEngine:
    """
    Self-learning engine for STT error corrections.
    Learns from user confirmations and corrections over time.
    """

    LEARNING_FILE = Path.home() / ".jarvis" / "stt_learned_mappings.json"

    def __init__(self, config: CommandConfig):
        self.config = config
        self._learned_mappings: Dict[str, Dict[str, Any]] = {}
        self._pending_corrections: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "correction": "",
            "count": 0,
            "last_seen": None,
        })
        self._loaded = False

    async def load(self):
        """Load learned mappings from disk"""
        if self._loaded:
            return

        if self.LEARNING_FILE.exists():
            try:
                async with aiofiles.open(self.LEARNING_FILE, 'r') as f:
                    content = await f.read()
                    data = json.loads(content)
                    self._learned_mappings = data.get("mappings", {})
                    logger.info(f"Loaded {len(self._learned_mappings)} learned STT mappings")
            except Exception as e:
                logger.warning(f"Failed to load learned mappings: {e}")

        self._loaded = True

    async def save(self):
        """Save learned mappings to disk"""
        try:
            self.LEARNING_FILE.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(self.LEARNING_FILE, 'w') as f:
                data = {
                    "mappings": self._learned_mappings,
                    "updated_at": datetime.now().isoformat(),
                }
                await f.write(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save learned mappings: {e}")

    def get_correction(self, text: str) -> Optional[str]:
        """Get learned correction for text if available"""
        normalized = text.lower().strip()
        mapping = self._learned_mappings.get(normalized)
        if mapping and mapping.get("confidence", 0) >= self.config["learning_threshold"]:
            return mapping.get("correction")
        return None

    async def suggest_correction(self, original: str, correction: str):
        """Suggest a correction (will be learned after threshold confirmations)"""
        if not self.config["learning_enabled"]:
            return

        normalized = original.lower().strip()

        pending = self._pending_corrections[normalized]
        if pending["correction"] == correction:
            pending["count"] += 1
            pending["last_seen"] = datetime.now().isoformat()
        else:
            pending["correction"] = correction
            pending["count"] = 1
            pending["last_seen"] = datetime.now().isoformat()

        # Promote to learned if threshold reached
        if pending["count"] >= self.config["learning_threshold"]:
            self._learned_mappings[normalized] = {
                "correction": correction,
                "confidence": pending["count"],
                "learned_at": datetime.now().isoformat(),
            }

            # Limit total mappings
            if len(self._learned_mappings) > self.config["max_learned_mappings"]:
                # Remove oldest mappings
                sorted_mappings = sorted(
                    self._learned_mappings.items(),
                    key=lambda x: x[1].get("learned_at", ""),
                )
                for key, _ in sorted_mappings[:100]:
                    del self._learned_mappings[key]

            await self.save()
            logger.info(f"Learned new STT mapping: '{normalized}' -> '{correction}'")

            # Clear pending
            del self._pending_corrections[normalized]

    async def confirm_correction(self, original: str, correction: str):
        """Confirm a correction was correct (boosts confidence)"""
        normalized = original.lower().strip()
        if normalized in self._learned_mappings:
            self._learned_mappings[normalized]["confidence"] += 1
            await self.save()


# =============================================================================
# Command Pattern Registry
# =============================================================================

@dataclass
class CommandPattern:
    """A command pattern with metadata"""
    pattern: str
    command_type: CommandType
    capture_group: Optional[str] = None  # 'user' for capturing username
    priority: int = 0  # Higher = checked first
    description: str = ""


class CommandPatternRegistry:
    """
    Dynamic registry for command patterns.
    Patterns can be loaded from config or added at runtime.
    """

    # Built-in patterns (can be overridden by config)
    BUILTIN_PATTERNS = [
        # Unlock patterns
        CommandPattern(
            r"(?:hey |hi |hello )?jarvis[,.]? (?:please )?unlock (?:my |the )?(?:mac|screen|computer|system)",
            CommandType.UNLOCK, priority=10, description="Standard unlock with JARVIS prefix"
        ),
        CommandPattern(
            r"unlock (?:my |the )?(?:screen|mac|computer|system)",
            CommandType.UNLOCK, priority=5, description="Direct unlock without JARVIS"
        ),
        CommandPattern(
            r"jarvis[,.\s]* (?:this is |it'?s )(\w+)",
            CommandType.UNLOCK, capture_group='user', priority=8, description="Identity announcement"
        ),
        CommandPattern(
            r"(?:hey )?jarvis[,.]? (\w+) (?:is )?here",
            CommandType.UNLOCK, capture_group='user', priority=7, description="Presence announcement"
        ),
        CommandPattern(
            r"jarvis[,.]? authenticate (?:me|user)?\s*(\w+)?",
            CommandType.UNLOCK, capture_group='user', priority=9, description="Explicit authentication"
        ),
        CommandPattern(
            r"open sesame[,.]? jarvis",
            CommandType.UNLOCK, priority=3, description="Fun unlock phrase"
        ),
        CommandPattern(
            r"(?:hey |hi )?jarvis[,.]? (?:please )?(?:open|access) (?:my |the )?(?:mac|screen|computer)",
            CommandType.UNLOCK, priority=6, description="Alternative unlock with open/access"
        ),

        # Lock patterns
        CommandPattern(
            r"(?:hey )?jarvis[,.]? (?:please )?lock (?:my |the )?(?:mac|computer|system|screen)",
            CommandType.LOCK, priority=10, description="Standard lock with JARVIS"
        ),
        CommandPattern(
            r"lock (?:my |the )?(?:screen|mac|computer)",
            CommandType.LOCK, priority=5, description="Direct lock without JARVIS"
        ),
        CommandPattern(
            r"jarvis[,.]? (?:activate |enable )?(?:security|lock)",
            CommandType.LOCK, priority=8, description="Security activation"
        ),
        CommandPattern(
            r"jarvis[,.]? (?:i'?m |i am )?(?:leaving|going away|done)",
            CommandType.LOCK, priority=7, description="Departure announcement"
        ),

        # Status patterns
        CommandPattern(
            r"jarvis[,.]? (?:what'?s |what is )?(?:the )?(?:status|state)",
            CommandType.STATUS, priority=10, description="Status query"
        ),
        CommandPattern(
            r"jarvis[,.]? (?:am i |is user )?authenticated",
            CommandType.STATUS, priority=8, description="Auth status check"
        ),
        CommandPattern(
            r"jarvis[,.]? (?:who'?s |who is )?(?:logged in|authenticated)",
            CommandType.STATUS, priority=8, description="User status check"
        ),

        # Enrollment patterns
        CommandPattern(
            r"jarvis[,.]? (?:please )?(?:enroll|register|add) (?:me|user)?\s*(\w+)?",
            CommandType.ENROLL, capture_group='user', priority=10, description="Voice enrollment"
        ),
        CommandPattern(
            r"jarvis[,.]? (?:create|setup) (?:voice )?profile (?:for )?\s*(\w+)?",
            CommandType.ENROLL, capture_group='user', priority=8, description="Profile creation"
        ),

        # Security test patterns
        CommandPattern(
            r"jarvis[,.]? (?:please )?(?:test|check|verify) (?:my )?voice (?:security|authentication|biometric)",
            CommandType.SECURITY_TEST, priority=10, description="Voice security test"
        ),
        CommandPattern(
            r"jarvis[,.]? (?:run|start|perform) (?:a )?(?:voice )?security (?:test|check)",
            CommandType.SECURITY_TEST, priority=8, description="Security test execution"
        ),

        # Help patterns
        CommandPattern(
            r"jarvis[,.]? (?:help|what can you do|commands)",
            CommandType.HELP, priority=10, description="Help request"
        ),

        # Cancel patterns
        CommandPattern(
            r"jarvis[,.]? (?:cancel|stop|never ?mind|abort)",
            CommandType.CANCEL, priority=10, description="Cancel command"
        ),
    ]

    def __init__(self):
        self._patterns: List[CommandPattern] = list(self.BUILTIN_PATTERNS)
        self._compiled_patterns: Dict[str, re.Pattern] = {}
        self._compile_patterns()

    def _compile_patterns(self):
        """Pre-compile regex patterns for performance"""
        self._compiled_patterns = {}
        for pattern in self._patterns:
            try:
                self._compiled_patterns[pattern.pattern] = re.compile(pattern.pattern, re.IGNORECASE)
            except re.error as e:
                logger.error(f"Invalid regex pattern '{pattern.pattern}': {e}")

    def add_pattern(self, pattern: CommandPattern):
        """Add a new pattern at runtime"""
        self._patterns.append(pattern)
        try:
            self._compiled_patterns[pattern.pattern] = re.compile(pattern.pattern, re.IGNORECASE)
        except re.error as e:
            logger.error(f"Invalid regex pattern '{pattern.pattern}': {e}")

    def get_patterns_by_type(self, command_type: CommandType) -> List[CommandPattern]:
        """Get all patterns for a command type, sorted by priority"""
        patterns = [p for p in self._patterns if p.command_type == command_type]
        return sorted(patterns, key=lambda p: p.priority, reverse=True)

    def match(self, text: str) -> Optional[Tuple[CommandPattern, re.Match]]:
        """Try to match text against all patterns"""
        text_lower = text.lower().strip()

        # Sort by priority and try each pattern
        sorted_patterns = sorted(self._patterns, key=lambda p: p.priority, reverse=True)

        for pattern in sorted_patterns:
            compiled = self._compiled_patterns.get(pattern.pattern)
            if compiled:
                match = compiled.search(text_lower)
                if match:
                    return (pattern, match)

        return None


# =============================================================================
# Fuzzy Matching Engine
# =============================================================================

class FuzzyMatcher:
    """
    Advanced fuzzy matching engine for handling STT transcription errors.
    Uses multiple strategies: phonetic similarity, keyword detection, n-gram matching.
    """

    def __init__(self, config: CommandConfig):
        self.config = config
        self.encoder = PhoneticEncoder()

        # Dynamic keyword sets (loaded from config or defaults)
        self._unlock_keywords: Set[str] = set()
        self._lock_keywords: Set[str] = set()
        self._unlock_phonetic_codes: Set[str] = set()
        self._lock_phonetic_codes: Set[str] = set()
        self._stt_mappings: Dict[str, str] = {}

        self._build_keywords()

    def _build_keywords(self):
        """Build keyword sets from config or defaults"""
        # Default keywords (extensible via config)
        unlock_words = [
            'unlock', 'unlok', 'unlog', 'unluck', 'unblock', 'unlocc',
            'open', 'access', 'authenticate', 'auth',
            'screen', 'skreen', 'scream', 'green', 'scren',
            'mac', 'mack', 'max', 'matt', 'mak',
            'computer', 'komputer', 'puter',
            'system', 'sistem',
            'jarvis', 'jarves', 'jarv', 'javis', 'jarvas', 'jarvus',
        ]

        lock_words = [
            'lock', 'log', 'lok', 'locked', 'locking', 'locc',
            'secure', 'security',
            'leaving', 'leave', 'going', 'done', 'away', 'bye',
        ]

        self._unlock_keywords = set(unlock_words)
        self._lock_keywords = set(lock_words)

        # Build phonetic codes
        for word in self._unlock_keywords:
            primary, secondary = self.encoder.encode(word)
            if primary:
                self._unlock_phonetic_codes.add(primary)
            if secondary and secondary != primary:
                self._unlock_phonetic_codes.add(secondary)

        for word in self._lock_keywords:
            primary, secondary = self.encoder.encode(word)
            if primary:
                self._lock_phonetic_codes.add(primary)
            if secondary and secondary != primary:
                self._lock_phonetic_codes.add(secondary)

        # Default STT error mappings
        self._stt_mappings = {
            # "unlock my screen" variations
            "lach ma's green": "unlock my screen",
            "loch ma green": "unlock my screen",
            "unlucky screen": "unlock my screen",
            "unlock green": "unlock my screen",
            "unlock scream": "unlock my screen",
            "unblock my screen": "unlock my screen",
            "im lach ma's green": "unlock my screen",
            "i'm lach ma's green": "unlock my screen",
            "on lock my screen": "unlock my screen",
            "unlocking screen": "unlock my screen",
            "an lock my screen": "unlock my screen",
            "and lock my screen": "unlock my screen",

            # "unlock my mac" variations
            "unlock my max": "unlock my mac",
            "unlock my mack": "unlock my mac",
            "unblock my mac": "unlock my mac",

            # JARVIS variations
            "jar vis": "jarvis",
            "jar vase": "jarvis",
            "jarvas": "jarvis",
            "javis": "jarvis",
            "jar vice": "jarvis",
            "jar miss": "jarvis",
            "jar fist": "jarvis",
            "service": "jarvis",
            "nervous": "jarvis",

            # Other common errors
            "all thin to kate": "authenticate",
            "authentic eight": "authenticate",
        }

    def normalize_text(self, text: str) -> str:
        """Normalize text and apply known STT error corrections"""
        text = text.lower().strip()

        # Apply known mappings
        for error, correction in self._stt_mappings.items():
            if error in text:
                text = text.replace(error, correction)

        return text

    def fuzzy_similarity(self, text1: str, text2: str) -> float:
        """Calculate fuzzy similarity between two strings (0.0 to 1.0)"""
        return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()

    def phonetic_match(self, word: str, target_codes: Set[str]) -> bool:
        """Check if word phonetically matches any target codes"""
        primary, secondary = self.encoder.encode(word)
        return primary in target_codes or secondary in target_codes

    async def detect_intent(
        self,
        text: str,
        target_type: CommandType
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Detect if text contains intent for target command type.

        Returns:
            (is_match, confidence, debug_info)
        """
        # Normalize text
        normalized = self.normalize_text(text)

        # Select appropriate keyword sets
        if target_type == CommandType.UNLOCK:
            keywords = self._unlock_keywords
            phonetic_codes = self._unlock_phonetic_codes
            target_phrases = [
                "unlock my screen", "unlock my mac", "unlock the screen",
                "unlock the mac", "jarvis unlock", "hey jarvis unlock",
                "unlock computer", "open my mac",
            ]
        elif target_type == CommandType.LOCK:
            keywords = self._lock_keywords
            phonetic_codes = self._lock_phonetic_codes
            target_phrases = [
                "lock my screen", "lock my mac", "lock the computer",
                "jarvis lock", "activate security", "im leaving",
            ]
        else:
            return (False, 0.0, {"error": f"Unsupported type: {target_type}"})

        debug_info: Dict[str, Any] = {
            "original": text,
            "normalized": normalized,
            "strategies_matched": [],
            "keyword_matches": [],
            "phonetic_matches": [],
        }

        confidence = 0.0
        words = re.findall(r'\b\w+\b', normalized)

        # Strategy 1: Keyword matching
        keyword_matches = []
        for word in words:
            if word in keywords:
                keyword_matches.append(word)
            else:
                # Fuzzy match against keywords
                for keyword in keywords:
                    if self.fuzzy_similarity(word, keyword) > 0.75:
                        keyword_matches.append(f"{word}~{keyword}")
                        break

        debug_info["keyword_matches"] = keyword_matches

        # Key word combinations
        if target_type == CommandType.UNLOCK:
            has_action = any(w in normalized for w in ['unlock', 'unlok', 'unblock', 'unlog', 'open', 'access'])
            has_target = any(w in normalized for w in ['screen', 'mac', 'computer', 'system', 'green', 'scream'])
            has_jarvis = any(w in normalized for w in ['jarvis', 'jarvas', 'javis', 'jarves'])

            if has_action and has_target:
                confidence += self.config["keyword_match_weight"]
                debug_info["strategies_matched"].append("action+target keyword combo")
            elif has_action:
                confidence += self.config["keyword_match_weight"] * 0.7
                debug_info["strategies_matched"].append("action keyword")
            elif has_target and has_jarvis:
                confidence += self.config["keyword_match_weight"] * 0.5
                debug_info["strategies_matched"].append("target+jarvis combo")

        elif target_type == CommandType.LOCK:
            has_lock = any(w in normalized for w in ['lock', 'lok', 'log', 'secure'])
            has_leaving = any(w in normalized for w in ['leaving', 'leave', 'going', 'done', 'away', 'bye'])

            if has_lock:
                confidence += self.config["keyword_match_weight"]
                debug_info["strategies_matched"].append("lock keyword")
            if has_leaving:
                confidence += self.config["keyword_match_weight"] * 0.6
                debug_info["strategies_matched"].append("leaving keyword")

        # Strategy 2: Phonetic matching
        phonetic_matches = []
        for word in words:
            if self.phonetic_match(word, phonetic_codes):
                phonetic_matches.append(word)

        debug_info["phonetic_matches"] = phonetic_matches

        if len(phonetic_matches) >= 2:
            confidence += self.config["phonetic_match_weight"]
            debug_info["strategies_matched"].append("phonetic match (2+)")
        elif len(phonetic_matches) >= 1:
            confidence += self.config["phonetic_match_weight"] * 0.5
            debug_info["strategies_matched"].append("phonetic match (1)")

        # Strategy 3: Fuzzy phrase matching
        best_phrase_match = 0.0
        best_phrase = ""
        for phrase in target_phrases:
            similarity = self.fuzzy_similarity(normalized, phrase)
            if similarity > best_phrase_match:
                best_phrase_match = similarity
                best_phrase = phrase

        if best_phrase_match > 0.5:
            confidence += best_phrase_match * self.config["phrase_match_weight"]
            debug_info["strategies_matched"].append(f"fuzzy phrase ({best_phrase}: {best_phrase_match:.2f})")

        # Strategy 4: N-gram analysis
        if target_type == CommandType.UNLOCK:
            bigrams = [normalized[i:i+2] for i in range(len(normalized)-1)]
            target_bigrams = {'un', 'nl', 'lo', 'oc', 'ck', 'sc', 'cr', 'ee', 'en'}
            bigram_hits = sum(1 for bg in bigrams if bg in target_bigrams)

            if bigram_hits >= 4:
                confidence += self.config["ngram_match_weight"]
                debug_info["strategies_matched"].append(f"n-gram match ({bigram_hits} hits)")
            elif bigram_hits >= 2:
                confidence += self.config["ngram_match_weight"] * 0.5
                debug_info["strategies_matched"].append(f"partial n-gram ({bigram_hits} hits)")

        # Cap confidence
        confidence = min(1.0, confidence)

        # Determine if match
        is_match = confidence >= self.config["fuzzy_match_threshold"]

        logger.debug(f"Fuzzy intent detection for {target_type.name}: match={is_match}, confidence={confidence:.2f}")

        return (is_match, confidence, debug_info)


# =============================================================================
# Response Generator
# =============================================================================

class ResponseGenerator:
    """
    Dynamic response generator for JARVIS voice feedback.
    Supports templates and context-aware responses.
    """

    def __init__(self):
        self._templates: Dict[str, List[str]] = {
            "unlock_success": [
                "Welcome back, {user}. System unlocked.",
                "Authentication successful. Good to see you, {user}.",
                "Access granted. How may I assist you today, {user}?",
                "Voice verified. Welcome, {user}.",
                "Unlocking for you now, {user}.",
            ],
            "unlock_success_watch": [
                "Your Apple Watch confirms it's you. Welcome back, {user}.",
                "Watch proximity verified. Unlocking for you, {user}.",
            ],
            "unlock_failed": [
                "Authentication failed. {error}",
                "I'm unable to verify your identity. {error}",
                "Access denied. {error}",
            ],
            "unlock_failed_watch": [
                "Please ensure your Apple Watch is nearby and unlocked.",
            ],
            "unlock_failed_voice": [
                "I didn't recognize your voice. Please try again.",
            ],
            "lock_success": [
                "System locked. Have a good day.",
                "Security activated. System is now locked.",
                "Locking system. See you later.",
            ],
            "lock_failed": [
                "Unable to lock the system.",
                "Lock command failed.",
            ],
            "status_locked": [
                "The system is currently locked.",
                "Security is active. Authentication required.",
            ],
            "status_unlocked": [
                "System is unlocked. Current user: {user}.",
                "{user} is currently authenticated.",
            ],
            "enroll_success": [
                "Voice profile created for {user}.",
                "Enrollment successful. {user} can now use voice authentication.",
                "I've registered your voice, {user}.",
            ],
            "enroll_failed": [
                "Enrollment failed. Please try again.",
                "I was unable to create a voice profile.",
            ],
            "fuzzy_match_notice": [
                "I understood that as an unlock request.",
                "Processing your unlock command.",
            ],
            "help": [
                "Available commands: unlock, lock, status, enroll. Say 'Hey JARVIS' followed by your command.",
            ],
        }

    async def generate(
        self,
        template_key: str,
        context: Dict[str, Any] = None
    ) -> str:
        """Generate a response from template with context"""
        import random

        context = context or {}
        templates = self._templates.get(template_key, ["Command processed."])

        template = random.choice(templates)

        try:
            return template.format(**context)
        except KeyError:
            # Return template without formatting if context missing
            return template

    def add_templates(self, key: str, templates: List[str]):
        """Add or extend templates for a key"""
        if key in self._templates:
            self._templates[key].extend(templates)
        else:
            self._templates[key] = templates


# =============================================================================
# Main Command Handler
# =============================================================================

class JARVISCommandHandler:
    """
    Fully async JARVIS voice command handler.

    Features:
    - Multi-strategy command matching (regex, fuzzy, phonetic, learned)
    - Self-learning STT error corrections
    - Dynamic configuration
    - Full async support
    - Comprehensive logging and tracing
    """

    def __init__(self):
        self._config: Optional[CommandConfig] = None
        self._pattern_registry: Optional[CommandPatternRegistry] = None
        self._fuzzy_matcher: Optional[FuzzyMatcher] = None
        self._learning_engine: Optional[STTLearningEngine] = None
        self._response_generator: Optional[ResponseGenerator] = None
        self._command_history: List[VoiceCommand] = []
        self._last_command: Optional[VoiceCommand] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize the command handler asynchronously"""
        async with self._lock:
            if self._initialized:
                return

            # Load configuration
            self._config = await CommandConfig.load()

            # Initialize components
            self._pattern_registry = CommandPatternRegistry()
            self._fuzzy_matcher = FuzzyMatcher(self._config)
            self._learning_engine = STTLearningEngine(self._config)
            self._response_generator = ResponseGenerator()

            # Load learned mappings
            await self._learning_engine.load()

            self._initialized = True
            logger.info("JARVISCommandHandler initialized")

    async def parse_command(self, text: str) -> Optional[VoiceCommand]:
        """
        Parse voice command from transcribed text using multi-strategy matching.

        Strategies (in order):
        1. Learned corrections (from STT learning engine)
        2. Exact regex pattern matching
        3. Fuzzy matching with phonetic similarity

        Args:
            text: Transcribed voice text

        Returns:
            Parsed VoiceCommand or None
        """
        await self.initialize()

        original_text = text
        text_lower = text.lower().strip()
        trace_id = f"cmd_{hashlib.md5(f'{text}{datetime.now()}'.encode()).hexdigest()[:12]}"

        logger.info(f"[{trace_id}] Parsing command: '{text_lower}'")

        # =================================================================
        # PHASE 1: Check learned corrections first
        # =================================================================

        learned_correction = self._learning_engine.get_correction(text_lower)
        if learned_correction:
            logger.info(f"[{trace_id}] Applied learned correction: '{text_lower}' -> '{learned_correction}'")
            text_lower = learned_correction

        # =================================================================
        # PHASE 2: Try regex pattern matching
        # =================================================================

        match_result = self._pattern_registry.match(text_lower)
        if match_result:
            pattern, match = match_result
            user_name = None

            if pattern.capture_group == 'user' and match.lastindex:
                user_name = match.group(1)

            # Confidence boost for polite requests
            confidence = self._config["min_confidence_regex"]
            if 'please' in text_lower:
                confidence += self._config["confidence_boost_polite"]

            command = VoiceCommand(
                command_type=pattern.command_type,
                user_name=user_name,
                parameters={"pattern_desc": pattern.description},
                confidence=confidence,
                raw_text=original_text,
                normalized_text=text_lower,
                match_type=MatchType.LEARNED if learned_correction else MatchType.REGEX,
                match_details={"pattern": pattern.pattern, "match": match.group(0)},
                trace_id=trace_id,
            )

            logger.info(f"[{trace_id}] Regex match: {pattern.command_type.name} (conf={confidence:.2f})")
            await self._record_command(command)
            return command

        # =================================================================
        # PHASE 3: Fuzzy matching for STT error recovery
        # =================================================================

        logger.info(f"[{trace_id}] No regex match, trying fuzzy matching")

        # Try unlock intent
        is_unlock, unlock_conf, unlock_debug = await self._fuzzy_matcher.detect_intent(
            text_lower, CommandType.UNLOCK
        )

        if is_unlock:
            command = VoiceCommand(
                command_type=CommandType.UNLOCK,
                user_name=None,
                parameters={},
                confidence=unlock_conf,
                raw_text=original_text,
                normalized_text=unlock_debug.get("normalized", text_lower),
                match_type=MatchType.FUZZY,
                match_details=unlock_debug,
                trace_id=trace_id,
            )

            logger.info(f"[{trace_id}] Fuzzy match: UNLOCK (conf={unlock_conf:.2f})")
            logger.info(f"[{trace_id}] Strategies: {unlock_debug.get('strategies_matched', [])}")

            # Suggest learning this correction
            await self._learning_engine.suggest_correction(
                original_text,
                "unlock my screen"  # Canonical form
            )

            await self._record_command(command)
            return command

        # Try lock intent
        is_lock, lock_conf, lock_debug = await self._fuzzy_matcher.detect_intent(
            text_lower, CommandType.LOCK
        )

        if is_lock:
            command = VoiceCommand(
                command_type=CommandType.LOCK,
                user_name=None,
                parameters={},
                confidence=lock_conf,
                raw_text=original_text,
                normalized_text=lock_debug.get("normalized", text_lower),
                match_type=MatchType.FUZZY,
                match_details=lock_debug,
                trace_id=trace_id,
            )

            logger.info(f"[{trace_id}] Fuzzy match: LOCK (conf={lock_conf:.2f})")
            await self._record_command(command)
            return command

        # =================================================================
        # PHASE 4: No match found
        # =================================================================

        logger.warning(f"[{trace_id}] No command matched for: '{text_lower}'")
        logger.debug(f"[{trace_id}] Unlock confidence: {unlock_conf:.2f}, Lock confidence: {lock_conf:.2f}")

        return None

    async def _record_command(self, command: VoiceCommand):
        """Record command in history"""
        self._last_command = command
        self._command_history.append(command)

        # Keep history limited
        limit = self._config["command_history_limit"]
        if len(self._command_history) > limit:
            self._command_history = self._command_history[-limit:]

    async def generate_response(
        self,
        command: VoiceCommand,
        result: Dict[str, Any]
    ) -> str:
        """Generate JARVIS response based on command and result"""
        await self.initialize()

        context = {
            "user": result.get("user_id", command.user_name or "User"),
            "error": result.get("error", "Unknown error"),
        }

        if command.command_type == CommandType.UNLOCK:
            if result.get("authenticated"):
                if result.get("watch_nearby"):
                    return await self._response_generator.generate("unlock_success_watch", context)
                return await self._response_generator.generate("unlock_success", context)
            else:
                if "Apple Watch" in context["error"]:
                    base = await self._response_generator.generate("unlock_failed", context)
                    extra = await self._response_generator.generate("unlock_failed_watch", context)
                    return f"{base} {extra}"
                elif "voice" in context["error"].lower():
                    return await self._response_generator.generate("unlock_failed_voice", context)
                return await self._response_generator.generate("unlock_failed", context)

        elif command.command_type == CommandType.LOCK:
            if result.get("success"):
                return await self._response_generator.generate("lock_success", context)
            return await self._response_generator.generate("lock_failed", context)

        elif command.command_type == CommandType.STATUS:
            if result.get("is_locked"):
                return await self._response_generator.generate("status_locked", context)
            return await self._response_generator.generate("status_unlocked", context)

        elif command.command_type == CommandType.ENROLL:
            if result.get("success"):
                return await self._response_generator.generate("enroll_success", context)
            return await self._response_generator.generate("enroll_failed", context)

        elif command.command_type == CommandType.HELP:
            return await self._response_generator.generate("help", context)

        return "Command processed."

    async def confirm_command_success(self, command: VoiceCommand):
        """Confirm a command was successfully executed (for learning)"""
        if command.match_type == MatchType.FUZZY:
            # Boost learning confidence for successful fuzzy matches
            canonical = "unlock my screen" if command.command_type == CommandType.UNLOCK else "lock my screen"
            await self._learning_engine.confirm_correction(command.raw_text, canonical)

    async def validate_command_context(
        self,
        command: VoiceCommand,
        system_state: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate if command is appropriate for current system state.

        Returns:
            (is_valid, error_message)
        """
        if command.command_type == CommandType.UNLOCK:
            if not system_state.get("is_locked"):
                return False, "System is already unlocked"

        elif command.command_type == CommandType.LOCK:
            if system_state.get("is_locked"):
                return False, "System is already locked"

        elif command.command_type == CommandType.ENROLL:
            if system_state.get("is_locked"):
                return False, "Please unlock the system first"

        return True, None

    def get_command_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent command history"""
        return [cmd.to_dict() for cmd in self._command_history[-limit:]]

    @property
    def last_command(self) -> Optional[VoiceCommand]:
        """Get the last parsed command"""
        return self._last_command


# =============================================================================
# Module-level convenience functions
# =============================================================================

_handler_instance: Optional[JARVISCommandHandler] = None
_handler_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_command_handler() -> JARVISCommandHandler:
    """Get or create the singleton command handler instance"""
    global _handler_instance

    async with _handler_lock:
        if _handler_instance is None:
            _handler_instance = JARVISCommandHandler()
            await _handler_instance.initialize()

    return _handler_instance


async def parse_command(text: str) -> Optional[VoiceCommand]:
    """Convenience function to parse a command"""
    handler = await get_command_handler()
    return await handler.parse_command(text)


# =============================================================================
# Test Function
# =============================================================================

async def test_jarvis_commands():
    """Test JARVIS command parsing with various inputs"""
    handler = JARVISCommandHandler()
    await handler.initialize()

    test_phrases = [
        # Standard commands (should use regex)
        "Hey JARVIS, unlock my Mac",
        "JARVIS, this is Derek",
        "unlock my screen",
        "JARVIS, lock the computer",
        "JARVIS, what's the status?",
        "JARVIS, enroll user Sarah",

        # STT error variations (should use fuzzy)
        "Hey Jarvis, I'm Lach Ma's Green",  # "unlock my screen"
        "unlock scream",
        "unlucky screen",
        "jar vis unlock my max",
        "unblock my screen",
        "loch ma green",

        # Edge cases
        "hello jarvis please unlock my computer",
        "open sesame jarvis",
        "JARVIS I'm leaving",
    ]

    print("=" * 60)
    print("JARVIS Command Handler - Async Test Suite")
    print("=" * 60)

    for phrase in test_phrases:
        print(f"\nInput: '{phrase}'")
        command = await handler.parse_command(phrase)

        if command:
            print(f"  Type: {command.command_type.name}")
            print(f"  Confidence: {command.confidence:.2f}")
            print(f"  Match Type: {command.match_type.name}")
            if command.user_name:
                print(f"  User: {command.user_name}")
            if command.match_type == MatchType.FUZZY:
                strategies = command.match_details.get("strategies_matched", [])
                print(f"  Strategies: {strategies}")
        else:
            print("  No command detected")

    print("\n" + "=" * 60)
    print("Test complete!")


if __name__ == "__main__":
    asyncio.run(test_jarvis_commands())
