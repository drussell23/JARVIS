"""What an op has already learned survives its own retries.

## The defect

When a diff fails to place (``MalformedDiffException``: context diverges), up
to ~13 stateless retry layers fire for the same op — DiffContextReAlignment,
TerminalDiffCascade, provider attempts, ForegroundCooldown, the local
reroute. Each one enters ``ToolLoopCoordinator.run`` at round 0 with a blank
slate, and the prompt's exploration-first rule makes the model re-read the
same files before it may write. The tool calls are cheap; the MODEL ROUNDS
spent re-reading are not, and they are what pushed retries past their
deadlines on the local lane.

## The cache

Op-scoped, keyed by ``(tool, arguments_hash)`` — the same identity the
exploration ledger dedups on — and holding exactly what the model was shown
(post-redaction). Only exploration tools are cached
(``exploration_engine._TOOL_CATEGORY``, all read-only).

A result is VALID only while what it describes is unchanged:

* every path the call names that exists in the repo: same ``(mtime_ns, size)``;
* the op's own mutation epoch: any successful non-exploration tool this op
  runs (an edit, a write, a shell command) invalidates everything;
* the repository's index: another op's landing moves ``.git/index``.

An entry that fails its check is dropped, never shown.

## The opening block

At the start of every tool loop for an op that already has valid entries,
the loop opens with an "Already gathered" block (budget-aware, rendered by the
loop's own ``_format_tool_result``) and credits each shown entry to the
exploration ledger as the read it is. A PINNED region (the exact current
source around a hunk that failed to place) goes first and is read fresh.

Master ``JARVIS_OP_CONTEXT_CACHE_ENABLED`` (default true). NEVER raises.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance import source_lines

logger = logging.getLogger("Ouroboros.OpContextCache")

__all__ = ["enabled", "note_mutation", "opening", "pin_failed_hunk", "record",
           "forget", "reset_for_tests"]

_TRUTHY = ("1", "true", "yes", "on")


def enabled() -> bool:
    return os.environ.get("JARVIS_OP_CONTEXT_CACHE_ENABLED", "true").strip().lower() in _TRUTHY


def _env_int(name: str, default: int, lo: int) -> int:
    try:
        return max(lo, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def _cacheable(tool_name: str) -> bool:
    try:
        from backend.core.ouroboros.governance.exploration_engine import _TOOL_CATEGORY
        return tool_name in _TOOL_CATEGORY
    except Exception:  # noqa: BLE001
        return False


def _passive(tool_name: str) -> bool:
    """Tools that cannot change the repository: exploration, the web, MCP
    servers (they act outside the repo), asking the human."""
    return (_cacheable(tool_name) or tool_name.startswith(("web_", "mcp_"))
            or tool_name == "ask_human")


@dataclass
class _Entry:
    call: Any                 # ToolCall
    result: Any               # ToolResult, as the model saw it
    files: Tuple[Tuple[str, int, int], ...]
    epoch: int
    index_stamp: int


@dataclass
class _OpState:
    entries: "OrderedDict[Tuple[str, str], _Entry]" = field(default_factory=OrderedDict)
    epoch: int = 0
    pins: List[Tuple[str, int]] = field(default_factory=list)   # (path, 1-based line)


_lock = threading.Lock()
_OPS: "OrderedDict[str, _OpState]" = OrderedDict()
_observing = False


def _on_phase(op_id: str, phase_name: str) -> None:
    """An op that has ended has no more retries: release what it held."""
    try:
        from backend.core.ouroboros.governance.op_context import PHASE_TRANSITIONS, OperationPhase
        if not PHASE_TRANSITIONS.get(OperationPhase[phase_name]):
            forget(op_id)
    except Exception:  # noqa: BLE001
        pass


def _ensure_observer() -> None:
    global _observing
    if _observing:
        return
    try:
        from backend.core.ouroboros.governance.op_context import (
            register_phase_transition_observer,
        )
        register_phase_transition_observer(_on_phase)
        _observing = True
    except Exception:  # noqa: BLE001
        pass


def _state(op_id: str) -> _OpState:
    _ensure_observer()
    st = _OPS.get(op_id)
    if st is None:
        st = _OPS[op_id] = _OpState()
        while len(_OPS) > _env_int("JARVIS_OP_CONTEXT_CACHE_OPS", 64, 1):
            _OPS.popitem(last=False)
    _OPS.move_to_end(op_id)
    return st


def _index_stamp(repo_root: Path) -> int:
    """``.git/index``'s mtime: moves when anything lands. A worktree's
    ``.git`` is a file naming its gitdir."""
    try:
        git = repo_root / ".git"
        if git.is_file():
            m = re.match(r"gitdir:\s*(.+)", git.read_text(encoding="utf-8").strip())
            git = Path(m.group(1)) if m else git
            if not git.is_absolute():
                git = (repo_root / git).resolve()
        return (git / "index").stat().st_mtime_ns
    except OSError:
        return 0


def _files(repo_root: Path, arguments: Dict[str, Any]) -> Tuple[Tuple[str, int, int], ...]:
    out = []
    for value in (arguments or {}).values():
        if not isinstance(value, str) or not value or len(value) > 512:
            continue
        try:
            p = (repo_root / value).resolve()
            if p.is_file() and repo_root.resolve() in p.parents:
                st = p.stat()
                out.append((str(p), st.st_mtime_ns, st.st_size))
        except (OSError, ValueError):
            continue
    return tuple(sorted(out))


def _still_valid(entry: _Entry, state: _OpState, repo_root: Path) -> bool:
    if entry.epoch != state.epoch or entry.index_stamp != _index_stamp(repo_root):
        return False
    for path, mtime, size in entry.files:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if (st.st_mtime_ns, st.st_size) != (mtime, size):
            return False
    return True


def record(op_id: str, repo_root: Path, call: Any, result: Any, *, args_hash: str) -> None:
    """Keep a successful exploration result, or advance the op's mutation
    epoch for anything that may have changed the tree."""
    if not enabled() or not op_id:
        return
    try:
        name = str(getattr(call, "name", "") or "")
        ok = getattr(getattr(result, "status", None), "value", "success") == "success" \
            and not getattr(result, "error", None)
        with _lock:
            st = _state(op_id)
            if not _passive(name):
                st.epoch += 1          # an edit / write / shell command: trust nothing older
                st.entries.clear()
                return
            if not ok or not _cacheable(name):
                return
            st.entries[(name, args_hash)] = _Entry(
                call=call, result=result,
                files=_files(Path(repo_root), dict(getattr(call, "arguments", {}) or {})),
                epoch=st.epoch, index_stamp=_index_stamp(Path(repo_root)))
            st.entries.move_to_end((name, args_hash))
            while len(st.entries) > _env_int("JARVIS_OP_CONTEXT_CACHE_ENTRIES", 64, 1):
                st.entries.popitem(last=False)
    except Exception:  # noqa: BLE001
        logger.debug("[OpContextCache] record degraded", exc_info=True)


def note_mutation(op_id: str) -> None:
    """Something outside the tool loop changed the op's tree (an APPLY)."""
    with _lock:
        if op_id in _OPS:
            _OPS[op_id].epoch += 1
            _OPS[op_id].entries.clear()


