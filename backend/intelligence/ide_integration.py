"""
IDE Integration - VS Code/Cursor Extension Support
====================================================

Production-grade IDE integration system with:
- VS Code/Cursor extension management
- Keyboard shortcut handling
- Command palette integration
- Context menu actions
- Statusbar items
- Webview panels

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    JARVIS IDE Integration v1.0                           │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
    │   │   VS Code   │     │  Extension  │     │   JARVIS    │               │
    │   │   Cursor    │◀───▶│   Bridge    │◀───▶│   Backend   │               │
    │   └─────────────┘     └─────────────┘     └─────────────┘               │
    │          │                   │                   │                      │
    │          └───────────────────┴───────────────────┘                      │
    │                              │                                          │
    │                    ┌─────────▼─────────┐                                │
    │                    │   Command Router  │                                │
    │                    │                   │                                │
    │                    │ • Keyboard cmds   │                                │
    │                    │ • Context menu    │                                │
    │                    │ • Command palette │                                │
    │                    └───────────────────┘                                │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger("JARVIS.IDEIntegration")


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class IDEIntegrationConfig:
    """Environment-driven IDE integration configuration."""

    # Extension settings
    extension_id: str = os.getenv("IDE_EXTENSION_ID", "jarvis.jarvis-ai")
    extension_version: str = os.getenv("IDE_EXTENSION_VERSION", "1.0.0")
    extension_name: str = os.getenv("IDE_EXTENSION_NAME", "JARVIS AI Assistant")

    # Communication
    socket_path: str = os.getenv("IDE_SOCKET_PATH", str(Path.home() / ".jarvis/ide.sock"))
    websocket_port: int = int(os.getenv("IDE_WEBSOCKET_PORT", "2088"))
    use_websocket: bool = os.getenv("IDE_USE_WEBSOCKET", "true").lower() == "true"

    # Features
    enable_context_menu: bool = os.getenv("IDE_CONTEXT_MENU", "true").lower() == "true"
    enable_statusbar: bool = os.getenv("IDE_STATUSBAR", "true").lower() == "true"
    enable_inline_completion: bool = os.getenv("IDE_INLINE_COMPLETION", "true").lower() == "true"
    enable_code_lens: bool = os.getenv("IDE_CODE_LENS", "true").lower() == "true"

    # Keyboard shortcuts
    shortcut_prefix: str = os.getenv("IDE_SHORTCUT_PREFIX", "cmd+shift+j")  # macOS
    enable_shortcuts: bool = os.getenv("IDE_ENABLE_SHORTCUTS", "true").lower() == "true"

    # Cross-repo
    enable_cross_repo: bool = os.getenv("IDE_CROSS_REPO_ENABLED", "true").lower() == "true"

    @classmethod
    def from_env(cls) -> "IDEIntegrationConfig":
        """Create configuration from environment."""
        return cls()


# =============================================================================
# ENUMS
# =============================================================================

class CommandCategory(Enum):
    """Categories of IDE commands."""
    IMPROVEMENT = "improvement"
    REFACTORING = "refactoring"
    EXPLANATION = "explanation"
    GENERATION = "generation"
    DEBUGGING = "debugging"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    NAVIGATION = "navigation"


class ShortcutPlatform(Enum):
    """Platform for keyboard shortcuts."""
    MACOS = "mac"
    WINDOWS = "win"
    LINUX = "linux"


class ContextMenuLocation(Enum):
    """Where context menu appears."""
    EDITOR = "editor"
    EXPLORER = "explorer"
    TERMINAL = "terminal"
    DEBUG = "debug"


class StatusBarAlignment(Enum):
    """Alignment of statusbar items."""
    LEFT = "left"
    RIGHT = "right"


class WebviewPanelType(Enum):
    """Types of webview panels."""
    CHAT = "chat"
    DIFF = "diff"
    PREVIEW = "preview"
    SETTINGS = "settings"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class KeyboardShortcut:
    """Represents a keyboard shortcut."""
    key: str  # e.g., "cmd+k cmd+i"
    mac: Optional[str] = None  # macOS-specific
    win: Optional[str] = None  # Windows-specific
    linux: Optional[str] = None  # Linux-specific
    when: Optional[str] = None  # Context condition

    def get_for_platform(self, platform: ShortcutPlatform) -> str:
        """Get shortcut for specific platform."""
        if platform == ShortcutPlatform.MACOS and self.mac:
            return self.mac
        elif platform == ShortcutPlatform.WINDOWS and self.win:
            return self.win
        elif platform == ShortcutPlatform.LINUX and self.linux:
            return self.linux
        return self.key

    def to_dict(self) -> Dict[str, Any]:
        """Convert to VS Code keybinding format."""
        result = {"key": self.key}
        if self.mac:
            result["mac"] = self.mac
        if self.win:
            result["win"] = self.win
        if self.linux:
            result["linux"] = self.linux
        if self.when:
            result["when"] = self.when
        return result


@dataclass
class Command:
    """Represents an IDE command."""
    id: str
    title: str
    category: CommandCategory
    shortcut: Optional[KeyboardShortcut] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    handler: Optional[Callable] = None
    enabled_when: Optional[str] = None

    def to_package_json(self) -> Dict[str, Any]:
        """Convert to VS Code package.json format."""
        return {
            "command": f"jarvis.{self.id}",
            "title": self.title,
            "category": "JARVIS",
            "icon": self.icon or "$(sparkle)",
        }


@dataclass
class ContextMenuItem:
    """Represents a context menu item."""
    command_id: str
    group: str = "navigation"
    when: Optional[str] = None
    location: ContextMenuLocation = ContextMenuLocation.EDITOR

    def to_package_json(self) -> Dict[str, Any]:
        """Convert to VS Code menu contribution."""
        result = {
            "command": f"jarvis.{self.command_id}",
            "group": self.group,
        }
        if self.when:
            result["when"] = self.when
        return result


@dataclass
class StatusBarItem:
    """Represents a statusbar item."""
    id: str
    text: str
    tooltip: Optional[str] = None
    command: Optional[str] = None
    alignment: StatusBarAlignment = StatusBarAlignment.LEFT
    priority: int = 100
    show_when: Optional[str] = None

    def to_activation(self) -> Dict[str, Any]:
        """Convert to activation data."""
        return {
            "id": f"jarvis.statusbar.{self.id}",
            "text": self.text,
            "tooltip": self.tooltip,
            "command": f"jarvis.{self.command}" if self.command else None,
            "alignment": self.alignment.value,
            "priority": self.priority,
        }


@dataclass
class CodeLens:
    """Represents a code lens annotation."""
    range_start: int
    range_end: int
    command_id: str
    title: str
    tooltip: Optional[str] = None
    arguments: List[Any] = field(default_factory=list)


@dataclass
class InlineCompletionItem:
    """Represents an inline completion suggestion."""
    text: str
    range_start: int
    range_end: int
    filter_text: Optional[str] = None
    insert_text: Optional[str] = None


@dataclass
class WebviewPanel:
    """Represents a webview panel."""
    id: str
    title: str
    panel_type: WebviewPanelType
    html_content: str = ""
    scripts: List[str] = field(default_factory=list)
    styles: List[str] = field(default_factory=list)
    retain_context_when_hidden: bool = True


@dataclass
class IDEContext:
    """Current IDE context information."""
    file_path: Optional[Path] = None
    language_id: Optional[str] = None
    selection_start: Optional[Tuple[int, int]] = None  # (line, char)
    selection_end: Optional[Tuple[int, int]] = None
    selected_text: Optional[str] = None
    cursor_position: Optional[Tuple[int, int]] = None
    visible_range: Optional[Tuple[int, int]] = None  # (start_line, end_line)
    workspace_folders: List[Path] = field(default_factory=list)
    active_editor_content: Optional[str] = None


# =============================================================================
# COMMAND REGISTRY
# =============================================================================

class CommandRegistry:
    """
    Registry for IDE commands.

    Manages all available JARVIS commands and their handlers.
    """

    def __init__(self, config: IDEIntegrationConfig):
        self.config = config
        self._commands: Dict[str, Command] = {}
        self._handlers: Dict[str, Callable] = {}
        self._register_builtin_commands()

    def _register_builtin_commands(self) -> None:
        """Register built-in JARVIS commands."""

        # Improvement commands
        self.register(Command(
            id="improve",
            title="Improve Code",
            category=CommandCategory.IMPROVEMENT,
            shortcut=KeyboardShortcut(
                key="cmd+shift+i",
                mac="cmd+shift+i",
                win="ctrl+shift+i",
                when="editorTextFocus",
            ),
            icon="$(sparkle)",
            description="Improve the selected code with AI",
        ))

        self.register(Command(
            id="improve.performance",
            title="Improve Performance",
            category=CommandCategory.IMPROVEMENT,
            shortcut=KeyboardShortcut(
                key="cmd+shift+p cmd+i",
                mac="cmd+shift+p cmd+i",
                win="ctrl+shift+p ctrl+i",
                when="editorTextFocus",
            ),
            description="Optimize code for better performance",
        ))

        # Refactoring commands
        self.register(Command(
            id="refactor.extract",
            title="Extract to Function",
            category=CommandCategory.REFACTORING,
            shortcut=KeyboardShortcut(
                key="cmd+shift+r cmd+e",
                mac="cmd+shift+r cmd+e",
                win="ctrl+shift+r ctrl+e",
                when="editorHasSelection",
            ),
            icon="$(symbol-method)",
            description="Extract selected code to a new function",
        ))

        self.register(Command(
            id="refactor.rename",
            title="Smart Rename",
            category=CommandCategory.REFACTORING,
            shortcut=KeyboardShortcut(
                key="cmd+shift+r cmd+r",
                mac="cmd+shift+r cmd+r",
                win="ctrl+shift+r ctrl+r",
                when="editorTextFocus",
            ),
            description="Intelligently rename symbol across project",
        ))

        self.register(Command(
            id="refactor.inline",
            title="Inline Variable",
            category=CommandCategory.REFACTORING,
            shortcut=KeyboardShortcut(
                key="cmd+shift+r cmd+i",
                mac="cmd+shift+r cmd+i",
                win="ctrl+shift+r ctrl+i",
                when="editorTextFocus",
            ),
            description="Inline variable at all use sites",
        ))

        # Explanation commands
        self.register(Command(
            id="explain",
            title="Explain Code",
            category=CommandCategory.EXPLANATION,
            shortcut=KeyboardShortcut(
                key="cmd+shift+e",
                mac="cmd+shift+e",
                win="ctrl+shift+e",
                when="editorTextFocus",
            ),
            icon="$(question)",
            description="Get AI explanation of the selected code",
        ))

        self.register(Command(
            id="explain.complexity",
            title="Explain Complexity",
            category=CommandCategory.EXPLANATION,
            description="Analyze and explain code complexity",
        ))

        # Generation commands
        self.register(Command(
            id="generate.docstring",
            title="Generate Docstring",
            category=CommandCategory.GENERATION,
            shortcut=KeyboardShortcut(
                key="cmd+shift+d",
                mac="cmd+shift+d",
                win="ctrl+shift+d",
                when="editorTextFocus",
            ),
            icon="$(symbol-text)",
            description="Generate documentation for function/class",
        ))

        self.register(Command(
            id="generate.types",
            title="Generate Type Hints",
            category=CommandCategory.GENERATION,
            description="Add type hints to function",
        ))

        self.register(Command(
            id="generate.tests",
            title="Generate Tests",
            category=CommandCategory.TESTING,
            shortcut=KeyboardShortcut(
                key="cmd+shift+t",
                mac="cmd+shift+t",
                win="ctrl+shift+t",
                when="editorTextFocus",
            ),
            icon="$(beaker)",
            description="Generate unit tests for selected code",
        ))

        # Debugging commands
        self.register(Command(
            id="debug.fix",
            title="Fix Bug",
            category=CommandCategory.DEBUGGING,
            shortcut=KeyboardShortcut(
                key="cmd+shift+f",
                mac="cmd+shift+f",
                win="ctrl+shift+f",
                when="editorTextFocus",
            ),
            icon="$(debug)",
            description="Analyze and fix the bug at cursor",
        ))

        # Navigation commands
        self.register(Command(
            id="goto.implementation",
            title="Go to AI Implementation",
            category=CommandCategory.NAVIGATION,
            description="Navigate to AI-suggested implementation",
        ))

        # Chat command
        self.register(Command(
            id="chat.open",
            title="Open JARVIS Chat",
            category=CommandCategory.EXPLANATION,
            shortcut=KeyboardShortcut(
                key="cmd+shift+j",
                mac="cmd+shift+j",
                win="ctrl+shift+j",
            ),
            icon="$(comment-discussion)",
            description="Open JARVIS AI chat panel",
        ))

    def register(self, command: Command) -> None:
        """Register a command."""
        self._commands[command.id] = command
        logger.debug(f"Registered command: {command.id}")

    def unregister(self, command_id: str) -> None:
        """Unregister a command."""
        if command_id in self._commands:
            del self._commands[command_id]

    def get_command(self, command_id: str) -> Optional[Command]:
        """Get a command by ID."""
        return self._commands.get(command_id)

    def get_commands_by_category(self, category: CommandCategory) -> List[Command]:
        """Get all commands in a category."""
        return [c for c in self._commands.values() if c.category == category]

    def get_all_commands(self) -> List[Command]:
        """Get all registered commands."""
        return list(self._commands.values())

    def set_handler(self, command_id: str, handler: Callable) -> None:
        """Set handler for a command."""
        self._handlers[command_id] = handler

    async def execute(
        self,
        command_id: str,
        context: IDEContext,
        args: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Execute a command."""
        handler = self._handlers.get(command_id)
        if not handler:
            logger.warning(f"No handler for command: {command_id}")
            return None

        try:
            if asyncio.iscoroutinefunction(handler):
                return await handler(context, args or {})
            else:
                return handler(context, args or {})
        except Exception as e:
            logger.error(f"Command execution error: {e}")
            raise

    def generate_keybindings(self) -> List[Dict[str, Any]]:
        """Generate VS Code keybindings.json content."""
        keybindings = []
        for command in self._commands.values():
            if command.shortcut:
                binding = command.shortcut.to_dict()
                binding["command"] = f"jarvis.{command.id}"
                keybindings.append(binding)
        return keybindings

    def generate_package_json_commands(self) -> List[Dict[str, Any]]:
        """Generate commands section for package.json."""
        return [c.to_package_json() for c in self._commands.values()]


