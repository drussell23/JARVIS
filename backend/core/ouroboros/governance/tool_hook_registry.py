"""
ToolCallHookRegistry
====================

Per-tool pre/post interception hooks for the Ouroboros governance pipeline.

Design
------
- Pre-hooks run BEFORE a tool call; any hook returning BLOCK stops the call.
- Post-hooks run AFTER a tool call for logging/auditing; fire-and-forget.
- Hooks are matched by tool name and an optional fnmatch glob pattern applied
  to the "target" extracted from tool_input (file path, command string, etc.).
- Hook errors are swallowed — the registry always fails-open (ALLOW) to avoid
  crashing the pipeline due to a bad hook implementation.
- Static configuration can be loaded from a YAML file via ``from_yaml()``.

YAML schema
-----------
.. code-block:: yaml

    hooks:
      - tool: edit
        event: pre          # "pre" | "post"
        pattern: "**/.env*" # optional fnmatch glob (None = match all)
        action: block       # "block" | "allow" | "log"
        reason: "protect env files"

For ``action: block`` a blocking pre-hook is registered.
For ``action: allow`` or ``action: log`` an allow/log post-hook is registered
(pre allow is a no-op from a gate perspective, but the hook still runs for
side effects like metrics).
"""

from __future__ import annotations

import asyncio
import enum
import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

logger = logging.getLogger("Ouroboros.ToolHookRegistry")


# ---------------------------------------------------------------------------
# Public API types
# ---------------------------------------------------------------------------


