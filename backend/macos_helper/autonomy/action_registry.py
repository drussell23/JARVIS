"""
Centralized Action Registry for JARVIS Autonomous System.

This module provides a comprehensive registry of all executable actions,
their metadata, handlers, and validation rules. It serves as the single
source of truth for what actions JARVIS can perform.

Key Features:
    - Dynamic action registration and discovery
    - Metadata-driven action configuration
    - Handler mapping with validation
    - Action categorization and risk assessment
    - Permission requirements specification
    - Async-first design with no hardcoding

Environment Variables:
    JARVIS_ACTION_REGISTRY_AUTO_DISCOVER: Enable auto-discovery (default: true)
    JARVIS_ACTION_REGISTRY_CACHE_TTL: Cache TTL in seconds (default: 300)
    JARVIS_ACTION_REGISTRY_VALIDATION_MODE: strict/lenient (default: strict)
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ActionCategory(Enum):
    """Categories of actions for organizational and permission purposes."""

    APPLICATION = "application"  # App management (open, close, focus)
    FILE_SYSTEM = "file_system"  # File operations
    SYSTEM = "system"  # System settings and controls
    NETWORK = "network"  # Network and connectivity
    MEDIA = "media"  # Audio, video, media controls
    NOTIFICATION = "notification"  # Notification management
    SECURITY = "security"  # Security-related actions
    COMMUNICATION = "communication"  # Messaging, email
    PRODUCTIVITY = "productivity"  # Workspace, focus, automation
    DISPLAY = "display"  # Screen, display controls
    HARDWARE = "hardware"  # Hardware interactions
    WORKFLOW = "workflow"  # Multi-step workflows
    CUSTOM = "custom"  # User-defined actions


class ActionRiskLevel(Enum):
    """Risk levels for actions to guide permission and safety decisions."""

    MINIMAL = 1  # Read-only, no side effects (e.g., get status)
    LOW = 2  # Safe reversible actions (e.g., focus app)
    MODERATE = 3  # Potentially disruptive (e.g., close app)
    HIGH = 4  # System-affecting (e.g., change settings)
    CRITICAL = 5  # Irreversible or security-sensitive (e.g., delete files)

    @classmethod
    def from_string(cls, value: str) -> "ActionRiskLevel":
        """Convert string to risk level."""
        return cls[value.upper()]

    def requires_confirmation(self) -> bool:
        """Check if this risk level requires user confirmation."""
        return self.value >= ActionRiskLevel.HIGH.value


class ActionType(Enum):
    """All supported action types in the system."""

    # Application actions
    APP_OPEN = auto()
    APP_CLOSE = auto()
    APP_FOCUS = auto()
    APP_HIDE = auto()
    APP_SHOW = auto()
    APP_MINIMIZE = auto()
    APP_MAXIMIZE = auto()
    APP_FULLSCREEN = auto()
    APP_SWITCH = auto()
    APP_LIST = auto()

    # Window actions
    WINDOW_MOVE = auto()
    WINDOW_RESIZE = auto()
    WINDOW_CLOSE = auto()
    WINDOW_MINIMIZE = auto()
    WINDOW_MAXIMIZE = auto()
    WINDOW_ARRANGE = auto()
    WINDOW_LIST = auto()

    # File system actions
    FILE_OPEN = auto()
    FILE_CREATE = auto()
    FILE_DELETE = auto()
    FILE_MOVE = auto()
    FILE_COPY = auto()
    FILE_RENAME = auto()
    FILE_SEARCH = auto()
    FOLDER_CREATE = auto()
    FOLDER_DELETE = auto()
    FOLDER_OPEN = auto()

    # System actions
    SYSTEM_INFO = auto()
    SYSTEM_SLEEP = auto()
    SYSTEM_WAKE = auto()
    SYSTEM_RESTART = auto()
    SYSTEM_SHUTDOWN = auto()
    SYSTEM_LOCK = auto()
    SYSTEM_UNLOCK = auto()

    # Media actions
    VOLUME_SET = auto()
    VOLUME_MUTE = auto()
    VOLUME_UNMUTE = auto()
    VOLUME_GET = auto()
    BRIGHTNESS_SET = auto()
    BRIGHTNESS_GET = auto()
    MEDIA_PLAY = auto()
    MEDIA_PAUSE = auto()
    MEDIA_NEXT = auto()
    MEDIA_PREVIOUS = auto()

    # Network actions
    WIFI_TOGGLE = auto()
    WIFI_STATUS = auto()
    BLUETOOTH_TOGGLE = auto()
    BLUETOOTH_STATUS = auto()
    NETWORK_STATUS = auto()

    # Display actions
    DISPLAY_SLEEP = auto()
    DISPLAY_WAKE = auto()
    SCREENSHOT = auto()
    SCREEN_RECORD_START = auto()
    SCREEN_RECORD_STOP = auto()

    # Notification actions
    NOTIFICATION_SEND = auto()
    NOTIFICATION_DISMISS = auto()
    NOTIFICATION_BATCH = auto()
    DND_ENABLE = auto()
    DND_DISABLE = auto()
    DND_STATUS = auto()

    # Security actions
    SCREEN_LOCK = auto()
    SCREEN_UNLOCK = auto()
    KEYCHAIN_ACCESS = auto()
    SECURITY_ALERT = auto()

    # Communication actions
    MESSAGE_SEND = auto()
    MESSAGE_READ = auto()
    EMAIL_SEND = auto()
    EMAIL_CHECK = auto()

    # Productivity actions
    WORKSPACE_ORGANIZE = auto()
    WORKSPACE_CLEANUP = auto()
    FOCUS_MODE_START = auto()
    FOCUS_MODE_END = auto()
    ROUTINE_EXECUTE = auto()
    MEETING_PREPARE = auto()

    # Input actions
    KEYSTROKE_SEND = auto()
    MOUSE_CLICK = auto()
    MOUSE_MOVE = auto()
    MOUSE_SCROLL = auto()
    TEXT_TYPE = auto()

    # Web actions
    WEB_OPEN = auto()
    WEB_SEARCH = auto()
    WEB_NEW_TAB = auto()

    # Workflow actions
    WORKFLOW_EXECUTE = auto()
    WORKFLOW_ABORT = auto()
    WORKFLOW_STATUS = auto()

    # Custom actions
    CUSTOM_SCRIPT = auto()
    CUSTOM_APPLESCRIPT = auto()
    CUSTOM_SHELL = auto()


# Type for action handlers
ActionHandler = Callable[..., Awaitable[Dict[str, Any]]]


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class ActionParameter:
    """Specification for an action parameter."""

    name: str
    description: str
    param_type: type
    required: bool = True
    default: Any = None
    validator: Optional[Callable[[Any], bool]] = None
    choices: Optional[List[Any]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def validate(self, value: Any) -> Tuple[bool, Optional[str]]:
        """Validate a parameter value."""
        if value is None:
            if self.required and self.default is None:
                return False, f"Required parameter '{self.name}' is missing"
            return True, None

        # Type check
        if not isinstance(value, self.param_type):
            try:
                value = self.param_type(value)
            except (ValueError, TypeError):
                return False, f"Parameter '{self.name}' must be of type {self.param_type.__name__}"

        # Choices check
        if self.choices and value not in self.choices:
            return False, f"Parameter '{self.name}' must be one of {self.choices}"

        # Range check
        if self.min_value is not None and value < self.min_value:
            return False, f"Parameter '{self.name}' must be >= {self.min_value}"
        if self.max_value is not None and value > self.max_value:
            return False, f"Parameter '{self.name}' must be <= {self.max_value}"

        # Custom validator
        if self.validator and not self.validator(value):
            return False, f"Parameter '{self.name}' failed custom validation"

        return True, None


@dataclass
class ActionMetadata:
    """Complete metadata for an action type."""

    action_type: ActionType
    name: str
    description: str
    category: ActionCategory
    risk_level: ActionRiskLevel

    # Execution properties
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    supports_dry_run: bool = True
    supports_rollback: bool = False
    is_async: bool = True

    # Permission requirements
    required_permissions: List[str] = field(default_factory=list)
    requires_confirmation: bool = False
    requires_authentication: bool = False

    # Safety properties
    safety_constraints: List[str] = field(default_factory=list)
    affected_resources: List[str] = field(default_factory=list)
    side_effects: List[str] = field(default_factory=list)

    # Parameters
    parameters: List[ActionParameter] = field(default_factory=list)

    # Metadata
    version: str = "1.0.0"
    author: str = "system"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    tags: List[str] = field(default_factory=list)

    def validate_params(self, params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate parameters against the action specification."""
        errors = []

        for param_spec in self.parameters:
            value = params.get(param_spec.name, param_spec.default)
            valid, error = param_spec.validate(value)
            if not valid:
                errors.append(error)

        return len(errors) == 0, errors

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "action_type": self.action_type.name,
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "risk_level": self.risk_level.name,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "supports_dry_run": self.supports_dry_run,
            "supports_rollback": self.supports_rollback,
            "required_permissions": self.required_permissions,
            "requires_confirmation": self.requires_confirmation,
            "parameters": [
                {
                    "name": p.name,
                    "description": p.description,
                    "type": p.param_type.__name__,
                    "required": p.required,
                    "default": p.default,
                    "choices": p.choices,
                }
                for p in self.parameters
            ],
            "tags": self.tags,
        }