# =============================================================================
# CONTEXT MENU MANAGER
# =============================================================================

class ContextMenuManager:
    """
    Manages context menu contributions.

    Handles right-click menus in editor, explorer, etc.
    """

    def __init__(self, config: IDEIntegrationConfig):
        self.config = config
        self._items: Dict[ContextMenuLocation, List[ContextMenuItem]] = {
            loc: [] for loc in ContextMenuLocation
        }
        self._register_builtin_menus()

    def _register_builtin_menus(self) -> None:
        """Register built-in context menu items."""
        if not self.config.enable_context_menu:
            return

        # Editor context menu
        self.add_item(ContextMenuItem(
            command_id="improve",
            group="jarvis@1",
            when="editorTextFocus",
            location=ContextMenuLocation.EDITOR,
        ))

        self.add_item(ContextMenuItem(
            command_id="explain",
            group="jarvis@2",
            when="editorTextFocus && editorHasSelection",
            location=ContextMenuLocation.EDITOR,
        ))

        self.add_item(ContextMenuItem(
            command_id="refactor.extract",
            group="jarvis@3",
            when="editorHasSelection",
            location=ContextMenuLocation.EDITOR,
        ))

        self.add_item(ContextMenuItem(
            command_id="generate.docstring",
            group="jarvis@4",
            when="editorTextFocus",
            location=ContextMenuLocation.EDITOR,
        ))

        self.add_item(ContextMenuItem(
            command_id="generate.tests",
            group="jarvis@5",
            when="editorTextFocus",
            location=ContextMenuLocation.EDITOR,
        ))

        # Explorer context menu
        self.add_item(ContextMenuItem(
            command_id="improve",
            group="jarvis",
            when="resourceExtname =~ /\\.(py|js|ts|tsx|jsx)$/",
            location=ContextMenuLocation.EXPLORER,
        ))

    def add_item(self, item: ContextMenuItem) -> None:
        """Add a context menu item."""
        self._items[item.location].append(item)

    def get_items(self, location: ContextMenuLocation) -> List[ContextMenuItem]:
        """Get items for a location."""
        return self._items.get(location, [])

    def generate_menu_contributions(self) -> Dict[str, List[Dict]]:
        """Generate VS Code menu contributions."""
        contributions = {}

        location_map = {
            ContextMenuLocation.EDITOR: "editor/context",
            ContextMenuLocation.EXPLORER: "explorer/context",
            ContextMenuLocation.TERMINAL: "terminal/context",
            ContextMenuLocation.DEBUG: "debug/callstack/context",
        }

        for location, menu_key in location_map.items():
            items = self._items.get(location, [])
            if items:
                contributions[menu_key] = [item.to_package_json() for item in items]

        return contributions


