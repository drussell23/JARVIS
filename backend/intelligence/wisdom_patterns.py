"""
Wisdom Patterns System for JARVIS - Fabric-Inspired
====================================================

Implements a pattern-based system prompt library, inspired by Fabric.
Patterns are optimized system prompts for specific tasks that make
JARVIS sound like a senior consultant instead of generic ChatGPT.

Features:
- Load patterns from Fabric's patterns directory
- Dynamic pattern selection based on task type
- Pattern composition (combine multiple patterns)
- Custom pattern creation
- Pattern caching and indexing
- Integration with existing JARVIS systems

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Literal, Optional, Set, Tuple, Union
)
from uuid import uuid4

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# ============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_path(key: str, default: str) -> Path:
    return Path(os.path.expanduser(_get_env(key, default)))


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class WisdomPatternsConfig:
    """Configuration for the Wisdom Patterns system."""
    # Pattern sources
    fabric_patterns_dir: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_FABRIC_PATTERNS_DIR",
            "~/reference_repos/fabric/data/patterns"
        )
    )
    custom_patterns_dir: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_CUSTOM_PATTERNS_DIR",
            "~/.jarvis/patterns"
        )
    )

    # Cache settings
    cache_patterns: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_CACHE_PATTERNS", True)
    )

    # Pattern behavior
    auto_select_pattern: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_AUTO_SELECT_PATTERN", True)
    )
    fallback_to_default: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_PATTERN_FALLBACK", True)
    )


# ============================================================================
# Enums
# ============================================================================

class PatternCategory(str, Enum):
    """Categories of wisdom patterns."""
    ANALYSIS = "analysis"           # Analyze content, data, code
    EXTRACTION = "extraction"       # Extract insights, wisdom, facts
    CREATION = "creation"           # Create content, code, plans
    TRANSFORMATION = "transformation"  # Transform, summarize, rewrite
    EVALUATION = "evaluation"       # Evaluate, critique, review
    COMMUNICATION = "communication" # Explain, present, teach
    SECURITY = "security"           # Security analysis, threat modeling
    CODING = "coding"               # Code-related tasks
    RESEARCH = "research"           # Research, investigation


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class WisdomPattern:
    """
    A wisdom pattern - an optimized system prompt for a specific task.

    Structure (from Fabric):
    - IDENTITY AND PURPOSE: Who the AI is for this task
    - STEPS: What the AI should do
    - OUTPUT INSTRUCTIONS: How to format the output
    """
    name: str
    category: PatternCategory
    system_prompt: str
    user_prompt_template: Optional[str] = None
    description: str = ""
    tags: List[str] = field(default_factory=list)
    source: str = "custom"
    examples: List[Dict[str, str]] = field(default_factory=list)

    # Computed
    identity: str = ""
    steps: List[str] = field(default_factory=list)
    output_instructions: List[str] = field(default_factory=list)

    def __post_init__(self):
        """Parse the system prompt to extract sections."""
        self._parse_system_prompt()

    def _parse_system_prompt(self):
        """Parse the system prompt into structured sections."""
        # Extract IDENTITY AND PURPOSE
        identity_match = re.search(
            r"#\s*IDENTITY\s+(?:AND|&)\s*PURPOSE\s*\n(.*?)(?=\n#|\Z)",
            self.system_prompt,
            re.IGNORECASE | re.DOTALL
        )
        if identity_match:
            self.identity = identity_match.group(1).strip()

        # Extract STEPS
        steps_match = re.search(
            r"#\s*STEPS\s*\n(.*?)(?=\n#|\Z)",
            self.system_prompt,
            re.IGNORECASE | re.DOTALL
        )
        if steps_match:
            steps_text = steps_match.group(1).strip()
            self.steps = [
                s.strip().lstrip("- ").lstrip("* ")
                for s in steps_text.split("\n")
                if s.strip() and not s.strip().startswith("#")
            ]

        # Extract OUTPUT INSTRUCTIONS
        output_match = re.search(
            r"#\s*OUTPUT\s+INSTRUCTIONS\s*\n(.*?)(?=\n#|\Z)",
            self.system_prompt,
            re.IGNORECASE | re.DOTALL
        )
        if output_match:
            output_text = output_match.group(1).strip()
            self.output_instructions = [
                s.strip().lstrip("- ").lstrip("* ")
                for s in output_text.split("\n")
                if s.strip() and not s.strip().startswith("#")
            ]

    def render(self, input_text: str = "") -> str:
        """Render the pattern as a complete prompt."""
        prompt = self.system_prompt

        # Replace INPUT placeholder if present
        if "INPUT:" in prompt:
            prompt = prompt.replace("INPUT:", f"INPUT:\n{input_text}")
        elif input_text:
            prompt = f"{prompt}\n\n# INPUT\n{input_text}"

        return prompt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "tags": self.tags,
            "source": self.source,
            "identity": self.identity,
            "steps": self.steps,
            "output_instructions": self.output_instructions,
        }


@dataclass
class PatternMatch:
    """Result of pattern matching."""
    pattern: WisdomPattern
    confidence: float
    reason: str


# ============================================================================
# Pattern Registry
# ============================================================================

class WisdomPatternRegistry:
    """
    Registry of wisdom patterns.

    Loads patterns from Fabric and custom directories,
    indexes them for quick lookup, and provides pattern selection.
    """

    # Pattern name to category mapping for common Fabric patterns
    CATEGORY_HINTS = {
        "analyze_": PatternCategory.ANALYSIS,
        "extract_": PatternCategory.EXTRACTION,
        "create_": PatternCategory.CREATION,
        "summarize": PatternCategory.TRANSFORMATION,
        "explain": PatternCategory.COMMUNICATION,
        "review_": PatternCategory.EVALUATION,
        "security": PatternCategory.SECURITY,
        "code": PatternCategory.CODING,
        "coding": PatternCategory.CODING,
        "research": PatternCategory.RESEARCH,
        "threat": PatternCategory.SECURITY,
        "wisdom": PatternCategory.EXTRACTION,
    }

    def __init__(self, config: Optional[WisdomPatternsConfig] = None):
        self.config = config or WisdomPatternsConfig()
        self._patterns: Dict[str, WisdomPattern] = {}
        self._by_category: Dict[PatternCategory, List[str]] = {c: [] for c in PatternCategory}
        self._by_tag: Dict[str, List[str]] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Load and index all patterns."""
        async with self._lock:
            if self._initialized:
                return

            # Load from Fabric
            if self.config.fabric_patterns_dir.exists():
                await self._load_fabric_patterns()

            # Load custom patterns
            if self.config.custom_patterns_dir.exists():
                await self._load_custom_patterns()

            # Add JARVIS-specific patterns
            self._add_jarvis_patterns()

            self._initialized = True
            logger.info(f"Wisdom Patterns initialized: {len(self._patterns)} patterns loaded")

    async def _load_fabric_patterns(self):
        """Load patterns from Fabric's patterns directory."""
        patterns_dir = self.config.fabric_patterns_dir

        for pattern_dir in patterns_dir.iterdir():
            if not pattern_dir.is_dir():
                continue

            system_file = pattern_dir / "system.md"
            if not system_file.exists():
                continue

            try:
                system_prompt = system_file.read_text(encoding="utf-8")
                pattern_name = pattern_dir.name

                # Determine category from name
                category = PatternCategory.ANALYSIS  # Default
                for hint, cat in self.CATEGORY_HINTS.items():
                    if hint in pattern_name.lower():
                        category = cat
                        break

                # Load user prompt if exists
                user_file = pattern_dir / "user.md"
                user_prompt = user_file.read_text(encoding="utf-8") if user_file.exists() else None

                pattern = WisdomPattern(
                    name=pattern_name,
                    category=category,
                    system_prompt=system_prompt,
                    user_prompt_template=user_prompt,
                    description=f"Fabric pattern: {pattern_name}",
                    source="fabric",
                    tags=[category.value, "fabric"],
                )

                self._register_pattern(pattern)

            except Exception as e:
                logger.debug(f"Failed to load pattern {pattern_dir.name}: {e}")

    async def _load_custom_patterns(self):
        """Load custom JARVIS patterns."""
        custom_dir = self.config.custom_patterns_dir

        for pattern_file in custom_dir.glob("*.md"):
            try:
                content = pattern_file.read_text(encoding="utf-8")
                pattern_name = pattern_file.stem

                pattern = WisdomPattern(
                    name=pattern_name,
                    category=PatternCategory.ANALYSIS,
                    system_prompt=content,
                    source="custom",
                    tags=["custom"],
                )

                self._register_pattern(pattern)

            except Exception as e:
                logger.debug(f"Failed to load custom pattern {pattern_file.name}: {e}")

    def _add_jarvis_patterns(self):
        """Add JARVIS-specific wisdom patterns."""
        jarvis_patterns = [
            WisdomPattern(
                name="jarvis_code_review",
                category=PatternCategory.EVALUATION,
                system_prompt="""# IDENTITY AND PURPOSE

You are JARVIS, an elite code reviewer with deep expertise in software engineering best practices. You review code with the precision of a senior architect and the clarity of an exceptional teacher.

Take a deep breath and think step-by-step about how to best accomplish this goal.

# STEPS

- Read the entire code carefully, understanding its purpose and architecture
- Identify security vulnerabilities (OWASP Top 10, injection, XSS, etc.)
- Check for performance issues and optimization opportunities
- Evaluate code clarity, naming, and documentation
- Assess test coverage and testability
- Look for code smells and anti-patterns
- Consider maintainability and extensibility

# OUTPUT INSTRUCTIONS

- Output a SUMMARY section with 2-3 sentences about the code
- Output a SECURITY section with any security concerns
- Output a PERFORMANCE section with optimization suggestions
- Output a QUALITY section with code quality observations
- Output a RECOMMENDATIONS section with prioritized action items
- Each bullet should be exactly 16 words
- Do not include generic praise - be specific and actionable

# INPUT

INPUT:
""",
                description="Deep code review with security, performance, and quality analysis",
                tags=["jarvis", "coding", "review"],
            ),
            WisdomPattern(
                name="jarvis_architecture_design",
                category=PatternCategory.CREATION,
                system_prompt="""# IDENTITY AND PURPOSE

You are JARVIS, a senior software architect with expertise in distributed systems, microservices, and modern cloud architecture. You design systems that are scalable, maintainable, and elegant.

Take a deep breath and think step-by-step about how to best accomplish this goal.

# STEPS

- Understand the requirements and constraints
- Identify key components and their responsibilities
- Design data flow and communication patterns
- Consider scalability, reliability, and security
- Propose technology choices with justification
- Identify potential risks and mitigations
- Create clear documentation

# OUTPUT INSTRUCTIONS

- Output a REQUIREMENTS section summarizing key requirements
- Output an ARCHITECTURE section with the system design
- Output a COMPONENTS section listing major components
- Output a DATA FLOW section describing how data moves
- Output a TECHNOLOGY section with recommended tech stack
- Output a RISKS section with potential issues and mitigations
- Include ASCII diagrams where helpful
- Be specific about trade-offs

# INPUT

INPUT:
""",
                description="Design scalable, maintainable system architectures",
                tags=["jarvis", "architecture", "design"],
            ),
            WisdomPattern(
                name="jarvis_debug_assistant",
                category=PatternCategory.ANALYSIS,
                system_prompt="""# IDENTITY AND PURPOSE

You are JARVIS, an expert debugging assistant with deep knowledge of runtime errors, stack traces, and system diagnostics. You approach problems systematically and find root causes efficiently.

Take a deep breath and think step-by-step about how to best accomplish this goal.

# STEPS

- Analyze the error message and stack trace carefully
- Identify the immediate cause vs root cause
- Look for patterns that suggest the type of issue
- Consider environmental factors (versions, configs, state)
- Generate hypotheses in order of likelihood
- Suggest specific diagnostic steps
- Propose targeted fixes

# OUTPUT INSTRUCTIONS

- Output a DIAGNOSIS section with the likely cause
- Output a ROOT CAUSE section explaining the underlying issue
- Output a HYPOTHESES section with ranked possibilities
- Output a DIAGNOSTIC STEPS section with commands to run
- Output a FIX section with specific code changes
- Output a PREVENTION section on avoiding this in the future
- Be direct and actionable - no fluff

# INPUT

INPUT:
""",
                description="Systematic debugging with root cause analysis",
                tags=["jarvis", "debugging", "analysis"],
            ),
            WisdomPattern(
                name="jarvis_voice_auth_reasoning",
                category=PatternCategory.SECURITY,
                system_prompt="""# IDENTITY AND PURPOSE

You are JARVIS, a voice biometric authentication system with advanced reasoning capabilities. You make authentication decisions by weighing multiple factors including voice similarity, behavioral patterns, and contextual information.

Take a deep breath and think step-by-step about how to best accomplish this goal.

# STEPS

- Analyze the voice biometric confidence score
- Evaluate behavioral context (time, location, device)
- Check for anomaly indicators (spoofing, replay attacks)
- Consider environmental factors (noise, microphone quality)
- Fuse multiple factors using appropriate weights
- Make a confident decision with clear reasoning
- Provide user-friendly feedback

# OUTPUT INSTRUCTIONS

- Output a VOICE ANALYSIS section with confidence breakdown
- Output a BEHAVIORAL CONTEXT section with pattern matching
- Output a SECURITY CHECK section with anomaly detection
- Output a DECISION section with authenticate/challenge/deny
- Output a REASONING section explaining the decision
- Output a USER FEEDBACK section with what to tell the user
- Be transparent about uncertainty

# INPUT

INPUT:
""",
                description="Voice authentication reasoning for borderline cases",
                tags=["jarvis", "security", "voice", "authentication"],
            ),
        ]

        for pattern in jarvis_patterns:
            self._register_pattern(pattern)

    def _register_pattern(self, pattern: WisdomPattern):
        """Register a pattern in the registry."""
        self._patterns[pattern.name] = pattern
        self._by_category[pattern.category].append(pattern.name)

        for tag in pattern.tags:
            if tag not in self._by_tag:
                self._by_tag[tag] = []
            self._by_tag[tag].append(pattern.name)

    def get_pattern(self, name: str) -> Optional[WisdomPattern]:
        """Get a pattern by name."""
        return self._patterns.get(name)

    def get_patterns_by_category(self, category: PatternCategory) -> List[WisdomPattern]:
        """Get all patterns in a category."""
        return [self._patterns[n] for n in self._by_category.get(category, [])]

    def get_patterns_by_tag(self, tag: str) -> List[WisdomPattern]:
        """Get all patterns with a specific tag."""
        return [self._patterns[n] for n in self._by_tag.get(tag, [])]

    def search_patterns(self, query: str) -> List[PatternMatch]:
        """Search for patterns matching a query."""
        query_lower = query.lower()
        matches = []

        for name, pattern in self._patterns.items():
            score = 0.0
            reasons = []

            # Name match
            if query_lower in name.lower():
                score += 0.5
                reasons.append("name match")

            # Description match
            if query_lower in pattern.description.lower():
                score += 0.3
                reasons.append("description match")

            # Identity match
            if query_lower in pattern.identity.lower():
                score += 0.4
                reasons.append("identity match")

            # Tag match
            for tag in pattern.tags:
                if query_lower in tag.lower():
                    score += 0.2
                    reasons.append(f"tag: {tag}")

            if score > 0:
                matches.append(PatternMatch(
                    pattern=pattern,
                    confidence=min(1.0, score),
                    reason=", ".join(reasons),
                ))

        return sorted(matches, key=lambda m: -m.confidence)

    def suggest_pattern(self, task_description: str) -> Optional[PatternMatch]:
        """Suggest the best pattern for a task."""
        matches = self.search_patterns(task_description)

        # Also check for keyword matches
        keywords = {
            "review": "jarvis_code_review",
            "analyze": "analyze_paper",
            "extract": "extract_wisdom",
            "summarize": "summarize",
            "debug": "jarvis_debug_assistant",
            "architecture": "jarvis_architecture_design",
            "security": "analyze_threat",
            "voice": "jarvis_voice_auth_reasoning",
        }

        task_lower = task_description.lower()
        for keyword, pattern_name in keywords.items():
            if keyword in task_lower:
                pattern = self.get_pattern(pattern_name)
                if pattern:
                    return PatternMatch(
                        pattern=pattern,
                        confidence=0.8,
                        reason=f"keyword match: {keyword}",
                    )

        return matches[0] if matches else None

    def list_patterns(self) -> List[Dict[str, Any]]:
        """List all available patterns."""
        return [p.to_dict() for p in self._patterns.values()]