@dataclass
class RegisteredAction:
    """A registered action with its handler and metadata."""

    metadata: ActionMetadata
    handler: ActionHandler
    enabled: bool = True
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_executed: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    average_execution_time: float = 0.0

    def record_execution(
        self,
        success: bool,
        execution_time: float
    ) -> None:
        """Record an execution result."""
        self.execution_count += 1

        if success:
            self.success_count += 1
            self.last_success = datetime.now()
        else:
            self.failure_count += 1
            self.last_failure = datetime.now()

        self.last_executed = datetime.now()

        # Update rolling average
        if self.execution_count == 1:
            self.average_execution_time = execution_time
        else:
            alpha = 0.1  # Exponential moving average factor
            self.average_execution_time = (
                alpha * execution_time +
                (1 - alpha) * self.average_execution_time
            )

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.execution_count == 0:
            return 1.0
        return self.success_count / self.execution_count


@dataclass
class ActionRegistryConfig:
    """Configuration for the action registry."""

    auto_discover: bool = True
    cache_ttl_seconds: float = 300.0
    validation_mode: str = "strict"  # strict or lenient
    allow_custom_actions: bool = True
    max_custom_actions: int = 100

    @classmethod
    def from_env(cls) -> "ActionRegistryConfig":
        """Create configuration from environment variables."""
        return cls(
            auto_discover=os.getenv(
                "JARVIS_ACTION_REGISTRY_AUTO_DISCOVER", "true"
            ).lower() == "true",
            cache_ttl_seconds=float(os.getenv(
                "JARVIS_ACTION_REGISTRY_CACHE_TTL", "300"
            )),
            validation_mode=os.getenv(
                "JARVIS_ACTION_REGISTRY_VALIDATION_MODE", "strict"
            ),
            allow_custom_actions=os.getenv(
                "JARVIS_ACTION_REGISTRY_ALLOW_CUSTOM", "true"
            ).lower() == "true",
            max_custom_actions=int(os.getenv(
                "JARVIS_ACTION_REGISTRY_MAX_CUSTOM", "100"
            )),
        )