# =============================================================================
# STATUSBAR MANAGER
# =============================================================================

class StatusBarManager:
    """
    Manages statusbar items.

    Shows JARVIS status and quick actions in the IDE status bar.
    """

    def __init__(self, config: IDEIntegrationConfig):
        self.config = config
        self._items: Dict[str, StatusBarItem] = {}
        self._register_builtin_items()

    def _register_builtin_items(self) -> None:
        """Register built-in statusbar items."""
        if not self.config.enable_statusbar:
            return

        # Main JARVIS status
        self.add_item(StatusBarItem(
            id="status",
            text="$(sparkle) JARVIS",
            tooltip="JARVIS AI Assistant - Click to open chat",
            command="chat.open",
            alignment=StatusBarAlignment.RIGHT,
            priority=100,
        ))

        # Connection status
        self.add_item(StatusBarItem(
            id="connection",
            text="$(plug) Connected",
            tooltip="JARVIS Backend Connection Status",
            alignment=StatusBarAlignment.RIGHT,
            priority=99,
        ))

    def add_item(self, item: StatusBarItem) -> None:
        """Add a statusbar item."""
        self._items[item.id] = item

    def update_item(self, item_id: str, text: str, tooltip: Optional[str] = None) -> None:
        """Update a statusbar item."""
        if item_id in self._items:
            self._items[item_id].text = text
            if tooltip:
                self._items[item_id].tooltip = tooltip

    def get_items(self) -> List[StatusBarItem]:
        """Get all statusbar items."""
        return list(self._items.values())


