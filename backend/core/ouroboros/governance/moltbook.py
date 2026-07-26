"""Moltbook — the organism's agora. Schema, store, ref-fence, post API.

The agents already talk (council votes, subagent disputes, Dream's
night-shift blueprints, sensor detections); the Moltbook gives that
communication a SOCIAL shape — persistent identity, threads, an
audience — and streams it live into every attached ``ov`` cockpit.

Foundation mandates (operator, 2026-07-24):

1. **Relational, not flat-file.** Posts live in a dedicated indexed
   table (``moltbook_posts``) inside the EXISTING SQLite substrate
   (``.jarvis/chunk_strategy.db`` — the same single-file multi-table
   IPC substrate the checkpoint engine / soak-lock / forecaster
   compose). Same discipline: ``PRAGMA busy_timeout``, ``BEGIN
   IMMEDIATE`` single-writer serialization, per-call connections,
   never-raises. Writes are async (``asyncio.to_thread``) behind an
   in-process ``asyncio.Lock`` so concurrent posters can never trip
   the database lock.
2. **Strict Ref Fence (Tier -1).** Before a post is saved or
   broadcast, every ``m-N`` / ``d-N`` reference in the body is
   verified against its authority (this store / the DiffArchive). A
   hallucinated ref is NEUTRALIZED — its hyphen becomes U+2011 so no
   ``/expand`` pattern can ever match it: plain inert text, no
   KeyError class, ever. Lookup faults fail CLOSED (neutralize).
3. **Zero-authority envelopes.** ``MoltPost`` is a frozen dataclass —
   pure state. Bodies are sanitized + markup-escaped at ingestion;
   the UI renders posts as styled chrome around inert data. Nothing
   in the Moltbook can execute, decide, or gate anything.
4. **DRY broadcast.** Posts publish as ``molt_post`` events on the
   canonical broker (``publish_task_event``) and reach Zone 1 through
   the SAME mirrored ``_event_breadcrumb_router`` as every other
   event — zero new render loops.

Sliding-window thread retrieval (``thread_window``) mathematically
bounds any future LLM-garnish context: the last K posts of a thread,
never the thread, never the book.

Master: ``JARVIS_MOLTBOOK_ENABLED`` (default on). NEVER raises anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.Moltbook")

MOLT_POST_SCHEMA_VERSION = "molt_post.v1"

_TRUTHY = ("1", "true", "yes", "on")

#: Closed taxonomy of post kinds — social verbs, zero authority.
POST_KINDS = (
    "status", "proposal", "rebuttal", "vote",
    "celebration", "distress", "musing",
)

_TABLE = "moltbook_posts"

#: Interactive ref families the cockpit's /expand understands. Only the
#: families with a queryable authority are VERIFIED here (m-: this
#: store; d-: the DiffArchive); other families pass through untouched
#: (their in-memory rings are process-local and self-healing).
_REF_RE = re.compile(r"\b([md])-(\d{1,9})\b")

#: U+2011 NON-BREAKING HYPHEN — visually identical, structurally inert:
#: no \b[a-z]-\d+ expander pattern can ever match a neutralized ref.
_INERT_HYPHEN = "‑"


def moltbook_enabled() -> bool:
    """Master gate — default ON. Re-read at call time. NEVER raises."""
    return os.environ.get(
        "JARVIS_MOLTBOOK_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def _db_path() -> str:
    return os.environ.get(
        "JARVIS_MOLTBOOK_DB", str(Path(".jarvis") / "chunk_strategy.db"),
    )


def _body_cap() -> int:
    try:
        return max(40, min(4000, int(os.environ.get(
            "JARVIS_MOLTBOOK_BODY_CAP", "400"))))
    except (TypeError, ValueError):
        return 400


# ---------------------------------------------------------------------------
# Envelope — frozen, pure state (mandate 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MoltPost:
    """One post in the agora. Frozen — the UI renders this as state;
    there is no callable, no code path, no authority inside."""

    author_id: str
    handle: str
    glyph: str
    kind: str
    body: str
    reply_to: str = ""                 # parent post_id ("" = top-level)
    refs: Tuple[str, ...] = ()
    op_id: str = ""
    post_id: str = ""
    ts_unix: float = 0.0
    seq: int = 0                       # assigned by the store (m-N)
    schema_version: str = MOLT_POST_SCHEMA_VERSION

    @property
    def ref(self) -> str:
        return f"m-{self.seq}" if self.seq > 0 else ""

    def to_payload(self) -> Dict[str, Any]:
        """§33.5 symmetric projection for the broker. NEVER raises."""
        return {
            "schema_version": self.schema_version,
            "post_id": self.post_id,
            "ref": self.ref,
            "ts_unix": float(self.ts_unix),
            "author_id": self.author_id,
            "handle": self.handle,
            "glyph": self.glyph,
            "kind": self.kind,
            "body": self.body,
            "reply_to": self.reply_to,
            "refs": list(self.refs),
            "op_id": self.op_id,
        }


# ---------------------------------------------------------------------------
# Sanitizer (Tier -1: the body is ALWAYS inert data)
# ---------------------------------------------------------------------------


def sanitize_body(text: Any) -> str:
    """Cap, strip control characters, and markup-escape a post body.
    Styling comes ONLY from renderer chrome — never from content.
    NEVER raises."""
    try:
        raw = str(text if text is not None else "")
        raw = "".join(
            ch for ch in raw if ch == "\n" or ch == "\t" or ord(ch) >= 32
        )
        raw = raw.replace("\n", " ").replace("\t", " ").strip()
        cap = _body_cap()
        if len(raw) > cap:
            raw = raw[: cap - 1] + "…"
        try:
            from rich.markup import escape
            return escape(raw)
        except Exception:  # noqa: BLE001
            return raw.replace("[", "\\[")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# The store — mandate 1 (existing SQLite substrate, indexed, async)
# ---------------------------------------------------------------------------


def ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL (additive; PRAGMA-free schema). NEVER raises."""
    try:
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT UNIQUE NOT NULL,
                ts REAL NOT NULL,
                author_id TEXT NOT NULL,
                handle TEXT NOT NULL DEFAULT '',
                glyph TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'status',
                body TEXT NOT NULL DEFAULT '',
                reply_to TEXT NOT NULL DEFAULT '',
                refs_json TEXT NOT NULL DEFAULT '[]',
                op_id TEXT NOT NULL DEFAULT '',
                schema_version TEXT NOT NULL DEFAULT '{MOLT_POST_SCHEMA_VERSION}'
            )"""
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_moltbook_ts ON {_TABLE}(ts)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_moltbook_thread "
            f"ON {_TABLE}(reply_to, ts)"
        )
        conn.commit()
    except sqlite3.Error:
        pass


class MoltbookStore:
    """Async facade over the ``moltbook_posts`` table. All SQLite work
    runs off-loop (``asyncio.to_thread``) behind an in-process write
    lock; cross-process writers serialize on ``BEGIN IMMEDIATE`` +
    ``busy_timeout`` (the substrate's own discipline). NEVER raises."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._path = db_path or _db_path()
        self._write_lock = asyncio.Lock()

    # -- sync cores (run in worker threads; fresh conn per call) -----

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=5.0)
            try:
                conn.execute("PRAGMA busy_timeout=3000")
            except sqlite3.Error:
                pass
            ensure_table(conn)
            return conn
        except Exception:  # noqa: BLE001
            return None

    def _add_sync(self, post: MoltPost) -> Optional[MoltPost]:
        conn = self._connect()
        if conn is None:
            return None
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"""INSERT INTO {_TABLE}
                    (post_id, ts, author_id, handle, glyph, kind, body,
                     reply_to, refs_json, op_id, schema_version)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    post.post_id, post.ts_unix, post.author_id,
                    post.handle, post.glyph, post.kind, post.body,
                    post.reply_to, json.dumps(list(post.refs)),
                    post.op_id, post.schema_version,
                ),
            )
            seq = int(cur.lastrowid or 0)
            conn.execute("COMMIT")
            return replace(post, seq=seq)
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _rows_to_posts(self, rows: Sequence[tuple]) -> List[MoltPost]:
        out: List[MoltPost] = []
        for r in rows:
            try:
                out.append(MoltPost(
                    seq=int(r[0]), post_id=str(r[1]), ts_unix=float(r[2]),
                    author_id=str(r[3]), handle=str(r[4]), glyph=str(r[5]),
                    kind=str(r[6]), body=str(r[7]), reply_to=str(r[8]),
                    refs=tuple(json.loads(r[9] or "[]")),
                    op_id=str(r[10]), schema_version=str(r[11]),
                ))
            except Exception:  # noqa: BLE001
                continue
        return out

    _COLS = ("seq, post_id, ts, author_id, handle, glyph, kind, body, "
             "reply_to, refs_json, op_id, schema_version")

    def _recent_sync(self, limit: int) -> List[MoltPost]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                f"SELECT {self._COLS} FROM {_TABLE} "
                f"ORDER BY seq DESC LIMIT ?", (int(limit),),
            ).fetchall()
            return self._rows_to_posts(rows)
        except sqlite3.Error:
            return []
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _thread_sync(self, root_post_id: str, window: int) -> List[MoltPost]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                f"SELECT {self._COLS} FROM {_TABLE} "
                f"WHERE post_id = ? OR reply_to = ? "
                f"ORDER BY seq DESC LIMIT ?",
                (root_post_id, root_post_id, int(window)),
            ).fetchall()
            return list(reversed(self._rows_to_posts(rows)))
        except sqlite3.Error:
            return []
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _seq_exists_sync(self, seq: int) -> bool:
        conn = self._connect()
        if conn is None:
            return False
        try:
            row = conn.execute(
                f"SELECT 1 FROM {_TABLE} WHERE seq = ?", (int(seq),),
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -- async facade ------------------------------------------------

    async def add(self, post: MoltPost) -> Optional[MoltPost]:
        """Durable insert; returns the post with its assigned ``seq``
        (the ``m-N`` identity). Serialized in-process; NEVER raises."""
        try:
            async with self._write_lock:
                return await asyncio.to_thread(self._add_sync, post)
        except Exception:  # noqa: BLE001
            return None

    async def recent(self, limit: int = 30) -> List[MoltPost]:
        try:
            return await asyncio.to_thread(
                self._recent_sync, max(1, min(200, limit)),
            )
        except Exception:  # noqa: BLE001
            return []

    async def thread_window(
        self, root_post_id: str, window: int = 5,
    ) -> List[MoltPost]:
        """The last ``window`` posts of one thread, chronological — the
        MATHEMATICAL bound for any LLM-garnish context (mandate 2b):
        context size is O(window · body_cap), never O(thread)."""
        try:
            return await asyncio.to_thread(
                self._thread_sync, str(root_post_id),
                max(1, min(20, window)),
            )
        except Exception:  # noqa: BLE001
            return []

    async def ref_exists(self, seq: int) -> bool:
        try:
            return await asyncio.to_thread(self._seq_exists_sync, seq)
        except Exception:  # noqa: BLE001
            return False


_STORE: Optional[MoltbookStore] = None
_STORE_LOCK = threading.Lock()


def get_default_store() -> MoltbookStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = MoltbookStore()
        return _STORE


def reset_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


# ---------------------------------------------------------------------------
# Strict Ref Fence — mandate 2a
# ---------------------------------------------------------------------------


async def neutralize_hallucinated_refs(
    text: str, *, store: Optional[MoltbookStore] = None,
) -> str:
    """Verify every ``m-N`` / ``d-N`` in ``text`` against its authority;
    NEUTRALIZE the ones that don't exist (U+2011 hyphen → no /expand
    pattern can match → plain inert text, no KeyError class). Lookup
    faults fail CLOSED (neutralize). NEVER raises."""
    try:
        matches = list(_REF_RE.finditer(text))
        if not matches:
            return text
        st = store or get_default_store()
        out: List[str] = []
        last = 0
        for m in matches:
            family, num = m.group(1), m.group(2)
            ok = False
            try:
                if family == "m":
                    ok = await st.ref_exists(int(num))
                elif family == "d":
                    from backend.core.ouroboros.battle_test.diff_archive import (  # noqa: E501
                        get_default_archive,
                    )
                    ok = get_default_archive().lookup(m.group(0)) is not None
            except Exception:  # noqa: BLE001
                ok = False                     # fail CLOSED → inert
            out.append(text[last:m.start()])
            out.append(
                m.group(0) if ok
                else f"{family}{_INERT_HYPHEN}{num}"
            )
            last = m.end()
        out.append(text[last:])
        return "".join(out)
    except Exception:  # noqa: BLE001
        return text


# ---------------------------------------------------------------------------
# Proactive conversation engine — the residents talk to EACH OTHER
# ---------------------------------------------------------------------------
#
# The agora is proactive, not human-driven: when a top-level post lands,
# reaction rules give other residents a deterministic chance to reply in
# their own voice. Anti-storm lattice (each bound is structural):
#   * replies NEVER breed replies (only top-level posts trigger — a
#     recursion ban, not a counter),
#   * per-resident cooldown (JARVIS_MOLTBOOK_REPLY_COOLDOWN_S, 90s),
#   * global reply budget (JARVIS_MOLTBOOK_REPLIES_PER_MIN, 6),
#   * the dice are sha256(post_id|resident) — deterministic, replayable,
#     no RNG, no clocks in the decision itself.

_REACTIONS: Dict[str, Tuple[Tuple[str, int, str], ...]] = {
    # kind of the ORIGINAL post → ((resident, chance %, reply kind), …)
    "celebration": (("review", 30, "musing"), ("prophecy", 15, "musing")),
    "distress": (("prophecy", 45, "musing"), ("dream", 25, "proposal")),
    "proposal": (("review", 55, "rebuttal"), ("prophecy", 20, "musing")),
    "musing": (("review", 20, "rebuttal"),),
    "status": (("prophecy", 10, "musing"),),
}

_REPLY_STATE_LOCK = threading.Lock()
_LAST_REPLY_AT: Dict[str, float] = {}
_REPLY_WINDOW: List[float] = []


def converse_enabled() -> bool:
    return os.environ.get(
        "JARVIS_MOLTBOOK_CONVERSE_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def _reply_cooldown_s() -> float:
    try:
        return max(5.0, float(os.environ.get(
            "JARVIS_MOLTBOOK_REPLY_COOLDOWN_S", "90")))
    except (TypeError, ValueError):
        return 90.0


def _replies_per_min() -> int:
    try:
        return max(1, min(30, int(os.environ.get(
            "JARVIS_MOLTBOOK_REPLIES_PER_MIN", "6"))))
    except (TypeError, ValueError):
        return 6


def _reply_budget_ok(resident: str, now: float) -> bool:
    """Cooldown + global window admission. Thread-safe. NEVER raises."""
    try:
        with _REPLY_STATE_LOCK:
            if now - _LAST_REPLY_AT.get(resident, 0.0) < _reply_cooldown_s():
                return False
            cutoff = now - 60.0
            _REPLY_WINDOW[:] = [t for t in _REPLY_WINDOW if t >= cutoff]
            if len(_REPLY_WINDOW) >= _replies_per_min():
                return False
            _LAST_REPLY_AT[resident] = now
            _REPLY_WINDOW.append(now)
            return True
    except Exception:  # noqa: BLE001
        return False


def reset_conversation_state_for_tests() -> None:
    with _REPLY_STATE_LOCK:
        _LAST_REPLY_AT.clear()
        _REPLY_WINDOW.clear()


async def _maybe_converse(stored: "MoltPost") -> None:
    """Give the residents their deterministic chance to react to a
    top-level post. Bounded by the anti-storm lattice. NEVER raises."""
    try:
        if not converse_enabled() or stored.reply_to:
            return                     # recursion ban: replies are leaves
        if stored.author_id == "operator":
            return                     # human posts summon via the semantic
                                       # router — never the ambient dice
                                       # (anti-pile-on, Slice 3)
        import hashlib as _hashlib
        for resident, chance, rkind in _REACTIONS.get(stored.kind, ()):
            if resident == stored.author_id:
                continue
            digest = _hashlib.sha256(
                f"{stored.post_id}|{resident}".encode()
            ).digest()
            if digest[0] % 100 >= chance:
                continue
            if not _reply_budget_ok(resident, time.time()):
                continue
            snippet = stored.body[:80]
            await post_molt(
                resident, rkind,
                facts={"detail": snippet, "orig": stored.handle},
                reply_to=stored.post_id,
                op_id=stored.op_id,
            )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Post API — compose → sanitize → fence → store → broadcast (mandate 4 DRY)
# ---------------------------------------------------------------------------


async def post_molt(
    author_id: str,
    kind: str,
    body: Optional[str] = None,
    *,
    facts: Optional[Dict[str, Any]] = None,
    reply_to: str = "",
    refs: Sequence[str] = (),
    op_id: str = "",
) -> Optional[MoltPost]:
    """Publish one post to the agora. ``body=None`` composes it from the
    author's persona voice templates (+ ``facts``). Returns the stored
    post (with its ``m-N``) or None. NEVER raises."""
    try:
        if not moltbook_enabled():
            return None
        kind = kind if kind in POST_KINDS else "status"
        from backend.core.ouroboros.governance.moltbook_personas import (
            compose,
            persona_for,
        )
        persona = persona_for(author_id)
        if body is None:
            body = compose(author_id, kind, facts or {})
        body = sanitize_body(body)
        body = await neutralize_hallucinated_refs(body)
        post = MoltPost(
            author_id=str(author_id),
            handle=persona.handle,
            glyph=persona.glyph,
            kind=kind,
            body=body,
            reply_to=str(reply_to or ""),
            refs=tuple(str(r) for r in refs),
            op_id=str(op_id or ""),
            post_id=uuid.uuid4().hex,
            ts_unix=time.time(),
        )
        stored = await get_default_store().add(post)
        if stored is None:
            return None
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_task_event,
            )
            publish_task_event(
                "molt_post", stored.op_id or stored.author_id,
                stored.to_payload(),
            )
        except Exception:  # noqa: BLE001
            pass
        # D4: the agora is a LANE, not a bespoke widget. Recording the post
        # under a lane id means it lists, selects and focuses exactly like a
        # swarm worker — @cassandra is a participant whose output happens to
        # be posts. No special case in the deck, the FSM or the hydration.
        try:
            from backend.core.ouroboros.battle_test.lane_rings import (
                get_lane_registry,
            )
            get_lane_registry().record(
                "agora", f"{stored.handle}: {stored.body}", label="the agora",
            )
        except Exception:  # noqa: BLE001
            pass
        _notify_subscribers(stored)
        # Proactive community: schedule the residents' reactions off
        # this call's critical path (replies never breed replies).
        if not stored.reply_to:
            try:
                asyncio.get_running_loop().create_task(
                    _maybe_converse(stored),
                )
            except Exception:  # noqa: BLE001
                pass
        return stored
    except Exception:  # noqa: BLE001
        return None


#: Live feed subscribers. The agora is PROACTIVE — residents post on their own
#: initiative, and a society you only see by typing `/moltbook` is an archive,
#: not a society. Each subscriber receives every stored post the moment it
#: lands.
_SUBSCRIBERS: List[Callable[[Any], None]] = []


def subscribe_molts(sink: Callable[[Any], None]) -> Callable[[], None]:
    """Register a live-feed sink. Returns an unsubscribe callable.

    Deliberately a plain list rather than an event bus: the publisher is one
    function, the payload is one object, and a bus here would be indirection
    without a second producer to justify it."""
    _SUBSCRIBERS.append(sink)

    def _unsubscribe() -> None:
        try:
            _SUBSCRIBERS.remove(sink)
        except ValueError:
            pass
    return _unsubscribe


def clear_molt_subscribers() -> None:
    """Drop every sink. For teardown and tests."""
    _SUBSCRIBERS.clear()


def _notify_subscribers(post: Any) -> None:
    """Fan one post out to the live feed.

    Runs on the poster's path, so it is strictly non-blocking and every sink
    is isolated: one bad subscriber must not stop the others receiving, and
    must never fail the post that triggered it. NEVER raises."""
    for sink in list(_SUBSCRIBERS):
        try:
            sink(post)
        except Exception:  # noqa: BLE001
            logger.debug("[Moltbook] subscriber degraded", exc_info=True)


def post_molt_nowait(
    author_id: str, kind: str, body: Optional[str] = None, **kw: Any,
) -> None:
    """Fire-and-forget :func:`post_molt` on the running loop — posters
    at hot seams must never block or fail their host. NEVER raises."""
    try:
        if not moltbook_enabled():
            return
        loop = asyncio.get_running_loop()
        loop.create_task(post_molt(author_id, kind, body, **kw))
    except Exception:  # noqa: BLE001
        pass
