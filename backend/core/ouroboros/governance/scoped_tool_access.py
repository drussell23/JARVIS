"""
ScopedToolAccess — Per-agent tool restrictions.

Each agent role gets only the tools its responsibilities require.
Researchers get read-only introspection; workers get the full toolkit;
unknown roles default to read-only to prevent accidental mutation.

The ScopedToolGate enforces three layers of filtering:
  1. Explicit deny list (overrides everything)
  2. Read-only flag (blocks all mutation tools)
  3. Allow list (if non-empty, only listed tools pass)

Env vars:
  JARVIS_TOOL_SCOPE_STRICT — "1" to hard-error on denied tools (default: soft deny)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Mapping, Tuple

logger = logging.getLogger(__name__)

_STRICT_MODE = os.environ.get("JARVIS_TOOL_SCOPE_STRICT", "0") == "1"

# ---------------------------------------------------------------------------
# Mutation tools — any tool that can modify the filesystem or run commands
# ---------------------------------------------------------------------------

_MUTATION_TOOLS: FrozenSet[str] = frozenset({
    "edit_file",
    "write_file",
    "bash",
    "apply_patch",
    "delete_file",
})


# ---------------------------------------------------------------------------
# ToolScope dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolScope:
    """Defines which tools an agent role is permitted to use.

    Parameters
    ----------
    allowed_tools:
        Tool names this scope explicitly permits.  Empty means "all allowed"
        (subject to denied_tools and read_only).
    denied_tools:
        Tool names explicitly denied — takes precedence over allowed_tools.
    read_only:
        If True, every tool in ``_MUTATION_TOOLS`` is implicitly denied.
    """
    allowed_tools: FrozenSet[str] = field(default_factory=frozenset)
    denied_tools: FrozenSet[str] = field(default_factory=frozenset)
    read_only: bool = False


# ---------------------------------------------------------------------------
# Role -> ToolScope mapping
# ---------------------------------------------------------------------------

ROLE_TOOL_SCOPES: Dict[str, ToolScope] = {
    "researcher": ToolScope(
        read_only=True,
        allowed_tools=frozenset({
            "read_file",
            "search_code",
            "list_symbols",
            "get_callers",
            "web_search",
        }),
    ),
    "reviewer": ToolScope(
        read_only=True,
        allowed_tools=frozenset({
            "read_file",
            "search_code",
            "list_symbols",
            "run_tests",
        }),
    ),
    "worker": ToolScope(),   # all tools allowed, not read-only
    "lead": ToolScope(),     # all tools allowed, not read-only
}

# Default for unknown roles: read-only, unrestricted allowlist
_DEFAULT_SCOPE = ToolScope(read_only=True)


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def get_scope_for_role(role: str) -> ToolScope:
    """Return the ToolScope associated with *role*.

    Falls back to a read-only default for unrecognised roles so that
    newly invented agents can never accidentally mutate the workspace.
    """
    scope = ROLE_TOOL_SCOPES.get(role)
    if scope is not None:
        return scope
    logger.warning(
        "[ScopedToolAccess] Unknown role %r — defaulting to read-only scope",
        role,
    )
    return _DEFAULT_SCOPE


# ---------------------------------------------------------------------------
# ScopedToolGate — enforcement point
# ---------------------------------------------------------------------------

class ScopedToolGate:
    """Evaluates whether a specific tool invocation is permitted under a scope.

    Usage::

        gate = ScopedToolGate(get_scope_for_role("researcher"))
        allowed, reason = gate.can_use("bash")
        # allowed=False, reason="read-only scope"
    """

    def __init__(self, scope: ToolScope) -> None:
        self._scope = scope

    # -- public API ---------------------------------------------------------

    def can_use(self, tool_name: str) -> Tuple[bool, str]:
        """Return ``(True, "")`` if *tool_name* is permitted, else ``(False, reason)``."""

        # Layer 1: explicit deny (highest priority)
        if tool_name in self._scope.denied_tools:
            return self._deny(tool_name, "tool denied by scope")

        # Layer 2: read-only blocks all mutation tools
        if self._scope.read_only and tool_name in _MUTATION_TOOLS:
            return self._deny(tool_name, "read-only scope")

        # Layer 3: allowlist (empty = permissive)
        if self._scope.allowed_tools and tool_name not in self._scope.allowed_tools:
            return self._deny(tool_name, "tool not in allowlist")

        return (True, "")

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _deny(tool_name: str, reason: str) -> Tuple[bool, str]:
        logger.debug("[ScopedToolGate] DENIED %s — %s", tool_name, reason)
        return (False, reason)

    # -- convenience --------------------------------------------------------

    @property
    def scope(self) -> ToolScope:
        return self._scope

    def __repr__(self) -> str:
        return (
            f"ScopedToolGate(allowed={sorted(self._scope.allowed_tools)}, "
            f"denied={sorted(self._scope.denied_tools)}, "
            f"read_only={self._scope.read_only})"
        )