class HookDecision(enum.Enum):
    """Decision returned by a pre-hook."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


PreHookCallable = Callable[[str, dict], Awaitable[HookDecision]]
PostHookCallable = Callable[[str, dict, Any], Awaitable[None]]


@dataclass
class _HookEntry:
    """Internal registry entry for a single hook."""

    tool: str  # tool name this hook applies to, or "*" for all tools
    pattern: Optional[str]  # fnmatch pattern for target path/command, or None
    handler: Union[PreHookCallable, PostHookCallable]


# ---------------------------------------------------------------------------
# ToolCallHookRegistry
# ---------------------------------------------------------------------------


class ToolCallHookRegistry:
    """Registry of pre/post interception hooks for tool calls.

    Usage::

        registry = ToolCallHookRegistry()

        async def env_guard(tool_name, tool_input):
            return HookDecision.BLOCK

        registry.register_pre("edit", "**/.env*", env_guard)

        decision = await registry.run_pre("edit", {"file": ".env"})
        # => HookDecision.BLOCK

    The registry can also be loaded from a YAML config file via
    ``ToolCallHookRegistry.from_yaml(path)``.
    """

    def __init__(self) -> None:
        self._pre_hooks: List[_HookEntry] = []
        self._post_hooks: List[_HookEntry] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_pre(
        self,
        tool_name: str,
        pattern: Optional[str],
        handler: PreHookCallable,
    ) -> None:
        """Register a pre-hook for *tool_name*.

        Parameters
        ----------
        tool_name:
            Name of the tool to intercept (e.g. ``"edit"``). Use ``"*"``
            to match all tools.
        pattern:
            fnmatch glob applied to the extracted target path/command. Pass
            ``None`` to match all inputs regardless of target.
        handler:
            Async callable ``(tool_name, tool_input) -> HookDecision``.
        """
        self._pre_hooks.append(_HookEntry(tool=tool_name, pattern=pattern, handler=handler))

    def register_post(
        self,
        tool_name: str,
        pattern: Optional[str],
        handler: PostHookCallable,
    ) -> None:
        """Register a post-hook for *tool_name*.

        Parameters
        ----------
        tool_name:
            Name of the tool to intercept. Use ``"*"`` to match all tools.
        pattern:
            fnmatch glob applied to the extracted target. Pass ``None`` to
            match all inputs.
        handler:
            Async callable ``(tool_name, tool_input, result) -> None``.
        """
        self._post_hooks.append(_HookEntry(tool=tool_name, pattern=pattern, handler=handler))

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run_pre(self, tool_name: str, tool_input: dict) -> HookDecision:
        """Run all matching pre-hooks for *tool_name*.

        Returns :attr:`HookDecision.BLOCK` as soon as any hook blocks;
        subsequent hooks are skipped.  Exceptions in hooks are caught and
        logged — the method always returns :attr:`HookDecision.ALLOW` on
        exception (fail-open).

        Parameters
        ----------
        tool_name:
            The tool being called.
        tool_input:
            The input dict for the tool call.

        Returns
        -------
        HookDecision
            ALLOW if all hooks pass (or no hooks matched); BLOCK if any hook
            returned BLOCK.
        """
        target = self._extract_target(tool_input)

        for entry in self._pre_hooks:
            if not self._tool_matches(entry.tool, tool_name):
                continue
            if entry.pattern is not None and not self._matches(target, entry.pattern):
                continue
            try:
                decision = await entry.handler(tool_name, tool_input)  # type: ignore[arg-type]
                if decision == HookDecision.BLOCK:
                    logger.debug(
                        "Pre-hook BLOCKED tool=%s target=%s handler=%s",
                        tool_name,
                        target,
                        getattr(entry.handler, "__name__", repr(entry.handler)),
                    )
                    return HookDecision.BLOCK
            except Exception:
                logger.exception(
                    "Pre-hook error (fail-open) tool=%s handler=%s",
                    tool_name,
                    getattr(entry.handler, "__name__", repr(entry.handler)),
                )
                # fail-open: continue to next hook
                continue

        return HookDecision.ALLOW

    async def run_post(self, tool_name: str, tool_input: dict, result: Any) -> None:
        """Run all matching post-hooks for *tool_name*.

        Errors in individual hooks are logged and swallowed; all matching
        post-hooks always attempt to run (fire-and-forget semantics).

        Parameters
        ----------
        tool_name:
            The tool that was called.
        tool_input:
            The input dict that was passed to the tool.
        result:
            The return value from the tool call.
        """
        target = self._extract_target(tool_input)

        for entry in self._post_hooks:
            if not self._tool_matches(entry.tool, tool_name):
                continue
            if entry.pattern is not None and not self._matches(target, entry.pattern):
                continue
            try:
                await entry.handler(tool_name, tool_input, result)  # type: ignore[arg-type]
            except Exception:
                logger.exception(
                    "Post-hook error (swallowed) tool=%s handler=%s",
                    tool_name,
                    getattr(entry.handler, "__name__", repr(entry.handler)),
                )

    # ------------------------------------------------------------------
    # YAML loader
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, config_path: str) -> "ToolCallHookRegistry":
        """Load a ToolCallHookRegistry from a YAML configuration file.

        The path may be an env-var reference: if ``config_path`` is a bare
        env-var name it is resolved via ``os.environ``; otherwise the path
        is used directly.

        YAML schema::

            hooks:
              - tool: edit
                event: pre          # "pre" | "post"
                pattern: "**/.env*" # optional; omit or null to match all
                action: block       # "block" | "allow" | "log"
                reason: "protect env files"

        Parameters
        ----------
        config_path:
            Path to the YAML file (or env-var name containing the path).

        Returns
        -------
        ToolCallHookRegistry
            Populated registry with hooks from the YAML config.
        """
        import yaml  # optional dependency; only needed for YAML loading

        resolved_path = os.environ.get(config_path, config_path)
        path = Path(resolved_path)

        registry = cls()

        if not path.exists():
            logger.warning("Hook config not found at %s — empty registry", path)
            return registry

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        hook_defs = raw.get("hooks", [])

        for hook_def in hook_defs:
            tool = hook_def.get("tool", "*")
            event = hook_def.get("event", "pre").lower()
            pattern = hook_def.get("pattern") or None
            action = hook_def.get("action", "allow").lower()
            reason = hook_def.get("reason", "")

            if event == "pre":
                if action == "block":
                    # Build a blocking pre-hook closure
                    _reason = reason

                    async def _blocking_hook(
                        tool_name: str,
                        tool_input: dict,
                        _r: str = _reason,
                    ) -> HookDecision:
                        logger.info(
                            "YAML pre-hook BLOCK tool=%s reason=%s", tool_name, _r
                        )
                        return HookDecision.BLOCK

                    registry.register_pre(tool, pattern, _blocking_hook)
                else:
                    # allow/log — still run as pre-hook but return ALLOW
                    _reason = reason

                    async def _allow_hook(
                        tool_name: str,
                        tool_input: dict,
                        _r: str = _reason,
                    ) -> HookDecision:
                        logger.debug(
                            "YAML pre-hook ALLOW tool=%s reason=%s", tool_name, _r
                        )
                        return HookDecision.ALLOW

                    registry.register_pre(tool, pattern, _allow_hook)

            elif event == "post":
                _reason = reason

                async def _post_hook(
                    tool_name: str,
                    tool_input: dict,
                    result: Any,
                    _r: str = _reason,
                ) -> None:
                    logger.debug(
                        "YAML post-hook tool=%s reason=%s result=%r",
                        tool_name,
                        _r,
                        result,
                    )

                registry.register_post(tool, pattern, _post_hook)

            else:
                logger.warning("Unknown hook event %r in %s — skipping", event, path)

        logger.info("Loaded %d hook(s) from %s", len(hook_defs), path)
        return registry

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_target(tool_input: dict) -> str:
        """Extract the primary target string from a tool_input dict.

        Tries common keys in priority order: file, path, command, url, target.
        Falls back to an empty string so pattern matching is well-defined.
        """
        for key in ("file", "path", "command", "url", "target"):
            val = tool_input.get(key)
            if val is not None:
                return str(val)
        return ""

    @staticmethod
    def _matches(target: str, pattern: str) -> bool:
        """Return True if *target* matches *pattern* (fnmatch with basename fallback).

        Tries a full-path match first.  If that fails, tries matching only the
        basename of *target* against *pattern* (handles ``**/.env*`` matching
        ``/project/.env``).
        """
        if not target:
            return False
        if fnmatch.fnmatch(target, pattern):
            return True
        # basename fallback
        basename = Path(target).name
        if fnmatch.fnmatch(basename, pattern):
            return True
        # strip leading **/ from pattern and try suffix match
        stripped = pattern.lstrip("*").lstrip("/")
        if stripped and fnmatch.fnmatch(target, f"*{stripped}"):
            return True
        return False

    @staticmethod
    def _tool_matches(entry_tool: str, tool_name: str) -> bool:
        """Return True if the hook entry's tool spec matches *tool_name*."""
        return entry_tool == "*" or entry_tool == tool_name
