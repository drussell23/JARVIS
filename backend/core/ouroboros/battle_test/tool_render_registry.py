"""ToolRenderRegistry — descriptor-based rendering for Venom tool results.
==========================================================================

Slice 1 of the **Gap #2 closure arc** (Claude-Code-parity TUI rendering).

Root problem
------------

Tool-result rendering today is hardcoded across two files:

* ``serpent_flow.op_tool_call`` (production CC-style path) hard-branches
  on ``tool_name == "edit_file"`` / ``"write_file"`` / ``"read_file"`` and
  carries a ``tool_icons`` dict literal — adding a new Venom tool means
  patching three locations.
* ``ouroboros_tui.show_tool_call`` (legacy/expand path) hard-codes
  ``8/20``-line truncation caps + a per-tool ``if/elif`` chain on
  ``read_file``/``bash``/``search_code``/``run_tests``.

Both paths re-truncate output that ``tool_executor`` already capped at
storage time — duplicate, uncoordinated policies. Operators see either
unbounded body bloat (legacy path) or near-total suppression (CC path),
with nothing in between.

Slice 1 scope
-------------

This module supplies the **declarative substrate** that subsequent slices
build on:

* :class:`ToolRenderDescriptor` — frozen per-tool render contract: icon,
  CC verb (``"Read"``/``"Update"``/``"Write"``/``None``), body shape,
  syntax lexer, args/result summarizers.
* ``_DESCRIPTORS`` — declarative ``Mapping[str, ToolRenderDescriptor]``
  covering all 18 Venom tools (15 in ``tool_executor._dispatch`` plus
  3 async-native: ``web_fetch``, ``web_search``, ``ask_human``).
* :data:`_DEFAULT_DESCRIPTOR` — fallback for unknown tool kinds (MCP
  tools forwarded via Gap #7 land here unless registered).
* :func:`render` — pure rendering function returning a structured
  :class:`RenderedToolResult`. Zero Console import, zero Rich import,
  zero ``if tool_name == "..."`` chains.

Authority boundary
------------------

* §1 deterministic — no LLM, no tool use, no authority surface.
* §7 fail-closed — defensive summarizers never raise; on any internal
  failure they return a generic 1-line summary so the render path
  always produces something displayable.
* §8 observable — schema-versioned frozen records suitable for SSE
  serialization in later slices.

What this module does NOT do (deferred to later slices)
--------------------------------------------------------

* Slice 2 — adaptive density resolution from posture × layout × env
  (lives in ``tool_render_policy.py``).
* Slice 3 — bounded body store + expansion ref keying (lives in
  ``tool_render_store.py``).
* Slice 4 — wiring into ``serpent_flow`` / ``ouroboros_tui`` call
  sites + Rich markup composition.
* Slice 5 — FlagRegistry seeds + ``shipped_code_invariants`` AST pins
  + memory file.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple


# ===========================================================================
# Schema
# ===========================================================================


TOOL_RENDER_REGISTRY_SCHEMA_VERSION: str = "tool_render_registry.v1"


# ===========================================================================
# Closed taxonomy — body shape vocabulary
# ===========================================================================


class BodyShape(str, enum.Enum):
    """How a tool's result body should be rendered.

    Closed 6-value taxonomy. Slice 4 maps each shape to a concrete Rich
    primitive; the descriptor stays renderer-agnostic.
    """

    NONE = "none"            # Header-only (read_file CC convention)
    SINGLE_LINE = "single"   # 1-line summary, no body block (default)
    MULTI_LINE = "multi"     # Plain multi-line body (head+tail bounded)
    DIFF = "diff"            # Diff body — green/red coloring downstream
    CODE = "code"            # Source code body — syntax-highlight downstream
    LOG = "log"              # Bash / log output — dim styling downstream


class ToolStatus(str, enum.Enum):
    """Closed status vocabulary mirroring ``ToolExecStatus`` shape but
    decoupled — descriptors should not depend on the executor module."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    DENIED = "denied"

    @classmethod
    def coerce(cls, raw: object) -> "ToolStatus":
        """Lenient parse — anything that isn't recognized becomes ERROR.
        NEVER raises."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.ERROR


# ===========================================================================
# Frozen descriptor — one per tool kind
# ===========================================================================


# A summarizer takes (raw_input, status) and returns a 1-line human
# string. NEVER raises — defensive contract.
ArgsSummarizer = Callable[[str], str]
ResultSummarizer = Callable[[str, ToolStatus], str]


@dataclass(frozen=True)
class ToolRenderDescriptor:
    """Frozen per-tool render contract.

    The descriptor is **renderer-agnostic** — it produces strings, not
    Rich markup. The Slice 4 wiring layer is responsible for wrapping
    fields with the existing ``_C`` palette + ``⏺``/``⎿`` glyphs.

    Fields
    ------
    * ``tool_kind`` — canonical Venom tool name (matches
      ``tool_executor._dispatch`` key or ``ToolManifest.name``).
    * ``icon`` — single emoji for the legacy/non-CC render path.
    * ``cc_verb`` — Claude-Code-style verb (``"Read"``, ``"Update"``,
      ``"Write"``) or ``None`` for the icon-prefixed one-liner path.
    * ``body_shape`` — :class:`BodyShape` member; controls whether the
      caller emits a body block at all.
    * ``body_lexer`` — Rich Syntax lexer hint (``"python"``, ``"diff"``,
      ``"bash"``, ``"text"``) or ``None`` for plain dim text.
    * ``summarize_args`` — formats the tool's invocation summary
      (e.g., ``"backend/foo.py"`` for read_file, ``"$ pytest -x"`` for
      bash). Never raises.
    * ``summarize_result`` — formats a 1-line summary of the tool's
      output (e.g., ``"42 lines read"``, ``"Added 5, removed 2"``).
      Never raises.
    """

    tool_kind: str
    icon: str
    cc_verb: Optional[str]
    body_shape: BodyShape
    body_lexer: Optional[str]
    summarize_args: ArgsSummarizer
    summarize_result: ResultSummarizer
    schema_version: str = TOOL_RENDER_REGISTRY_SCHEMA_VERSION


@dataclass(frozen=True)
class RenderedToolResult:
    """Structured render output. The caller wraps fields in Rich markup
    using the existing ``_C`` palette + ``⏺``/``⎿`` glyphs.

    Fields
    ------
    * ``header_line`` — first display line; either ``"Read(path)"``
      style for CC verbs or ``"📄 read_file path"`` for icon style.
    * ``body_summary`` — one-line summary printed under the header
      with the ``⎿`` glyph (e.g., ``"42 lines read"``).
    * ``body_lines`` — bounded, head+tail-elided multi-line body
      chunk. Empty tuple for ``BodyShape.NONE`` or empty results.
    * ``elided_line_count`` — number of source lines that were elided
      from ``body_lines`` (0 if no truncation occurred). Lets the
      caller render an ``[expand t-12]`` hint when > 0.
    * ``expansion_ref`` — opaque handle issued by Slice 3's
      :class:`BoundedBodyStore`. ``None`` until Slice 4 wires the
      store; ``"t-N"`` once it lands.
    """

    header_line: str
    body_summary: str
    body_lines: Tuple[str, ...]
    elided_line_count: int
    expansion_ref: Optional[str] = None
    schema_version: str = TOOL_RENDER_REGISTRY_SCHEMA_VERSION


# ===========================================================================
# Args summarizers — pure, defensive
# ===========================================================================


def _safe_str(raw: object) -> str:
    """Coerce arbitrary input to a string. NEVER raises."""
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


def _truncate(s: str, limit: int) -> str:
    """Truncate without breaking mid-word when possible.

    The ellipsis is a single character ``…`` (U+2026); not three dots,
    so the visible width stays within ``limit``."""
    if not isinstance(limit, int) or limit < 1:
        return ""
    if len(s) <= limit:
        return s
    if limit < 4:
        return s[:limit]
    return s[: limit - 1] + "…"


def _summarize_path_arg(args: str) -> str:
    """Path-shaped args (read_file, edit_file, write_file, glob, list_dir)."""
    s = _safe_str(args).strip()
    return _truncate(s or "file", 80)


def _summarize_bash_arg(args: str) -> str:
    """Bash command — show with leading ``$``."""
    s = _safe_str(args).strip()
    if not s:
        return "$"
    # Collapse internal whitespace for one-line display
    s = re.sub(r"\s+", " ", s)
    return _truncate(f"$ {s}", 80)


def _summarize_search_arg(args: str) -> str:
    """search_code / glob_files — show pattern in quotes."""
    s = _safe_str(args).strip()
    return _truncate(f'"{s}"' if s else '""', 80)


def _summarize_default_arg(args: str) -> str:
    """Generic args — straight truncation."""
    s = _safe_str(args).strip()
    return _truncate(s, 80)


# ===========================================================================
# Result summarizers — pure, defensive, never raise
# ===========================================================================


def _line_count(s: str) -> int:
    """Lines in ``s``; 0 for empty/None."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _summarize_read(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"read failed ({status.value})"
    n = _line_count(result)
    return f"{n} line{'s' if n != 1 else ''} read"