_LINE_RX = re.compile(r"(?:file )?line (\d+)")


def pin_failed_hunk(op_id: str, path: str, rejection: str) -> bool:
    """Put the exact current source around a hunk that would not place at
    the FRONT of the op's next opening block. The rejection names the line
    (``_diagnose_hunk``: "file line N" / "near line N"). Returns whether a
    region was pinned."""
    if not enabled() or not op_id or not path:
        return False
    m = _LINE_RX.findall(str(rejection or ""))
    if not m:
        return False
    with _lock:
        st = _state(op_id)
        pin = (str(path), int(m[-1]))
        if pin not in st.pins:
            st.pins.append(pin)
    return True


def _pinned_region(repo_root: Path, path: str, line: int) -> str:
    half = _env_int("JARVIS_OP_CONTEXT_CACHE_PIN_LINES", 40, 4) // 2
    try:
        p = (repo_root / path).resolve()
        if repo_root.resolve() not in p.parents:
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lo, hi = max(1, line - half), min(len(lines), line + half)
    body = "\n".join(source_lines.number_lines(lines[lo - 1:hi], lo))
    return (f"[CURRENT SOURCE] {path} lines {lo}-{hi} (read now; your rejected hunk "
            f"was aimed here — copy context lines EXACTLY from this)\n{body}\n[END CURRENT SOURCE]\n")


def opening(op_id: str, repo_root: Path, *, budget_chars: int) -> Tuple[str, List[Tuple[Any, Any]]]:
    """The block a retry's tool loop opens with, and the (call, result)
    pairs it shows (credited to the exploration ledger by the caller).
    Empty when there is nothing valid. NEVER raises."""
    if not enabled() or not op_id or budget_chars <= 0:
        return "", []
    try:
        from backend.core.ouroboros.governance.tool_executor import _format_tool_result
        root = Path(repo_root)
        with _lock:
            st = _OPS.get(op_id)
            if st is None:
                return "", []
            for key in [k for k, e in st.entries.items() if not _still_valid(e, st, root)]:
                del st.entries[key]
            entries = list(reversed(st.entries.values()))      # newest first
            pins = list(st.pins)
        parts: List[str] = []
        used = 0
        for path, line in pins:
            region = _pinned_region(root, path, line)
            if region and used + len(region) <= budget_chars:
                parts.append(region)
                used += len(region)
        shown: List[Tuple[Any, Any]] = []
        omitted = 0
        for e in entries:
            text = _format_tool_result(e.call, e.result)
            if used + len(text) > budget_chars:
                omitted += 1
                continue
            parts.append(text)
            used += len(text)
            shown.append((e.call, e.result))
        if not parts:
            return "", []
        head = ("## Already gathered in this operation\n"
                "These results were read earlier in THIS operation and verified "
                "unchanged just now (file stamps, repository index, no edits since). "
                "They count as your exploration: do not re-read them. Call a tool "
                "only for what is NOT here.\n")
        if omitted:
            head += f"({omitted} older result(s) omitted for space; re-read those if needed.)\n"
        return head + "\n".join(parts) + "## End of already gathered\n\n", shown
    except Exception:  # noqa: BLE001
        logger.debug("[OpContextCache] opening degraded", exc_info=True)
        return "", []


def forget(op_id: str) -> None:
    with _lock:
        _OPS.pop(op_id, None)


def reset_for_tests() -> None:
    with _lock:
        _OPS.clear()