# =============================================================================
# WEBVIEW MANAGER
# =============================================================================

class WebviewManager:
    """
    Manages webview panels.

    Creates interactive panels for chat, diff view, etc.
    """

    def __init__(self, config: IDEIntegrationConfig):
        self.config = config
        self._panels: Dict[str, WebviewPanel] = {}

    def create_panel(
        self,
        panel_type: WebviewPanelType,
        title: str,
        html_content: str = "",
    ) -> WebviewPanel:
        """Create a new webview panel."""
        panel = WebviewPanel(
            id=f"jarvis.{panel_type.value}.{uuid.uuid4().hex[:8]}",
            title=title,
            panel_type=panel_type,
            html_content=html_content or self._get_default_html(panel_type),
        )
        self._panels[panel.id] = panel
        return panel

    def _get_default_html(self, panel_type: WebviewPanelType) -> str:
        """Get default HTML for a panel type."""
        if panel_type == WebviewPanelType.CHAT:
            return self._get_chat_html()
        elif panel_type == WebviewPanelType.DIFF:
            return self._get_diff_html()
        return "<html><body>JARVIS</body></html>"

    def _get_chat_html(self) -> str:
        """Get chat panel HTML."""
        return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JARVIS Chat</title>
    <style>
        body {
            font-family: var(--vscode-font-family);
            background: var(--vscode-editor-background);
            color: var(--vscode-editor-foreground);
            padding: 16px;
            margin: 0;
        }
        .chat-container {
            display: flex;
            flex-direction: column;
            height: 100vh;
        }
        .messages {
            flex: 1;
            overflow-y: auto;
            padding-bottom: 16px;
        }
        .message {
            margin-bottom: 12px;
            padding: 8px 12px;
            border-radius: 8px;
        }
        .user-message {
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            margin-left: 20%;
        }
        .ai-message {
            background: var(--vscode-input-background);
            margin-right: 20%;
        }
        .input-container {
            display: flex;
            gap: 8px;
            padding-top: 16px;
            border-top: 1px solid var(--vscode-panel-border);
        }
        .input-container input {
            flex: 1;
            padding: 8px 12px;
            background: var(--vscode-input-background);
            color: var(--vscode-input-foreground);
            border: 1px solid var(--vscode-input-border);
            border-radius: 4px;
        }
        .input-container button {
            padding: 8px 16px;
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        .input-container button:hover {
            background: var(--vscode-button-hoverBackground);
        }
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="messages" id="messages">
            <div class="message ai-message">
                Hello! I'm JARVIS, your AI coding assistant. How can I help you today?
            </div>
        </div>
        <div class="input-container">
            <input type="text" id="input" placeholder="Ask JARVIS anything..." />
            <button onclick="sendMessage()">Send</button>
        </div>
    </div>
    <script>
        const vscode = acquireVsCodeApi();
        const messagesEl = document.getElementById('messages');
        const inputEl = document.getElementById('input');

        function sendMessage() {
            const text = inputEl.value.trim();
            if (!text) return;

            // Add user message
            const userMsg = document.createElement('div');
            userMsg.className = 'message user-message';
            userMsg.textContent = text;
            messagesEl.appendChild(userMsg);

            // Send to extension
            vscode.postMessage({ type: 'message', text });

            inputEl.value = '';
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        inputEl.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') sendMessage();
        });

        window.addEventListener('message', (event) => {
            const message = event.data;
            if (message.type === 'response') {
                const aiMsg = document.createElement('div');
                aiMsg.className = 'message ai-message';
                aiMsg.textContent = message.text;
                messagesEl.appendChild(aiMsg);
                messagesEl.scrollTop = messagesEl.scrollHeight;
            }
        });
    </script>
