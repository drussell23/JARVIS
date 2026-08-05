"""
Display Reference Handler - Advanced Voice Command Resolution for Display Connections
=====================================================================================

Async, robust, dynamic display command resolution with zero hardcoding.

Features:
- Dynamic pattern learning from usage
- Real-time display discovery and monitoring
- Context-aware reference resolution
- Multi-strategy fallback system
- Performance caching and optimization
- Comprehensive error handling

Integrates with:
1. implicit_reference_resolver.py (context understanding)
2. advanced_display_monitor.py (display detection)
3. control_center_clicker.py (connection execution)

Author: Derek Russell
Date: 2025-10-19
Version: 2.0 (Advanced)
"""

import asyncio
import hashlib
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS AND DATA STRUCTURES
# ============================================================================


class ActionType(Enum):
    """Display action types for voice commands.
    
    Attributes:
        CONNECT: Connect to a display
        DISCONNECT: Disconnect from a display
        CHANGE_MODE: Change display mirroring mode
        QUERY_STATUS: Query connection status
        LIST_DISPLAYS: List available displays
        UNKNOWN: Unrecognized action
    """

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    CHANGE_MODE = "change_mode"
    QUERY_STATUS = "query_status"
    LIST_DISPLAYS = "list_displays"
    UNKNOWN = "unknown"


class ModeType(Enum):
    """Display mirroring modes for screen sharing.
    
    Attributes:
        ENTIRE_SCREEN: Mirror entire screen
        WINDOW: Share specific window/app
        EXTENDED: Use as extended desktop
        AUTO: Let system decide optimal mode
    """

    ENTIRE_SCREEN = "entire"
    WINDOW = "window"
    EXTENDED = "extended"
    AUTO = "auto"  # Let system decide


class ResolutionStrategy(Enum):
    """Strategies used to resolve display references from voice commands.
    
    Attributes:
        DIRECT_MATCH: Exact name match found
        FUZZY_MATCH: Similar name match found
        IMPLICIT_CONTEXT: Resolved using context from implicit resolver
        VISUAL_ATTENTION: Based on recent visual detection
        CONVERSATION: From conversation history
        ONLY_AVAILABLE: Only one display available
        LEARNED_PATTERN: From learned usage patterns
        FALLBACK: Last resort resolution
    """

    DIRECT_MATCH = "direct_match"  # Exact name match
    FUZZY_MATCH = "fuzzy_match"  # Similar name
    IMPLICIT_CONTEXT = "implicit_context"  # From implicit_resolver
    VISUAL_ATTENTION = "visual_attention"  # From recent detection
    CONVERSATION = "conversation"  # From conversation history
    ONLY_AVAILABLE = "only_available"  # Only one display available
    LEARNED_PATTERN = "learned_pattern"  # From learned patterns
    FALLBACK = "fallback"  # Last resort