# =============================================================================
# ACTION REGISTRY
# =============================================================================


class ActionRegistry:
    """
    Centralized registry for all executable actions.

    This class manages action registration, discovery, and lookup.
    It provides a single source of truth for what actions JARVIS can perform.
    """

    def __init__(self, config: Optional[ActionRegistryConfig] = None):
        """Initialize the action registry."""
        self.config = config or ActionRegistryConfig.from_env()
        self._actions: Dict[ActionType, RegisteredAction] = {}
        self._handlers: Dict[ActionType, ActionHandler] = {}
        self._category_index: Dict[ActionCategory, Set[ActionType]] = defaultdict(set)
        self._risk_index: Dict[ActionRiskLevel, Set[ActionType]] = defaultdict(set)
        self._tag_index: Dict[str, Set[ActionType]] = defaultdict(set)
        self._name_index: Dict[str, ActionType] = {}

        self._is_running = False
        self._callbacks: List[weakref.ref] = []
        self._lock = asyncio.Lock()

        # Register default actions
        self._register_default_actions()

    async def start(self) -> None:
        """Start the action registry."""
        if self._is_running:
            return

        logger.info("Starting ActionRegistry...")
        self._is_running = True

        if self.config.auto_discover:
            await self._discover_actions()

        logger.info(f"ActionRegistry started with {len(self._actions)} registered actions")

    async def stop(self) -> None:
        """Stop the action registry."""
        if not self._is_running:
            return

        logger.info("Stopping ActionRegistry...")
        self._is_running = False
        logger.info("ActionRegistry stopped")

    @property
    def is_running(self) -> bool:
        """Check if registry is running."""
        return self._is_running

    def register_action(
        self,
        metadata: ActionMetadata,
        handler: ActionHandler,
        override: bool = False
    ) -> bool:
        """
        Register an action with the registry.

        Args:
            metadata: Action metadata
            handler: Async handler function
            override: Whether to override existing registration

        Returns:
            True if registration successful
        """
        action_type = metadata.action_type

        if action_type in self._actions and not override:
            logger.warning(
                f"Action {action_type.name} already registered, use override=True to replace"
            )
            return False

        # Create registered action
        registered = RegisteredAction(
            metadata=metadata,
            handler=handler,
            enabled=True
        )

        # Store in main registry
        self._actions[action_type] = registered
        self._handlers[action_type] = handler

        # Update indexes
        self._category_index[metadata.category].add(action_type)
        self._risk_index[metadata.risk_level].add(action_type)
        self._name_index[metadata.name.lower()] = action_type

        for tag in metadata.tags:
            self._tag_index[tag.lower()].add(action_type)

        logger.debug(f"Registered action: {action_type.name}")
        return True

    def unregister_action(self, action_type: ActionType) -> bool:
        """Unregister an action from the registry."""
        if action_type not in self._actions:
            return False

        registered = self._actions[action_type]
        metadata = registered.metadata

        # Remove from indexes
        self._category_index[metadata.category].discard(action_type)
        self._risk_index[metadata.risk_level].discard(action_type)
        self._name_index.pop(metadata.name.lower(), None)

        for tag in metadata.tags:
            self._tag_index[tag.lower()].discard(action_type)

        # Remove from main registry
        del self._actions[action_type]
        self._handlers.pop(action_type, None)

        logger.debug(f"Unregistered action: {action_type.name}")
        return True

    def get_action(self, action_type: ActionType) -> Optional[RegisteredAction]:
        """Get a registered action by type."""
        return self._actions.get(action_type)

    def get_handler(self, action_type: ActionType) -> Optional[ActionHandler]:
        """Get a handler for an action type."""
        return self._handlers.get(action_type)

    def get_metadata(self, action_type: ActionType) -> Optional[ActionMetadata]:
        """Get metadata for an action type."""
        registered = self._actions.get(action_type)
        return registered.metadata if registered else None

    def lookup_by_name(self, name: str) -> Optional[ActionType]:
        """Look up action type by name."""
        return self._name_index.get(name.lower())

    def get_by_category(self, category: ActionCategory) -> List[RegisteredAction]:
        """Get all actions in a category."""
        action_types = self._category_index.get(category, set())
        return [self._actions[at] for at in action_types if at in self._actions]

    def get_by_risk_level(self, risk_level: ActionRiskLevel) -> List[RegisteredAction]:
        """Get all actions at a specific risk level."""
        action_types = self._risk_index.get(risk_level, set())
        return [self._actions[at] for at in action_types if at in self._actions]

    def get_by_tag(self, tag: str) -> List[RegisteredAction]:
        """Get all actions with a specific tag."""
        action_types = self._tag_index.get(tag.lower(), set())
        return [self._actions[at] for at in action_types if at in self._actions]

    def search_actions(
        self,
        query: Optional[str] = None,
        category: Optional[ActionCategory] = None,
        risk_level: Optional[ActionRiskLevel] = None,
        max_risk: Optional[ActionRiskLevel] = None,
        tags: Optional[List[str]] = None,
        enabled_only: bool = True
    ) -> List[RegisteredAction]:
        """
        Search for actions matching criteria.

        Args:
            query: Text search in name/description
            category: Filter by category
            risk_level: Filter by exact risk level
            max_risk: Filter by maximum risk level
            tags: Filter by tags (any match)
            enabled_only: Only return enabled actions

        Returns:
            List of matching registered actions
        """
        results = []

        for action_type, registered in self._actions.items():
            if enabled_only and not registered.enabled:
                continue

            metadata = registered.metadata

            # Category filter
            if category and metadata.category != category:
                continue

            # Risk level filter
            if risk_level and metadata.risk_level != risk_level:
                continue

            # Max risk filter
            if max_risk and metadata.risk_level.value > max_risk.value:
                continue

            # Tags filter
            if tags:
                metadata_tags = {t.lower() for t in metadata.tags}
                search_tags = {t.lower() for t in tags}
                if not metadata_tags.intersection(search_tags):
                    continue

            # Text query filter
            if query:
                query_lower = query.lower()
                searchable = (
                    metadata.name.lower() +
                    metadata.description.lower() +
                    " ".join(t.lower() for t in metadata.tags)
                )
                if query_lower not in searchable:
                    continue

            results.append(registered)

        return results

    def list_all_actions(self) -> List[RegisteredAction]:
        """List all registered actions."""
        return list(self._actions.values())

    def get_statistics(self) -> Dict[str, Any]:
        """Get registry statistics."""
        total_executions = sum(r.execution_count for r in self._actions.values())
        total_successes = sum(r.success_count for r in self._actions.values())

        return {
            "total_actions": len(self._actions),
            "enabled_actions": sum(1 for r in self._actions.values() if r.enabled),
            "disabled_actions": sum(1 for r in self._actions.values() if not r.enabled),
            "total_executions": total_executions,
            "total_successes": total_successes,
            "overall_success_rate": total_successes / max(1, total_executions),
            "by_category": {
                cat.value: len(types)
                for cat, types in self._category_index.items()
            },
            "by_risk_level": {
                level.name: len(types)
                for level, types in self._risk_index.items()
            },
        }

    async def _discover_actions(self) -> None:
        """Auto-discover and register actions from handlers."""
        logger.debug("Auto-discovering actions...")
        # This can be extended to discover actions from plugins/modules
        pass

    def _register_default_actions(self) -> None:
        """Register all default system actions."""

        # Application actions
        self._register_application_actions()

        # File system actions
        self._register_file_actions()

        # System actions
        self._register_system_actions()

        # Media actions
        self._register_media_actions()

        # Display actions
        self._register_display_actions()

        # Notification actions
        self._register_notification_actions()

        # Security actions
        self._register_security_actions()

        # Productivity actions
        self._register_productivity_actions()

        # Input actions
        self._register_input_actions()

        # Web actions
        self._register_web_actions()

    def _register_application_actions(self) -> None:
        """Register application-related actions."""

        # APP_OPEN
        self.register_action(
            ActionMetadata(
                action_type=ActionType.APP_OPEN,
                name="Open Application",
                description="Launch and activate an application",
                category=ActionCategory.APPLICATION,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=15.0,
                supports_rollback=True,
                required_permissions=["application.launch"],
                parameters=[
                    ActionParameter(
                        name="app_name",
                        description="Name of the application to open",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="arguments",
                        description="Optional command line arguments",
                        param_type=list,
                        required=False,
                        default=[]
                    ),
                ],
                tags=["application", "launch", "open"],
            ),
            handler=self._placeholder_handler
        )

        # APP_CLOSE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.APP_CLOSE,
                name="Close Application",
                description="Gracefully quit an application",
                category=ActionCategory.APPLICATION,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=10.0,
                supports_rollback=False,
                required_permissions=["application.quit"],
                safety_constraints=["check_unsaved_work"],
                parameters=[
                    ActionParameter(
                        name="app_name",
                        description="Name of the application to close",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="force",
                        description="Force quit without saving",
                        param_type=bool,
                        required=False,
                        default=False
                    ),
                ],
                tags=["application", "quit", "close"],
            ),
            handler=self._placeholder_handler
        )

        # APP_FOCUS
        self.register_action(
            ActionMetadata(
                action_type=ActionType.APP_FOCUS,
                name="Focus Application",
                description="Bring an application to the foreground",
                category=ActionCategory.APPLICATION,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["application.focus"],
                parameters=[
                    ActionParameter(
                        name="app_name",
                        description="Name of the application to focus",
                        param_type=str,
                        required=True
                    ),
                ],
                tags=["application", "focus", "activate"],
            ),
            handler=self._placeholder_handler
        )

        # APP_HIDE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.APP_HIDE,
                name="Hide Application",
                description="Hide an application from view",
                category=ActionCategory.APPLICATION,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["application.hide"],
                parameters=[
                    ActionParameter(
                        name="app_name",
                        description="Name of the application to hide",
                        param_type=str,
                        required=True
                    ),
                ],
                tags=["application", "hide"],
            ),
            handler=self._placeholder_handler
        )

        # APP_LIST
        self.register_action(
            ActionMetadata(
                action_type=ActionType.APP_LIST,
                name="List Applications",
                description="Get list of running applications",
                category=ActionCategory.APPLICATION,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["application.list"],
                parameters=[
                    ActionParameter(
                        name="include_hidden",
                        description="Include hidden applications",
                        param_type=bool,
                        required=False,
                        default=False
                    ),
                ],
                tags=["application", "list", "query"],
            ),
            handler=self._placeholder_handler
        )

    def _register_file_actions(self) -> None:
        """Register file system actions."""

        # FILE_OPEN
        self.register_action(
            ActionMetadata(
                action_type=ActionType.FILE_OPEN,
                name="Open File",
                description="Open a file with its default application",
                category=ActionCategory.FILE_SYSTEM,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=10.0,
                supports_rollback=False,
                required_permissions=["file.read"],
                safety_constraints=["safe_path_only"],
                parameters=[
                    ActionParameter(
                        name="file_path",
                        description="Path to the file to open",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="app_name",
                        description="Specific application to open with",
                        param_type=str,
                        required=False
                    ),
                ],
                tags=["file", "open"],
            ),
            handler=self._placeholder_handler
        )

        # FILE_CREATE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.FILE_CREATE,
                name="Create File",
                description="Create a new file",
                category=ActionCategory.FILE_SYSTEM,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=10.0,
                supports_rollback=True,
                required_permissions=["file.write"],
                safety_constraints=["safe_path_only", "no_overwrite"],
                parameters=[
                    ActionParameter(
                        name="file_path",
                        description="Path for the new file",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="content",
                        description="Initial file content",
                        param_type=str,
                        required=False,
                        default=""
                    ),
                ],
                tags=["file", "create", "write"],
            ),
            handler=self._placeholder_handler
        )

        # FILE_DELETE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.FILE_DELETE,
                name="Delete File",
                description="Delete a file (moves to trash by default)",
                category=ActionCategory.FILE_SYSTEM,
                risk_level=ActionRiskLevel.HIGH,
                timeout_seconds=10.0,
                supports_rollback=True,
                requires_confirmation=True,
                required_permissions=["file.delete"],
                safety_constraints=["safe_path_only", "not_system_file"],
                parameters=[
                    ActionParameter(
                        name="file_path",
                        description="Path to the file to delete",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="permanent",
                        description="Permanently delete (bypass trash)",
                        param_type=bool,
                        required=False,
                        default=False
                    ),
                ],
                tags=["file", "delete", "remove"],
            ),
            handler=self._placeholder_handler
        )

        # FILE_SEARCH
        self.register_action(
            ActionMetadata(
                action_type=ActionType.FILE_SEARCH,
                name="Search Files",
                description="Search for files using Spotlight",
                category=ActionCategory.FILE_SYSTEM,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=30.0,
                supports_rollback=False,
                required_permissions=["file.read"],
                parameters=[
                    ActionParameter(
                        name="query",
                        description="Search query",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="directory",
                        description="Directory to search in",
                        param_type=str,
                        required=False
                    ),
                    ActionParameter(
                        name="max_results",
                        description="Maximum number of results",
                        param_type=int,
                        required=False,
                        default=50,
                        min_value=1,
                        max_value=1000
                    ),
                ],
                tags=["file", "search", "find"],
            ),
            handler=self._placeholder_handler
        )

    def _register_system_actions(self) -> None:
        """Register system control actions."""

        # SYSTEM_INFO
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SYSTEM_INFO,
                name="Get System Info",
                description="Get system information (CPU, memory, disk)",
                category=ActionCategory.SYSTEM,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["system.read"],
                parameters=[],
                tags=["system", "info", "status"],
            ),
            handler=self._placeholder_handler
        )

        # SYSTEM_SLEEP
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SYSTEM_SLEEP,
                name="System Sleep",
                description="Put the system to sleep",
                category=ActionCategory.SYSTEM,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=5.0,
                supports_rollback=False,
                requires_confirmation=True,
                required_permissions=["system.power"],
                safety_constraints=["check_unsaved_work", "check_active_processes"],
                parameters=[],
                tags=["system", "sleep", "power"],
            ),
            handler=self._placeholder_handler
        )

        # SYSTEM_LOCK
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SYSTEM_LOCK,
                name="Lock System",
                description="Lock the screen",
                category=ActionCategory.SYSTEM,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["system.lock"],
                parameters=[],
                tags=["system", "lock", "security"],
            ),
            handler=self._placeholder_handler
        )

        # SYSTEM_UNLOCK
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SYSTEM_UNLOCK,
                name="Unlock System",
                description="Unlock the screen (requires authentication)",
                category=ActionCategory.SECURITY,
                risk_level=ActionRiskLevel.HIGH,
                timeout_seconds=15.0,
                supports_rollback=False,
                requires_authentication=True,
                required_permissions=["system.unlock", "security.authenticate"],
                parameters=[],
                tags=["system", "unlock", "security"],
            ),
            handler=self._placeholder_handler
        )

    def _register_media_actions(self) -> None:
        """Register media control actions."""

        # VOLUME_SET
        self.register_action(
            ActionMetadata(
                action_type=ActionType.VOLUME_SET,
                name="Set Volume",
                description="Set the system volume level",
                category=ActionCategory.MEDIA,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["media.volume"],
                parameters=[
                    ActionParameter(
                        name="level",
                        description="Volume level (0-100)",
                        param_type=int,
                        required=True,
                        min_value=0,
                        max_value=100
                    ),
                ],
                tags=["media", "volume", "audio"],
            ),
            handler=self._placeholder_handler
        )

        # VOLUME_MUTE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.VOLUME_MUTE,
                name="Mute Volume",
                description="Mute the system volume",
                category=ActionCategory.MEDIA,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["media.volume"],
                parameters=[],
                tags=["media", "volume", "mute", "audio"],
            ),
            handler=self._placeholder_handler
        )

        # VOLUME_UNMUTE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.VOLUME_UNMUTE,
                name="Unmute Volume",
                description="Unmute the system volume",
                category=ActionCategory.MEDIA,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["media.volume"],
                parameters=[],
                tags=["media", "volume", "unmute", "audio"],
            ),
            handler=self._placeholder_handler
        )

        # BRIGHTNESS_SET
        self.register_action(
            ActionMetadata(
                action_type=ActionType.BRIGHTNESS_SET,
                name="Set Brightness",
                description="Set the display brightness",
                category=ActionCategory.MEDIA,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["display.brightness"],
                parameters=[
                    ActionParameter(
                        name="level",
                        description="Brightness level (0.0-1.0)",
                        param_type=float,
                        required=True,
                        min_value=0.0,
                        max_value=1.0
                    ),
                ],
                tags=["display", "brightness"],
            ),
            handler=self._placeholder_handler
        )

    def _register_display_actions(self) -> None:
        """Register display control actions."""

        # DISPLAY_SLEEP
        self.register_action(
            ActionMetadata(
                action_type=ActionType.DISPLAY_SLEEP,
                name="Sleep Display",
                description="Put the display to sleep",
                category=ActionCategory.DISPLAY,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["display.power"],
                parameters=[],
                tags=["display", "sleep", "screen"],
            ),
            handler=self._placeholder_handler
        )

        # SCREENSHOT
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SCREENSHOT,
                name="Take Screenshot",
                description="Capture a screenshot",
                category=ActionCategory.DISPLAY,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=10.0,
                supports_rollback=True,
                required_permissions=["display.capture"],
                parameters=[
                    ActionParameter(
                        name="save_path",
                        description="Path to save screenshot",
                        param_type=str,
                        required=False
                    ),
                    ActionParameter(
                        name="region",
                        description="Region to capture (x, y, width, height)",
                        param_type=dict,
                        required=False
                    ),
                ],
                tags=["display", "screenshot", "capture"],
            ),
            handler=self._placeholder_handler
        )

    def _register_notification_actions(self) -> None:
        """Register notification actions."""

        # NOTIFICATION_SEND
        self.register_action(
            ActionMetadata(
                action_type=ActionType.NOTIFICATION_SEND,
                name="Send Notification",
                description="Display a system notification",
                category=ActionCategory.NOTIFICATION,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["notification.send"],
                parameters=[
                    ActionParameter(
                        name="title",
                        description="Notification title",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="message",
                        description="Notification message",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="sound",
                        description="Play notification sound",
                        param_type=bool,
                        required=False,
                        default=True
                    ),
                ],
                tags=["notification", "alert", "message"],
            ),
            handler=self._placeholder_handler
        )

        # DND_ENABLE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.DND_ENABLE,
                name="Enable Do Not Disturb",
                description="Enable Do Not Disturb mode",
                category=ActionCategory.NOTIFICATION,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["notification.dnd"],
                parameters=[
                    ActionParameter(
                        name="duration_minutes",
                        description="Duration in minutes (0 for indefinite)",
                        param_type=int,
                        required=False,
                        default=0,
                        min_value=0
                    ),
                ],
                tags=["notification", "dnd", "focus"],
            ),
            handler=self._placeholder_handler
        )

        # DND_DISABLE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.DND_DISABLE,
                name="Disable Do Not Disturb",
                description="Disable Do Not Disturb mode",
                category=ActionCategory.NOTIFICATION,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=5.0,
                supports_rollback=True,
                required_permissions=["notification.dnd"],
                parameters=[],
                tags=["notification", "dnd", "focus"],
            ),
            handler=self._placeholder_handler
        )

    def _register_security_actions(self) -> None:
        """Register security actions."""

        # SCREEN_LOCK
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SCREEN_LOCK,
                name="Lock Screen",
                description="Lock the screen for security",
                category=ActionCategory.SECURITY,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["security.lock"],
                parameters=[],
                tags=["security", "lock", "screen"],
            ),
            handler=self._placeholder_handler
        )

        # SCREEN_UNLOCK
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SCREEN_UNLOCK,
                name="Unlock Screen",
                description="Unlock the screen (requires voice biometric)",
                category=ActionCategory.SECURITY,
                risk_level=ActionRiskLevel.CRITICAL,
                timeout_seconds=30.0,
                supports_rollback=False,
                requires_authentication=True,
                required_permissions=["security.unlock", "biometric.verify"],
                safety_constraints=["biometric_required", "audit_logging"],
                parameters=[],
                tags=["security", "unlock", "screen", "biometric"],
            ),
            handler=self._placeholder_handler
        )

        # SECURITY_ALERT
        self.register_action(
            ActionMetadata(
                action_type=ActionType.SECURITY_ALERT,
                name="Security Alert",
                description="Trigger a security alert response",
                category=ActionCategory.SECURITY,
                risk_level=ActionRiskLevel.HIGH,
                timeout_seconds=10.0,
                supports_rollback=True,
                required_permissions=["security.alert"],
                parameters=[
                    ActionParameter(
                        name="alert_type",
                        description="Type of security alert",
                        param_type=str,
                        required=True,
                        choices=["intrusion", "data_exposure", "suspicious_activity"]
                    ),
                    ActionParameter(
                        name="target",
                        description="Target of the alert (app, file, etc.)",
                        param_type=str,
                        required=True
                    ),
                ],
                tags=["security", "alert", "protection"],
            ),
            handler=self._placeholder_handler
        )

    def _register_productivity_actions(self) -> None:
        """Register productivity actions."""

        # WORKSPACE_ORGANIZE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.WORKSPACE_ORGANIZE,
                name="Organize Workspace",
                description="Organize windows and applications for a task",
                category=ActionCategory.PRODUCTIVITY,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=15.0,
                supports_rollback=True,
                required_permissions=["workspace.organize"],
                parameters=[
                    ActionParameter(
                        name="task",
                        description="Task description for context",
                        param_type=str,
                        required=False
                    ),
                    ActionParameter(
                        name="layout",
                        description="Window layout configuration",
                        param_type=dict,
                        required=False
                    ),
                ],
                tags=["productivity", "workspace", "organize"],
            ),
            handler=self._placeholder_handler
        )

        # WORKSPACE_CLEANUP
        self.register_action(
            ActionMetadata(
                action_type=ActionType.WORKSPACE_CLEANUP,
                name="Cleanup Workspace",
                description="Close unnecessary windows and minimize distractions",
                category=ActionCategory.PRODUCTIVITY,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=15.0,
                supports_rollback=True,
                required_permissions=["workspace.cleanup"],
                safety_constraints=["check_unsaved_work"],
                parameters=[
                    ActionParameter(
                        name="preserve_apps",
                        description="Apps to preserve open",
                        param_type=list,
                        required=False,
                        default=[]
                    ),
                ],
                tags=["productivity", "workspace", "cleanup"],
            ),
            handler=self._placeholder_handler
        )

        # FOCUS_MODE_START
        self.register_action(
            ActionMetadata(
                action_type=ActionType.FOCUS_MODE_START,
                name="Start Focus Mode",
                description="Enter focus mode to minimize distractions",
                category=ActionCategory.PRODUCTIVITY,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=10.0,
                supports_rollback=True,
                required_permissions=["focus.control"],
                parameters=[
                    ActionParameter(
                        name="duration_minutes",
                        description="Focus duration in minutes",
                        param_type=int,
                        required=False,
                        default=25,
                        min_value=5,
                        max_value=240
                    ),
                    ActionParameter(
                        name="block_apps",
                        description="Apps to hide/block during focus",
                        param_type=list,
                        required=False,
                        default=[]
                    ),
                ],
                tags=["productivity", "focus", "concentration"],
            ),
            handler=self._placeholder_handler
        )

        # MEETING_PREPARE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.MEETING_PREPARE,
                name="Prepare for Meeting",
                description="Prepare workspace for an upcoming meeting",
                category=ActionCategory.PRODUCTIVITY,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=15.0,
                supports_rollback=True,
                required_permissions=["meeting.prepare"],
                parameters=[
                    ActionParameter(
                        name="meeting_app",
                        description="Meeting application (Zoom, Meet, etc.)",
                        param_type=str,
                        required=False,
                        default="zoom"
                    ),
                    ActionParameter(
                        name="hide_sensitive",
                        description="Hide sensitive applications",
                        param_type=bool,
                        required=False,
                        default=True
                    ),
                ],
                tags=["productivity", "meeting", "prepare"],
            ),
            handler=self._placeholder_handler
        )

        # ROUTINE_EXECUTE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.ROUTINE_EXECUTE,
                name="Execute Routine",
                description="Execute a predefined routine/workflow",
                category=ActionCategory.PRODUCTIVITY,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=60.0,
                supports_rollback=True,
                required_permissions=["routine.execute"],
                parameters=[
                    ActionParameter(
                        name="routine_name",
                        description="Name of the routine to execute",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="params",
                        description="Routine parameters",
                        param_type=dict,
                        required=False,
                        default={}
                    ),
                ],
                tags=["productivity", "routine", "workflow", "automation"],
            ),
            handler=self._placeholder_handler
        )

    def _register_input_actions(self) -> None:
        """Register input simulation actions."""

        # KEYSTROKE_SEND
        self.register_action(
            ActionMetadata(
                action_type=ActionType.KEYSTROKE_SEND,
                name="Send Keystroke",
                description="Send a keyboard keystroke or combination",
                category=ActionCategory.HARDWARE,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["input.keyboard"],
                safety_constraints=["target_app_required"],
                parameters=[
                    ActionParameter(
                        name="key",
                        description="Key or key combination (e.g., 'cmd+c')",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="target_app",
                        description="Target application for keystroke",
                        param_type=str,
                        required=False
                    ),
                ],
                tags=["input", "keyboard", "keystroke"],
            ),
            handler=self._placeholder_handler
        )

        # MOUSE_CLICK
        self.register_action(
            ActionMetadata(
                action_type=ActionType.MOUSE_CLICK,
                name="Mouse Click",
                description="Perform a mouse click at coordinates",
                category=ActionCategory.HARDWARE,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=5.0,
                supports_rollback=False,
                required_permissions=["input.mouse"],
                parameters=[
                    ActionParameter(
                        name="x",
                        description="X coordinate",
                        param_type=int,
                        required=True,
                        min_value=0
                    ),
                    ActionParameter(
                        name="y",
                        description="Y coordinate",
                        param_type=int,
                        required=True,
                        min_value=0
                    ),
                    ActionParameter(
                        name="button",
                        description="Mouse button",
                        param_type=str,
                        required=False,
                        default="left",
                        choices=["left", "right", "middle"]
                    ),
                    ActionParameter(
                        name="clicks",
                        description="Number of clicks",
                        param_type=int,
                        required=False,
                        default=1,
                        min_value=1,
                        max_value=3
                    ),
                ],
                tags=["input", "mouse", "click"],
            ),
            handler=self._placeholder_handler
        )

        # TEXT_TYPE
        self.register_action(
            ActionMetadata(
                action_type=ActionType.TEXT_TYPE,
                name="Type Text",
                description="Type text string",
                category=ActionCategory.HARDWARE,
                risk_level=ActionRiskLevel.MODERATE,
                timeout_seconds=30.0,
                supports_rollback=False,
                required_permissions=["input.keyboard"],
                safety_constraints=["no_sensitive_data"],
                parameters=[
                    ActionParameter(
                        name="text",
                        description="Text to type",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="delay_ms",
                        description="Delay between keystrokes in milliseconds",
                        param_type=int,
                        required=False,
                        default=0,
                        min_value=0,
                        max_value=1000
                    ),
                ],
                tags=["input", "keyboard", "type", "text"],
            ),
            handler=self._placeholder_handler
        )

    def _register_web_actions(self) -> None:
        """Register web-related actions."""

        # WEB_OPEN
        self.register_action(
            ActionMetadata(
                action_type=ActionType.WEB_OPEN,
                name="Open URL",
                description="Open a URL in the default or specified browser",
                category=ActionCategory.NETWORK,
                risk_level=ActionRiskLevel.LOW,
                timeout_seconds=15.0,
                supports_rollback=False,
                required_permissions=["web.navigate"],
                safety_constraints=["url_whitelist_check"],
                parameters=[
                    ActionParameter(
                        name="url",
                        description="URL to open",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="browser",
                        description="Browser to use",
                        param_type=str,
                        required=False
                    ),
                ],
                tags=["web", "url", "browser", "navigate"],
            ),
            handler=self._placeholder_handler
        )

        # WEB_SEARCH
        self.register_action(
            ActionMetadata(
                action_type=ActionType.WEB_SEARCH,
                name="Web Search",
                description="Perform a web search",
                category=ActionCategory.NETWORK,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=15.0,
                supports_rollback=False,
                required_permissions=["web.search"],
                parameters=[
                    ActionParameter(
                        name="query",
                        description="Search query",
                        param_type=str,
                        required=True
                    ),
                    ActionParameter(
                        name="engine",
                        description="Search engine",
                        param_type=str,
                        required=False,
                        default="google",
                        choices=["google", "bing", "duckduckgo"]
                    ),
                    ActionParameter(
                        name="browser",
                        description="Browser to use",
                        param_type=str,
                        required=False
                    ),
                ],
                tags=["web", "search"],
            ),
            handler=self._placeholder_handler
        )

        # WEB_NEW_TAB
        self.register_action(
            ActionMetadata(
                action_type=ActionType.WEB_NEW_TAB,
                name="New Browser Tab",
                description="Open a new browser tab",
                category=ActionCategory.NETWORK,
                risk_level=ActionRiskLevel.MINIMAL,
                timeout_seconds=10.0,
                supports_rollback=True,
                required_permissions=["web.navigate"],
                parameters=[
                    ActionParameter(
                        name="url",
                        description="URL to open in new tab",
                        param_type=str,
                        required=False
                    ),
                    ActionParameter(
                        name="browser",
                        description="Browser to use",
                        param_type=str,
                        required=False
                    ),
                ],
                tags=["web", "browser", "tab"],
            ),
            handler=self._placeholder_handler
        )

    async def _placeholder_handler(self, **kwargs) -> Dict[str, Any]:
        """Placeholder handler for actions - will be replaced with real implementations."""
        return {
            "success": True,
            "message": "Placeholder handler executed",
            "params": kwargs
        }


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================


_registry_instance: Optional[ActionRegistry] = None
_registry_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_action_registry() -> ActionRegistry:
    """Get the global action registry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ActionRegistry()
    return _registry_instance


async def start_action_registry() -> ActionRegistry:
    """Start the global action registry."""
    async with _registry_lock:
        registry = get_action_registry()
        if not registry.is_running:
            await registry.start()
        return registry


async def stop_action_registry() -> None:
    """Stop the global action registry."""
    async with _registry_lock:
        global _registry_instance
        if _registry_instance and _registry_instance.is_running:
            await _registry_instance.stop()
