"""Q1 Slice 1 — /compact operator REPL.

Closes the operator-UX hole on context compaction. Today
``ContextCompactor`` is substrate-only (``context_compaction.py``)
and operators have no way to inspect compaction config, see how
many entries are queued for compaction, or trigger a compaction
pass on demand.

This REPL exposes three subcommands:

    /compact                    status (alias for 'status')
    /compact status             render CompactionConfig + last result
    /compact run                fire compaction over caller-supplied entries
    /compact help               verb surface

Authority discipline (mirror of posture_repl):

  * Read-mostly. ``run`` invokes the existing ``ContextCompactor.compact``
    primitive — does NOT mutate gate state, risk tiers, approvals, or
    orchestrator FSM. The compaction itself is a substrate operation
    operators were already capable of triggering through the runtime;
    the REPL just makes it visible + on-demand.
  * No imports from orchestrator / iron_gate / policy / risk_engine /
    change_engine / candidate_generator / gate. AST-pinned by tests.
  * Master flag ``JARVIS_COMPACT_REPL_ENABLED`` (default ``true`` —
    read-only safe + run gates on substrate's own master via the
    underlying compactor's PRE_COMPACT hooks).

Rendering — flat text only (no rich dependency); the REPL output
is consumed by SerpentFlow's plain-text channel.
"""
from __future__ import annotations

import logging
import shlex
import textwrap
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.context_compaction import (
    CompactionConfig,
    CompactionResult,
)

logger = logging.getLogger("Ouroboros.CompactREPL")


COMPACT_REPL_SCHEMA_VERSION: str = "compact_repl.1"


_COMMANDS = frozenset({"/compact"})

_VALID_SUBCOMMANDS = frozenset({
    "status", "run", "help", "?",
})

_HELP = textwrap.dedent(
    """
    Context compaction — operator surface
    -------------------------------------
      /compact                    current status (alias for 'status')
      /compact status             config + last result + thresholds
      /compact run                force-fire compaction over caller-
                                  supplied entries (returns
                                  CompactionResult summary)
      /compact help               this text

    Tunables (env):
      JARVIS_COMPACT_MAX_ENTRIES         trigger threshold (default 50)
      JARVIS_COMPACT_PRESERVE_COUNT      always-keep most recent N (default 10)
      JARVIS_COMPACT_PRESERVE_PATTERNS   comma-separated regex patterns

    Master flag:
      JARVIS_COMPACT_REPL_ENABLED        master kill switch (default true)
    """
).strip()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def compact_repl_enabled() -> bool:
    """``JARVIS_COMPACT_REPL_ENABLED`` (default ``true``). Empty /
    unset / whitespace = default. Truthy = ``1``/``true``/``yes``/
    ``on`` (case-insensitive). NEVER raises."""
    import os
    try:
        raw = os.environ.get(
            "JARVIS_COMPACT_REPL_ENABLED", "",
        ).strip().lower()
        if raw == "":
            return True  # default-on; help always works regardless
        return raw in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Module-level provider — tests inject via param; production wires
# the singleton compactor at boot via set_default_compactor.
# ---------------------------------------------------------------------------


_default_compactor: Optional[Any] = None
_last_result: Optional[CompactionResult] = None


def set_default_compactor(compactor: Any) -> None:
    """Production wires the runtime ``ContextCompactor`` instance
    here at boot. Tests inject directly via ``compactor`` kwarg."""
    global _default_compactor
    _default_compactor = compactor


def reset_default_compactor() -> None:
    global _default_compactor, _last_result
    _default_compactor = None
    _last_result = None