# ============================================================================
# Wisdom Agent (Uses Patterns for Enhanced Reasoning)
# ============================================================================

class WisdomAgent:
    """
    Agent that uses wisdom patterns to enhance JARVIS's reasoning.

    Automatically selects and applies appropriate patterns based on
    the task at hand.
    """

    def __init__(
        self,
        registry: Optional[WisdomPatternRegistry] = None,
        config: Optional[WisdomPatternsConfig] = None,
    ):
        self.config = config or WisdomPatternsConfig()
        self.registry = registry or WisdomPatternRegistry(self.config)

    async def initialize(self):
        """Initialize the wisdom agent."""
        await self.registry.initialize()

    async def enhance_prompt(
        self,
        task: str,
        pattern_name: Optional[str] = None,
        input_text: str = "",
    ) -> str:
        """
        Enhance a task with a wisdom pattern.

        Args:
            task: The task description
            pattern_name: Specific pattern to use, or None for auto-select
            input_text: Input text to process

        Returns:
            Enhanced prompt with wisdom pattern applied
        """
        await self.initialize()

        # Get pattern
        pattern = None
        if pattern_name:
            pattern = self.registry.get_pattern(pattern_name)

        if not pattern and self.config.auto_select_pattern:
            match = self.registry.suggest_pattern(task)
            if match and match.confidence > 0.5:
                pattern = match.pattern
                logger.info(f"Auto-selected pattern: {pattern.name} ({match.reason})")

        if not pattern:
            if self.config.fallback_to_default:
                # Use a generic analysis pattern
                pattern = self.registry.get_pattern("analyze_paper")
            if not pattern:
                return f"{task}\n\n{input_text}" if input_text else task

        # Render the pattern
        return pattern.render(input_text or task)

    async def get_pattern_for_task(self, task: str) -> Optional[WisdomPattern]:
        """Get the suggested pattern for a task."""
        await self.initialize()
        match = self.registry.suggest_pattern(task)
        return match.pattern if match else None