</body>
</html>
"""

    def _get_diff_html(self) -> str:
        """Get diff view HTML."""
        return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>JARVIS Diff View</title>
    <style>
        body {
            font-family: var(--vscode-editor-font-family);
            font-size: var(--vscode-editor-font-size);
            background: var(--vscode-editor-background);
            color: var(--vscode-editor-foreground);
            padding: 16px;
            margin: 0;
        }
        .diff-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }
        .diff-pane {
            border: 1px solid var(--vscode-panel-border);
            border-radius: 4px;
            overflow: auto;
        }
        .diff-header {
            padding: 8px 12px;
            background: var(--vscode-titleBar-activeBackground);
            font-weight: bold;
        }
        .diff-content {
            padding: 12px;
            white-space: pre-wrap;
            font-family: monospace;
        }
        .line-added { background: rgba(0, 255, 0, 0.1); }
        .line-removed { background: rgba(255, 0, 0, 0.1); }
    </style>
</head>
<body>
    <div class="diff-container">
        <div class="diff-pane">
            <div class="diff-header">Original</div>
            <div class="diff-content" id="original"></div>
        </div>
        <div class="diff-pane">
            <div class="diff-header">Improved</div>
            <div class="diff-content" id="improved"></div>
        </div>
    </div>
    <script>
        const vscode = acquireVsCodeApi();

        window.addEventListener('message', (event) => {
            const message = event.data;
            if (message.type === 'diff') {
                document.getElementById('original').textContent = message.original;
                document.getElementById('improved').textContent = message.improved;
            }
        });
    </script>
</body>
</html>
"""

    def get_panel(self, panel_id: str) -> Optional[WebviewPanel]:
        """Get a panel by ID."""
        return self._panels.get(panel_id)

    def close_panel(self, panel_id: str) -> None:
        """Close a panel."""
        if panel_id in self._panels:
            del self._panels[panel_id]


