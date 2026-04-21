"""
SessionIndex + SessionBrowser + /session REPL — Slices 2/3/4 bundled.
======================================================================

Three tightly-coupled primitives for the operator session history
browser:

* :class:`SessionIndex` — filesystem scan of the sessions root,
  caching of :class:`SessionRecord` entries, mtime-based freshness,
  and filtering.
* :class:`SessionBrowser` — high-level navigation: ``list``,
  ``show``, ``recent``, ``bookmark`` / ``unbookmark`` with JSON
  persistence, replay-HTML resolution.
* :func:`dispatch_session_command` — ``/session`` REPL dispatcher
  covering list / show / recent / bookmark / replay / help.

Manifesto alignment
-------------------

* §1 read-only — this module NEVER writes to the session directories.
  Bookmarks live in a SEPARATE JSON file the operator owns.
* §4 Privacy — we echo only :meth:`SessionRecord.project` output and
  :meth:`SessionRecord.one_line_summary`; never raw debug.log bodies.
* §7 fail-closed — corrupt sessions still appear in the index with
  a ``PARSE-ERROR`` marker. Failed bookmark I/O logs a warning and
  keeps the in-memory bookmark set intact.
* §8 observable — the index + browser both expose listener hooks
  (new-record / bookmarked / unbookmarked / rescan-complete).
"""
from __future__ import annotations

import enum
import json
import logging
import os
import re
import shlex
import textwrap
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.session_record import (
    SessionRecord,
    default_sessions_root,
    parse_session_dir,
)

logger = logging.getLogger("Ouroboros.SessionBrowser")


SESSION_BROWSER_SCHEMA_VERSION: str = "session_browser.v1"


_SESSION_ID_RX = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionBrowserError(Exception):
    """Base for browser-level errors."""


# ===========================================================================
# SessionIndex
# ===========================================================================


@dataclass
class _IndexEntry:
    record: SessionRecord
    scanned_at_ts: float


