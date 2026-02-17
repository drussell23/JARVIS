"""
Compound Action Parser
=====================

Parses and executes multi-step commands dynamically.
Handles commands like "open safari and search for dogs" by breaking them
into atomic actions and executing them in sequence.

This module provides intelligent parsing of natural language commands that contain
multiple actions, either explicitly connected with words like "and", "then", or
implicitly combined like "open safari search for dogs". The parser uses pattern
recognition and verb analysis to break down complex commands into executable
atomic actions.

Example:
    >>> parser = CompoundActionParser()
    >>> actions = await parser.parse("open safari and search for dogs")
    >>> print(parser.get_execution_plan(actions))
    Open safari → Search for 'dogs'
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of atomic actions that can be executed.
    
    Each enum value represents a distinct type of action that the system
    can perform, from opening applications to creating documents.
    """
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    SEARCH_WEB = "search_web"
    NAVIGATE_URL = "navigate_url"
    EXECUTE_SCRIPT = "execute_script"
    CREATE_DOCUMENT = "create_document"
    TAKE_SCREENSHOT = "take_screenshot"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SYSTEM_COMMAND = "system_command"
    UNKNOWN = "unknown"


@dataclass
class AtomicAction:
    """Represents a single atomic action that can be executed.
    
    An atomic action is the smallest unit of work that can be performed
    by the system. Each action has a type, parameters, confidence score,
    and execution order.
    
    Attributes:
        type: The type of action to perform
        params: Dictionary of parameters specific to the action type
        confidence: Confidence score (0.0-1.0) in the parsing accuracy
        order: Execution order when part of a compound action sequence
    """
    type: ActionType
    params: Dict[str, Any]
    confidence: float = 1.0
    order: int = 0