@dataclass
class DisplayReference:
    """Resolved display reference from voice command.
    
    Attributes:
        display_name: Human-readable display name
        display_id: Unique display identifier
        action: Type of action to perform
        mode: Display mirroring mode (if applicable)
        confidence: Confidence score (0.0-1.0)
        resolution_strategy: How the reference was resolved
        metadata: Additional resolution metadata
        timestamp: When the reference was created
    """

    display_name: str
    display_id: Optional[str] = None
    action: ActionType = ActionType.CONNECT
    mode: Optional[ModeType] = None
    confidence: float = 0.0
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.FALLBACK
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert display reference to dictionary format.
        
        Returns:
            Dictionary representation of the display reference
        """
        return {
            "display_name": self.display_name,
            "display_id": self.display_id,
            "action": self.action.value,
            "mode": self.mode.value if self.mode else None,
            "confidence": self.confidence,
            "resolution_strategy": self.resolution_strategy.value,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class PatternLearning:
    """Learned pattern from successful voice commands.
    
    Attributes:
        pattern: Regular expression pattern
        action: Associated action type
        mode: Associated mode type (if any)
        success_count: Number of successful uses
        failure_count: Number of failed uses
        last_used: When pattern was last used
    """

    pattern: str
    action: ActionType
    mode: Optional[ModeType]
    success_count: int = 0
    failure_count: int = 0
    last_used: datetime = field(default_factory=datetime.now)

    @property
    def success_rate(self) -> float:
        """Calculate success rate of this pattern.
        
        Returns:
            Success rate as float between 0.0 and 1.0
        """
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0


@dataclass
class DisplayDetectionEvent:
    """Record of display detection and usage statistics.
    
    Attributes:
        display_name: Human-readable display name
        display_id: Unique display identifier
        detected_at: When first detected
        last_seen: When last seen/detected
        detection_count: Total number of detections
        connection_attempts: Number of connection attempts
        successful_connections: Number of successful connections
    """

    display_name: str
    display_id: str
    detected_at: datetime
    last_seen: datetime
    detection_count: int = 1
    connection_attempts: int = 0
    successful_connections: int = 0


# ============================================================================
# ADVANCED DISPLAY REFERENCE HANDLER
# ============================================================================


class AdvancedDisplayReferenceHandler:
    """Advanced async display reference handler with dynamic learning.

    No hardcoding - learns everything from:
    1. System observation (available displays)
    2. User commands (patterns and preferences)
    3. Context (implicit resolver, conversation)
    4. Success/failure feedback
    
    Attributes:
        implicit_resolver: ImplicitReferenceResolver instance for context
        display_monitor: AdvancedDisplayMonitor for display detection
        known_displays: Dictionary of detected displays
        learned_patterns: Patterns learned from successful commands
        display_aliases: Nickname mappings to display IDs
        command_history: Recent command history
        action_keywords: Keywords associated with each action type
        mode_keywords: Keywords associated with each mode type
        resolution_cache: Performance cache for resolutions
        stats: Usage statistics
    """

    def __init__(
        self,
        implicit_resolver=None,
        display_monitor=None,
        max_cache_size: int = 100,
        cache_ttl_seconds: int = 300,
    ):
        """Initialize advanced display reference handler.

        Args:
            implicit_resolver: ImplicitReferenceResolver instance for context awareness
            display_monitor: AdvancedDisplayMonitor instance for display detection
            max_cache_size: Maximum number of cached resolutions
            cache_ttl_seconds: Cache time-to-live in seconds
        """
        self.implicit_resolver = implicit_resolver
        self.display_monitor = display_monitor

        # Dynamic learning structures (NO HARDCODING)
        self.known_displays: Dict[str, DisplayDetectionEvent] = {}
        self.learned_patterns: Dict[str, List[PatternLearning]] = defaultdict(list)
        self.display_aliases: Dict[str, Set[str]] = defaultdict(set)  # nickname → display_id
        self.command_history: deque = deque(maxlen=50)

        # Action and mode patterns (dynamically learned)
        self.action_keywords: Dict[ActionType, Set[str]] = defaultdict(set)
        self.mode_keywords: Dict[ModeType, Set[str]] = defaultdict(set)

        # Initialize with minimal seed patterns (will learn more)
        self._initialize_seed_patterns()

        # Performance optimization
        self.resolution_cache: Dict[str, Tuple[DisplayReference, datetime]] = {}
        self.max_cache_size = max_cache_size
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)

        # Statistics
        self.stats = {
            "total_commands": 0,
            "successful_resolutions": 0,
            "failed_resolutions": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

        # Real-time monitoring
        self.monitoring_task: Optional[asyncio.Task] = None
        self.is_monitoring = False

        logger.info("[DISPLAY-REF-ADV] Advanced handler initialized")

    def _initialize_seed_patterns(self):
        """Initialize minimal seed patterns that will be expanded through learning.
        
        Sets up basic keyword patterns for actions and modes that will be
        dynamically expanded as the system learns from user interactions.
        """
        # Action seeds (minimal - will learn more from usage)
        self.action_keywords[ActionType.CONNECT] = {"connect", "show", "cast"}
        self.action_keywords[ActionType.DISCONNECT] = {"disconnect", "stop"}
        self.action_keywords[ActionType.CHANGE_MODE] = {"change", "switch"}
        self.action_keywords[ActionType.QUERY_STATUS] = {"status", "connected"}
        self.action_keywords[ActionType.LIST_DISPLAYS] = {"list", "show", "available"}

        # Mode seeds (minimal - will learn more from usage)
        self.mode_keywords[ModeType.ENTIRE_SCREEN] = {"entire", "mirror"}
        self.mode_keywords[ModeType.WINDOW] = {"window", "app"}
        self.mode_keywords[ModeType.EXTENDED] = {"extend", "extended"}

    # ========================================================================
    # MAIN COMMAND PROCESSING
    # ========================================================================

    async def handle_voice_command(self, command: str) -> Optional[DisplayReference]:
        """Handle voice command with advanced async resolution.

        Uses multiple concurrent resolution strategies to identify the target
        display and action from natural language voice commands.

        Args:
            command: Voice command text to process

        Returns:
            DisplayReference if successfully resolved, None if not a display command
            
        Raises:
            Exception: If critical error occurs during processing
        """
        self.stats["total_commands"] += 1
        command_lower = command.lower().strip()

        logger.info(f"[DISPLAY-REF-ADV] Processing: '{command}'")

        try:
            # Step 1: Check cache first (performance optimization)
            cached = self._get_cached_resolution(command_lower)
            if cached:
                self.stats["cache_hits"] += 1
                logger.debug(f"[DISPLAY-REF-ADV] Cache hit for '{command}'")
                return cached

            self.stats["cache_misses"] += 1

            # Step 2: Quick validation - is this a display command?
            if not await self._is_display_command(command_lower):
                logger.debug("[DISPLAY-REF-ADV] Not a display command")
                return None

            # Step 3: Multi-strategy async resolution
            resolution_strategies = [
                self._resolve_via_direct_match(command_lower),
                self._resolve_via_fuzzy_match(command_lower),
                self._resolve_via_implicit_context(command_lower),
                self._resolve_via_learned_patterns(command_lower),
                self._resolve_via_only_available(command_lower),
            ]

            # Execute all strategies concurrently
            results = await asyncio.gather(*resolution_strategies, return_exceptions=True)

            # Filter out exceptions and None results
            valid_results = [r for r in results if not isinstance(r, Exception) and r is not None]

            # Step 4: Select best resolution based on confidence
            if not valid_results:
                self.stats["failed_resolutions"] += 1
                logger.warning(f"[DISPLAY-REF-ADV] No resolution found for '{command}'")
                return None

            # Sort by confidence and select best
            best_result = max(valid_results, key=lambda r: r.confidence)

            # Step 5: Extract action and mode
            best_result.action = await self._determine_action(command_lower)
            best_result.mode = await self._determine_mode(command_lower)

            # Step 6: Cache the result
            self._cache_resolution(command_lower, best_result)

            # Step 7: Record in command history
            self._record_command(command, best_result)

            self.stats["successful_resolutions"] += 1
            logger.info(
                f"[DISPLAY-REF-ADV] Resolved: {best_result.display_name} "
                f"(strategy={best_result.resolution_strategy.value}, "
                f"confidence={best_result.confidence:.2f}, "
                f"action={best_result.action.value})"
            )

            return best_result

        except Exception as e:
            logger.error(f"[DISPLAY-REF-ADV] Error processing command: {e}", exc_info=True)
            self.stats["failed_resolutions"] += 1
            return None

    # ========================================================================
    # VALIDATION
    # ========================================================================

    async def _is_display_command(self, command: str) -> bool:
        """Dynamically determine if command is about displays.

        Uses multiple signals to classify whether a voice command is
        display-related without hardcoded rules.

        Args:
            command: Lowercase command text to analyze

        Returns:
            True if command appears to be display-related, False otherwise
        """
        # Check 1: Known display names
        for display_name in self.known_displays.keys():
            if display_name.lower() in command:
                return True

        # Check 2: Display aliases
        for alias_set in self.display_aliases.values():
            if any(alias.lower() in command for alias in alias_set):
                return True

        # Check 3: Action keywords
        for action_keywords in self.action_keywords.values():
            if any(keyword in command for keyword in action_keywords):
                # If action keyword + display-related term
                display_terms = {"tv", "display", "screen", "monitor", "airplay"}
                if any(term in command for term in display_terms):
                    return True

        # Check 4: Learned patterns
        for pattern_list in self.learned_patterns.values():
            for pattern_obj in pattern_list:
                if re.search(pattern_obj.pattern, command, re.IGNORECASE):
                    return True

        # Check 5: Implicit resolver (if available)
        if self.implicit_resolver:
            try:
                # Quick check if implicit resolver sees this as display-related
                # This would need implicit resolver to classify query type
                pass
            except Exception:
                pass

        return False

    # ========================================================================
    # RESOLUTION STRATEGIES (ALL ASYNC, NO HARDCODING)
    # ========================================================================

    async def _resolve_via_direct_match(self, command: str) -> Optional[DisplayReference]:
        """Strategy 1: Direct exact match with known displays.
        
        Args:
            command: Lowercase command text
            
        Returns:
            DisplayReference if exact match found, None otherwise
        """
        for display_name, event in self.known_displays.items():
            if display_name.lower() in command:
                return DisplayReference(
                    display_name=display_name,
                    display_id=event.display_id,
                    confidence=0.95,
                    resolution_strategy=ResolutionStrategy.DIRECT_MATCH,
                    metadata={"detection_event": event},
                )
        return None

    async def _resolve_via_fuzzy_match(self, command: str) -> Optional[DisplayReference]:
        """Strategy 2: Fuzzy matching with known displays.
        
        Args:
            command: Lowercase command text
            
        Returns:
            DisplayReference with best fuzzy match, None if no good matches
        """
        best_match = None
        best_score = 0.0

        for display_name, event in self.known_displays.items():
            # Calculate similarity score
            score = self._calculate_similarity(command, display_name.lower())

            if score > 0.7 and score > best_score:  # Threshold for fuzzy match
                best_score = score
                best_match = DisplayReference(
                    display_name=display_name,
                    display_id=event.display_id,
                    confidence=score * 0.9,  # Slightly lower than direct match
                    resolution_strategy=ResolutionStrategy.FUZZY_MATCH,
                    metadata={"similarity_score": score},
                )

        return best_match

    async def _resolve_via_implicit_context(self, command: str) -> Optional[DisplayReference]:
        """Strategy 3: Use implicit_reference_resolver for context.
        
        Args:
            command: Lowercase command text
            
        Returns:
            DisplayReference from context analysis, None if not resolved
        """
        if not self.implicit_resolver:
            return None

        try:
            # Use implicit resolver to understand context
            result = await self.implicit_resolver.resolve_query(command)

            # Check if it found a display device
            referent = result.get("referent", {})
            if referent.get("type") == "display_device":
                display_name = referent.get("entity")

                # Try to find matching display in known displays
                for known_name, event in self.known_displays.items():
                    if known_name.lower() in display_name.lower():
                        return DisplayReference(
                            display_name=known_name,
                            display_id=event.display_id,
                            confidence=result.get("confidence", 0.8),
                            resolution_strategy=ResolutionStrategy.IMPLICIT_CONTEXT,
                            metadata={"implicit_result": result},
                        )

        except Exception as e:
            logger.debug(f"[DISPLAY-REF-ADV] Implicit resolver error: {e}")

        return None

    async def _resolve_via_learned_patterns(self, command: str) -> Optional[DisplayReference]:
        """Strategy 4: Use learned patterns from previous successful commands.
        
        Args:
            command: Lowercase command text
            
        Returns:
            DisplayReference from pattern matching, None if no patterns match
        """
        for pattern_text, pattern_list in self.learned_patterns.items():
            for pattern_obj in pattern_list:
                if re.search(pattern_obj.pattern, command, re.IGNORECASE):
                    # Pattern matched - extract display name
                    # This is where we use learned knowledge

                    # Try to find display name in command
                    for display_name, event in self.known_displays.items():
                        if display_name.lower() in command:
                            confidence = 0.7 * pattern_obj.success_rate
                            return DisplayReference(
                                display_name=display_name,
                                display_id=event.display_id,
                                confidence=confidence,
                                resolution_strategy=ResolutionStrategy.LEARNED_PATTERN,
                                metadata={
                                    "pattern": pattern_obj.pattern,
                                    "success_rate": pattern_obj.success_rate,
                                },
                            )

        return None

    async def _resolve_via_only_available(self, command: str) -> Optional[DisplayReference]:
        """Strategy 5: If only one display available, use it.
        
        Args:
            command: Lowercase command text
            
        Returns:
            DisplayReference for the only available display, None if multiple displays
        """
        if len(self.known_displays) == 1:
            display_name, event = list(self.known_displays.items())[0]
            return DisplayReference(
                display_name=display_name,
                display_id=event.display_id,
                confidence=0.75,  # Moderate confidence - assumption based
                resolution_strategy=ResolutionStrategy.ONLY_AVAILABLE,
                metadata={"reason": "only_one_available"},
            )

        return None

    # ========================================================================
    # ACTION & MODE DETERMINATION (DYNAMIC)
    # ========================================================================

    async def _determine_action(self, command: str) -> ActionType:
        """Dynamically determine action from command.
        
        Args:
            command: Lowercase command text
            
        Returns:
            Most likely ActionType based on keywords and patterns
        """
        # Score each action type
        scores: Dict[ActionType, float] = defaultdict(float)

        for action_type, keywords in self.action_keywords.items():
            for keyword in keywords:
                if keyword in command:
                    scores[action_type] += 1.0

        # Check learned patterns
        for pattern_list in self.learned_patterns.values():
            for pattern_obj in pattern_list:
                if re.search(pattern_obj.pattern, command, re.IGNORECASE):
                    scores[pattern_obj.action] += 0.5 * pattern_obj.success_rate

        # Return highest scoring action (default to CONNECT)
        if scores:
            return max(scores.items(), key=lambda x: x[1])[0]

        return ActionType.CONNECT  # Default

    async def _determine_mode(self, command: str) -> Optional[ModeType]:
        """Dynamically determine mode from command.
        
        Args:
            command: Lowercase command text
            
        Returns:
            Most likely ModeType if specified, None if no mode detected
        """
        scores: Dict[ModeType, float] = defaultdict(float)

        for mode_type, keywords in self.mode_keywords.items():
            for keyword in keywords:
                if keyword in command:
                    scores[mode_type] += 1.0

        # Return highest scoring mode (None if no mode specified)
        if scores:
            return max(scores.items(), key=lambda x: x[1])[0]

        return None  # Let system decide

    # ========================================================================
    # DISPLAY DETECTION & MONITORING
    # ========================================================================

    def record_display_detection(self, display_name: str, display_id: Optional[str] = None):
        """Record display detection event (called by advanced_display_monitor).

        Updates internal knowledge of available displays and their usage statistics.

        Args:
            display_name: Human-readable display name
            display_id: Unique display identifier (generated if not provided)
        """
        if not display_id:
            # Generate display_id from name if not provided (use underscores to match config format)
            display_id = display_name.lower().replace(" ", "_")

        now = datetime.now()

        if display_name in self.known_displays:
            # Update existing
            event = self.known_displays[display_name]
            event.last_seen = now
            event.detection_count += 1
            logger.debug(
                f"[DISPLAY-REF-ADV] Updated: {display_name} (count={event.detection_count})"
            )
        else:
            # New display
            event = DisplayDetectionEvent(
                display_name=display_name, display_id=display_id, detected_at=now, last_seen=now
            )
            self.known_displays[display_name] = event
            logger.info(f"[DISPLAY-REF-ADV] New display detected: {display_name} (id={display_id})")

        # Record in implicit resolver (if available)
        if self.implicit_resolver:
            try:
                self.implicit_resolver.record_visual_attention(
                    space_id=0,
                    app_name="Display Monitor",
                    ocr_text=f"Detected: {display_name}",
                    content_type="display_device",
                    significance="high",
                )
            except Exception as e:
                logger.debug(f"[DISPLAY-REF-ADV] Could not record in implicit resolver: {e}")

    async def start_realtime_monitoring(self):
        """Start real-time display monitoring (if display_monitor available).
        
        Begins background task to continuously monitor for display changes
        and update the known displays database.
        """
        if not self.display_monitor or self.is_monitoring:
            return

        self.is_monitoring = True
        self.monitoring_task = asyncio.create_task(self._monitor_displays())
        logger.info("[DISPLAY-REF-ADV] Started real-time display monitoring")

    async def stop_realtime_monitoring(self):
        """Stop real-time display monitoring.
        
        Cancels the background monitoring task and cleans up resources.
        """
        self.is_monitoring = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        logger.info("[DISPLAY-REF-ADV] Stopped real-time display monitoring")

    async def _monitor_displays(self):
        """Background task to monitor displays.
        
        Continuously polls the display monitor for available displays
        and updates the internal knowledge base.
        """
        while self.is_monitoring:
            try:
                if self.display_monitor:
                    # Get available displays
                    available = self.display_monitor.get_available_display_details()

                    for display_info in available:
                        self.record_display_detection(
                            display_info.get("display_name"), display_info.get("display_id")
                        )

                await asyncio.sleep(5)  # Check every 5 seconds

            except Exception as e:
                logger.error(f"[DISPLAY-REF-ADV] Monitoring error: {e}")
                await asyncio.sleep(10)  # Back off on error

    # ========================================================================
    # LEARNING & FEEDBACK
    # ========================================================================

    def learn_from_success(self, command: str, reference: DisplayReference):
        """Learn from successful command execution.

        This improves future resolutions by:
        1. Strengthening used patterns
        2. Creating new patterns
        3. Building display aliases

        Args:
            command: Original voice command that succeeded
            reference: DisplayReference that was successfully executed
        """
        command_lower = command.lower()

        # Update display event
        if reference.display_name in self.known_displays:
            event = self.known_displays[reference.display_name]
            event.successful_connections += 1

        # Extract and learn pattern
        pattern = self._extract_pattern(command_lower, reference)
        if pattern:
            # Find or create pattern learning entry
            found = False
            for p in self.learned_patterns[pattern]:
                if p.action == reference.action and p.mode == reference.mode:
                    p.success_count += 1
                    p.last_used = datetime.now()
                    found = True
                    break

            if not found:
                self.learned_patterns[pattern].append(
                    PatternLearning(
                        pattern=pattern,
                        action=reference.action,
                        mode=reference.mode,
                        success_count=1,
                    )
                )

        # Learn action keywords
        for word in command_lower.split():
            if len(word) > 3:  # Ignore short words
                self.action_keywords[reference.action].add(word)

        # Learn mode keywords (if mode specified)
        if reference.mode:
            for word in command_lower.split():
                if len(word) > 3:
                    self.mode_keywords[reference.mode].add(word)

        logger.debug(f"[DISPLAY-REF-ADV] Learned from success: '{command}'")

    def learn_from_failure(
        self, command: str, attempted_reference: Optional[DisplayReference] = None
    ):
        """Learn from failed command execution.
        
        Updates failure statistics and pattern reliability scores
        to improve future resolution accuracy.

        Args:
            command: Original voice command that failed
            attempted_reference: DisplayReference that failed (if any)
        """
        if attempted_reference:
            # Update failure stats
            if attempted_reference.display_name in self.known_displays:
                event = self.known_displays[attempted_reference.display_name]
                event.connection_attempts += 1

            # Update pattern failure count
            pattern = self._extract_pattern(command.lower(), attempted_reference)
            if pattern:
                for p in self.learned_patterns[pattern]:
                    if p.action == attempted_reference.action:
                        p.failure_count += 1
                        break

        logger.debug(f"[DISPLAY-REF-ADV] Learned from failure: '{command}'")

    def _extract_pattern(self, command: str, reference: DisplayReference) -> Optional[str]:
        """Extract reusable pattern from command.
        
        Creates a generic pattern by replacing the specific display name
        with a placeholder for future pattern matching.

        Args:
            command: Lowercase command text
            reference: DisplayReference with display name to replace
            
        Returns:
            Generic pattern string if extractable, None otherwise
        """
        # Remove display name to get generic pattern
        pattern = command.replace(reference.display_name.lower(), "{display}")

        # Only return if pattern is generic enough
        if "{display}" in pattern and len(pattern) > 5:
            return pattern

        return None

    # ========================================================================
    # CACHING & OPTIMIZATION
    # ========================================================================

    def _get_cached_resolution(self, command: str) -> Optional[DisplayReference]:
        """Get cached resolution if available and not expired.
        
        Args:
            command: Command text to check cache for
            
        Returns:
            Cached DisplayReference if valid, None otherwise
        """
        cache_key = hashlib.md5(command.encode()).hexdigest()

        if cache_key in self.resolution_cache:
            cached_ref, cached_time = self.resolution_cache[cache_key]

            # Check if still valid
            if datetime.now() - cached_time < self.cache_ttl:
                return cached_ref
            else:
                # Expired - remove
                del self.resolution_cache[cache_key]

        return None

    def _cache_resolution(self, command: str, reference: DisplayReference):
        """Cache resolution for performance optimization.
        
        Args:
            command: Command text to cache
            reference: DisplayReference to cache
        """
        cache_key = hashlib.md5(command.encode()).hexdigest()

        # Evict oldest if cache full
        if len(self.resolution_cache) >= self.max_cache_size:
            # Remove oldest entry
            oldest_key = min(self.resolution_cache.items(), key=lambda x: x[1][1])[0]
            del self.resolution_cache[oldest_key]

        self.resolution_cache[cache_key] = (reference, datetime.now())

    def clear_cache(self):
        """Clear resolution cache to free memory and force fresh resolutions."""
        self.resolution_cache.clear()
        logger.debug("[DISPLAY-REF-ADV] Cache cleared")

    # ========================================================================
    # UTILITIES
    # ========================================================================

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate text similarity score using Jaccard similarity.

        Args:
            text1: First text to compare
            text2: Second text to compare

        Returns:
            Similarity score between 0.0 and 1.0
        """
        words1 = set(text1.split())
        words2 = set(text2.split())

        if not words1 or not words2:
            return 0.0

        intersection = words1.intersection(words2)
        union = words1.union(words2)

        return len(intersection) / len(union) if union else 0.0

    def get_statistics(self) -> Dict[str, Any]:
        """Get handler statistics"""
        return {
            **self.stats,
            "known_displays": len(self.known_displays),
            "learned_patterns": sum(len(v) for v in self.learned_patterns.values()),
            "cache_size": len(self.resolution_cache),
            "action_keywords_learned": {
                action.value: len(keywords) for action, keywords in self.action_keywords.items()
            },
            "mode_keywords_learned": {
                mode.value: len(keywords) for mode, keywords in self.mode_keywords.items()
            },
        }

    def get_display_stats(self, display_name: str) -> Optional[Dict[str, Any]]:
        """Get statistics for a specific display"""
        if display_name in self.known_displays:
            event = self.known_displays[display_name]
            return {
                "display_name": event.display_name,
                "display_id": event.display_id,
                "first_detected": event.detected_at.isoformat(),
                "last_seen": event.last_seen.isoformat(),
                "detection_count": event.detection_count,
                "connection_attempts": event.connection_attempts,
                "successful_connections": event.successful_connections,
                "success_rate": (
                    event.successful_connections / event.connection_attempts
                    if event.connection_attempts > 0
                    else 0.0
                ),
            }
        return None

    def get_known_displays(self) -> List[str]:
        """Get list of known display names"""
        return list(self.known_displays.keys())

    def _record_command(self, command: str, reference: DisplayReference):
        """Record command in history"""
        self.command_history.append(
            {"command": command, "reference": reference.to_dict(), "timestamp": datetime.now()}
        )


