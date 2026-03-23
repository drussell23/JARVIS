"""
JARVIS Workflow Parser - Multi-Command Decomposition Engine
Parses complex natural language commands into executable workflow steps

Pillar 2 (Progressive Awakening): parse_command() provides instant local parsing
via reflex-arc regex. parse_command_async() enhances with agentic DAG planning
via J-Prime when the Mind is available.

Pillar 5 (Intelligence-Driven Routing): parse_command_async() delegates full
command decomposition to PrimeRouter, letting the AI semantically understand
intent, dependencies, and context propagation.
"""

import asyncio
import json
import os
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions that can be performed in workflows"""
    UNLOCK = "unlock"
    OPEN_APP = "open_app"
    SEARCH = "search"
    NAVIGATE = "navigate"
    CREATE = "create"
    CHECK = "check"
    SET = "set"
    MUTE = "mute"
    START = "start"
    STOP = "stop"
    READ = "read"
    WRITE = "write"
    SEND = "send"
    SCHEDULE = "schedule"
    ANALYZE = "analyze"
    ORGANIZE = "organize"
    MONITOR = "monitor"
    PREPARE = "prepare"
    UNKNOWN = "unknown"


@dataclass
class WorkflowAction:
    """Represents a single action in a workflow"""
    action_type: ActionType
    target: str  # What to act upon (app name, file, etc.)
    parameters: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[int] = field(default_factory=list)  # Indices of actions that must complete first
    optional: bool = False
    timeout: int = 30  # seconds
    retry_count: int = 3
    description: str = ""
    

@dataclass 
class Workflow:
    """Represents a complete workflow with multiple actions"""
    original_command: str
    actions: List[WorkflowAction]
    context: Dict[str, Any] = field(default_factory=dict)
    estimated_duration: int = 0  # seconds
    complexity: str = "simple"  # simple, moderate, complex
    confidence: float = 0.0


class WorkflowParser:
    """Parses natural language commands into executable workflows"""
    
    # Command patterns for different action types
    ACTION_PATTERNS = {
        ActionType.UNLOCK: [
            r"unlock(?:\s+(?:my|the))?\s*(?:screen|computer|mac|device)",
            r"sign\s*(?:me\s*)?in",
            r"log\s*(?:me\s*)?in"
        ],
        ActionType.OPEN_APP: [
            r"open\s+(\w+(?:\s+\w+)*)",
            r"launch\s+(\w+(?:\s+\w+)*)",
            r"start\s+(\w+(?:\s+\w+)*)",
            r"run\s+(\w+(?:\s+\w+)*)"
        ],
        ActionType.NAVIGATE: [
            r"(?:go|navigate)\s+to\s+(.+)",
            r"visit\s+(.+)",
            r"browse\s+to\s+(.+)"
        ],
        ActionType.SEARCH: [
            r"search\s+(?:for\s+)?['\"]?(.+?)(?:['\"]|\s+on\s+(\w+)|$)",
            r"look\s+(?:up\s+)?(?:for\s+)?['\"]?(.+?)(?:['\"]|$)",
            r"find\s+(?:me\s+)?['\"]?(.+?)(?:['\"]|$)"
        ],
        ActionType.CHECK: [
            r"check\s+(?:my\s+)?(\w+(?:\s+\w+)*)",
            r"review\s+(?:my\s+)?(\w+(?:\s+\w+)*)",
            r"show\s+(?:me\s+)?(?:my\s+)?(\w+(?:\s+\w+)*)"
        ],
        ActionType.CREATE: [
            r"create\s+(?:a\s+)?(?:new\s+)?(\w+(?:\s+\w+)*)",
            r"make\s+(?:a\s+)?(?:new\s+)?(\w+(?:\s+\w+)*)",
            r"set\s*up\s+(?:a\s+)?(?:new\s+)?(\w+(?:\s+\w+)*)"
        ],
        ActionType.MUTE: [
            r"mute\s+(?:all\s+)?(\w+)?",
            r"silence\s+(?:all\s+)?(\w+)?",
            r"turn\s+off\s+(\w+)"
        ],
        ActionType.PREPARE: [
            r"prepare\s+(?:for\s+)?(?:my\s+)?(.+)",
            r"get\s+ready\s+(?:for\s+)?(?:my\s+)?(.+)",
            r"set\s*up\s+(?:for\s+)?(?:my\s+)?(.+)"
        ]
    }
    
    # Connector words that indicate multiple commands
    CONNECTORS = [
        "and", "then", "after that", "next", "also", "plus",
        "followed by", "afterwards", "subsequently", "finally",
        "before", "after", "while", "during", "once"
    ]
    
    # App name mappings
    # v283.3: Default browser is config-driven (was hardcoded "Safari")
    _DEFAULT_BROWSER = os.getenv("JARVIS_DEFAULT_BROWSER", "Google Chrome")
    APP_MAPPINGS = {
        "browser": _DEFAULT_BROWSER,
        "web browser": _DEFAULT_BROWSER,
        "internet": _DEFAULT_BROWSER,
        "mail": "Mail",
        "email": "Mail",
        "calendar": "Calendar",
        "schedule": "Calendar",
        "word": "Microsoft Word",
        "excel": "Microsoft Excel",
        "powerpoint": "Microsoft PowerPoint",
        "slack": "Slack",
        "teams": "Microsoft Teams",
        "zoom": "Zoom",
        "notes": "Notes",
        "messages": "Messages",
        "finder": "Finder"
    }
    
    def __init__(self):
        """Initialize the workflow parser"""
        self.compiled_patterns = self._compile_patterns()
        
    def _compile_patterns(self) -> Dict[ActionType, List]:
        """Compile regex patterns for better performance"""
        compiled = {}
        for action_type, patterns in self.ACTION_PATTERNS.items():
            compiled[action_type] = [re.compile(p, re.IGNORECASE) for p in patterns]
        return compiled
        
    # System prompt for agentic command decomposition (Pillar 5)
    _DECOMPOSE_SYSTEM_PROMPT = (
        "You are a command decomposition engine for a macOS desktop assistant. "
        "Given a natural language command, decompose it into an ordered list of actions.\n\n"
        "Each action has:\n"
        '- "action_type": one of: unlock, open_app, search, navigate, create, check, '
        "set, mute, start, stop, read, write, send, schedule, analyze, organize, monitor, prepare\n"
        '- "target": what to act upon (app name, service name, file, etc.)\n'
        '- "parameters": dict of extra params (e.g. {"query": "NBA", "platform": "youtube"})\n'
        '- "dependencies": list of action indices this depends on (0-indexed)\n'
        '- "description": human-readable description\n\n'
        "IMPORTANT: If an open/launch action precedes a search, the search should:\n"
        "1. Depend on the open action (add its index to dependencies)\n"
        "2. Set platform parameter to the opened target\n\n"
        "Respond ONLY with valid JSON array, no markdown:\n"
        '[{"action_type": "open_app", "target": "youtube", "parameters": {}, '
        '"dependencies": [], "description": "Open YouTube"}]\n'
    )

    def parse_command(self, command: str) -> Workflow:
        """Sync parse — reflex-arc decomposition (Pillar 2: instant local capability).

        Uses regex patterns for instant parsing. For agentic decomposition
        with AI-powered DAG planning, use parse_command_async() instead.
        """
        logger.info(f"Parsing multi-command (reflex): '{command}'")

        command = self._normalize_command(command)
        sub_commands = self._split_commands(command)
        logger.info(f"Split into {len(sub_commands)} sub-commands")

        actions = []
        for i, sub_cmd in enumerate(sub_commands):
            action = self._parse_single_command(sub_cmd, i)
            if action:
                actions.append(action)

        self._analyze_dependencies(actions)

        workflow = Workflow(
            original_command=command,
            actions=actions,
            complexity=self._determine_complexity(actions),
            estimated_duration=self._estimate_duration(actions),
            confidence=self._calculate_confidence(actions)
        )

        logger.info(f"Created workflow with {len(actions)} actions, complexity: {workflow.complexity}")
        return workflow

    async def parse_command_async(self, command: str) -> Workflow:
        """Async parse — agentic decomposition via J-Prime (Pillar 5).

        Asks the Mind to semantically decompose the command into a DAG.
        Falls back to reflex-arc (sync parse) if the Mind is unavailable.
        """
        logger.info(f"Parsing multi-command (agentic): '{command}'")

        try:
            from backend.core.prime_router import get_prime_router
            router = await get_prime_router()

            response = await router.generate(
                prompt=f'Decompose this command into actions: "{command}"',
                system_prompt=self._DECOMPOSE_SYSTEM_PROMPT,
                max_tokens=1024,
                temperature=0.0,
                deadline=asyncio.get_event_loop().time() + 5.0,
            )

            raw_actions = json.loads(response.content)
            actions = self._actions_from_ai(raw_actions)

            if actions:
                workflow = Workflow(
                    original_command=command,
                    actions=actions,
                    complexity=self._determine_complexity(actions),
                    estimated_duration=self._estimate_duration(actions),
                    confidence=0.95,
                )
                logger.info(
                    f"AI decomposed '{command}' into {len(actions)} actions "
                    f"(source={response.source}, latency={response.latency_ms:.0f}ms)"
                )
                return workflow

        except Exception as e:
            logger.warning(f"Agentic decomposition failed, falling back to reflex: {e}")

        # Graceful degradation: reflex-arc fallback
        return self.parse_command(command)

    def _actions_from_ai(self, raw_actions: List[Dict[str, Any]]) -> List[WorkflowAction]:
        """Convert AI-generated action dicts into WorkflowAction objects."""
        actions = []
        for raw in raw_actions:
            try:
                action_type_str = raw.get("action_type", "unknown")
                try:
                    action_type = ActionType(action_type_str)
                except ValueError:
                    action_type = ActionType.UNKNOWN

                actions.append(WorkflowAction(
                    action_type=action_type,
                    target=raw.get("target", ""),
                    parameters=raw.get("parameters", {}),
                    dependencies=raw.get("dependencies", []),
                    description=raw.get("description", ""),
                ))
            except Exception as e:
                logger.warning(f"Skipping malformed AI action: {raw} ({e})")
        return actions
        
    def _normalize_command(self, command: str) -> str:
        """Normalize command text for parsing"""
        # Remove extra whitespace
        command = re.sub(r'\s+', ' ', command.strip())
        
        # Expand common contractions
        contractions = {
            "don't": "do not",
            "won't": "will not",
            "can't": "cannot",
            "let's": "let us",
            "I'll": "I will",
            "I'm": "I am",
            "I'd": "I would",
            "I've": "I have"
        }
        for contraction, expanded in contractions.items():
            command = command.replace(contraction, expanded)
            
        return command
        
    def _split_commands(self, command: str) -> List[str]:
        """Split command into sub-commands based on commas and connectors"""
        # First split by commas
        comma_parts = [p.strip() for p in command.split(',')]
        
        # Create regex pattern for connectors
        connector_pattern = r'\s+(?:' + '|'.join(self.CONNECTORS) + r')\s+'
        
        # Then split each comma-separated part by connectors
        sub_commands = []
        for part in comma_parts:
            # Split by connectors
            connector_parts = re.split(connector_pattern, part, flags=re.IGNORECASE)
            
            # Add non-empty parts
            for sub_part in connector_parts:
                sub_part = sub_part.strip()
                if sub_part and len(sub_part) > 3:  # Filter out tiny fragments
                    sub_commands.append(sub_part)
        
        # If no splits found, treat as single command
        if not sub_commands:
            sub_commands = [command]
            
        return sub_commands
        
    def _parse_single_command(self, command: str, index: int) -> Optional[WorkflowAction]:
        """Parse a single command into an action"""
        command = command.strip()
        if not command:
            return None
            
        # Try to match against each action type
        for action_type, patterns in self.compiled_patterns.items():
            for pattern in patterns:
                match = pattern.search(command)
                if match:
                    return self._create_action(action_type, command, match, index)
                    
        # If no pattern matches, try to infer from keywords
        return self._infer_action(command, index)
        
    def _create_action(self, action_type: ActionType, command: str, 
                      match: re.Match, index: int) -> WorkflowAction:
        """Create an action from a regex match"""
        # Extract target and parameters based on action type
        target = ""
        parameters = {}
        
        if action_type == ActionType.UNLOCK:
            target = "system"
            
        elif action_type == ActionType.OPEN_APP:
            target = match.group(1) if match.groups() else ""
            # Map common names to actual app names
            target = self.APP_MAPPINGS.get(target.lower(), target)
            
        elif action_type == ActionType.SEARCH:
            groups = match.groups()
            if groups:
                parameters["query"] = groups[0]
                if len(groups) > 1 and groups[1]:
                    parameters["platform"] = groups[1]
                target = parameters.get("platform", "web")

        elif action_type == ActionType.NAVIGATE:
            target = match.group(1).strip() if match.groups() and match.group(1) else ""
            parameters["destination"] = target
                
        elif action_type == ActionType.CHECK:
            target = match.group(1) if match.groups() else ""
            
        elif action_type == ActionType.CREATE:
            target = match.group(1) if match.groups() else ""
            
        elif action_type == ActionType.MUTE:
            target = match.group(1) if match.groups() else "notifications"
            
        elif action_type == ActionType.PREPARE:
            target = match.group(1) if match.groups() else ""
            
        # Create the action
        return WorkflowAction(
            action_type=action_type,
            target=target,
            parameters=parameters,
            description=f"{action_type.value} {target}".strip()
        )
        
    def _infer_action(self, command: str, index: int) -> Optional[WorkflowAction]:
        """Try to infer action from keywords when no pattern matches"""
        # Keywords that suggest actions
        keyword_actions = {
            "weather": (ActionType.CHECK, "weather"),
            "time": (ActionType.CHECK, "time"),
            "date": (ActionType.CHECK, "date"),
            "email": (ActionType.CHECK, "email"),
            "calendar": (ActionType.CHECK, "calendar"),
            "meeting": (ActionType.PREPARE, "meeting"),
            "document": (ActionType.CREATE, "document"),
            "file": (ActionType.OPEN_APP, "Finder"),
            "repo": (ActionType.NAVIGATE, "repository"),
            "repository": (ActionType.NAVIGATE, "repository"),
            "music": (ActionType.OPEN_APP, "Music"),
            "video": (ActionType.OPEN_APP, "TV"),
            "photo": (ActionType.OPEN_APP, "Photos")
        }
        
        # Check for keywords
        lower_command = command.lower()
        for keyword, (action_type, target) in keyword_actions.items():
            if keyword in lower_command:
                return WorkflowAction(
                    action_type=action_type,
                    target=target,
                    description=command
                )
                
        # v263.1: Default to unknown action, marked optional so it doesn't
        # halt the workflow if the fallback executor can't interpret it
        return WorkflowAction(
            action_type=ActionType.UNKNOWN,
            target=command,
            description=command,
            optional=True,
        )
        
    def _analyze_dependencies(self, actions: List[WorkflowAction]):
        """Analyze and set dependencies between actions.

        Also propagates context: if a SEARCH follows an OPEN_APP and has no
        explicit platform, the search inherits the opened app/service as its
        platform so "open youtube and search for NBA" searches ON youtube.
        """
        for i, action in enumerate(actions):
            # Unlock must happen before anything else
            if i > 0 and actions[0].action_type == ActionType.UNLOCK:
                action.dependencies.append(0)

            # Opening an app before searching in it
            if action.action_type == ActionType.SEARCH:
                explicit_platform = action.parameters.get("platform", "")
                # Look for the nearest preceding OPEN_APP
                for j in range(i - 1, -1, -1):
                    if actions[j].action_type != ActionType.OPEN_APP:
                        continue

                    open_target = actions[j].target.lower()

                    # Explicit platform match
                    if explicit_platform and explicit_platform.lower() in open_target:
                        action.dependencies.append(j)
                        break

                    # Context propagation: no explicit platform (defaulted to "web")
                    # and there's a preceding OPEN_APP → inherit it as the platform
                    if not explicit_platform or explicit_platform.lower() == "web":
                        action.parameters["platform"] = actions[j].target
                        action.dependencies.append(j)
                        logger.info(
                            f"Propagated search platform from action {j} "
                            f"('{actions[j].target}') to search action {i}"
                        )
                        break

            # Creating something requires app to be open
            if action.action_type == ActionType.CREATE:
                for j in range(i):
                    if actions[j].action_type == ActionType.OPEN_APP:
                        action.dependencies.append(j)
                        break
                        
    def _determine_complexity(self, actions: List[WorkflowAction]) -> str:
        """Determine workflow complexity"""
        num_actions = len(actions)
        
        if num_actions <= 2:
            return "simple"
        elif num_actions <= 5:
            return "moderate"
        else:
            return "complex"
            
    def _estimate_duration(self, actions: List[WorkflowAction]) -> int:
        """Estimate total workflow duration in seconds"""
        # Base estimates for each action type
        action_durations = {
            ActionType.UNLOCK: 3,
            ActionType.OPEN_APP: 2,
            ActionType.SEARCH: 5,
            ActionType.NAVIGATE: 3,
            ActionType.CREATE: 5,
            ActionType.CHECK: 3,
            ActionType.SET: 2,
            ActionType.MUTE: 1,
            ActionType.START: 2,
            ActionType.STOP: 1,
            ActionType.PREPARE: 10,
            ActionType.UNKNOWN: 5
        }
        
        total = 0
        for action in actions:
            total += action_durations.get(action.action_type, 5)
            
        return total
        
    def _calculate_confidence(self, actions: List[WorkflowAction]) -> float:
        """Calculate confidence in the parsed workflow"""
        if not actions:
            return 0.0
            
        # Count unknown actions
        unknown_count = sum(1 for a in actions if a.action_type == ActionType.UNKNOWN)
        
        # Base confidence
        confidence = 1.0 - (unknown_count / len(actions))
        
        # Adjust based on complexity
        if len(actions) > 5:
            confidence *= 0.9
            
        return min(max(confidence, 0.0), 1.0)