def _summarize_search(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"search failed ({status.value})"
    if not result:
        return "no matches"
    # Heuristic: count non-empty result lines as match-equivalent units
    matches = sum(1 for ln in result.splitlines() if ln.strip())
    return f"{matches} match{'es' if matches != 1 else ''}"


def _summarize_edit(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"edit failed ({status.value})"
    # Parse +/- counts from a unified-diff-shaped result if present
    added = sum(
        1 for ln in result.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    removed = sum(
        1 for ln in result.splitlines()
        if ln.startswith("-") and not ln.startswith("---")
    )
    if added == 0 and removed == 0:
        n = _line_count(result)
        return f"edit applied ({n} line{'s' if n != 1 else ''} affected)"
    return f"+{added} / -{removed} lines"


def _summarize_write(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"write failed ({status.value})"
    n = _line_count(result)
    return f"{n} line{'s' if n != 1 else ''} written"


def _summarize_delete(_result: str, status: ToolStatus) -> str:
    # ``_result`` deliberately unused — delete confirms the action,
    # the body content (if any) carries no operator-relevant signal.
    del _result
    if status is not ToolStatus.SUCCESS:
        return f"delete failed ({status.value})"
    return "file deleted"


def _summarize_bash(result: str, status: ToolStatus) -> str:
    if status is ToolStatus.TIMEOUT:
        return "command timed out"
    if status is not ToolStatus.SUCCESS:
        return f"command failed ({status.value})"
    n = _line_count(result)
    return f"{n} line{'s' if n != 1 else ''} of output"


def _summarize_tests(result: str, status: ToolStatus) -> str:
    if status is ToolStatus.TIMEOUT:
        return "tests timed out"
    # Pull pytest summary line if present (e.g. "5 passed, 2 failed in 1.23s")
    summary_rx = re.compile(
        r"(\d+\s+passed|\d+\s+failed|\d+\s+error|\d+\s+skipped)",
        re.IGNORECASE,
    )
    summary_lines = [
        ln.strip() for ln in result.splitlines()
        if summary_rx.search(ln)
    ]
    if summary_lines:
        return _truncate(summary_lines[-1], 80)
    if status is not ToolStatus.SUCCESS:
        return f"tests {status.value}"
    return "tests completed"


def _summarize_callers(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"caller scan failed ({status.value})"
    if not result:
        return "no callers found"
    # Each line typically corresponds to one caller site
    n = sum(1 for ln in result.splitlines() if ln.strip())
    return f"{n} caller{'s' if n != 1 else ''} found"


def _summarize_glob(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"glob failed ({status.value})"
    n = sum(1 for ln in result.splitlines() if ln.strip())
    return f"{n} path{'s' if n != 1 else ''} matched"


def _summarize_list_dir(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"list_dir failed ({status.value})"
    n = sum(1 for ln in result.splitlines() if ln.strip())
    return f"{n} entr{'ies' if n != 1 else 'y'}"


def _summarize_list_symbols(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"list_symbols failed ({status.value})"
    n = sum(1 for ln in result.splitlines() if ln.strip())
    return f"{n} symbol{'s' if n != 1 else ''}"


def _summarize_git_log(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"git log failed ({status.value})"
    # Each commit typically starts with "commit <sha>" or a short hash
    commit_rx = re.compile(r"^(commit\s+[0-9a-f]{7,40}|[0-9a-f]{7,40}\s)")
    n = sum(1 for ln in result.splitlines() if commit_rx.match(ln.strip()))
    if n == 0:
        n = _line_count(result)
        return f"{n} line{'s' if n != 1 else ''}"
    return f"{n} commit{'s' if n != 1 else ''}"


def _summarize_git_diff(result: str, status: ToolStatus) -> str:
    return _summarize_edit(result, status)  # Reuse +/- parser


def _summarize_git_blame(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"git blame failed ({status.value})"
    n = _line_count(result)
    return f"{n} line{'s' if n != 1 else ''} blamed"


def _summarize_type_check(result: str, status: ToolStatus) -> str:
    if status is ToolStatus.TIMEOUT:
        return "type check timed out"
    # Pull mypy/pyright summary line if present
    err_rx = re.compile(r"(\d+\s+error|\d+\s+warning|Success:)", re.IGNORECASE)
    matched = [ln.strip() for ln in result.splitlines() if err_rx.search(ln)]
    if matched:
        return _truncate(matched[-1], 80)
    if status is not ToolStatus.SUCCESS:
        return f"type check {status.value}"
    return "type check ok"


def _summarize_web_fetch(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"fetch failed ({status.value})"
    n = _line_count(result)
    return f"{n} line{'s' if n != 1 else ''} fetched"


def _summarize_web_search(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"search failed ({status.value})"
    n = sum(1 for ln in result.splitlines() if ln.strip())
    return f"{n} result{'s' if n != 1 else ''}"


def _summarize_ask_human(result: str, status: ToolStatus) -> str:
    if status is ToolStatus.TIMEOUT:
        return "operator did not respond"
    if status is not ToolStatus.SUCCESS:
        return f"ask_human {status.value}"
    n = _line_count(result)
    return f"operator replied ({n} line{'s' if n != 1 else ''})"


def _summarize_default(result: str, status: ToolStatus) -> str:
    if status is not ToolStatus.SUCCESS:
        return f"failed ({status.value})"
    if not result:
        return "completed"
    n = _line_count(result)
    return f"completed ({n} line{'s' if n != 1 else ''})"


# Wrap any summarizer so an internal exception degrades gracefully
def _safe_args_summarizer(fn: ArgsSummarizer) -> ArgsSummarizer:
    def _wrapped(args: str) -> str:
        try:
            return fn(args)
        except Exception:  # noqa: BLE001
            return _safe_str(args)[:40]
    return _wrapped


def _safe_result_summarizer(fn: ResultSummarizer) -> ResultSummarizer:
    def _wrapped(result: str, status: ToolStatus) -> str:
        try:
            return fn(result, status)
        except Exception:  # noqa: BLE001
            if status is not ToolStatus.SUCCESS:
                return f"failed ({status.value})"
            return "completed"
    return _wrapped


# ===========================================================================
# The descriptor table — declarative, source of truth
# ===========================================================================


def _make(
    tool_kind: str,
    icon: str,
    cc_verb: Optional[str],
    body_shape: BodyShape,
    body_lexer: Optional[str],
    args_fn: ArgsSummarizer,
    result_fn: ResultSummarizer,
) -> ToolRenderDescriptor:
    return ToolRenderDescriptor(
        tool_kind=tool_kind,
        icon=icon,
        cc_verb=cc_verb,
        body_shape=body_shape,
        body_lexer=body_lexer,
        summarize_args=_safe_args_summarizer(args_fn),
        summarize_result=_safe_result_summarizer(result_fn),
    )


_DESCRIPTORS: Mapping[str, ToolRenderDescriptor] = {
    # ---- Read-shape (header-only by CC convention) ----------------------
    "read_file": _make(
        "read_file", "📄", "Read",
        BodyShape.NONE, None,
        _summarize_path_arg, _summarize_read,
    ),
    "list_symbols": _make(
        "list_symbols", "📋", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_path_arg, _summarize_list_symbols,
    ),
    "list_dir": _make(
        "list_dir", "📂", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_path_arg, _summarize_list_dir,
    ),
    "glob_files": _make(
        "glob_files", "📁", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_search_arg, _summarize_glob,
    ),
    "search_code": _make(
        "search_code", "🔍", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_search_arg, _summarize_search,
    ),
    "get_callers": _make(
        "get_callers", "🔗", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_default_arg, _summarize_callers,
    ),
    # ---- Write-shape (CC-style Update/Write headers) --------------------
    "edit_file": _make(
        "edit_file", "✏️", "Update",
        BodyShape.DIFF, "diff",
        _summarize_path_arg, _summarize_edit,
    ),
    "write_file": _make(
        "write_file", "📝", "Write",
        BodyShape.SINGLE_LINE, None,
        _summarize_path_arg, _summarize_write,
    ),
    "delete_file": _make(
        "delete_file", "🗑️", None,
        BodyShape.SINGLE_LINE, None,
        _summarize_path_arg, _summarize_delete,
    ),
    # ---- Execution-shape (LOG body) -------------------------------------
    "bash": _make(
        "bash", "💻", None,
        BodyShape.LOG, "bash",
        _summarize_bash_arg, _summarize_bash,
    ),
    "run_tests": _make(
        "run_tests", "🧪", None,
        BodyShape.LOG, "text",
        _summarize_default_arg, _summarize_tests,
    ),
    "type_check": _make(
        "type_check", "🔬", None,
        BodyShape.LOG, "text",
        _summarize_default_arg, _summarize_type_check,
    ),
    # ---- Git-shape ------------------------------------------------------
    "git_log": _make(
        "git_log", "📜", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_default_arg, _summarize_git_log,
    ),
    "git_diff": _make(
        "git_diff", "📊", None,
        BodyShape.DIFF, "diff",
        _summarize_default_arg, _summarize_git_diff,
    ),
    "git_blame": _make(
        "git_blame", "🔎", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_path_arg, _summarize_git_blame,
    ),
    # ---- Async-native (ToolManifest-only, not in _dispatch) -------------
    "web_fetch": _make(
        "web_fetch", "🌐", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_default_arg, _summarize_web_fetch,
    ),
    "web_search": _make(
        "web_search", "🌐", None,
        BodyShape.MULTI_LINE, "text",
        _summarize_search_arg, _summarize_web_search,
    ),
    "ask_human": _make(
        "ask_human", "🗣️", None,
        BodyShape.SINGLE_LINE, None,
        _summarize_default_arg, _summarize_ask_human,
    ),
}


# Fallback for unknown tool kinds (MCP forwarded tools, plugin tools)
_DEFAULT_DESCRIPTOR: ToolRenderDescriptor = _make(
    "_default", "🔧", None,
    BodyShape.SINGLE_LINE, None,
    _summarize_default_arg, _summarize_default,
)


# ===========================================================================
# Public lookup API
# ===========================================================================


def get_descriptor(tool_kind: object) -> ToolRenderDescriptor:
    """Resolve descriptor for ``tool_kind``.

    Falls back to :data:`_DEFAULT_DESCRIPTOR` for unknown tools (MCP
    tools forwarded via Gap #7 land here unless explicitly registered).
    NEVER raises — empty / non-string input also yields the default.

    The parameter is typed ``object`` (not ``str``) because the
    function's contract explicitly accepts arbitrary inputs from
    untrusted call sites (deserialized JSON, plugin-provided names,
    test fixtures). Callers that pass ``str`` get full type-narrowing
    inside the function.
    """
    if not isinstance(tool_kind, str):
        return _DEFAULT_DESCRIPTOR
    return _DESCRIPTORS.get(tool_kind, _DEFAULT_DESCRIPTOR)


def known_tool_kinds() -> Tuple[str, ...]:
    """Return the registered tool kinds in stable alphabetical order.

    Used by Slice 5's descriptor-completeness AST pin to verify the
    table covers every Venom tool kind in ``tool_executor._dispatch``.
    """
    return tuple(sorted(_DESCRIPTORS.keys()))


def is_known_tool(tool_kind: str) -> bool:
    """``True`` iff a non-default descriptor is registered for the kind."""
    return isinstance(tool_kind, str) and tool_kind in _DESCRIPTORS


def default_descriptor() -> ToolRenderDescriptor:
    """Expose the fallback descriptor for tests + introspection."""
    return _DEFAULT_DESCRIPTOR


# ===========================================================================
# Pure render function — no Console, no Rich
# ===========================================================================


def render(
    descriptor: object,
    args_str: object,
    result_str: object,
    status: object = ToolStatus.SUCCESS,
    *,
    max_body_lines: int = 0,
    expansion_ref: object = None,
) -> RenderedToolResult:
    """Pure render — produces a structured result the caller wraps in Rich.

    Parameters
    ----------
    descriptor :
        Resolved via :func:`get_descriptor`.
    args_str :
        Tool invocation summary (path, command, pattern, etc.).
    result_str :
        Full tool output. May be empty or huge — bounded internally.
    status :
        :class:`ToolStatus` member or string; coerced via
        :meth:`ToolStatus.coerce`.
    max_body_lines :
        Body line budget. ``0`` disables the body block entirely
        (header + summary only). Slice 2 derives this from
        :class:`DensityPolicy`; Slice 4 wires the call site.
    expansion_ref :
        Opaque handle from the Slice 3 store (e.g. ``"t-12"``); shown
        as part of the truncation marker when the body is elided.
        ``None`` until Slice 4 wires the store.

    NEVER raises — defensive at every step. A failure inside the
    descriptor's summarizer degrades to a generic 1-line summary
    (handled by :func:`_safe_*_summarizer` wrappers).
    """
    if not isinstance(descriptor, ToolRenderDescriptor):
        descriptor = _DEFAULT_DESCRIPTOR

    args_safe = _safe_str(args_str)
    result_safe = _safe_str(result_str)
    status_enum = ToolStatus.coerce(status)

    args_summary = descriptor.summarize_args(args_safe)
    body_summary = descriptor.summarize_result(result_safe, status_enum)

    # Header line: CC verb (Read/Update/Write/...) takes precedence;
    # otherwise icon-prefixed kind for the legacy-style one-liner path.
    if descriptor.cc_verb:
        header_line = f"{descriptor.cc_verb}({args_summary})"
    else:
        header_line = (
            f"{descriptor.icon} {descriptor.tool_kind} {args_summary}".rstrip()
        )

    # Body lines: only when shape allows AND budget > 0 AND result has content.
    body_lines: Tuple[str, ...] = ()
    elided = 0
    body_eligible = (
        descriptor.body_shape is not BodyShape.NONE
        and isinstance(max_body_lines, int)
        and max_body_lines > 0
        and bool(result_safe)
    )
    if body_eligible:
        body_lines, elided = _extract_body(
            result_safe, max_body_lines,
        )

    return RenderedToolResult(
        header_line=header_line,
        body_summary=body_summary,
        body_lines=body_lines,
        elided_line_count=elided,
        expansion_ref=expansion_ref if isinstance(expansion_ref, str) else None,
    )


def _extract_body(
    result: str, max_lines: int,
) -> Tuple[Tuple[str, ...], int]:
    """Head + tail elision (CC pattern).

    Returns ``(body_lines, elided_count)``. When the source fits in
    the budget no truncation marker is inserted. When the source
    exceeds budget, head + tail are kept and a single ``…`` marker
    line replaces the elided middle. The marker counts toward the
    budget so total line count never exceeds ``max_lines``.
    """
    lines = result.splitlines()
    if len(lines) <= max_lines:
        return tuple(lines), 0

    # Reserve 1 slot for the truncation marker.
    payload = max_lines - 1
    if payload < 2:
        # Degenerate budget — just return head, no marker.
        return tuple(lines[:max_lines]), len(lines) - max_lines

    head_count = max(1, (payload * 2) // 3)
    tail_count = payload - head_count
    if tail_count < 1:
        tail_count = 1
        head_count = payload - 1

    elided = len(lines) - head_count - tail_count
    marker = f"… +{elided} more line{'s' if elided != 1 else ''} elided …"
    return (
        (*lines[:head_count], marker, *lines[-tail_count:]),
        elided,
    )


__all__ = [
    "TOOL_RENDER_REGISTRY_SCHEMA_VERSION",
    "BodyShape",
    "RenderedToolResult",
    "ToolRenderDescriptor",
    "ToolStatus",
    "default_descriptor",
    "get_descriptor",
    "is_known_tool",
    "known_tool_kinds",
    "render",
]