# =============================================================================
# CODE LENS PROVIDER
# =============================================================================

class CodeLensProvider:
    """
    Provides code lens annotations.

    Shows inline actions above functions, classes, etc.
    """

    def __init__(self, config: IDEIntegrationConfig):
        self.config = config

    async def provide_code_lens(
        self,
        content: str,
        language_id: str,
    ) -> List[CodeLens]:
        """Provide code lens annotations for a document."""
        if not self.config.enable_code_lens:
            return []

        lenses = []

        if language_id == "python":
            lenses.extend(await self._get_python_lenses(content))

        return lenses

    async def _get_python_lenses(self, content: str) -> List[CodeLens]:
        """Get code lens for Python files."""
        import ast

        lenses = []

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Add lens above function
                lenses.append(CodeLens(
                    range_start=node.lineno - 1,
                    range_end=node.lineno - 1,
                    command_id="improve",
                    title="$(sparkle) Improve",
                    tooltip="Improve this function with JARVIS",
                ))

                # Add test generation lens
                lenses.append(CodeLens(
                    range_start=node.lineno - 1,
                    range_end=node.lineno - 1,
                    command_id="generate.tests",
                    title="$(beaker) Tests",
                    tooltip="Generate tests for this function",
                ))

            elif isinstance(node, ast.ClassDef):
                # Add lens above class
                lenses.append(CodeLens(
                    range_start=node.lineno - 1,
                    range_end=node.lineno - 1,
                    command_id="explain",
                    title="$(question) Explain",
                    tooltip="Explain this class",
                ))

        return lenses