class SessionIndex:
    """Per-process cache of :class:`SessionRecord` entries.

    ``scan`` walks the sessions root and refreshes records whose on-disk
    mtime has advanced since the last scan. Idempotent — calling
    ``scan()`` repeatedly is cheap.

    The index does NOT pre-load eagerly; callers call ``scan`` (or a
    convenience method that implies scan) when they want fresh data.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        debug_log_head_lines: int = 3,
    ) -> None:
        self._root = Path(root) if root is not None else default_sessions_root()
        self._debug_log_head_lines = max(0, debug_log_head_lines)
        self._lock = threading.Lock()
        self._entries: Dict[str, _IndexEntry] = {}
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    @property
    def root(self) -> Path:
        return self._root

    # --- scanning -------------------------------------------------------

    def scan(self, *, force: bool = False) -> List[SessionRecord]:
        """Walk the root; return the list of all current records.

        When ``force=True`` every record is re-parsed regardless of
        mtime. Otherwise records only re-parse when their directory's
        mtime is newer than the last cache entry.
        """
        if not self._root.exists():
            logger.debug(
                "[SessionIndex] root does not exist yet: %s", self._root,
            )
            return []
        try:
            children = [p for p in self._root.iterdir() if p.is_dir()]
        except OSError as exc:
            logger.warning(
                "[SessionIndex] root iter error: %s", exc,
            )
            return []
        now = time.time()
        updated: List[SessionRecord] = []
        with self._lock:
            seen_ids = set()
            for child in children:
                session_id = child.name
                if not _SESSION_ID_RX.match(session_id):
                    continue
                seen_ids.add(session_id)
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    mtime = 0.0
                existing = self._entries.get(session_id)
                needs_parse = (
                    force
                    or existing is None
                    or existing.record.mtime_ts != mtime
                )
                if not needs_parse and existing is not None:
                    continue
                record = parse_session_dir(
                    child,
                    debug_log_head_lines=self._debug_log_head_lines,
                )
                self._entries[session_id] = _IndexEntry(
                    record=record, scanned_at_ts=now,
                )
                updated.append(record)
            # Evict entries whose directory disappeared
            for missing in list(self._entries.keys()):
                if missing not in seen_ids:
                    self._entries.pop(missing, None)
            # Emit listener events
            summary = {
                "event_type": "session_rescan_complete",
                "new_or_updated": [r.session_id for r in updated],
                "total_records": len(self._entries),
                "scanned_at_ts": now,
            }
            for l in list(self._listeners):
                try:
                    l(summary)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[SessionIndex] listener raise: %s", exc,
                    )
            for record in updated:
                payload = {
                    "event_type": "session_record_added",
                    "session_id": record.session_id,
                    "projection": record.project(),
                }
                for l in list(self._listeners):
                    try:
                        l(payload)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[SessionIndex] listener raise on "
                            "session_record_added: %s", exc,
                        )
        logger.info(
            "[SessionIndex] scan root=%s updated=%d total=%d",
            self._root, len(updated), len(self._entries),
        )
        return self.all_records()

    # --- queries --------------------------------------------------------

    def get(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            entry = self._entries.get(session_id)
            return entry.record if entry is not None else None

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._entries

    def all_records(self) -> List[SessionRecord]:
        with self._lock:
            entries = list(self._entries.values())
        # Sort by mtime descending — most recent first
        entries.sort(
            key=lambda e: e.record.mtime_ts or 0.0, reverse=True,
        )
        return [e.record for e in entries]

    def filter(
        self,
        *,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        stop_reason: Optional[str] = None,
        ok_outcome: Optional[bool] = None,
        min_ops: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        session_id_prefix: Optional[str] = None,
        has_replay: Optional[bool] = None,
        parse_error: Optional[bool] = None,
    ) -> List[SessionRecord]:
        """Return records matching every supplied predicate.

        Predicates left as ``None`` are ignored. Returned list is sorted
        most-recent-first (by mtime).
        """
        def _matches(r: SessionRecord) -> bool:
            if since_ts is not None and r.mtime_ts < since_ts:
                return False
            if until_ts is not None and r.mtime_ts > until_ts:
                return False
            if stop_reason is not None and r.stop_reason != stop_reason:
                return False
            if ok_outcome is not None and r.ok_outcome != ok_outcome:
                return False
            if min_ops is not None and r.ops_total < min_ops:
                return False
            if max_cost_usd is not None and r.cost_spent_usd > max_cost_usd:
                return False
            if session_id_prefix is not None and not r.session_id.startswith(
                session_id_prefix,
            ):
                return False
            if has_replay is not None and r.has_replay_html != has_replay:
                return False
            if parse_error is not None and r.parse_error != parse_error:
                return False
            return True

        return [r for r in self.all_records() if _matches(r)]

    def recent(self, *, limit: int = 10) -> List[SessionRecord]:
        limit = max(1, int(limit))
        return self.all_records()[:limit]

    # --- listener hooks -------------------------------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._listeners.clear()


# ===========================================================================
# BookmarkStore — JSON-backed, operator-owned
# ===========================================================================


_BOOKMARK_FILENAME = "session_bookmarks.json"


@dataclass(frozen=True)
class Bookmark:
    session_id: str
    note: str = ""
    created_at_iso: str = ""


class BookmarkStore:
    """Per-operator bookmark set with JSON persistence.

    Bookmarks live in ``<bookmark_root>/session_bookmarks.json`` — a
    separate file from the session directories, so the browser never
    touches the original run data.
    """

    def __init__(
        self,
        *,
        bookmark_root: Optional[Path] = None,
    ) -> None:
        base = bookmark_root or self._default_bookmark_root()
        self._path = Path(base) / _BOOKMARK_FILENAME
        self._lock = threading.Lock()
        self._bookmarks: Dict[str, Bookmark] = {}
        self._load()

    # --- persistence ----------------------------------------------------

    def _default_bookmark_root(self) -> Path:
        env = os.environ.get("JARVIS_SESSION_BOOKMARK_ROOT")
        if env:
            return Path(env).expanduser().resolve()
        return Path(".ouroboros").resolve()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[BookmarkStore] load failed %s: %s — starting empty",
                self._path, exc,
            )
            return
        if not isinstance(data, list):
            logger.warning(
                "[BookmarkStore] %s is not a list — starting empty",
                self._path,
            )
            return
        for item in data:
            if not isinstance(item, Mapping):
                continue
            sid = str(item.get("session_id", "") or "")
            if not sid or not _SESSION_ID_RX.match(sid):
                continue
            bm = Bookmark(
                session_id=sid,
                note=str(item.get("note", "") or "")[:500],
                created_at_iso=str(item.get("created_at_iso", "") or ""),
            )
            self._bookmarks[sid] = bm

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "session_id": bm.session_id,
                    "note": bm.note,
                    "created_at_iso": bm.created_at_iso,
                }
                for bm in self._bookmarks.values()
            ]
            self._path.write_text(
                json.dumps(data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "[BookmarkStore] persist failed %s: %s",
                self._path, exc,
            )

    # --- public API -----------------------------------------------------

    def add(self, session_id: str, *, note: str = "") -> Bookmark:
        if not _SESSION_ID_RX.match(session_id):
            raise SessionBrowserError(
                f"invalid session id: {session_id!r}"
            )
        iso = datetime.now(timezone.utc).replace(
            microsecond=0,
        ).isoformat()
        bm = Bookmark(
            session_id=session_id,
            note=(note or "").strip()[:500],
            created_at_iso=iso,
        )
        with self._lock:
            self._bookmarks[session_id] = bm
            self._persist()
        return bm

    def remove(self, session_id: str) -> bool:
        with self._lock:
            existed = session_id in self._bookmarks
            self._bookmarks.pop(session_id, None)
            if existed:
                self._persist()
        return existed

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._bookmarks

    def list_all(self) -> List[Bookmark]:
        with self._lock:
            return sorted(
                self._bookmarks.values(),
                key=lambda bm: bm.created_at_iso, reverse=True,
            )

    def reset(self) -> None:
        with self._lock:
            self._bookmarks.clear()
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass

    @property
    def path(self) -> Path:
        return self._path


# ===========================================================================
# SessionBrowser — high-level navigation
# ===========================================================================


class SessionBrowser:
    """Glueing :class:`SessionIndex` + :class:`BookmarkStore`.

    Provides the operator-facing verbs the REPL dispatcher calls.
    """

    def __init__(
        self,
        *,
        index: Optional[SessionIndex] = None,
        bookmarks: Optional[BookmarkStore] = None,
    ) -> None:
        self._index = index or SessionIndex()
        self._bookmarks = bookmarks or BookmarkStore()

    @property
    def index(self) -> SessionIndex:
        return self._index

    @property
    def bookmarks(self) -> BookmarkStore:
        return self._bookmarks

    # --- list / show / recent -------------------------------------------

    def list_records(
        self,
        *,
        limit: Optional[int] = None,
        filters: Optional[Mapping[str, Any]] = None,
        rescan: bool = True,
    ) -> List[SessionRecord]:
        if rescan:
            self._index.scan()
        if filters:
            records = self._index.filter(**dict(filters))
        else:
            records = self._index.all_records()
        if limit is not None:
            records = records[: max(1, int(limit))]
        return records

    def show(
        self, session_id: str, *, rescan: bool = True,
    ) -> Optional[SessionRecord]:
        if rescan:
            self._index.scan()
        return self._index.get(session_id)

    def recent(self, *, limit: int = 10, rescan: bool = True) -> List[SessionRecord]:
        if rescan:
            self._index.scan()
        return self._index.recent(limit=limit)

    # --- bookmarks ------------------------------------------------------

    def bookmark(
        self, session_id: str, *, note: str = "",
    ) -> Bookmark:
        return self._bookmarks.add(session_id, note=note)

    def unbookmark(self, session_id: str) -> bool:
        return self._bookmarks.remove(session_id)

    def list_bookmarks_with_records(
        self, *, rescan: bool = True,
    ) -> List[Tuple[Bookmark, Optional[SessionRecord]]]:
        if rescan:
            self._index.scan()
        return [
            (bm, self._index.get(bm.session_id))
            for bm in self._bookmarks.list_all()
        ]

    # --- replay resolution ---------------------------------------------

    def replay_html_path(self, session_id: str) -> Optional[Path]:
        record = self._index.get(session_id)
        if record is None or not record.has_replay_html:
            return None
        return Path(record.replay_html_path)


# ===========================================================================
# Module singletons
# ===========================================================================


_default_index: Optional[SessionIndex] = None
_default_bookmarks: Optional[BookmarkStore] = None
_default_browser: Optional[SessionBrowser] = None
_singleton_lock = threading.Lock()


def get_default_session_index() -> SessionIndex:
    global _default_index
    with _singleton_lock:
        if _default_index is None:
            _default_index = SessionIndex()
        return _default_index


def get_default_bookmark_store() -> BookmarkStore:
    global _default_bookmarks
    with _singleton_lock:
        if _default_bookmarks is None:
            _default_bookmarks = BookmarkStore()
        return _default_bookmarks


def get_default_session_browser() -> SessionBrowser:
    global _default_browser
    with _singleton_lock:
        if _default_browser is None:
            _default_browser = SessionBrowser(
                index=get_default_session_index(),
                bookmarks=get_default_bookmark_store(),
            )
        return _default_browser


def reset_default_session_singletons() -> None:
    global _default_index, _default_bookmarks, _default_browser
    with _singleton_lock:
        if _default_index is not None:
            _default_index.reset()
        if _default_bookmarks is not None:
            _default_bookmarks.reset()
        _default_index = None
        _default_bookmarks = None
        _default_browser = None


def set_default_session_browser(browser: SessionBrowser) -> None:
    """Install a caller-built browser as the default.

    Used by tests + production wiring where the sessions root / bookmark
    root need to be configured before the singletons are first accessed.
    """
    global _default_browser, _default_index, _default_bookmarks
    with _singleton_lock:
        _default_browser = browser
        _default_index = browser.index
        _default_bookmarks = browser.bookmarks


# ===========================================================================
# /session REPL dispatcher
# ===========================================================================


@dataclass
class SessionDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_SESSION_HELP = textwrap.dedent(
    """
    Session history browser
    -----------------------
      /session                         — recent sessions (default 10)
      /session list [--limit N] [--ok] [--bad] [--has-replay]
                                       — full list with filters
      /session show <session-id>       — detail for one session
      /session recent [N]              — N most recent sessions
      /session bookmark <session-id> [note...]
                                       — bookmark a session
      /session unbookmark <session-id> — remove a bookmark
      /session bookmarks               — list bookmarked sessions
      /session replay <session-id>     — print replay.html path (if any)
      /session rescan                  — force re-scan of sessions root
      /session help                    — this text
    """
).strip()


_COMMANDS = frozenset({"/session"})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_session_command(
    line: str,
    *,
    browser: Optional[SessionBrowser] = None,
) -> SessionDispatchResult:
    if not _matches(line):
        return SessionDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return SessionDispatchResult(
            ok=False, text=f"  /session parse error: {exc}",
        )
    if not tokens:
        return SessionDispatchResult(ok=False, text="", matched=False)
    b = browser or get_default_session_browser()
    args = tokens[1:]
    if not args:
        return _session_recent(b, limit=10)
    head = args[0]
    if head == "list":
        return _session_list(b, args[1:])
    if head == "show":
        if len(args) < 2:
            return SessionDispatchResult(
                ok=False, text="  /session show <session-id>",
            )
        return _session_show(b, args[1])
    if head == "recent":
        n = 10
        if len(args) >= 2:
            try:
                n = max(1, int(args[1]))
            except ValueError:
                return SessionDispatchResult(
                    ok=False,
                    text=f"  /session recent: not an integer: {args[1]}",
                )
        return _session_recent(b, limit=n)
    if head == "bookmark":
        if len(args) < 2:
            return SessionDispatchResult(
                ok=False,
                text="  /session bookmark <session-id> [note...]",
            )
        note = " ".join(args[2:]).strip()
        return _session_bookmark(b, args[1], note)
    if head == "unbookmark":
        if len(args) < 2:
            return SessionDispatchResult(
                ok=False, text="  /session unbookmark <session-id>",
            )
        return _session_unbookmark(b, args[1])
    if head == "bookmarks":
        return _session_bookmarks(b)
    if head == "replay":
        if len(args) < 2:
            return SessionDispatchResult(
                ok=False, text="  /session replay <session-id>",
            )
        return _session_replay(b, args[1])
    if head == "rescan":
        n = len(b.index.scan(force=True))
        return SessionDispatchResult(
            ok=True, text=f"  /session rescan: {n} record(s) now in index",
        )
    if head == "help":
        return SessionDispatchResult(ok=True, text=_SESSION_HELP)
    # Short form: /session <session-id> → show
    return _session_show(b, head)


def _session_list(
    browser: SessionBrowser, args: Sequence[str],
) -> SessionDispatchResult:
    limit: Optional[int] = None
    filters: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                return SessionDispatchResult(
                    ok=False,
                    text=f"  /session list: bad --limit value {args[i+1]!r}",
                )
            i += 2
            continue
        if tok == "--ok":
            filters["ok_outcome"] = True
            i += 1
            continue
        if tok == "--bad":
            filters["ok_outcome"] = False
            i += 1
            continue
        if tok == "--has-replay":
            filters["has_replay"] = True
            i += 1
            continue
        if tok == "--parse-error":
            filters["parse_error"] = True
            i += 1
            continue
        if tok.startswith("--prefix=") and len(tok) > 9:
            filters["session_id_prefix"] = tok[9:]
            i += 1
            continue
        # Unknown flag — error, not silent
        return SessionDispatchResult(
            ok=False, text=f"  /session list: unknown flag {tok!r}",
        )
    records = browser.list_records(limit=limit, filters=filters)
    if not records:
        return SessionDispatchResult(
            ok=True, text="  (no sessions match)",
        )
    lines: List[str] = [f"  {len(records)} session(s):"]
    for r in records:
        marker = "★ " if browser.bookmarks.has(r.session_id) else "  "
        lines.append(f"  {marker}{r.one_line_summary()}")
    return SessionDispatchResult(ok=True, text="\n".join(lines))


def _session_show(
    browser: SessionBrowser, session_id: str,
) -> SessionDispatchResult:
    rec = browser.show(session_id)
    if rec is None:
        return SessionDispatchResult(
            ok=False, text=f"  /session: unknown session: {session_id}",
        )
    p = rec.project()
    lines = [
        f"  Session {rec.session_id}",
        f"    path              : {p['path']}",
        f"    summary_found     : {p['summary_found']}",
    ]
    if p["parse_error"]:
        lines.append(f"    PARSE ERROR       : {p['parse_error_reason']}")
    lines.extend([
        f"    stop_reason       : {p['stop_reason']}",
        f"    started           : {p['started_at_iso']}",
        f"    ended             : {p['ended_at_iso']}",
        f"    duration_s        : {p['duration_s']}",
        f"    ops_total         : {p['ops_total']}",
        f"    ops_applied       : {p['ops_applied']}",
        f"    verify            : "
        f"{p['ops_verified_pass']}/{p['ops_verified_total']}",
        f"    cost_spent_usd    : {p['cost_spent_usd']}",
        f"    cost_budget_usd   : {p['cost_budget_usd']}",
        f"    commit_hash       : {p['commit_hash']}",
        f"    on_disk_bytes     : {p['on_disk_bytes']}",
        f"    mtime             : {p['mtime_iso']}",
        f"    has_debug_log     : {p['has_debug_log']}",
        f"    has_replay_html   : {p['has_replay_html']}",
        f"    bookmarked        : {browser.bookmarks.has(rec.session_id)}",
        f"    ok_outcome        : {p['ok_outcome']}",
    ])
    return SessionDispatchResult(ok=True, text="\n".join(lines))


def _session_recent(
    browser: SessionBrowser, *, limit: int,
) -> SessionDispatchResult:
    records = browser.recent(limit=limit)
    if not records:
        return SessionDispatchResult(
            ok=True, text="  (no sessions found)",
        )
    lines = [f"  {len(records)} recent session(s):"]
    for r in records:
        marker = "★ " if browser.bookmarks.has(r.session_id) else "  "
        lines.append(f"  {marker}{r.one_line_summary()}")
    return SessionDispatchResult(ok=True, text="\n".join(lines))


def _session_bookmark(
    browser: SessionBrowser, session_id: str, note: str,
) -> SessionDispatchResult:
    # Even if the session doesn't exist yet in the index, operators
    # might want to bookmark an id they'll scan later — but fail-closed
    # on malformed ids.
    try:
        bm = browser.bookmark(session_id, note=note)
    except SessionBrowserError as exc:
        return SessionDispatchResult(
            ok=False, text=f"  /session bookmark: {exc}",
        )
    return SessionDispatchResult(
        ok=True,
        text=f"  bookmarked: {bm.session_id}"
             + (f" ({bm.note})" if bm.note else ""),
    )


def _session_unbookmark(
    browser: SessionBrowser, session_id: str,
) -> SessionDispatchResult:
    if not browser.unbookmark(session_id):
        return SessionDispatchResult(
            ok=False, text=f"  /session unbookmark: not bookmarked: {session_id}",
        )
    return SessionDispatchResult(
        ok=True, text=f"  unbookmarked: {session_id}",
    )


def _session_bookmarks(
    browser: SessionBrowser,
) -> SessionDispatchResult:
    pairs = browser.list_bookmarks_with_records()
    if not pairs:
        return SessionDispatchResult(
            ok=True, text="  (no bookmarks)",
        )
    lines: List[str] = [f"  {len(pairs)} bookmark(s):"]
    for bm, rec in pairs:
        note_part = f" — {bm.note}" if bm.note else ""
        if rec is None:
            lines.append(
                f"  ★ {bm.session_id} (not in index){note_part}"
            )
        else:
            lines.append(
                f"  ★ {rec.one_line_summary()}{note_part}"
            )
    return SessionDispatchResult(ok=True, text="\n".join(lines))


def _session_replay(
    browser: SessionBrowser, session_id: str,
) -> SessionDispatchResult:
    path = browser.replay_html_path(session_id)
    if path is None:
        return SessionDispatchResult(
            ok=False,
            text=f"  /session replay: no replay HTML for {session_id}",
        )
    return SessionDispatchResult(
        ok=True, text=f"  replay: {path}",
    )


__all__ = [
    "SESSION_BROWSER_SCHEMA_VERSION",
    "Bookmark",
    "BookmarkStore",
    "SessionBrowser",
    "SessionBrowserError",
    "SessionDispatchResult",
    "SessionIndex",
    "dispatch_session_command",
    "get_default_bookmark_store",
    "get_default_session_browser",
    "get_default_session_index",
    "reset_default_session_singletons",
    "set_default_session_browser",
]

_ = (FrozenSet, enum)  # silence unused-import guards
