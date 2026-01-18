"""
LSP Server - Language Server Protocol Provider
===============================================

Production-grade LSP server providing:
- Code completions with AI suggestions
- Diagnostics (errors, warnings, hints)
- Code actions (quick fixes, refactoring)
- Hover information
- Go to definition/references
- Document symbols
- Formatting

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    JARVIS LSP Server v1.0                                │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
    │   │   IDE/      │     │    LSP      │     │   JARVIS    │               │
    │   │   Editor    │◀───▶│   Server    │◀───▶│   AI Core   │               │
    │   └─────────────┘     └─────────────┘     └─────────────┘               │
    │          │                   │                   │                      │
    │          └───────────────────┴───────────────────┘                      │
    │                              │                                          │
    │                    ┌─────────▼─────────┐                                │
    │                    │  Feature Handlers │                                │
    │                    │                   │                                │
    │                    │ • Completion      │                                │
    │                    │ • Diagnostics     │                                │
    │                    │ • Code Actions    │                                │
    │                    │ • Hover           │                                │
    │                    └───────────────────┘                                │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

LSP Specification: https://microsoft.github.io/language-server-protocol/

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntEnum
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

logger = logging.getLogger("JARVIS.LSP")


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class LSPServerConfig:
    """Environment-driven LSP server configuration."""

    # Server settings
    server_name: str = os.getenv("LSP_SERVER_NAME", "jarvis-lsp")
    server_version: str = os.getenv("LSP_SERVER_VERSION", "1.0.0")
    transport: str = os.getenv("LSP_TRANSPORT", "stdio")  # stdio, tcp, websocket
    tcp_host: str = os.getenv("LSP_TCP_HOST", "127.0.0.1")
    tcp_port: int = int(os.getenv("LSP_TCP_PORT", "2087"))

    # Feature toggles
    enable_completion: bool = os.getenv("LSP_ENABLE_COMPLETION", "true").lower() == "true"
    enable_diagnostics: bool = os.getenv("LSP_ENABLE_DIAGNOSTICS", "true").lower() == "true"
    enable_hover: bool = os.getenv("LSP_ENABLE_HOVER", "true").lower() == "true"
    enable_definition: bool = os.getenv("LSP_ENABLE_DEFINITION", "true").lower() == "true"
    enable_references: bool = os.getenv("LSP_ENABLE_REFERENCES", "true").lower() == "true"
    enable_code_actions: bool = os.getenv("LSP_ENABLE_CODE_ACTIONS", "true").lower() == "true"
    enable_formatting: bool = os.getenv("LSP_ENABLE_FORMATTING", "true").lower() == "true"

    # AI settings
    enable_ai_completion: bool = os.getenv("LSP_AI_COMPLETION", "true").lower() == "true"
    ai_completion_delay: float = float(os.getenv("LSP_AI_COMPLETION_DELAY", "0.5"))
    max_completion_items: int = int(os.getenv("LSP_MAX_COMPLETIONS", "50"))

    # Diagnostics settings
    diagnostic_delay: float = float(os.getenv("LSP_DIAGNOSTIC_DELAY", "0.3"))
    max_diagnostics: int = int(os.getenv("LSP_MAX_DIAGNOSTICS", "100"))

    # Cross-repo
    enable_cross_repo: bool = os.getenv("LSP_CROSS_REPO_ENABLED", "true").lower() == "true"

    @classmethod
    def from_env(cls) -> "LSPServerConfig":
        """Create configuration from environment."""
        return cls()


# =============================================================================
# LSP PROTOCOL TYPES
# =============================================================================

class MessageType(IntEnum):
    """LSP message types for notifications."""
    ERROR = 1
    WARNING = 2
    INFO = 3
    LOG = 4


class DiagnosticSeverity(IntEnum):
    """Severity of a diagnostic."""
    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


class CompletionItemKind(IntEnum):
    """Kind of a completion item."""
    TEXT = 1
    METHOD = 2
    FUNCTION = 3
    CONSTRUCTOR = 4
    FIELD = 5
    VARIABLE = 6
    CLASS = 7
    INTERFACE = 8
    MODULE = 9
    PROPERTY = 10
    UNIT = 11
    VALUE = 12
    ENUM = 13
    KEYWORD = 14
    SNIPPET = 15
    COLOR = 16
    FILE = 17
    REFERENCE = 18
    FOLDER = 19
    ENUM_MEMBER = 20
    CONSTANT = 21
    STRUCT = 22
    EVENT = 23
    OPERATOR = 24
    TYPE_PARAMETER = 25


class SymbolKind(IntEnum):
    """Kind of a symbol."""
    FILE = 1
    MODULE = 2
    NAMESPACE = 3
    PACKAGE = 4
    CLASS = 5
    METHOD = 6
    PROPERTY = 7
    FIELD = 8
    CONSTRUCTOR = 9
    ENUM = 10
    INTERFACE = 11
    FUNCTION = 12
    VARIABLE = 13
    CONSTANT = 14
    STRING = 15
    NUMBER = 16
    BOOLEAN = 17
    ARRAY = 18
    OBJECT = 19
    KEY = 20
    NULL = 21
    ENUM_MEMBER = 22
    STRUCT = 23
    EVENT = 24
    OPERATOR = 25
    TYPE_PARAMETER = 26


class CodeActionKind:
    """Standard code action kinds."""
    EMPTY = ""
    QUICK_FIX = "quickfix"
    REFACTOR = "refactor"
    REFACTOR_EXTRACT = "refactor.extract"
    REFACTOR_INLINE = "refactor.inline"
    REFACTOR_REWRITE = "refactor.rewrite"
    SOURCE = "source"
    SOURCE_ORGANIZE_IMPORTS = "source.organizeImports"
    SOURCE_FIX_ALL = "source.fixAll"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Position:
    """A position in a text document."""
    line: int
    character: int

    def to_dict(self) -> Dict[str, int]:
        return {"line": self.line, "character": self.character}

    @classmethod
    def from_dict(cls, data: Dict) -> "Position":
        return cls(line=data["line"], character=data["character"])


@dataclass
class Range:
    """A range in a text document."""
    start: Position
    end: Position

    def to_dict(self) -> Dict[str, Any]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}

    @classmethod
    def from_dict(cls, data: Dict) -> "Range":
        return cls(
            start=Position.from_dict(data["start"]),
            end=Position.from_dict(data["end"]),
        )


@dataclass
class Location:
    """A location in a document."""
    uri: str
    range: Range

    def to_dict(self) -> Dict[str, Any]:
        return {"uri": self.uri, "range": self.range.to_dict()}


@dataclass
class TextEdit:
    """A text edit operation."""
    range: Range
    new_text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"range": self.range.to_dict(), "newText": self.new_text}


@dataclass
class TextDocumentIdentifier:
    """Identifies a text document."""
    uri: str

    @classmethod
    def from_dict(cls, data: Dict) -> "TextDocumentIdentifier":
        return cls(uri=data["uri"])


@dataclass
class TextDocumentItem:
    """An item describing a text document."""
    uri: str
    language_id: str
    version: int
    text: str

    @classmethod
    def from_dict(cls, data: Dict) -> "TextDocumentItem":
        return cls(
            uri=data["uri"],
            language_id=data["languageId"],
            version=data["version"],
            text=data["text"],
        )


@dataclass
class Diagnostic:
    """A diagnostic (error, warning, etc.)."""
    range: Range
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    code: Optional[str] = None
    source: str = "jarvis"
    related_information: List[Dict] = field(default_factory=list)
    tags: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "range": self.range.to_dict(),
            "message": self.message,
            "severity": self.severity.value,
            "source": self.source,
        }
        if self.code:
            result["code"] = self.code
        if self.related_information:
            result["relatedInformation"] = self.related_information
        if self.tags:
            result["tags"] = self.tags
        return result


@dataclass
class CompletionItem:
    """A completion suggestion."""
    label: str
    kind: CompletionItemKind = CompletionItemKind.TEXT
    detail: Optional[str] = None
    documentation: Optional[str] = None
    sort_text: Optional[str] = None
    filter_text: Optional[str] = None
    insert_text: Optional[str] = None
    insert_text_format: int = 1  # 1 = PlainText, 2 = Snippet
    text_edit: Optional[TextEdit] = None
    additional_text_edits: List[TextEdit] = field(default_factory=list)
    command: Optional[Dict] = None
    data: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "label": self.label,
            "kind": self.kind.value,
        }
        if self.detail:
            result["detail"] = self.detail
        if self.documentation:
            result["documentation"] = self.documentation
        if self.sort_text:
            result["sortText"] = self.sort_text
        if self.filter_text:
            result["filterText"] = self.filter_text
        if self.insert_text:
            result["insertText"] = self.insert_text
        if self.insert_text_format != 1:
            result["insertTextFormat"] = self.insert_text_format
        if self.text_edit:
            result["textEdit"] = self.text_edit.to_dict()
        if self.additional_text_edits:
            result["additionalTextEdits"] = [e.to_dict() for e in self.additional_text_edits]
        if self.command:
            result["command"] = self.command
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class DocumentSymbol:
    """A document symbol."""
    name: str
    kind: SymbolKind
    range: Range
    selection_range: Range
    detail: Optional[str] = None
    children: List["DocumentSymbol"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "name": self.name,
            "kind": self.kind.value,
            "range": self.range.to_dict(),
            "selectionRange": self.selection_range.to_dict(),
        }
        if self.detail:
            result["detail"] = self.detail
        if self.children:
            result["children"] = [c.to_dict() for c in self.children]
        return result


@dataclass
class CodeAction:
    """A code action."""
    title: str
    kind: str = CodeActionKind.QUICK_FIX
    diagnostics: List[Diagnostic] = field(default_factory=list)
    is_preferred: bool = False
    edit: Optional[Dict] = None
    command: Optional[Dict] = None
    data: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "title": self.title,
            "kind": self.kind,
        }
        if self.diagnostics:
            result["diagnostics"] = [d.to_dict() for d in self.diagnostics]
        if self.is_preferred:
            result["isPreferred"] = True
        if self.edit:
            result["edit"] = self.edit
        if self.command:
            result["command"] = self.command
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class Hover:
    """Hover information."""
    contents: str  # MarkupContent as string
    range: Optional[Range] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "contents": {
                "kind": "markdown",
                "value": self.contents,
            }
        }
        if self.range:
            result["range"] = self.range.to_dict()
        return result


# =============================================================================
# DOCUMENT MANAGER
# =============================================================================

class DocumentManager:
    """
    Manages open documents in the workspace.

    Tracks document content, versions, and provides text access.
    """

    def __init__(self):
        self._documents: Dict[str, TextDocumentItem] = {}
        self._lock = asyncio.Lock()

    async def open_document(self, document: TextDocumentItem) -> None:
        """Open a document."""
        async with self._lock:
            self._documents[document.uri] = document
            logger.debug(f"Opened document: {document.uri}")

    async def close_document(self, uri: str) -> None:
        """Close a document."""
        async with self._lock:
            if uri in self._documents:
                del self._documents[uri]
                logger.debug(f"Closed document: {uri}")

    async def update_document(
        self,
        uri: str,
        version: int,
        changes: List[Dict],
    ) -> None:
        """Apply incremental changes to a document."""
        async with self._lock:
            doc = self._documents.get(uri)
            if not doc:
                return

            # Apply changes
            content = doc.text
            for change in changes:
                if "range" in change:
                    # Incremental change
                    content = self._apply_change(content, change)
                else:
                    # Full content change
                    content = change["text"]

            # Update document
            self._documents[uri] = TextDocumentItem(
                uri=uri,
                language_id=doc.language_id,
                version=version,
                text=content,
            )

    def _apply_change(self, content: str, change: Dict) -> str:
        """Apply a single incremental change."""
        range_data = change["range"]
        start = Position.from_dict(range_data["start"])
        end = Position.from_dict(range_data["end"])
        new_text = change["text"]

        lines = content.split("\n")

        # Get text before change
        before = "\n".join(lines[:start.line])
        if start.line < len(lines):
            before += "\n" + lines[start.line][:start.character]

        # Get text after change
        after = ""
        if end.line < len(lines):
            after = lines[end.line][end.character:]
            if end.line + 1 < len(lines):
                after += "\n" + "\n".join(lines[end.line + 1:])

        return before + new_text + after

    def get_document(self, uri: str) -> Optional[TextDocumentItem]:
        """Get a document by URI."""
        return self._documents.get(uri)

    def get_document_content(self, uri: str) -> Optional[str]:
        """Get document content by URI."""
        doc = self._documents.get(uri)
        return doc.text if doc else None

    def get_line(self, uri: str, line: int) -> Optional[str]:
        """Get a specific line from a document."""
        doc = self._documents.get(uri)
        if not doc:
            return None

        lines = doc.text.split("\n")
        if 0 <= line < len(lines):
            return lines[line]
        return None

    def get_word_at_position(self, uri: str, position: Position) -> Optional[str]:
        """Get the word at a specific position."""
        line = self.get_line(uri, position.line)
        if not line:
            return None

        # Find word boundaries
        start = position.character
        end = position.character

        while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
            start -= 1

        while end < len(line) and (line[end].isalnum() or line[end] == "_"):
            end += 1

        return line[start:end] if start < end else None


# =============================================================================
# FEATURE HANDLERS
# =============================================================================

class CompletionHandler:
    """Handles completion requests."""

    def __init__(
        self,
        config: LSPServerConfig,
        document_manager: DocumentManager,
    ):
        self.config = config
        self.document_manager = document_manager
        self._completion_providers: List[Callable] = []

    def register_provider(self, provider: Callable) -> None:
        """Register a completion provider."""
        self._completion_providers.append(provider)

    async def get_completions(
        self,
        uri: str,
        position: Position,
        context: Optional[Dict] = None,
    ) -> List[CompletionItem]:
        """Get completion items for a position."""
        completions = []

        # Get document context
        doc = self.document_manager.get_document(uri)
        if not doc:
            return completions

        word = self.document_manager.get_word_at_position(uri, position)
        line = self.document_manager.get_line(uri, position.line)

        # Get completions from all providers
        for provider in self._completion_providers:
            try:
                items = await provider(uri, position, word, line, doc.text)
                completions.extend(items)
            except Exception as e:
                logger.error(f"Completion provider error: {e}")

        # Add built-in completions
        completions.extend(await self._get_builtin_completions(doc, position, word))

        # Limit and sort
        completions = sorted(
            completions,
            key=lambda c: c.sort_text or c.label
        )[:self.config.max_completion_items]

        return completions

    async def _get_builtin_completions(
        self,
        doc: TextDocumentItem,
        position: Position,
        word: Optional[str],
    ) -> List[CompletionItem]:
        """Get built-in completions based on language."""
        completions = []

        if doc.language_id == "python":
            completions.extend(self._get_python_completions(word))

        return completions

    def _get_python_completions(self, word: Optional[str]) -> List[CompletionItem]:
        """Get Python keyword completions."""
        keywords = [
            "async", "await", "class", "def", "for", "if", "elif", "else",
            "try", "except", "finally", "with", "import", "from", "return",
            "yield", "raise", "pass", "break", "continue", "lambda", "True",
            "False", "None", "and", "or", "not", "in", "is", "global", "nonlocal",
        ]

        completions = []
        for kw in keywords:
            if not word or kw.startswith(word):
                completions.append(CompletionItem(
                    label=kw,
                    kind=CompletionItemKind.KEYWORD,
                    detail="Python keyword",
                ))

        return completions


class DiagnosticHandler:
    """Handles diagnostic (error/warning) analysis."""

    def __init__(
        self,
        config: LSPServerConfig,
        document_manager: DocumentManager,
    ):
        self.config = config
        self.document_manager = document_manager
        self._diagnostic_providers: List[Callable] = []
        self._pending_diagnostics: Dict[str, asyncio.Task] = {}

    def register_provider(self, provider: Callable) -> None:
        """Register a diagnostic provider."""
        self._diagnostic_providers.append(provider)

    async def analyze_document(
        self,
        uri: str,
        delay: bool = True,
    ) -> List[Diagnostic]:
        """Analyze a document for diagnostics."""
        # Cancel pending analysis
        if uri in self._pending_diagnostics:
            self._pending_diagnostics[uri].cancel()

        if delay:
            # Delay analysis to avoid excessive processing
            await asyncio.sleep(self.config.diagnostic_delay)

        doc = self.document_manager.get_document(uri)
        if not doc:
            return []

        diagnostics = []

        # Get diagnostics from all providers
        for provider in self._diagnostic_providers:
            try:
                items = await provider(uri, doc.text, doc.language_id)
                diagnostics.extend(items)
            except Exception as e:
                logger.error(f"Diagnostic provider error: {e}")

        # Add built-in diagnostics
        diagnostics.extend(await self._get_builtin_diagnostics(doc))

        return diagnostics[:self.config.max_diagnostics]

    async def _get_builtin_diagnostics(
        self,
        doc: TextDocumentItem,
    ) -> List[Diagnostic]:
        """Get built-in syntax diagnostics."""
        diagnostics = []

        if doc.language_id == "python":
            diagnostics.extend(self._check_python_syntax(doc.text))

        return diagnostics

    def _check_python_syntax(self, content: str) -> List[Diagnostic]:
        """Check Python syntax errors."""
        diagnostics = []

        try:
            compile(content, "<string>", "exec")
        except SyntaxError as e:
            diagnostics.append(Diagnostic(
                range=Range(
                    start=Position(line=(e.lineno or 1) - 1, character=(e.offset or 1) - 1),
                    end=Position(line=(e.lineno or 1) - 1, character=(e.offset or 1)),
                ),
                message=str(e.msg),
                severity=DiagnosticSeverity.ERROR,
                code="E999",
                source="jarvis-python",
            ))

        return diagnostics


class HoverHandler:
    """Handles hover information requests."""

    def __init__(
        self,
        config: LSPServerConfig,
        document_manager: DocumentManager,
    ):
        self.config = config
        self.document_manager = document_manager
        self._hover_providers: List[Callable] = []

    def register_provider(self, provider: Callable) -> None:
        """Register a hover provider."""
        self._hover_providers.append(provider)

    async def get_hover(
        self,
        uri: str,
        position: Position,
    ) -> Optional[Hover]:
        """Get hover information at a position."""
        doc = self.document_manager.get_document(uri)
        if not doc:
            return None

        word = self.document_manager.get_word_at_position(uri, position)
        if not word:
            return None

        # Try all providers
        for provider in self._hover_providers:
            try:
                hover = await provider(uri, position, word, doc.text)
                if hover:
                    return hover
            except Exception as e:
                logger.error(f"Hover provider error: {e}")

        # Built-in hover
        return await self._get_builtin_hover(doc, position, word)

    async def _get_builtin_hover(
        self,
        doc: TextDocumentItem,
        position: Position,
        word: str,
    ) -> Optional[Hover]:
        """Get built-in hover information."""
        if doc.language_id == "python":
            return self._get_python_hover(word)
        return None

    def _get_python_hover(self, word: str) -> Optional[Hover]:
        """Get Python keyword documentation."""
        docs = {
            "async": "**async** - Declares an asynchronous function or context manager.",
            "await": "**await** - Pauses execution until an awaitable completes.",
            "class": "**class** - Defines a new class.",
            "def": "**def** - Defines a new function.",
            "lambda": "**lambda** - Creates an anonymous function.",
            "yield": "**yield** - Pauses function and returns a value to the caller.",
        }

        if word in docs:
            return Hover(contents=docs[word])
        return None


class CodeActionHandler:
    """Handles code action requests."""

    def __init__(
        self,
        config: LSPServerConfig,
        document_manager: DocumentManager,
    ):
        self.config = config
        self.document_manager = document_manager
        self._action_providers: List[Callable] = []

    def register_provider(self, provider: Callable) -> None:
        """Register a code action provider."""
        self._action_providers.append(provider)

    async def get_code_actions(
        self,
        uri: str,
        range: Range,
        context: Dict,
    ) -> List[CodeAction]:
        """Get code actions for a range."""
        doc = self.document_manager.get_document(uri)
        if not doc:
            return []

        actions = []

        # Get diagnostics in range
        diagnostics = context.get("diagnostics", [])

        # Get actions from all providers
        for provider in self._action_providers:
            try:
                items = await provider(uri, range, diagnostics, doc.text)
                actions.extend(items)
            except Exception as e:
                logger.error(f"Code action provider error: {e}")

        # Add built-in actions
        actions.extend(await self._get_builtin_actions(doc, range, diagnostics))

        return actions

    async def _get_builtin_actions(
        self,
        doc: TextDocumentItem,
        range: Range,
        diagnostics: List[Dict],
    ) -> List[CodeAction]:
        """Get built-in code actions."""
        actions = []

        # Quick fix for undefined variables (example)
        for diag in diagnostics:
            if "undefined" in diag.get("message", "").lower():
                actions.append(CodeAction(
                    title="Add import statement",
                    kind=CodeActionKind.QUICK_FIX,
                    is_preferred=True,
                ))

        return actions


class DefinitionHandler:
    """Handles go-to-definition requests."""

    def __init__(
        self,
        config: LSPServerConfig,
        document_manager: DocumentManager,
    ):
        self.config = config
        self.document_manager = document_manager
        self._definition_providers: List[Callable] = []

    def register_provider(self, provider: Callable) -> None:
        """Register a definition provider."""
        self._definition_providers.append(provider)

    async def get_definition(
        self,
        uri: str,
        position: Position,
    ) -> Optional[Location]:
        """Get definition location for a symbol."""
        doc = self.document_manager.get_document(uri)
        if not doc:
            return None

        word = self.document_manager.get_word_at_position(uri, position)
        if not word:
            return None

        # Try all providers
        for provider in self._definition_providers:
            try:
                location = await provider(uri, position, word, doc.text)
                if location:
                    return location
            except Exception as e:
                logger.error(f"Definition provider error: {e}")

        return None


# =============================================================================
# LSP MESSAGE HANDLER
# =============================================================================

class LSPMessageHandler:
    """
    Handles LSP protocol messages.

    Parses JSON-RPC messages and dispatches to appropriate handlers.
    """

    def __init__(self, server: "JARVISLSPServer"):
        self.server = server
        self._request_handlers: Dict[str, Callable] = {}
        self._notification_handlers: Dict[str, Callable] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register all message handlers."""
        # Lifecycle
        self._request_handlers["initialize"] = self._handle_initialize
        self._request_handlers["shutdown"] = self._handle_shutdown
        self._notification_handlers["exit"] = self._handle_exit
        self._notification_handlers["initialized"] = self._handle_initialized

        # Document sync
        self._notification_handlers["textDocument/didOpen"] = self._handle_did_open
        self._notification_handlers["textDocument/didClose"] = self._handle_did_close
        self._notification_handlers["textDocument/didChange"] = self._handle_did_change
        self._notification_handlers["textDocument/didSave"] = self._handle_did_save

        # Language features
        self._request_handlers["textDocument/completion"] = self._handle_completion
        self._request_handlers["textDocument/hover"] = self._handle_hover
        self._request_handlers["textDocument/definition"] = self._handle_definition
        self._request_handlers["textDocument/references"] = self._handle_references
        self._request_handlers["textDocument/documentSymbol"] = self._handle_document_symbol
        self._request_handlers["textDocument/codeAction"] = self._handle_code_action
        self._request_handlers["textDocument/formatting"] = self._handle_formatting

    async def handle_message(self, message: Dict) -> Optional[Dict]:
        """Handle an incoming LSP message."""
        method = message.get("method")
        params = message.get("params", {})
        msg_id = message.get("id")

        if msg_id is not None:
            # Request
            handler = self._request_handlers.get(method)
            if handler:
                try:
                    result = await handler(params)
                    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
                except Exception as e:
                    logger.error(f"Request handler error: {e}")
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(e)},
                    }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
        else:
            # Notification
            handler = self._notification_handlers.get(method)
            if handler:
                try:
                    await handler(params)
                except Exception as e:
                    logger.error(f"Notification handler error: {e}")

        return None

    async def _handle_initialize(self, params: Dict) -> Dict:
        """Handle initialize request."""
        capabilities = {
            "textDocumentSync": {
                "openClose": True,
                "change": 2,  # Incremental
                "save": {"includeText": True},
            },
        }

        if self.server.config.enable_completion:
            capabilities["completionProvider"] = {
                "triggerCharacters": [".", "(", "[", ",", " "],
                "resolveProvider": True,
            }

        if self.server.config.enable_hover:
            capabilities["hoverProvider"] = True

        if self.server.config.enable_definition:
            capabilities["definitionProvider"] = True

        if self.server.config.enable_references:
            capabilities["referencesProvider"] = True

        if self.server.config.enable_code_actions:
            capabilities["codeActionProvider"] = {
                "codeActionKinds": [
                    CodeActionKind.QUICK_FIX,
                    CodeActionKind.REFACTOR,
                    CodeActionKind.SOURCE,
                ],
            }

        if self.server.config.enable_formatting:
            capabilities["documentFormattingProvider"] = True

        capabilities["documentSymbolProvider"] = True

        return {
            "capabilities": capabilities,
            "serverInfo": {
                "name": self.server.config.server_name,
                "version": self.server.config.server_version,
            },
        }

    async def _handle_initialized(self, params: Dict) -> None:
        """Handle initialized notification."""
        logger.info("LSP client initialized")
        self.server._initialized = True

    async def _handle_shutdown(self, params: Dict) -> None:
        """Handle shutdown request."""
        logger.info("LSP shutdown requested")
        return None

    async def _handle_exit(self, params: Dict) -> None:
        """Handle exit notification."""
        logger.info("LSP exit requested")
        self.server._running = False

    async def _handle_did_open(self, params: Dict) -> None:
        """Handle textDocument/didOpen notification."""
        doc = TextDocumentItem.from_dict(params["textDocument"])
        await self.server.document_manager.open_document(doc)

        # Publish diagnostics
        if self.server.config.enable_diagnostics:
            diagnostics = await self.server.diagnostic_handler.analyze_document(
                doc.uri, delay=False
            )
            await self.server.publish_diagnostics(doc.uri, diagnostics)

    async def _handle_did_close(self, params: Dict) -> None:
        """Handle textDocument/didClose notification."""
        uri = params["textDocument"]["uri"]
        await self.server.document_manager.close_document(uri)

    async def _handle_did_change(self, params: Dict) -> None:
        """Handle textDocument/didChange notification."""
        uri = params["textDocument"]["uri"]
        version = params["textDocument"]["version"]
        changes = params["contentChanges"]

        await self.server.document_manager.update_document(uri, version, changes)

        # Re-analyze diagnostics
        if self.server.config.enable_diagnostics:
            diagnostics = await self.server.diagnostic_handler.analyze_document(uri)
            await self.server.publish_diagnostics(uri, diagnostics)

    async def _handle_did_save(self, params: Dict) -> None:
        """Handle textDocument/didSave notification."""
        uri = params["textDocument"]["uri"]

        # Re-analyze diagnostics on save
        if self.server.config.enable_diagnostics:
            diagnostics = await self.server.diagnostic_handler.analyze_document(
                uri, delay=False
            )
            await self.server.publish_diagnostics(uri, diagnostics)

    async def _handle_completion(self, params: Dict) -> Dict:
        """Handle textDocument/completion request."""
        uri = params["textDocument"]["uri"]
        position = Position.from_dict(params["position"])
        context = params.get("context")

        items = await self.server.completion_handler.get_completions(
            uri, position, context
        )

        return {
            "isIncomplete": len(items) >= self.server.config.max_completion_items,
            "items": [item.to_dict() for item in items],
        }

    async def _handle_hover(self, params: Dict) -> Optional[Dict]:
        """Handle textDocument/hover request."""
        uri = params["textDocument"]["uri"]
        position = Position.from_dict(params["position"])

        hover = await self.server.hover_handler.get_hover(uri, position)
        return hover.to_dict() if hover else None

    async def _handle_definition(self, params: Dict) -> Optional[Dict]:
        """Handle textDocument/definition request."""
        uri = params["textDocument"]["uri"]
        position = Position.from_dict(params["position"])

        location = await self.server.definition_handler.get_definition(uri, position)
        return location.to_dict() if location else None

    async def _handle_references(self, params: Dict) -> List[Dict]:
        """Handle textDocument/references request."""
        # Would need reference provider implementation
        return []

    async def _handle_document_symbol(self, params: Dict) -> List[Dict]:
        """Handle textDocument/documentSymbol request."""
        # Would need symbol provider implementation
        return []

    async def _handle_code_action(self, params: Dict) -> List[Dict]:
        """Handle textDocument/codeAction request."""
        uri = params["textDocument"]["uri"]
        range = Range.from_dict(params["range"])
        context = params["context"]

        actions = await self.server.code_action_handler.get_code_actions(
            uri, range, context
        )

        return [action.to_dict() for action in actions]

    async def _handle_formatting(self, params: Dict) -> List[Dict]:
        """Handle textDocument/formatting request."""
        # Would need formatting provider implementation
        return []