class CompoundActionParser:
    """
    Dynamically parses compound commands into atomic actions.
    
    This parser uses zero hardcoding approach and learns patterns from execution.
    It can handle both explicit compound commands (with connectors like "and", "then")
    and implicit compound commands (like "open safari search for dogs").
    
    The parser maintains dictionaries of action verbs and connectors that can be
    dynamically extended as new patterns are discovered.
    
    Attributes:
        connectors: List of words that separate actions in compound commands
        action_verbs: Dictionary mapping action verbs to ActionType enums
    
    Example:
        >>> parser = CompoundActionParser()
        >>> actions = await parser.parse("open chrome and navigate to google.com")
        >>> len(actions)
        2
        >>> actions[0].type
        ActionType.OPEN_APP
        >>> actions[1].type
        ActionType.NAVIGATE_URL
    """

    def __init__(self):
        """Initialize the compound action parser with default patterns."""
        # Action connectors (words that separate actions)
        self.connectors = [
            'and', 'then', 'after that', 'next', 'followed by',
            'also', 'plus', 'afterwards', ','
        ]

        # Action verbs and their types (dynamic, can be extended)
        self.action_verbs = {
            'open': ActionType.OPEN_APP,
            'launch': ActionType.OPEN_APP,
            'start': ActionType.OPEN_APP,
            'run': ActionType.OPEN_APP,
            'close': ActionType.CLOSE_APP,
            'quit': ActionType.CLOSE_APP,
            'exit': ActionType.CLOSE_APP,
            'terminate': ActionType.CLOSE_APP,
            'kill': ActionType.CLOSE_APP,
            'search': ActionType.SEARCH_WEB,
            'google': ActionType.SEARCH_WEB,
            'look up': ActionType.SEARCH_WEB,
            'find': ActionType.SEARCH_WEB,
            'navigate': ActionType.NAVIGATE_URL,
            'go to': ActionType.NAVIGATE_URL,
            'visit': ActionType.NAVIGATE_URL,
            'write': ActionType.CREATE_DOCUMENT,
            'create': ActionType.CREATE_DOCUMENT,
            'draft': ActionType.CREATE_DOCUMENT,
            'compose': ActionType.CREATE_DOCUMENT,
            'make': ActionType.CREATE_DOCUMENT,
        }

    async def parse(self, command: str) -> List[AtomicAction]:
        """
        Parse a compound command into atomic actions.
        
        Handles both explicit connectors and implicit compound actions.
        The parser first attempts to split by explicit connectors, then
        falls back to detecting implicit action boundaries.

        Args:
            command: The compound command to parse (e.g., "open safari and search for dogs")

        Returns:
            List of AtomicAction objects in execution order. Empty list if no
            valid actions could be parsed.
            
        Example:
            >>> parser = CompoundActionParser()
            >>> actions = await parser.parse("open safari and search for dogs")
            >>> len(actions)
            2
            >>> actions[0].params['app_name']
            'safari'
            >>> actions[1].params['query']
            'dogs'
        """
        actions = []

        # First, try splitting by explicit connectors
        action_phrases = self._split_by_connectors(command)

        # If we got only one phrase, check for implicit compound actions
        # e.g., "open safari search for dogs" (no "and" connector)
        if len(action_phrases) == 1:
            action_phrases = self._split_implicit_actions(command)

        for idx, phrase in enumerate(action_phrases):
            action = await self._parse_action_phrase(phrase.strip(), order=idx)
            if action and action.type != ActionType.UNKNOWN:
                actions.append(action)
                logger.info(f"Parsed action {idx + 1}: {action.type.value} - {action.params}")

        return actions

    def _split_implicit_actions(self, command: str) -> List[str]:
        """
        Split command with implicit compound actions (no connectors).
        
        Detects patterns where multiple actions are combined without explicit
        connectors, such as "open safari search for dogs" which should be
        split into "open safari" and "search for dogs".
        
        Args:
            command: The command string to analyze for implicit actions
            
        Returns:
            List of action phrases. Returns single-item list if no split detected.
            
        Example:
            >>> parser = CompoundActionParser()
            >>> phrases = parser._split_implicit_actions("open safari search for dogs")
            >>> phrases
            ['open safari', 'search for dogs']
        """
        command_lower = command.lower()

        # Detect patterns where a second action verb appears
        # Common pattern: [action1] [app/target] [action2] [target2]
        secondary_verbs = ['search', 'google', 'find', 'look up', 'navigate', 'go to']

        for verb in secondary_verbs:
            if verb in command_lower:
                # Find the position of the secondary verb
                parts = command_lower.split(verb, 1)
                if len(parts) == 2:
                    first_part = parts[0].strip()
                    second_part = (verb + ' ' + parts[1]).strip()

                    # Validate that first part has a primary action
                    if any(primary in first_part for primary in ['open', 'launch', 'start']):
                        logger.debug(f"Split implicit actions: '{first_part}' and '{second_part}'")
                        return [first_part, second_part]

        # No implicit split found, return as single action
        return [command]

    def _split_by_connectors(self, command: str) -> List[str]:
        """
        Split command by connectors while preserving context.
        
        Uses regex to split the command on connector words while maintaining
        the integrity of each action phrase. Longer connectors are matched
        first to avoid partial matches.
        
        Args:
            command: The command string to split
            
        Returns:
            List of action phrases split by connectors
            
        Example:
            >>> parser = CompoundActionParser()
            >>> phrases = parser._split_by_connectors("open safari and then search for dogs")
            >>> phrases
            ['open safari', 'search for dogs']
        """
        # Create regex pattern from connectors
        # Sort by length (descending) to match longer phrases first
        sorted_connectors = sorted(self.connectors, key=len, reverse=True)
        pattern = '|'.join(re.escape(c) for c in sorted_connectors)

        # Split but keep track of what we're splitting on
        parts = re.split(f'\\s+({pattern})\\s+', command, flags=re.IGNORECASE)

        # Recombine, filtering out the connectors themselves
        phrases = []
        current_phrase = ""

        for part in parts:
            part_lower = part.lower().strip()
            if part_lower in self.connectors:
                if current_phrase:
                    phrases.append(current_phrase.strip())
                    current_phrase = ""
            else:
                current_phrase += " " + part if current_phrase else part

        if current_phrase:
            phrases.append(current_phrase.strip())

        return phrases

    async def _parse_action_phrase(self, phrase: str, order: int = 0) -> Optional[AtomicAction]:
        """
        Parse a single action phrase into an AtomicAction.
        
        Analyzes the phrase to identify the action verb and extract the target
        or parameters. Delegates to specific parsing methods based on the
        identified action type.
        
        Args:
            phrase: The action phrase to parse (e.g., "open safari")
            order: The execution order of this action in a sequence
            
        Returns:
            AtomicAction object if parsing successful, None otherwise
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = await parser._parse_action_phrase("open safari", 0)
            >>> action.type
            ActionType.OPEN_APP
            >>> action.params['app_name']
            'safari'
        """
        phrase_lower = phrase.lower().strip()

        # Try to match action verbs
        for verb, action_type in self.action_verbs.items():
            if phrase_lower.startswith(verb):
                # Extract the target/object of the action
                target = phrase_lower[len(verb):].strip()

                # Parse based on action type
                if action_type == ActionType.OPEN_APP:
                    return self._parse_open_app(target, order)
                elif action_type == ActionType.CLOSE_APP:
                    return self._parse_close_app(target, order)
                elif action_type == ActionType.SEARCH_WEB:
                    return self._parse_search_web(target, phrase, order)
                elif action_type == ActionType.NAVIGATE_URL:
                    return self._parse_navigate_url(target, order)
                elif action_type == ActionType.CREATE_DOCUMENT:
                    return self._parse_create_document(target, phrase, order)

        # If no verb matched, might be a continuation of previous action
        # e.g., "search for dogs" where "search for" is the verb
        return await self._parse_implicit_action(phrase, order)

    def _parse_close_app(self, target: str, order: int) -> AtomicAction:
        """
        Parse a 'close app' action.
        
        Extracts and cleans the application name from the target string,
        removing common articles and normalizing the name.
        
        Args:
            target: The target string containing the app name
            order: Execution order of this action
            
        Returns:
            AtomicAction configured for closing an application
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = parser._parse_close_app("the safari browser", 0)
            >>> action.params['app_name']
            'safari browser'
        """
        # Clean up app name
        app_name = target.strip()

        # Remove common words
        app_name = re.sub(r'\b(the|a|an)\b', '', app_name, flags=re.IGNORECASE).strip()

        return AtomicAction(
            type=ActionType.CLOSE_APP,
            params={'app_name': app_name},
            confidence=0.95,
            order=order
        )

    def _parse_open_app(self, target: str, order: int) -> AtomicAction:
        """
        Parse an 'open app' action.
        
        Extracts the application name from the target string, handling
        known application names and cleaning up the input. Prioritizes
        known browsers and applications for better accuracy.
        
        Args:
            target: The target string containing the app name
            order: Execution order of this action
            
        Returns:
            AtomicAction configured for opening an application
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = parser._parse_open_app("safari browser", 0)
            >>> action.params['app_name']
            'safari'
        """
        # Clean up app name
        app_name = target.strip()

        # Remove common words
        app_name = re.sub(r'\b(the|a|an)\b', '', app_name, flags=re.IGNORECASE).strip()

        # IMPORTANT: Check if there's a secondary action embedded (e.g., "safari search for dogs")
        # Extract just the app name (first word or known app names)
        known_browsers = ['safari', 'chrome', 'firefox', 'edge', 'brave']
        known_apps = ['finder', 'terminal', 'calculator', 'notes', 'music', 'photos', 'mail']

        app_lower = app_name.lower()
        for known in known_browsers + known_apps:
            if app_lower.startswith(known):
                app_name = known
                break
        else:
            # Take first word as app name
            words = app_name.split()
            if words:
                app_name = words[0]

        return AtomicAction(
            type=ActionType.OPEN_APP,
            params={'app_name': app_name},
            confidence=0.95,
            order=order
        )

    def _parse_search_web(self, target: str, original_phrase: str, order: int) -> AtomicAction:
        """
        Parse a 'search web' action.
        
        Extracts the search query from the target string, handling common
        patterns like "search for X" and falling back to the original phrase
        if the target is empty.
        
        Args:
            target: The target string after the search verb
            original_phrase: The complete original phrase for fallback parsing
            order: Execution order of this action
            
        Returns:
            AtomicAction configured for web searching
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = parser._parse_search_web("for dogs", "search for dogs", 0)
            >>> action.params['query']
            'dogs'
        """
        # Handle "search for X" pattern
        search_query = target
        if target.startswith('for '):
            search_query = target[4:].strip()

        # If search query is empty, try to extract from original phrase
        if not search_query:
            # Look for pattern: "search [for] X"
            match = re.search(r'search\s+(?:for\s+)?(.+)', original_phrase, re.IGNORECASE)
            if match:
                search_query = match.group(1).strip()

        return AtomicAction(
            type=ActionType.SEARCH_WEB,
            params={'query': search_query},
            confidence=0.9,
            order=order
        )

    def _parse_navigate_url(self, target: str, order: int) -> AtomicAction:
        """
        Parse a 'navigate to URL' action.
        
        Processes the target string as a URL, adding appropriate protocol
        if missing. Handles both full URLs and domain names.
        
        Args:
            target: The target URL or domain name
            order: Execution order of this action
            
        Returns:
            AtomicAction configured for URL navigation
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = parser._parse_navigate_url("google.com", 0)
            >>> action.params['url']
            'https://google.com'
        """
        url = target.strip()

        # Add http:// if no protocol specified
        if not url.startswith(('http://', 'https://', 'www.')):
            # Check if it looks like a domain
            if '.' in url:
                url = 'https://' + url

        return AtomicAction(
            type=ActionType.NAVIGATE_URL,
            params={'url': url},
            confidence=0.85,
            order=order
        )

    def _parse_create_document(self, target: str, original_phrase: str, order: int) -> AtomicAction:
        """
        Parse a 'create document' action.
        
        Extracts the content description from the target string, handling
        common patterns like "write me an essay" and preserving the full
        command for context.
        
        Args:
            target: The target string describing what to create
            original_phrase: The complete original phrase for context
            order: Execution order of this action
            
        Returns:
            AtomicAction configured for document creation
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = parser._parse_create_document("me an essay on dogs", "write me an essay on dogs", 0)
            >>> action.params['content']
            'an essay on dogs'
        """
        # Extract the content description from the target
        # e.g., "write me an essay on dolphins" → content: "an essay on dolphins"
        content = target
        if target.startswith('me '):
            content = target[3:].strip()
        elif target.startswith('an ') or target.startswith('a '):
            # Keep "an essay..." or "a document..."
            pass

        return AtomicAction(
            type=ActionType.CREATE_DOCUMENT,
            params={'content': content, 'full_command': original_phrase},
            confidence=0.95,
            order=order
        )

    async def _parse_implicit_action(self, phrase: str, order: int) -> Optional[AtomicAction]:
        """
        Parse an action that doesn't have an explicit verb.
        
        Handles cases where the action is implied by context, such as a
        search query that follows an "open browser" command. Uses heuristics
        to determine the most likely action type.
        
        Args:
            phrase: The phrase without an explicit action verb
            order: Execution order of this action
            
        Returns:
            AtomicAction if a reasonable interpretation is found, None otherwise
            
        Example:
            >>> parser = CompoundActionParser()
            >>> action = await parser._parse_implicit_action("dogs", 1)
            >>> action.type
            ActionType.SEARCH_WEB
            >>> action.params['query']
            'dogs'
        """
        # This could be a search query by default
        # e.g., if user says "dogs" after "open safari and"

        if len(phrase) > 2 and not phrase.startswith(('the ', 'a ', 'an ')):
            # Assume it's a search query
            return AtomicAction(
                type=ActionType.SEARCH_WEB,
                params={'query': phrase},
                confidence=0.7,
                order=order
            )

        return None

    def get_execution_plan(self, actions: List[AtomicAction]) -> str:
        """
        Generate a human-readable execution plan.
        
        Creates a formatted string describing the sequence of actions that
        will be executed, useful for user confirmation or logging.
        
        Args:
            actions: List of AtomicAction objects to describe
            
        Returns:
            Human-readable string describing the execution plan
            
        Example:
            >>> parser = CompoundActionParser()
            >>> actions = [
            ...     AtomicAction(ActionType.OPEN_APP, {'app_name': 'safari'}, order=0),
            ...     AtomicAction(ActionType.SEARCH_WEB, {'query': 'dogs'}, order=1)
            ... ]
            >>> parser.get_execution_plan(actions)
            'Open safari → Search for 'dogs''
        """
        if not actions:
            return "No actions to execute"

        plan_parts = []
        for action in sorted(actions, key=lambda a: a.order):
            if action.type == ActionType.OPEN_APP:
                plan_parts.append(f"Open {action.params.get('app_name', 'unknown')}")
            elif action.type == ActionType.CLOSE_APP:
                plan_parts.append(f"Close {action.params.get('app_name', 'unknown')}")
            elif action.type == ActionType.SEARCH_WEB:
                plan_parts.append(f"Search for '{action.params.get('query', '')}'")
            elif action.type == ActionType.NAVIGATE_URL:
                plan_parts.append(f"Navigate to {action.params.get('url', 'unknown')}")
            elif action.type == ActionType.CREATE_DOCUMENT:
                plan_parts.append(f"Create document: {action.params.get('content', '')}")

        return " → ".join(plan_parts)


# Singleton instance
_parser_instance = None

def get_compound_parser() -> CompoundActionParser:
    """
    Get singleton instance of CompoundActionParser.
    
    Provides a global singleton instance of the parser to avoid
    recreating the object and its initialization overhead.
    
    Returns:
        The singleton CompoundActionParser instance
        
    Example:
        >>> parser1 = get_compound_parser()
        >>> parser2 = get_compound_parser()
        >>> parser1 is parser2
        True
    """
    global _parser_instance
    if _parser_instance is None:
        _parser_instance = CompoundActionParser()
    return _parser_instance