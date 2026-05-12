"""§41.3 Slice 4 — ``/conversation`` REPL dispatcher.

Closes the three mechanical UX items the audit identified as
ready engineering (out of 5 Slice 4 candidates):

  * **Export** — serialize the live :class:`ConversationBridge`
    ring buffer to disk in JSONL or Markdown form. Composes
    :func:`conversation_bridge.get_default_bridge().snapshot`.
  * **Search** — keyword substring scan across the live ring,
    case-insensitive. (Semantic search over turns is a future
    arc — would require a dedicated turn-corpus embedder; the
    canonical :class:`semantic_index.SemanticIndex` maintains a
    project-state centroid, not arbitrary text. Reusing it
    here would be parallel state.)
  * **Bookmark** — persistent star-this-turn ledger at
    ``.jarvis/conversation/bookmarks.jsonl``. Bookmarks survive
    process death (the bridge does not). Identified by stable
    ``bk-N`` refs (sibling to t-N/d-N/o-N/n-N/p-N/q-N family).

Composition contract (operator-binding 2026-05-12):

  * NO parallel state for turn data — composes
    :func:`get_default_bridge` exclusively. The bridge stays
    authoritative for the live window.
  * Bookmarks use their own JSONL ledger (the bridge is in-
    memory only by design; bookmarks need persistence).
  * NO hardcoded triggers — every entry point is operator-
    initiated via the REPL.
  * NEVER raises — every dispatcher path returns a structured
    :class:`ConversationReplDispatchResult`.
  * Auto-discovered by :mod:`repl_dispatch_registry` via
    the §33.3 naming-cage (filename basename ``conversation_repl
    .py`` → verb ``conversation`` → dispatcher
    ``dispatch_conversation_command``).

Authority asymmetry (AST-pinned): stdlib + ``conversation_bridge``
+ ``semantic_index`` (lazy, optional) ONLY. NEVER imports
orchestrator / iron_gate / policy / candidate_generator /
tool_executor / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
providers.

READ-ONLY over the canonical bridge — no subcommand mutates the
turn buffer. Bookmark write is to the ledger, not the bridge.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CONVERSATION_REPL_SCHEMA_VERSION: str = "conversation_repl.1"

# Bookmark ref prefix — joins the canonical artifact-ref family
# (t-N / d-N / o-N / n-N / p-N / q-N → bk-N).
BOOKMARK_REF_PREFIX: str = "bk-"

_DEFAULT_RECENT_LIMIT: int = 20
_MAX_RECENT_LIMIT: int = 500
_DEFAULT_SEARCH_LIMIT: int = 20
_DEFAULT_BOOKMARK_LIMIT: int = 20
_DEFAULT_EXPORT_TURNS: int = 200
_MAX_EXPORT_TURNS: int = 5000


_HELP = (
    "/conversation — §41.3 Slice 4 UX mechanical surface\n"
    "\n"
    "Subcommands:\n"
    "  /conversation                       alias for "
    "/conversation recent\n"
    "  /conversation recent [N]            most-recent N "
    "live-bridge turns (default 20, max 500)\n"
    "  /conversation export [path] [-f jsonl|md]\n"
    "                                       serialize live "
    "bridge to file (default jsonl; md alt)\n"
    "  /conversation search <query>        case-insensitive "
    "substring scan over live bridge\n"
    "  /conversation bookmark <op_id>      bookmark all live "
    "bridge turns matching op_id\n"
    "  /conversation bookmarks [N]         list most-recent "
    "N bookmarks (default 20)\n"
    "  /conversation bookmark show <bk-N>  show full turns "
    "for one bookmark\n"
    "  /conversation resume <session_id>   rehydrate persisted "
    "turns into live bridge\n"
    "  /conversation sessions [N]          list persisted "
    "sessions (default 10, max 50)\n"
    "  /conversation save                  force-persist "
    "current bridge snapshot to ledger\n"
    "  /conversation stats                 bridge state + "
    "bookmark ledger size\n"
    "  /conversation help                  this text\n"
    "\n"
    "Bookmark ledger: .jarvis/conversation/bookmarks.jsonl "
    "(operator-tunable via JARVIS_CONVERSATION_BOOKMARKS_PATH)\n"
    "Session ledger:  .jarvis/conversation/sessions/ "
    "(operator-tunable via JARVIS_CONVERSATION_LEDGER_DIR)\n"
    "Master flag:     JARVIS_CONVERSATION_BRIDGE_ENABLED (the "
    "bridge's own master)\n"
    "Ledger flag:     JARVIS_CONVERSATION_LEDGER_ENABLED "
    "(persistence master)\n"
    "Cross-substrate: each bookmark carries a bk-N ref usable "
    "with /expand <bk-N>\n"
)


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def bookmarks_jsonl_path() -> Path:
    """Operator-tunable bookmark ledger path. Default
    ``.jarvis/conversation/bookmarks.jsonl``. Resolved at call
    time so tests can override via env."""
    raw = _env_str("JARVIS_CONVERSATION_BOOKMARKS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "conversation" / "bookmarks.jsonl"


def export_default_format() -> str:
    """``JARVIS_CONVERSATION_EXPORT_FORMAT`` — operator-default
    export format. Either ``"jsonl"`` (default) or ``"md"``.
    Per-call ``-f`` flag overrides. NEVER raises."""
    raw = _env_str(
        "JARVIS_CONVERSATION_EXPORT_FORMAT", "jsonl",
    ).lower()
    return raw if raw in ("jsonl", "md") else "jsonl"


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationReplDispatchResult:
    """Result of a ``/conversation`` dispatch. Frozen."""

    ok: bool
    text: str
    matched: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class Bookmark:
    """One persisted bookmark record. Frozen.

    Inlines the turn text + role + source + ts so the bookmark
    survives even after the bridge has rotated past the original
    turn (the bridge is bounded; bookmarks need to outlive it)."""

    ref: str
    """Stable bk-N identifier. Monotonic per ledger; never reused."""

    op_id: str
    """The op_id the operator asked to bookmark."""

    turns: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    """Snapshot of (role, text, ts, source) tuples from the
    bridge at bookmark time."""

    note: str = ""
    """Optional operator-supplied note (future enhancement —
    Slice 4 ships without input prompt)."""

    bookmarked_at_unix: float = field(default_factory=time.time)
    schema_version: str = field(
        default=CONVERSATION_REPL_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ref": self.ref,
            "op_id": self.op_id[:128],
            "turns": [
                {
                    "role": str(t.get("role", ""))[:32],
                    "text": str(t.get("text", ""))[:8192],
                    "ts": float(t.get("ts", 0.0)),
                    "source": str(t.get("source", ""))[:64],
                }
                for t in self.turns[:128]
            ],
            "note": str(self.note)[:512],
            "bookmarked_at_unix": float(
                self.bookmarked_at_unix,
            ),
        }

    @classmethod
    def from_dict(
        cls, raw: Dict[str, Any],
    ) -> Optional["Bookmark"]:
        """Defensive parse — returns None on missing required
        fields. NEVER raises."""
        try:
            ref = str(raw.get("ref", "")).strip()
            op_id = str(raw.get("op_id", "")).strip()
            if not ref or not op_id:
                return None
            turns_raw = raw.get("turns", ()) or ()
            turns = tuple(
                {
                    "role": str(t.get("role", ""))[:32],
                    "text": str(t.get("text", ""))[:8192],
                    "ts": float(t.get("ts", 0.0)),
                    "source": str(t.get("source", ""))[:64],
                }
                for t in turns_raw
                if isinstance(t, dict)
            )
            return cls(
                ref=ref,
                op_id=op_id,
                turns=turns,
                note=str(raw.get("note", "")),
                bookmarked_at_unix=float(
                    raw.get("bookmarked_at_unix", 0.0),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# BookmarkStore — minimal JSONL ledger (mirrors proposal_store)
# ---------------------------------------------------------------------------


# Per-process monotonic counter for bk-N ref allocation. Persists
# in-process only — on restart we scan the existing ledger and
# pick max(existing) + 1.
_bookmark_seq_lock = threading.Lock()
_bookmark_seq: Optional[int] = None


def _initialize_seq_from_ledger() -> int:
    """Walk existing ledger to find max bk-N then return N+1.
    NEVER raises — returns 1 on any failure."""
    try:
        path = bookmarks_jsonl_path()
        if not path.exists():
            return 1
        max_n = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ref = str(obj.get("ref", ""))
                    if ref.startswith(BOOKMARK_REF_PREFIX):
                        n = int(ref[len(BOOKMARK_REF_PREFIX):])
                        if n > max_n:
                            max_n = n
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        return max_n + 1
    except Exception:  # noqa: BLE001 — defensive
        return 1


def _next_bookmark_ref() -> str:
    """Allocate the next monotonic bk-N ref. Thread-safe.
    NEVER raises."""
    global _bookmark_seq
    with _bookmark_seq_lock:
        if _bookmark_seq is None:
            _bookmark_seq = _initialize_seq_from_ledger()
        ref = f"{BOOKMARK_REF_PREFIX}{_bookmark_seq}"
        _bookmark_seq += 1
        return ref


def reset_bookmark_seq_for_tests() -> None:
    """Test helper — clear the in-process seq counter."""
    global _bookmark_seq
    with _bookmark_seq_lock:
        _bookmark_seq = None


def append_bookmark(bookmark: Bookmark) -> bool:
    """Append one bookmark row to the JSONL ledger. NEVER raises."""
    try:
        path = bookmarks_jsonl_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            bookmark.to_dict(),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[conversation_repl] append_bookmark failed: %r",
            exc,
        )
        return False


def read_all_bookmarks(
    *, limit: int = 1000,
) -> Tuple[Bookmark, ...]:
    """Walk the JSONL ledger and return parsed bookmarks. NEVER
    raises. Order: insertion order (oldest → newest)."""
    out: List[Bookmark] = []
    try:
        path = bookmarks_jsonl_path()
        if not path.exists():
            return ()
        with path.open(
            "r", encoding="utf-8", errors="replace",
        ) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bm = Bookmark.from_dict(obj)
                if bm is not None:
                    out.append(bm)
                if len(out) >= max(1, int(limit)):
                    break
    except Exception:  # noqa: BLE001 — defensive
        return tuple(out)
    return tuple(out)


def find_bookmark_by_ref(ref: object) -> Optional[Bookmark]:
    """Lookup a single bookmark by its bk-N ref. NEVER raises."""
    if not isinstance(ref, str) or not ref:
        return None
    for bm in read_all_bookmarks(limit=10_000):
        if bm.ref == ref:
            return bm
    return None


# ---------------------------------------------------------------------------
# Master-flag gate
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    """Defers to canonical
    :func:`conversation_bridge._is_enabled`. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            _is_enabled,
        )
        return bool(_is_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Bridge snapshot helper
# ---------------------------------------------------------------------------


def _snapshot_bridge_turns(
    *, max_turns: Optional[int] = None,
) -> List[Any]:
    """Compose canonical
    :func:`get_default_bridge`.snapshot. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            get_default_bridge,
        )
        bridge = get_default_bridge()
        return bridge.snapshot(max_turns=max_turns)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[conversation_repl] snapshot failed: %r", exc,
        )
        return []


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: object) -> bool:
    """Defensive coercion + match check."""
    try:
        s = str(line or "").strip()
    except Exception:  # noqa: BLE001
        return False
    if not s:
        return False
    return (
        s == "/conversation"
        or s == "conversation"
        or s.startswith("/conversation ")
        or s.startswith("conversation ")
    )


def _parse_limit(
    args: List[str], *, default: int, ceiling: int,
    arg_index: int = 1,
) -> int:
    """Parse limit from a positional arg slot. Falls through to
    default on parse failure / out-of-bounds."""
    if len(args) <= arg_index:
        return default
    try:
        n = int(args[arg_index])
        if n < 1:
            return 1
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def dispatch_conversation_command(
    line: str,
) -> ConversationReplDispatchResult:
    """Parse a ``/conversation`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ConversationReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ConversationReplDispatchResult(
            ok=False,
            text=f"  /conversation parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "recent")

    if head in ("help", "?"):
        return ConversationReplDispatchResult(
            ok=True, text=_HELP,
        )

    # Stats works even when master is off so operators can see
    # the bridge's disabled state.
    if head == "stats":
        return _render_stats()

    # Bookmarks read works master-off (the ledger persists
    # independently). Bookmark WRITE requires master + bridge.
    if head == "bookmarks":
        return _render_bookmarks(
            _parse_limit(
                args,
                default=_DEFAULT_BOOKMARK_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )

    # "bookmark show <ref>" is read-only (ledger only). Route it
    # before the master gate. The bookmark WRITE path
    # ("bookmark <op_id>") still requires the bridge to be on.
    if (
        head == "bookmark"
        and len(args) >= 3
        and args[1].lower() == "show"
    ):
        return _render_bookmark_show(args[2])

    if not _master_enabled():
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation: bridge disabled — set "
                "JARVIS_CONVERSATION_BRIDGE_ENABLED=true "
                "(default-FALSE per §33.1; see "
                "/conversation help)"
            ),
        )

    if head == "recent":
        return _render_recent(
            _parse_limit(
                args,
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "export":
        return _render_export(args[1:])
    if head == "search":
        return _render_search(args[1:])
    if head == "bookmark":
        if len(args) < 2:
            return ConversationReplDispatchResult(
                ok=False,
                text=(
                    "  /conversation bookmark <op_id>: missing "
                    "op_id argument."
                ),
            )
        # Subcommand: bookmark show <bk-N>
        if (
            len(args) >= 3
            and args[1].lower() == "show"
        ):
            return _render_bookmark_show(args[2])
        return _render_bookmark(args[1])
    if head == "resume":
        if len(args) < 2:
            return ConversationReplDispatchResult(
                ok=False,
                text=(
                    "  /conversation resume <session_id>: "
                    "missing session_id argument."
                ),
            )
        return _render_resume(args[1])
    if head == "sessions":
        return _render_sessions(
            _parse_limit(
                args,
                default=10,
                ceiling=50,
            ),
        )
    if head == "save":
        return _render_save()
    return ConversationReplDispatchResult(
        ok=False,
        text=(
            f"  /conversation: unknown subcommand {head!r}. "
            f"Try /conversation help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_turn_one_line(turn: Any) -> str:
    """One-line rendering. Duck-typed attr access."""
    role = (
        getattr(turn, "role", "?") or "?"
    )
    role_str = str(role)[:9]
    source = str(getattr(turn, "source", "?") or "?")[:20]
    op_id = str(getattr(turn, "op_id", "") or "")[:18]
    text = str(getattr(turn, "text", "") or "").replace("\n", " ")
    if len(text) > 72:
        text = text[:69] + "..."
    return (
        f"  [{role_str:<9}] {source:<20} op={op_id:<18} "
        f"{text}"
    )


def _format_turn_markdown(turn: Any) -> str:
    """Markdown rendering for export — preserves multi-line."""
    role = str(getattr(turn, "role", "?") or "?")
    source = str(getattr(turn, "source", "?") or "?")
    text = str(getattr(turn, "text", "") or "")
    try:
        ts = float(getattr(turn, "ts", 0.0) or 0.0)
        ts_str = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts),
        )
    except Exception:  # noqa: BLE001
        ts_str = "?"
    op_id = str(getattr(turn, "op_id", "") or "")
    op_suffix = f" (op={op_id})" if op_id else ""
    return (
        f"### {role} — {source} @ {ts_str}{op_suffix}\n\n"
        f"{text}\n"
    )


def _format_turn_jsonl(turn: Any) -> Optional[str]:
    """JSONL rendering — single line per turn."""
    try:
        obj = {
            "role": str(getattr(turn, "role", "")),
            "text": str(getattr(turn, "text", "")),
            "ts": float(getattr(turn, "ts", 0.0) or 0.0),
            "source": str(getattr(turn, "source", "")),
            "op_id": str(getattr(turn, "op_id", "")),
        }
        return json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — defensive
        return None


def _render_recent(
    limit: int,
) -> ConversationReplDispatchResult:
    turns = _snapshot_bridge_turns(max_turns=limit)
    if not turns:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                "  /conversation: live bridge is empty "
                "(or no turns since master flag flipped on)."
            ),
        )
    lines = [
        f"  /conversation recent (last {len(turns)}):",
    ]
    for t in turns:
        lines.append(_format_turn_one_line(t))
    return ConversationReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_export(
    sub_args: List[str],
) -> ConversationReplDispatchResult:
    """Parse export args: optional positional path + ``-f
    <jsonl|md>`` format flag."""
    fmt = export_default_format()
    target_path: Optional[str] = None
    i = 0
    while i < len(sub_args):
        arg = sub_args[i]
        if arg in ("-f", "--format"):
            if i + 1 < len(sub_args):
                fmt = sub_args[i + 1].lower()
                i += 2
                continue
            return ConversationReplDispatchResult(
                ok=False,
                text=(
                    "  /conversation export: -f requires a "
                    "format value (jsonl|md)"
                ),
            )
        if target_path is None and not arg.startswith("-"):
            target_path = arg
        i += 1

    if fmt not in ("jsonl", "md"):
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                f"  /conversation export: unknown format "
                f"{fmt!r}. Try jsonl or md."
            ),
        )

    turns = _snapshot_bridge_turns(max_turns=_MAX_EXPORT_TURNS)
    if not turns:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                "  /conversation export: live bridge is empty; "
                "nothing to write."
            ),
        )

    if target_path is None:
        ts_token = time.strftime(
            "%Y%m%d-%H%M%S", time.gmtime(),
        )
        target_path = (
            f".jarvis/conversation/export-{ts_token}.{fmt}"
        )

    try:
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with path.open("w", encoding="utf-8") as fh:
            if fmt == "jsonl":
                for t in turns:
                    line = _format_turn_jsonl(t)
                    if line is not None:
                        fh.write(line + "\n")
                        written += 1
            else:
                fh.write(
                    f"# Conversation Export — "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
                    f" ({len(turns)} turns)\n\n"
                )
                for t in turns:
                    fh.write(_format_turn_markdown(t))
                    fh.write("\n")
                    written += 1
    except OSError as exc:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                f"  /conversation export: write failed: {exc}"
            ),
        )
    return ConversationReplDispatchResult(
        ok=True,
        text=(
            f"  /conversation export: wrote {written} turn(s) "
            f"to {target_path} ({fmt})"
        ),
    )


def _render_search(
    sub_args: List[str],
) -> ConversationReplDispatchResult:
    if not sub_args:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation search <query>: missing query "
                "argument."
            ),
        )
    query = " ".join(sub_args).strip()
    if not query:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation search: empty query."
            ),
        )

    turns = _snapshot_bridge_turns(max_turns=_MAX_RECENT_LIMIT)
    if not turns:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                "  /conversation search: live bridge is empty."
            ),
        )

    return _render_search_keyword(query, turns)


def _render_search_keyword(
    query: str, turns: List[Any],
) -> ConversationReplDispatchResult:
    """Case-insensitive substring search. NEVER raises."""
    needle = query.lower()
    matches: List[Tuple[Any, int]] = []
    for t in turns:
        try:
            text = str(getattr(t, "text", "") or "").lower()
        except Exception:  # noqa: BLE001
            continue
        idx = text.find(needle)
        if idx >= 0:
            matches.append((t, idx))
    if not matches:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                f"  /conversation search {query!r}: no matches "
                f"in {len(turns)} turn(s)."
            ),
        )
    lines = [
        f"  /conversation search {query!r} — {len(matches)} "
        f"match(es) in {len(turns)} turn(s):",
    ]
    for t, _idx in matches[:_DEFAULT_SEARCH_LIMIT]:
        lines.append(_format_turn_one_line(t))
    if len(matches) > _DEFAULT_SEARCH_LIMIT:
        lines.append(
            f"    ... ({len(matches) - _DEFAULT_SEARCH_LIMIT} "
            f"more matches)"
        )
    return ConversationReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_bookmark(op_id: str) -> ConversationReplDispatchResult:
    """Bookmark all live-bridge turns matching ``op_id``."""
    op_id_clean = (op_id or "").strip()
    if not op_id_clean:
        return ConversationReplDispatchResult(
            ok=False,
            text="  /conversation bookmark: empty op_id",
        )
    turns = _snapshot_bridge_turns(max_turns=_MAX_RECENT_LIMIT)
    matching: List[Dict[str, Any]] = []
    for t in turns:
        try:
            if getattr(t, "op_id", "") == op_id_clean:
                matching.append({
                    "role": str(getattr(t, "role", "")),
                    "text": str(getattr(t, "text", "")),
                    "ts": float(
                        getattr(t, "ts", 0.0) or 0.0,
                    ),
                    "source": str(getattr(t, "source", "")),
                })
        except Exception:  # noqa: BLE001
            continue
    if not matching:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                f"  /conversation bookmark: no live-bridge "
                f"turns found for op_id={op_id_clean!r}"
            ),
        )
    bm = Bookmark(
        ref=_next_bookmark_ref(),
        op_id=op_id_clean,
        turns=tuple(matching),
    )
    if not append_bookmark(bm):
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                f"  /conversation bookmark: ledger append "
                f"failed (check write permissions on "
                f"{bookmarks_jsonl_path()})"
            ),
        )
    return ConversationReplDispatchResult(
        ok=True,
        text=(
            f"  /conversation bookmark: saved {len(matching)} "
            f"turn(s) for op={op_id_clean!r} as "
            f"ref={bm.ref}. /expand {bm.ref} to view."
        ),
    )


def _render_bookmarks(
    limit: int,
) -> ConversationReplDispatchResult:
    bookmarks = read_all_bookmarks(limit=10_000)
    if not bookmarks:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                "  /conversation bookmarks: ledger is empty.\n"
                "  hint: ledger lives at "
                f"{bookmarks_jsonl_path()}"
            ),
        )
    # Newest first.
    recent = list(reversed(bookmarks))[:limit]
    lines = [
        f"  /conversation bookmarks (most-recent "
        f"{len(recent)}):",
    ]
    for bm in recent:
        try:
            ts = time.strftime(
                "%Y-%m-%d %H:%M",
                time.gmtime(bm.bookmarked_at_unix),
            )
        except Exception:  # noqa: BLE001
            ts = "?"
        lines.append(
            f"  {bm.ref:<8}  op={bm.op_id[:24]:<24}  "
            f"turns={len(bm.turns):<3}  @ {ts}"
        )
    return ConversationReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_bookmark_show(
    ref: str,
) -> ConversationReplDispatchResult:
    ref_clean = (ref or "").strip()
    bm = find_bookmark_by_ref(ref_clean)
    if bm is None:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                f"  /conversation bookmark show {ref_clean!r}: "
                f"not found in ledger."
            ),
        )
    try:
        ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(bm.bookmarked_at_unix),
        )
    except Exception:  # noqa: BLE001
        ts = "?"
    lines = [
        f"  /conversation bookmark show {bm.ref}:",
        f"    op_id:          {bm.op_id}",
        f"    bookmarked_at:  {ts}",
        f"    turn_count:     {len(bm.turns)}",
        "",
    ]
    for i, t in enumerate(bm.turns, start=1):
        role = str(t.get("role", "?"))
        source = str(t.get("source", "?"))
        text = str(t.get("text", ""))
        try:
            tts = time.strftime(
                "%H:%M:%S",
                time.gmtime(float(t.get("ts", 0.0) or 0.0)),
            )
        except Exception:  # noqa: BLE001
            tts = "?"
        lines.append(
            f"  [{i}] {role}/{source} @ {tts}"
        )
        for line in text.splitlines() or [""]:
            lines.append(f"      {line}")
        lines.append("")
    return ConversationReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_stats() -> ConversationReplDispatchResult:
    """Stats works master-off so operators see the disabled state."""
    bridge_enabled = _master_enabled()
    turn_count = 0
    if bridge_enabled:
        try:
            turn_count = len(_snapshot_bridge_turns())
        except Exception:  # noqa: BLE001
            turn_count = 0
    bookmark_count = 0
    try:
        bookmark_count = len(read_all_bookmarks(limit=100_000))
    except Exception:  # noqa: BLE001
        bookmark_count = 0
    ledger_flag = False
    session_count = 0
    try:
        from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
            ledger_enabled as _ledger_on,
            list_sessions as _ls,
            ledger_dir as _ld,
        )
        ledger_flag = _ledger_on()
        if ledger_flag:
            session_count = len(_ls(limit=10_000))
    except Exception:  # noqa: BLE001
        pass
    lines = [
        "  /conversation stats:",
        f"    bridge_enabled:   {bridge_enabled}",
        f"    live_turn_count:  {turn_count}",
        f"    bookmark_count:   {bookmark_count}",
        f"    bookmarks_path:   {bookmarks_jsonl_path()}",
        f"    export_format:    {export_default_format()}",
        f"    ledger_enabled:   {ledger_flag}",
        f"    session_count:    {session_count}",
    ]
    return ConversationReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Resume / Sessions / Save renderers (Arc #1)
# ---------------------------------------------------------------------------


def _render_resume(
    session_id: str,
) -> ConversationReplDispatchResult:
    """Rehydrate persisted turns into the live bridge.

    Replay is NOT trust bypass: every turn is re-sanitized through
    the canonical Tier -1 ``sanitize_for_log`` + ``redact_secrets``
    before entering the bridge ring buffer.
    """
    sid = (session_id or "").strip()
    if not sid:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation resume: empty session_id"
            ),
        )
    try:
        from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
            ledger_enabled,
            read_tail,
            replay_tail_default,
            session_exists,
        )
    except ImportError:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation resume: conversation_ledger "
                "module not available."
            ),
        )
    if not ledger_enabled():
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation resume: ledger disabled — set "
                "JARVIS_CONVERSATION_LEDGER_ENABLED=true"
            ),
        )
    if not session_exists(sid):
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                f"  /conversation resume: session {sid!r} "
                f"not found in ledger."
            ),
        )
    tail = read_tail(sid, max_turns=replay_tail_default())
    if not tail:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                f"  /conversation resume: session {sid!r} "
                f"exists but contains no parseable turns."
            ),
        )

    # Re-sanitize + re-redact — replay is NOT trust bypass.
    from backend.core.secure_logging import sanitize_for_log
    from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
        _max_chars_per_turn,
        get_default_bridge,
        redact_secrets,
    )
    bridge = get_default_bridge()
    per_turn_cap = _max_chars_per_turn()
    injected = 0
    for pt in tail:
        sanitized = sanitize_for_log(
            pt.text, max_len=per_turn_cap,
        )
        if not sanitized:
            continue
        sanitized, _ = redact_secrets(sanitized)
        bridge.record_turn(
            role=pt.role,
            text=sanitized,
            source=pt.source or "tui_user",
            op_id=pt.op_id,
        )
        injected += 1
    return ConversationReplDispatchResult(
        ok=True,
        text=(
            f"  /conversation resume: rehydrated {injected} "
            f"turn(s) from session {sid!r} into live bridge."
        ),
    )


def _render_sessions(
    limit: int,
) -> ConversationReplDispatchResult:
    """List persisted sessions from the ledger directory."""
    try:
        from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
            ledger_enabled,
            list_sessions,
        )
    except ImportError:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation sessions: conversation_ledger "
                "module not available."
            ),
        )
    if not ledger_enabled():
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation sessions: ledger disabled — set "
                "JARVIS_CONVERSATION_LEDGER_ENABLED=true"
            ),
        )
    sessions = list_sessions(limit=limit)
    if not sessions:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                "  /conversation sessions: no persisted "
                "sessions found."
            ),
        )
    lines = [
        f"  /conversation sessions (most-recent "
        f"{len(sessions)}):",
    ]
    for s in sessions:
        try:
            ts = time.strftime(
                "%Y-%m-%d %H:%M",
                time.gmtime(s.last_ts),
            )
        except Exception:  # noqa: BLE001
            ts = "?"
        size_kb = s.file_size_bytes / 1024
        lines.append(
            f"  {s.session_id[:24]:<24}  "
            f"turns={s.turn_count:<4}  "
            f"size={size_kb:.1f}KB  @ {ts}"
        )
    return ConversationReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_save() -> ConversationReplDispatchResult:
    """Force-persist current bridge snapshot to the ledger."""
    try:
        from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
            append_turn,
            ledger_enabled,
        )
    except ImportError:
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation save: conversation_ledger "
                "module not available."
            ),
        )
    if not ledger_enabled():
        return ConversationReplDispatchResult(
            ok=False,
            text=(
                "  /conversation save: ledger disabled — set "
                "JARVIS_CONVERSATION_LEDGER_ENABLED=true"
            ),
        )
    turns = _snapshot_bridge_turns(
        max_turns=_MAX_EXPORT_TURNS,
    )
    if not turns:
        return ConversationReplDispatchResult(
            ok=True,
            text=(
                "  /conversation save: bridge is empty; "
                "nothing to persist."
            ),
        )
    # Resolve session_id.
    try:
        from backend.core.ouroboros.governance.conversation_ledger_observer import (  # noqa: E501
            _PROCESS_EPOCH_SESSION_ID,
        )
        from backend.core.ouroboros.governance.session_manager import (  # noqa: E501
            get_session_manager,
        )
        mgr = get_session_manager()
        active = mgr.list_active()
        sid = (
            active[0].session_id
            if active
            else _PROCESS_EPOCH_SESSION_ID
        )
    except Exception:  # noqa: BLE001
        sid = "manual-save"
    written = 0
    for t in turns:
        ok = append_turn(
            sid,
            role=str(getattr(t, "role", "")),
            text=str(getattr(t, "text", "")),
            source=str(getattr(t, "source", "")),
            op_id=str(getattr(t, "op_id", "")),
            ts=float(getattr(t, "ts", 0.0) or 0.0),
        )
        if ok:
            written += 1
    return ConversationReplDispatchResult(
        ok=True,
        text=(
            f"  /conversation save: persisted {written} "
            f"turn(s) to session {sid!r}."
        ),
    )


# ===========================================================================
# §33.1 — register_shipped_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Conversation REPL substrate invariants."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/conversation_repl.py"
    )

    _FORBIDDEN_IMPORT_MODULES = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.auto_committer",
        "backend.core.ouroboros.governance.risk_tier_floor",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.providers",
    )

    def _validate_dispatcher_signature(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        saw = False
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                if node.name == "dispatch_conversation_command":
                    saw = True
        return () if saw else (
            "module-level dispatch_conversation_command "
            "callable missing — §33.3 naming-cage hook broken",
        )

    def _validate_authority_asymmetry(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in _FORBIDDEN_IMPORT_MODULES:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden import {mod!r}"
                    )
        return tuple(violations)

    def _validate_composes_canonical(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for needle in (
            "conversation_bridge",
            "get_default_bridge",
            "BOOKMARK_REF_PREFIX",
        ):
            if needle not in source:
                violations.append(
                    f"must compose canonical {needle!r}"
                )
        return tuple(violations)

    def _validate_ref_prefix_pinned(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """bk-N prefix bytes-pinned for cross-substrate ref family
        compatibility."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.AnnAssign)
                and isinstance(node.target, _ast.Name)
                and node.target.id == "BOOKMARK_REF_PREFIX"
                and isinstance(node.value, _ast.Constant)
                and node.value.value == "bk-"
            ):
                return ()
        return (
            "BOOKMARK_REF_PREFIX must equal 'bk-' to slot into "
            "the t-N/d-N/o-N/n-N/p-N/q-N artifact-ref family",
        )

    def _validate_resume_resanitizes(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Resume path MUST call sanitize_for_log AND
        redact_secrets to enforce replay-is-not-trust-bypass."""
        violations: list = []
        if "sanitize_for_log" not in source:
            violations.append(
                "resume path must call sanitize_for_log "
                "(replay is not trust bypass)"
            )
        if "redact_secrets" not in source:
            violations.append(
                "resume path must call redact_secrets "
                "(replay is not trust bypass)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="conversation_repl_substrate",
            target_file=target,
            description=(
                "§33.3 naming-cage dispatcher present + "
                "authority-asymmetry + composes canonical "
                "conversation_bridge."
            ),
            validate=_validate_dispatcher_signature,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "conversation_repl_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "REPL surface MUST NOT import orchestrator / "
                "iron_gate / policy / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "conversation_repl_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes canonical conversation_bridge + "
                "get_default_bridge; bookmark ref prefix "
                "joins t-N/d-N/o-N/n-N/p-N/q-N family."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name="conversation_repl_ref_prefix_pinned",
            target_file=target,
            description=(
                "BOOKMARK_REF_PREFIX = 'bk-' bytes-pinned."
            ),
            validate=_validate_ref_prefix_pinned,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "conversation_resume_resanitizes"
            ),
            target_file=target,
            description=(
                "Resume path MUST call sanitize_for_log + "
                "redact_secrets — replay is not trust bypass."
            ),
            validate=_validate_resume_resanitizes,
        ),
    ]


__all__ = [
    "BOOKMARK_REF_PREFIX",
    "Bookmark",
    "CONVERSATION_REPL_SCHEMA_VERSION",
    "ConversationReplDispatchResult",
    "append_bookmark",
    "bookmarks_jsonl_path",
    "dispatch_conversation_command",
    "export_default_format",
    "find_bookmark_by_ref",
    "read_all_bookmarks",
    "register_shipped_invariants",
    "reset_bookmark_seq_for_tests",
]