# =============================================================================
# INLINE COMPLETION PROVIDER
# =============================================================================

class InlineCompletionProvider:
    """
    Provides inline code completions.

    Shows AI-powered ghost text suggestions while typing.
    """

    def __init__(self, config: IDEIntegrationConfig):
        self.config = config
        self._completion_cache: Dict[str, List[InlineCompletionItem]] = {}

    async def provide_inline_completions(
        self,
        content: str,
        position: Tuple[int, int],
        language_id: str,
    ) -> List[InlineCompletionItem]:
        """Provide inline completion suggestions."""
        if not self.config.enable_inline_completion:
            return []

        # This would integrate with JARVIS AI for real completions
        # For now, return empty list
        return []


# =============================================================================
# IDE INTEGRATION ENGINE
# =============================================================================

class IDEIntegrationEngine:
    """
    Main IDE integration engine.

    Coordinates all IDE integration features.
    """

    def __init__(self, config: Optional[IDEIntegrationConfig] = None):
        self.config = config or IDEIntegrationConfig.from_env()

        # Core components
        self.command_registry = CommandRegistry(self.config)
        self.context_menu_manager = ContextMenuManager(self.config)
        self.statusbar_manager = StatusBarManager(self.config)
        self.webview_manager = WebviewManager(self.config)
        self.code_lens_provider = CodeLensProvider(self.config)
        self.inline_completion_provider = InlineCompletionProvider(self.config)

        # State
        self._running = False
        self._connections: Set[str] = set()
        self._event_handlers: Dict[str, List[Callable]] = {}

    async def initialize(self) -> bool:
        """Initialize the IDE integration engine."""
        try:
            self._running = True
            logger.info("IDE integration engine initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize IDE integration: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the IDE integration engine."""
        self._running = False
        logger.info("IDE integration engine shutdown")

    async def handle_command(
        self,
        command_id: str,
        context: IDEContext,
        args: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Handle a command from the IDE."""
        return await self.command_registry.execute(command_id, context, args)

    def set_command_handler(self, command_id: str, handler: Callable) -> None:
        """Set handler for a command."""
        self.command_registry.set_handler(command_id, handler)

    def on_event(self, event_type: str, handler: Callable) -> None:
        """Register an event handler."""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    async def emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit an event to all handlers."""
        for handler in self._event_handlers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error(f"Event handler error: {e}")

    def generate_extension_package_json(self) -> Dict[str, Any]:
        """Generate VS Code extension package.json content."""
        return {
            "name": self.config.extension_id,
            "displayName": self.config.extension_name,
            "version": self.config.extension_version,
            "engines": {"vscode": "^1.70.0"},
            "categories": ["Programming Languages", "Machine Learning", "Other"],
            "activationEvents": ["onStartupFinished"],
            "main": "./out/extension.js",
            "contributes": {
                "commands": self.command_registry.generate_package_json_commands(),
                "keybindings": self.command_registry.generate_keybindings(),
                "menus": self.context_menu_manager.generate_menu_contributions(),
                "configuration": {
                    "title": "JARVIS AI",
                    "properties": {
                        "jarvis.enableInlineCompletion": {
                            "type": "boolean",
                            "default": True,
                            "description": "Enable AI inline completions",
                        },
                        "jarvis.enableCodeLens": {
                            "type": "boolean",
                            "default": True,
                            "description": "Enable code lens annotations",
                        },
                        "jarvis.serverUrl": {
                            "type": "string",
                            "default": "http://localhost:2088",
                            "description": "JARVIS backend server URL",
                        },
                    },
                },
            },
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get integration engine statistics."""
        return {
            "running": self._running,
            "registered_commands": len(self.command_registry._commands),
            "active_connections": len(self._connections),
            "active_panels": len(self.webview_manager._panels),
        }


# =============================================================================
# CROSS-REPO IDE COORDINATOR
# =============================================================================

class CrossRepoIDECoordinator:
    """
    Coordinates IDE integration across JARVIS, JARVIS-Prime, and Reactor-Core.

    Enables:
    - Cross-repo navigation
    - Unified command palette
    - Multi-repo search
    """

    def __init__(self, config: Optional[IDEIntegrationConfig] = None):
        self.config = config or IDEIntegrationConfig.from_env()
        self._engines: Dict[str, IDEIntegrationEngine] = {}
        self._running = False

    async def initialize(self) -> bool:
        """Initialize cross-repo IDE coordination."""
        if not self.config.enable_cross_repo:
            return True

        try:
            self._running = True
            logger.info("Cross-repo IDE coordinator initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize cross-repo IDE: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown cross-repo IDE coordination."""
        for engine in self._engines.values():
            await engine.shutdown()
        self._running = False


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

_ide_engine: Optional[IDEIntegrationEngine] = None
_cross_repo_coordinator: Optional[CrossRepoIDECoordinator] = None


def get_ide_integration_engine(
    config: Optional[IDEIntegrationConfig] = None
) -> IDEIntegrationEngine:
    """
    Get or create the global IDE integration engine.

    Args:
        config: Optional configuration. If provided and engine doesn't exist,
               uses this config. If engine exists, config is ignored.

    Returns:
        The global IDEIntegrationEngine instance.
    """
    global _ide_engine
    if _ide_engine is None:
        _ide_engine = IDEIntegrationEngine(config=config)
    return _ide_engine


def get_cross_repo_ide_coordinator() -> CrossRepoIDECoordinator:
    """Get the global cross-repo IDE coordinator."""
    global _cross_repo_coordinator
    if _cross_repo_coordinator is None:
        _cross_repo_coordinator = CrossRepoIDECoordinator()
    return _cross_repo_coordinator


async def initialize_ide_integration() -> bool:
    """Initialize IDE integration system."""
    engine = get_ide_integration_engine()
    success = await engine.initialize()

    if success:
        coordinator = get_cross_repo_ide_coordinator()
        await coordinator.initialize()

    return success


async def shutdown_ide_integration() -> None:
    """Shutdown IDE integration system."""
    global _ide_engine, _cross_repo_coordinator

    if _cross_repo_coordinator:
        await _cross_repo_coordinator.shutdown()
        _cross_repo_coordinator = None

    if _ide_engine:
        await _ide_engine.shutdown()
        _ide_engine = None
