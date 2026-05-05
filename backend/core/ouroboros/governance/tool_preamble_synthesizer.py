"""ToolPreambleSynthesizer — deterministic preamble fallback for tool calls.
=============================================================================

Slice 2 of the **Gap #6 closure arc**.

Root problem
------------

The model is *supposed* to emit a 1-sentence "WHY" before each tool
call (the ``preamble`` field on :class:`ToolCall`). When it does,
``serpent_flow.py:1681`` renders ``🗣 {preamble}`` above the tool
spinner — beautiful, CC-equivalent UX. But the model **doesn't always
emit one**: prompt drift, batch tool calls, fast read-only chains,
fallback paths. The result is silent tool execution, which feels
opaque.

This module supplies the **deterministic fallback**: when the model
omits a preamble, synthesize one from the tool name + args + a
per-tool template. No LLM call (zero cost), no hardcoded if/elif
chain (descriptor-driven, mirrors the Gap #2 ``ToolRenderRegistry``
pattern), and the synthesized preamble enforces the user's
**Tool Transparency** constraint: every tool call must briefly
narrate its WHY.

Architectural reuse
-------------------

* Descriptor-table pattern from
  :mod:`backend.core.ouroboros.battle_test.tool_render_registry`
  (Gap #2 Slice 1) — one declarative template per Venom tool kind +
  a generic fallback for unknown / MCP-forwarded tools.
* Frozen dataclass + closed taxonomy + ``schema_version`` house style.

Authority boundary
------------------

* §1 deterministic — pure string formatting, no LLM, no I/O
* §7 fail-closed — every input has a documented fallback; non-string
  args / unknown tool kinds yield generic preambles; NEVER raises
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

logger = logging.getLogger("Ouroboros.ToolPreambleSynthesizer")


# ===========================================================================
# Schema
# ===========================================================================


TOOL_PREAMBLE_SCHEMA_VERSION: str = "tool_preamble_synthesizer.v1"


# ===========================================================================
# Frozen template descriptor — mirrors ToolRenderDescriptor shape
# ===========================================================================


# A template formatter takes the tool's args summary string and returns
# a human-readable WHY sentence. NEVER raises.
TemplateFormatter = Callable[[str], str]


@dataclass(frozen=True)
class PreambleTemplate:
    """Per-tool synthesized-preamble descriptor.

    Fields
    ------
    * ``tool_kind`` — canonical Venom tool name.
    * ``template_fn`` — formatter taking the tool's args string and
      returning the synthesized preamble. Must NEVER raise.
    """

    tool_kind: str
    template_fn: TemplateFormatter
    schema_version: str = TOOL_PREAMBLE_SCHEMA_VERSION


# ===========================================================================
# Helpers
# ===========================================================================


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


def _truncate(s: str, limit: int = 60) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


# ===========================================================================
# Per-tool template formatters — pure, defensive
# ===========================================================================


def _tpl_read_file(args: str) -> str:
    path = _truncate(args) or "the file"
    return f"I need to read {path} to understand the current state"


def _tpl_search_code(args: str) -> str:
    pattern = _truncate(args) or "this pattern"
    return f"I'll search the codebase for {pattern} to find relevant call sites"


def _tpl_glob_files(args: str) -> str:
    pattern = _truncate(args) or "this pattern"
    return f"I'll glob for {pattern} to enumerate matching paths"


def _tpl_list_dir(args: str) -> str:
    path = _truncate(args) or "the directory"
    return f"I'll list {path} to see what's there"


def _tpl_list_symbols(args: str) -> str:
    path = _truncate(args) or "this file"
    return f"I'll inspect the symbols in {path} to map the structure"


def _tpl_get_callers(args: str) -> str:
    target = _truncate(args) or "this symbol"
    return f"I'll find callers of {target} to understand the blast radius"


def _tpl_run_tests(args: str) -> str:
    scope = _truncate(args) if args else "the affected scope"
    return f"I'll run tests on {scope} to verify behavior"


def _tpl_type_check(args: str) -> str:
    scope = _truncate(args) if args else "the changed files"
    return f"I'll type-check {scope} to catch annotation errors"


def _tpl_bash(args: str) -> str:
    cmd = _truncate(args, limit=50) or "a shell command"
    return f"I'll run `{cmd}` to gather more context"


def _tpl_edit_file(args: str) -> str:
    path = _truncate(args) or "the file"
    return f"I'll edit {path} to apply the fix"


def _tpl_write_file(args: str) -> str:
    path = _truncate(args) or "the file"
    return f"I'll write {path} with the new content"


def _tpl_delete_file(args: str) -> str:
    path = _truncate(args) or "the file"
    return f"I'll delete {path} since it's no longer needed"


def _tpl_git_log(args: str) -> str:
    scope = _truncate(args) if args else "the repo history"
    return f"I'll check git log for {scope} to understand recent changes"


def _tpl_git_diff(args: str) -> str:
    scope = _truncate(args) if args else "current changes"
    return f"I'll inspect git diff for {scope}"


def _tpl_git_blame(args: str) -> str:
    path = _truncate(args) or "this file"
    return f"I'll run git blame on {path} to trace the change history"


def _tpl_web_fetch(args: str) -> str:
    target = _truncate(args) or "this URL"
    return f"I'll fetch {target} to read external context"


def _tpl_web_search(args: str) -> str:
    query = _truncate(args) or "this query"
    return f"I'll search the web for \"{query}\" to find authoritative info"


def _tpl_ask_human(args: str) -> str:
    question = _truncate(args) if args else "the operator"
    return f"I need to ask: {question}"


def _tpl_default(args: str) -> str:
    """Fallback for unknown / MCP-forwarded tools."""
    summary = _truncate(args) if args else "additional information"
    return f"I need {summary} to make progress"


# Defensive wrapper — guarantees template formatters never crash the
# render path even on pathological input.
def _safe(fn: TemplateFormatter) -> TemplateFormatter:
    def _wrapped(args: str) -> str:
        try:
            out = fn(args)
            return out if isinstance(out, str) and out else _tpl_default(args)
        except Exception:  # noqa: BLE001
            return _tpl_default(args)
    return _wrapped


# ===========================================================================
# Declarative template table — source of truth
# ===========================================================================


_TEMPLATES: Mapping[str, PreambleTemplate] = {
    # Read-shape
    "read_file": PreambleTemplate("read_file", _safe(_tpl_read_file)),
    "search_code": PreambleTemplate("search_code", _safe(_tpl_search_code)),
    "glob_files": PreambleTemplate("glob_files", _safe(_tpl_glob_files)),
    "list_dir": PreambleTemplate("list_dir", _safe(_tpl_list_dir)),
    "list_symbols": PreambleTemplate("list_symbols", _safe(_tpl_list_symbols)),
    "get_callers": PreambleTemplate("get_callers", _safe(_tpl_get_callers)),
    # Execution-shape
    "run_tests": PreambleTemplate("run_tests", _safe(_tpl_run_tests)),
    "type_check": PreambleTemplate("type_check", _safe(_tpl_type_check)),
    "bash": PreambleTemplate("bash", _safe(_tpl_bash)),
    # Write-shape
    "edit_file": PreambleTemplate("edit_file", _safe(_tpl_edit_file)),
    "write_file": PreambleTemplate("write_file", _safe(_tpl_write_file)),
    "delete_file": PreambleTemplate("delete_file", _safe(_tpl_delete_file)),
    # Git-shape
    "git_log": PreambleTemplate("git_log", _safe(_tpl_git_log)),
    "git_diff": PreambleTemplate("git_diff", _safe(_tpl_git_diff)),
    "git_blame": PreambleTemplate("git_blame", _safe(_tpl_git_blame)),
    # Async-native
    "web_fetch": PreambleTemplate("web_fetch", _safe(_tpl_web_fetch)),
    "web_search": PreambleTemplate("web_search", _safe(_tpl_web_search)),
    "ask_human": PreambleTemplate("ask_human", _safe(_tpl_ask_human)),
}


_DEFAULT_TEMPLATE: PreambleTemplate = PreambleTemplate(
    "_default", _safe(_tpl_default),
)


# ===========================================================================
# Public API
# ===========================================================================


def get_template(tool_kind: object) -> PreambleTemplate:
    """Resolve a template for ``tool_kind``. Falls back to
    :data:`_DEFAULT_TEMPLATE` for unknown kinds. NEVER raises."""
    if not isinstance(tool_kind, str):
        return _DEFAULT_TEMPLATE
    return _TEMPLATES.get(tool_kind, _DEFAULT_TEMPLATE)


def known_tool_kinds() -> tuple:
    """Registered tool kinds in stable alphabetical order."""
    return tuple(sorted(_TEMPLATES.keys()))


def is_known_tool(tool_kind: object) -> bool:
    return isinstance(tool_kind, str) and tool_kind in _TEMPLATES


def synthesize_preamble(
    tool_kind: object,
    args: object,
    *,
    fallback_only: bool = True,
    existing_preamble: Optional[str] = None,
) -> str:
    """Produce a synthesized preamble for a tool call.

    Parameters
    ----------
    tool_kind :
        The Venom tool kind (``"read_file"``, ``"bash"``, ...).
    args :
        Args summary string from the tool call. Coerced safely.
    fallback_only :
        When ``True`` (default), if ``existing_preamble`` is non-empty
        we return it verbatim — synthesis is *fallback-only*. When
        ``False``, we always synthesize regardless. Slice 2's wiring
        passes ``True`` so the model's voluntary preambles win when
        present.
    existing_preamble :
        The model-emitted preamble, if any.

    Returns the synthesized (or pass-through) preamble string. NEVER
    raises — pathological inputs degrade to the generic fallback.
    """
    if fallback_only:
        existing_safe = _safe_str(existing_preamble).strip()
        if existing_safe:
            return existing_safe
    args_safe = _safe_str(args)
    template = get_template(tool_kind)
    return template.template_fn(args_safe)


__all__ = [
    "PreambleTemplate",
    "TOOL_PREAMBLE_SCHEMA_VERSION",
    "get_template",
    "is_known_tool",
    "known_tool_kinds",
    "synthesize_preamble",
]