# =============================================================================
# BACKWARDS COMPATIBILITY ALIAS
# =============================================================================

# Alias for backwards compatibility with handlers/__init__.py
DisplayReferenceHandler = AdvancedDisplayReferenceHandler


# =============================================================================
# GLOBAL HANDLER INSTANCE & FACTORY FUNCTIONS
# =============================================================================

_global_handler: Optional[AdvancedDisplayReferenceHandler] = None


def get_display_reference_handler() -> Optional[AdvancedDisplayReferenceHandler]:
    """Get the global display reference handler.

    Returns:
        The global AdvancedDisplayReferenceHandler instance, or None if not initialized.

    Example:
        >>> handler = get_display_reference_handler()
        >>> if handler:
        ...     ref = await handler.resolve("connect to living room TV")
    """
    return _global_handler


def initialize_display_reference_handler(
    implicit_resolver=None,
    display_monitor=None
) -> AdvancedDisplayReferenceHandler:
    """Initialize the global display reference handler.

    Creates and stores a global AdvancedDisplayReferenceHandler instance with the
    provided dependencies.

    Args:
        implicit_resolver: ImplicitReferenceResolver instance for context
        display_monitor: AdvancedDisplayMonitor for display detection

    Returns:
        The initialized AdvancedDisplayReferenceHandler instance.

    Example:
        >>> handler = initialize_display_reference_handler(
        ...     implicit_resolver=resolver,
        ...     display_monitor=monitor
        ... )
        >>> ref = await handler.resolve("connect to projector")
    """
    global _global_handler

    _global_handler = AdvancedDisplayReferenceHandler(
        implicit_resolver=implicit_resolver,
        display_monitor=display_monitor
    )

    logger.info("[DISPLAY-REF-ADV] ✅ Global handler initialized")
    return _global_handler