# ============================================================================
# Singleton and Convenience Functions
# ============================================================================

_registry_instance: Optional[WisdomPatternRegistry] = None
_agent_instance: Optional[WisdomAgent] = None


async def get_pattern_registry() -> WisdomPatternRegistry:
    """Get the singleton pattern registry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = WisdomPatternRegistry()
        await _registry_instance.initialize()
    return _registry_instance


async def get_wisdom_agent() -> WisdomAgent:
    """Get the singleton wisdom agent instance."""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = WisdomAgent()
        await _agent_instance.initialize()
    return _agent_instance


async def enhance_with_wisdom(
    task: str,
    pattern_name: Optional[str] = None,
    input_text: str = "",
) -> str:
    """Enhance a task with a wisdom pattern."""
    agent = await get_wisdom_agent()
    return await agent.enhance_prompt(task, pattern_name, input_text)


async def get_pattern(name: str) -> Optional[WisdomPattern]:
    """Get a pattern by name."""
    registry = await get_pattern_registry()
    return registry.get_pattern(name)


async def suggest_pattern(task: str) -> Optional[PatternMatch]:
    """Suggest a pattern for a task."""
    registry = await get_pattern_registry()
    return registry.suggest_pattern(task)


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Configuration
    "WisdomPatternsConfig",

    # Enums
    "PatternCategory",

    # Data Classes
    "WisdomPattern",
    "PatternMatch",

    # Core Classes
    "WisdomPatternRegistry",
    "WisdomAgent",

    # Convenience Functions
    "get_pattern_registry",
    "get_wisdom_agent",
    "enhance_with_wisdom",
    "get_pattern",
    "suggest_pattern",
]