def _record_result(result: CompactionResult) -> None:
    """Capture the latest result for ``status`` rendering. Module-
    level state is intentional — the REPL is the single consumer."""
    global _last_result
    _last_result = result


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CompactDispatchResult:
    """Outcome of one ``/compact`` invocation. Mirrors PostureDispatchResult.

    ``matched`` is False when the caller's line doesn't start with
    ``/compact`` — caller can chain to other dispatchers."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_compact_command(
    line: str,
    *,
    compactor: Optional[Any] = None,
    dialogue_entries: Optional[List[Dict[str, Any]]] = None,
    op_id: Optional[str] = None,
) -> CompactDispatchResult:
    """Parse a ``/compact`` line + dispatch.

    ``compactor`` is the substrate ``ContextCompactor`` instance —
    tests inject; production calls ``set_default_compactor`` at
    boot.

    ``dialogue_entries`` is the entry list to compact when running
    ``/compact run``. Production callers pass the live runtime
    context; tests pass synthetic lists. When ``None``, ``run``
    returns an explanatory error rather than running on an empty
    list (would compact nothing — wasted operator time).

    NEVER raises into the caller; every failure path returns a
    structured ``CompactDispatchResult``.
    """
    if not _matches(line):
        return CompactDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CompactDispatchResult(
            ok=False, text=f"  /compact parse error: {exc}",
        )
    if not tokens:
        return CompactDispatchResult(ok=False, text="", matched=False)

    args = tokens[1:]
    head = (args[0].lower() if args else "status")

    # help always works (discoverability) — bypasses master flag.
    if head in ("help", "?"):
        return CompactDispatchResult(ok=True, text=_HELP)

    if head not in _VALID_SUBCOMMANDS:
        return CompactDispatchResult(
            ok=False,
            text=(
                f"  /compact: unknown subcommand {head!r}. "
                f"Valid: {', '.join(sorted(_VALID_SUBCOMMANDS))}"
            ),
        )

    if not compact_repl_enabled():
        return CompactDispatchResult(
            ok=False,
            text=(
                "  /compact: REPL disabled — set "
                "JARVIS_COMPACT_REPL_ENABLED=true to enable. "
                "/compact help still works."
            ),
        )

    resolved_compactor = (
        compactor if compactor is not None else _default_compactor
    )

    if head == "status":
        return _handle_status(resolved_compactor)
    if head == "run":
        return _handle_run(
            resolved_compactor, dialogue_entries, op_id,
        )
    # Unreachable by construction (vocabulary check above) but
    # defense-in-depth.
    return CompactDispatchResult(
        ok=False, text=f"  /compact: unhandled subcommand {head!r}",
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_status(compactor: Optional[Any]) -> CompactDispatchResult:
    """Render CompactionConfig + last result + readiness."""
    try:
        cfg = CompactionConfig.from_env()
    except Exception as exc:  # noqa: BLE001 — defensive
        return CompactDispatchResult(
            ok=False, text=f"  /compact status: config read failed: {exc}",
        )

    lines: List[str] = ["  Context compaction — current status"]
    lines.append("")
    lines.append("  Config (from env):")
    lines.append(
        f"    max_context_entries: {cfg.max_context_entries}"
    )
    lines.append(
        f"    preserve_count:      {cfg.preserve_count}"
    )
    pp = list(cfg.preserve_patterns) if cfg.preserve_patterns else []
    if pp:
        lines.append("    preserve_patterns:")
        for pat in pp:
            lines.append(f"      - {pat}")
    else:
        lines.append("    preserve_patterns:   (none)")

    lines.append("")
    lines.append(
        f"  Compactor:           "
        f"{'wired' if compactor is not None else 'NOT WIRED — call /compact run with explicit compactor'}"
    )

    lines.append("")
    if _last_result is not None:
        r = _last_result
        lines.append("  Last compaction:")
        lines.append(f"    entries_before:    {r.entries_before}")
        lines.append(f"    entries_after:     {r.entries_after}")
        lines.append(f"    entries_compacted: {r.entries_compacted}")
        lines.append(f"    preserved_keys:    {len(r.preserved_keys)}")
        if r.summary:
            preview = r.summary[:200]
            if len(r.summary) > 200:
                preview = preview + "..."
            lines.append(f"    summary preview:   {preview}")
    else:
        lines.append("  Last compaction:    (none — REPL has not run a pass yet)")

    return CompactDispatchResult(ok=True, text="\n".join(lines))


def _handle_run(
    compactor: Optional[Any],
    dialogue_entries: Optional[List[Dict[str, Any]]],
    op_id: Optional[str],
) -> CompactDispatchResult:
    """Force-fire compaction. Caller MUST supply both compactor +
    dialogue_entries — this REPL is read-mostly and refuses to
    fabricate state."""
    if compactor is None:
        return CompactDispatchResult(
            ok=False,
            text=(
                "  /compact run: no compactor wired. Production "
                "boot calls set_default_compactor; tests inject "
                "via the compactor= kwarg."
            ),
        )
    if dialogue_entries is None:
        return CompactDispatchResult(
            ok=False,
            text=(
                "  /compact run: no dialogue_entries supplied. "
                "The caller (SerpentFlow / battle-test harness) "
                "must pass the live runtime context."
            ),
        )
    if not isinstance(dialogue_entries, list):
        return CompactDispatchResult(
            ok=False,
            text=(
                f"  /compact run: dialogue_entries must be a list, "
                f"got {type(dialogue_entries).__name__}"
            ),
        )

    cfg = CompactionConfig.from_env()
    try:
        result = _run_compaction_sync(
            compactor, dialogue_entries, cfg, op_id,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CompactREPL] /run dispatch raised: %s", exc,
        )
        return CompactDispatchResult(
            ok=False,
            text=f"  /compact run: dispatch failed: {exc!r}",
        )

    _record_result(result)

    # Render the result inline so the operator gets immediate
    # feedback (subsequent /compact status will show the same).
    lines = [
        "  /compact run: completed",
        "",
        f"    entries_before:    {result.entries_before}",
        f"    entries_after:     {result.entries_after}",
        f"    entries_compacted: {result.entries_compacted}",
        f"    preserved_keys:    {len(result.preserved_keys)}",
    ]
    if result.summary:
        preview = result.summary[:240]
        if len(result.summary) > 240:
            preview = preview + "..."
        lines.append(f"    summary:           {preview}")
    return CompactDispatchResult(ok=True, text="\n".join(lines))


def _run_compaction_sync(
    compactor: Any,
    dialogue_entries: List[Dict[str, Any]],
    cfg: CompactionConfig,
    op_id: Optional[str],
) -> CompactionResult:
    """Bridge the substrate's async ``compact`` into our sync REPL
    surface. Uses ``asyncio.run`` when no loop, else schedules in
    a fresh loop (matches replay_repl's bridging pattern)."""
    import asyncio
    coro = compactor.compact(
        dialogue_entries, cfg, op_id=op_id,
    )
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "running event loop" not in str(exc):
            raise
        # Re-entry: fresh loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "COMPACT_REPL_SCHEMA_VERSION",
    "CompactDispatchResult",
    "compact_repl_enabled",
    "dispatch_compact_command",
    "reset_default_compactor",
    "set_default_compactor",
]