# =============================================================================
# JARVIS LSP SERVER
# =============================================================================

class JARVISLSPServer:
    """
    Main JARVIS LSP Server.

    Provides IDE integration through Language Server Protocol.
    """

    def __init__(self, config: Optional[LSPServerConfig] = None):
        self.config = config or LSPServerConfig.from_env()

        # Core components
        self.document_manager = DocumentManager()

        # Feature handlers
        self.completion_handler = CompletionHandler(self.config, self.document_manager)
        self.diagnostic_handler = DiagnosticHandler(self.config, self.document_manager)
        self.hover_handler = HoverHandler(self.config, self.document_manager)
        self.code_action_handler = CodeActionHandler(self.config, self.document_manager)
        self.definition_handler = DefinitionHandler(self.config, self.document_manager)

        # Message handler
        self.message_handler = LSPMessageHandler(self)

        # State
        self._running = False
        self._initialized = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the LSP server."""
        logger.info(f"Starting JARVIS LSP Server ({self.config.transport})")
        self._running = True

        if self.config.transport == "stdio":
            await self._run_stdio()
        elif self.config.transport == "tcp":
            await self._run_tcp()
        else:
            raise ValueError(f"Unsupported transport: {self.config.transport}")

    async def stop(self) -> None:
        """Stop the LSP server."""
        self._running = False
        logger.info("JARVIS LSP Server stopped")

    async def _run_stdio(self) -> None:
        """Run server using stdio transport."""
        # Create readers/writers from stdio
        loop = asyncio.get_event_loop()

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        w_transport, w_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)

        self._reader = reader
        self._writer = writer

        await self._message_loop()

    async def _run_tcp(self) -> None:
        """Run server using TCP transport."""
        server = await asyncio.start_server(
            self._handle_connection,
            self.config.tcp_host,
            self.config.tcp_port,
        )

        logger.info(f"LSP server listening on {self.config.tcp_host}:{self.config.tcp_port}")

        async with server:
            await server.serve_forever()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a TCP connection."""
        self._reader = reader
        self._writer = writer
        await self._message_loop()

    async def _message_loop(self) -> None:
        """Main message processing loop."""
        while self._running:
            try:
                message = await self._read_message()
                if message is None:
                    break

                response = await self.message_handler.handle_message(message)
                if response:
                    await self._write_message(response)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Message loop error: {e}")

    async def _read_message(self) -> Optional[Dict]:
        """Read an LSP message from the transport."""
        if not self._reader:
            return None

        # Read headers
        headers = {}
        while True:
            line = await self._reader.readline()
            if not line:
                return None

            line = line.decode("utf-8").strip()
            if not line:
                break

            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        # Get content length
        content_length = int(headers.get("content-length", 0))
        if content_length == 0:
            return None

        # Read content
        content = await self._reader.readexactly(content_length)
        return json.loads(content.decode("utf-8"))

    async def _write_message(self, message: Dict) -> None:
        """Write an LSP message to the transport."""
        if not self._writer:
            return

        async with self._write_lock:
            content = json.dumps(message)
            content_bytes = content.encode("utf-8")

            header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
            self._writer.write(header.encode("utf-8"))
            self._writer.write(content_bytes)
            await self._writer.drain()

    async def publish_diagnostics(
        self,
        uri: str,
        diagnostics: List[Diagnostic],
    ) -> None:
        """Publish diagnostics for a document."""
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": [d.to_dict() for d in diagnostics],
            },
        })

    async def show_message(
        self,
        message: str,
        type: MessageType = MessageType.INFO,
    ) -> None:
        """Show a message to the user."""
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "window/showMessage",
            "params": {
                "type": type.value,
                "message": message,
            },
        })

    async def log_message(
        self,
        message: str,
        type: MessageType = MessageType.LOG,
    ) -> None:
        """Log a message."""
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "window/logMessage",
            "params": {
                "type": type.value,
                "message": message,
            },
        })

    def get_stats(self) -> Dict[str, Any]:
        """Get server statistics."""
        return {
            "running": self._running,
            "initialized": self._initialized,
            "transport": self.config.transport,
            "documents_open": len(self.document_manager._documents),
        }


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

_lsp_server: Optional[JARVISLSPServer] = None


def get_lsp_server(
    config: Optional[LSPServerConfig] = None
) -> JARVISLSPServer:
    """
    Get or create the global LSP server.

    Args:
        config: Optional configuration. If provided and server doesn't exist,
               uses this config. If server exists, config is ignored.

    Returns:
        The global JARVISLSPServer instance.
    """
    global _lsp_server
    if _lsp_server is None:
        _lsp_server = JARVISLSPServer(config=config)
    return _lsp_server


async def start_lsp_server() -> None:
    """Start the LSP server."""
    server = get_lsp_server()
    await server.start()


async def stop_lsp_server() -> None:
    """Stop the LSP server."""
    global _lsp_server
    if _lsp_server:
        await _lsp_server.stop()
        _lsp_server = None


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(start_lsp_server())
