"""§37 Tier 2 #11 — Session-search SQLite archive (Slice 1 substrate).

Operator-facing surface for the Phase 9 evidence ledger
accumulating across many sessions over weeks. Currently the
operator must scan ``.jarvis/live_fire_graduation_history.jsonl``
× 12+ flags by hand. SessionArchive indexes the ledger into
SQLite at ``.jarvis/session_archive.db`` so :mod:`history_repl`
(Slice 2) can answer ``/history flag <name>`` /
``/history since <days>`` / ``/history search <text>`` in one
shot. Composes existing canonical paths — zero parallel
metadata directories.

**Read-only browser** (mirrors §37 Tier 2 #10 ``replay_repl``
authority asymmetry):

  * Public API is pure-read — :func:`find_sessions` /
    :func:`get_session`. NEVER mutates orchestrator / risk-
    tier / iron-gate state.
  * Backfill (the only write surface) is idempotent ``INSERT
    OR REPLACE`` on session_id PK; safe to run every boot.
  * SQLite at ``.jarvis/session_archive.db``; mirrors
    ``performance_records.db`` pattern from
    ``backend/core/ouroboros/integration.py``.

**Sources composed** (no parallel metadata directories):

  * ``.jarvis/live_fire_graduation_history.jsonl`` — Phase 9
    primary source (flag_name + outcome + cost + duration +
    notes + runner_attributed).
  * ``.jarvis/graduation_ledger.jsonl`` — audit-trail
    complement (recorded_by + recorded_at).
  * ``.ouroboros/sessions/<id>/summary.json`` — per-session
    harness telemetry (ops_count + cost_breakdown when present).

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / urgency_router / change_engine /
semantic_guardian / candidate_generator / policy imports.

**Master flag** ``JARVIS_SESSION_ARCHIVE_ENABLED`` default-
FALSE per §33.1: when off, all public surfaces gracefully
return empty results (no DB touch, zero filesystem cost).

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("Ouroboros.SessionArchive")


SESSION_ARCHIVE_SCHEMA_VERSION: str = "session_archive.1"


_TRUTHY = frozenset({"1", "true", "yes", "on"})


_OUTCOME_VALUES = frozenset({
    "clean",
    "runner",
    "infra",
    "spec",
    "timeout",
    "unknown",
})


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_SESSION_ARCHIVE_ENABLED`` master switch.
    Default-FALSE per §33.1: when off, archive operations
    short-circuit (zero DB touch, zero filesystem cost)."""
    raw = os.environ.get(
        "JARVIS_SESSION_ARCHIVE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Path resolution — composes canonical .jarvis + .ouroboros dirs
# ---------------------------------------------------------------------------


def _resolve_repo_root() -> Path:
    """Resolve the project root from env override or fall back
    to the repo this module ships in. Defensive — returns ``.``
    when unable to determine (caller treats as no-archive)."""
    override = os.environ.get(
        "JARVIS_SESSION_ARCHIVE_REPO_ROOT", "",
    ).strip()
    if override:
        try:
            return Path(override).resolve()
        except Exception:  # noqa: BLE001 — defensive
            pass
    try:
        # Resolve via this file's location: file is at
        # backend/core/ouroboros/governance/session_archive.py;
        # repo root is 4 levels up.
        here = Path(__file__).resolve()
        return here.parents[4]
    except Exception:  # noqa: BLE001 — defensive
        return Path(".").resolve()


def default_db_path() -> Path:
    """``.jarvis/session_archive.db`` under the resolved repo
    root. Override via ``JARVIS_SESSION_ARCHIVE_DB_PATH``."""
    override = os.environ.get(
        "JARVIS_SESSION_ARCHIVE_DB_PATH", "",
    ).strip()
    if override:
        try:
            return Path(override).resolve()
        except Exception:  # noqa: BLE001 — defensive
            pass
    return _resolve_repo_root() / ".jarvis" / "session_archive.db"


def _live_fire_ledger_path() -> Path:
    return (
        _resolve_repo_root()
        / ".jarvis"
        / "live_fire_graduation_history.jsonl"
    )


def _graduation_ledger_path() -> Path:
    return (
        _resolve_repo_root()
        / ".jarvis"
        / "graduation_ledger.jsonl"
    )


def _sessions_root() -> Path:
    return _resolve_repo_root() / ".ouroboros" / "sessions"


# ---------------------------------------------------------------------------
# Frozen artifact — SessionRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """Indexed session metadata. Frozen for safe propagation
    across query result sets."""

    session_id: str
    started_at_epoch: float = 0.0
    ended_at_epoch: float = 0.0
    duration_s: float = 0.0
    outcome: str = "unknown"
    """One of clean / runner / infra / spec / timeout / unknown."""

    flag_name: str = ""
    """Flag under test (Phase 9 cadence) or empty string."""

    cost_usd: float = 0.0
    ops_count: int = 0
    runner_attributed: bool = False
    stop_reason: str = ""
    notes: str = ""
    recorded_by: str = ""
    session_type: str = "unknown"
    """live_fire / cron / manual / replay / unknown."""

    schema_version: str = field(
        default=SESSION_ARCHIVE_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": str(self.session_id),
            "started_at_epoch": float(self.started_at_epoch),
            "ended_at_epoch": float(self.ended_at_epoch),
            "duration_s": float(self.duration_s),
            "outcome": str(self.outcome),
            "flag_name": str(self.flag_name),
            "cost_usd": float(self.cost_usd),
            "ops_count": int(self.ops_count),
            "runner_attributed": bool(self.runner_attributed),
            "stop_reason": str(self.stop_reason)[:128],
            "notes": str(self.notes)[:512],
            "recorded_by": str(self.recorded_by)[:64],
            "session_type": str(self.session_type),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# SessionArchive — SQLite-backed index
# ---------------------------------------------------------------------------


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS session_index (
    session_id TEXT PRIMARY KEY,
    started_at_epoch REAL NOT NULL DEFAULT 0,
    ended_at_epoch REAL NOT NULL DEFAULT 0,
    duration_s REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'unknown',
    flag_name TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    ops_count INTEGER NOT NULL DEFAULT 0,
    runner_attributed INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL DEFAULT '',
    session_type TEXT NOT NULL DEFAULT 'unknown',
    schema_version TEXT NOT NULL DEFAULT 'session_archive.1'
)
"""


_INDEX_DEFINITIONS = (
    "CREATE INDEX IF NOT EXISTS idx_session_started "
    "ON session_index(started_at_epoch DESC)",
    "CREATE INDEX IF NOT EXISTS idx_session_flag "
    "ON session_index(flag_name, started_at_epoch DESC)",
    "CREATE INDEX IF NOT EXISTS idx_session_outcome "
    "ON session_index(outcome, started_at_epoch DESC)",
)


class SessionArchive:
    """SQLite-backed session index. Read-only public API +
    idempotent backfill. NEVER raises."""

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
    ) -> None:
        self._db_path = (
            Path(db_path) if db_path is not None
            else default_db_path()
        )
        self._db_initialized = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # DB plumbing
    # ------------------------------------------------------------------

    def _ensure_db(self) -> Optional[sqlite3.Connection]:
        """Open (and lazily initialize) the SQLite DB. Returns
        ``None`` on failure — caller treats as no-archive
        gracefully."""
        try:
            self._db_path.parent.mkdir(
                parents=True, exist_ok=True,
            )
        except Exception:  # noqa: BLE001 — defensive
            return None
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=5.0,
                isolation_level=None,  # autocommit; safer for
                                      # short read-only ops
            )
        except sqlite3.Error:
            return None
        try:
            if not self._db_initialized:
                conn.execute(_CREATE_TABLE_SQL)
                for idx_sql in _INDEX_DEFINITIONS:
                    conn.execute(idx_sql)
                self._db_initialized = True
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — defensive
                pass
            return None
        return conn

    # ------------------------------------------------------------------
    # Backfill — composes canonical ledgers + summary.json
    # ------------------------------------------------------------------

    def backfill(self) -> int:
        """Hydrate the SQLite index from existing JSONL ledgers
        + per-session summary.json. Idempotent (``INSERT OR
        REPLACE`` on session_id PK). Returns the number of
        rows upserted. NEVER raises."""
        if not master_enabled():
            return 0
        conn = self._ensure_db()
        if conn is None:
            return 0
        rows = 0
        try:
            # Live-fire ledger is the primary source — flag +
            # outcome + cost + duration + notes.
            rows += self._ingest_jsonl(
                conn,
                _live_fire_ledger_path(),
                session_type="live_fire",
                recorded_by_default="",
            )
            # Graduation ledger is the audit-trail complement.
            # When session_id collides with a live_fire row,
            # we MERGE missing fields rather than overwriting.
            rows += self._ingest_jsonl(
                conn,
                _graduation_ledger_path(),
                session_type="cron",
                recorded_by_default="live_fire_soak_cli",
                merge_only=True,
            )
            # Per-session harness telemetry — fills duration /
            # cost / ops_count when missing on the ledger row.
            rows += self._ingest_session_summaries(conn)
        except sqlite3.Error:
            logger.debug(
                "[SessionArchive] backfill SQL error",
                exc_info=True,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[SessionArchive] backfill failed (non-fatal)",
                exc_info=True,
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — defensive
                pass
        return rows

    def _ingest_jsonl(
        self,
        conn: sqlite3.Connection,
        path: Path,
        *,
        session_type: str,
        recorded_by_default: str,
        merge_only: bool = False,
    ) -> int:
        """Stream a JSONL ledger into the index. Idempotent —
        each row is upserted via ``INSERT OR REPLACE`` (or merged
        via UPDATE when ``merge_only=True``). NEVER raises."""
        if not path.is_file():
            return 0
        count = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    record = self._record_from_ledger_entry(
                        entry,
                        session_type=session_type,
                        recorded_by_default=recorded_by_default,
                    )
                    if record is None:
                        continue
                    try:
                        if merge_only:
                            self._merge_row(conn, record)
                        else:
                            self._upsert_row(conn, record)
                        count += 1
                    except sqlite3.Error:
                        continue
        except OSError:
            return count
        return count

    def _ingest_session_summaries(
        self, conn: sqlite3.Connection,
    ) -> int:
        """Walk ``.ouroboros/sessions/<id>/summary.json`` and
        merge harness telemetry (duration / cost / ops_count)
        into existing rows. NEVER raises."""
        root = _sessions_root()
        if not root.is_dir():
            return 0
        count = 0
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if not entry.is_dir():
                continue
            summary_path = entry / "summary.json"
            if not summary_path.is_file():
                continue
            try:
                with summary_path.open(
                    "r", encoding="utf-8",
                ) as f:
                    payload = json.load(f)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            record = self._record_from_summary(
                session_id=str(entry.name), payload=payload,
            )
            if record is None:
                continue
            try:
                self._merge_row(conn, record)
                count += 1
            except sqlite3.Error:
                continue
        return count

    @staticmethod
    def _record_from_ledger_entry(
        entry: Dict[str, Any],
        *,
        session_type: str,
        recorded_by_default: str,
    ) -> Optional[SessionRecord]:
        sid = str(entry.get("session_id", "")).strip()
        if not sid:
            return None
        outcome_raw = str(entry.get("outcome", "unknown")).lower()
        if outcome_raw not in _OUTCOME_VALUES:
            outcome_raw = "unknown"
        try:
            started = float(
                entry.get("started_at_epoch", 0) or 0,
            )
        except (TypeError, ValueError):
            started = 0.0
        try:
            ended = float(
                entry.get("finished_at_epoch", 0) or 0,
            )
        except (TypeError, ValueError):
            ended = 0.0
        if ended <= 0.0:
            try:
                ended = float(
                    entry.get("recorded_at_epoch", 0) or 0,
                )
            except (TypeError, ValueError):
                ended = 0.0
        try:
            dur = float(entry.get("duration_s", 0) or 0)
        except (TypeError, ValueError):
            dur = 0.0
        try:
            cost = float(entry.get("cost_total_usd", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            ops = int(entry.get("ops_count", 0) or 0)
        except (TypeError, ValueError):
            ops = 0
        runner = bool(entry.get("runner_attributed", False))
        recorded_by = str(
            entry.get("recorded_by", recorded_by_default)
            or recorded_by_default,
        )
        return SessionRecord(
            session_id=sid,
            started_at_epoch=started,
            ended_at_epoch=ended,
            duration_s=dur,
            outcome=outcome_raw,
            flag_name=str(entry.get("flag_name", "") or ""),
            cost_usd=cost,
            ops_count=ops,
            runner_attributed=runner,
            stop_reason=str(entry.get("stop_reason", "") or ""),
            notes=str(entry.get("notes", "") or ""),
            recorded_by=recorded_by,
            session_type=session_type,
        )

    @staticmethod
    def _record_from_summary(
        *, session_id: str, payload: Dict[str, Any],
    ) -> Optional[SessionRecord]:
        sid = str(session_id).strip()
        if not sid:
            return None
        try:
            dur = float(payload.get("duration_s", 0) or 0)
        except (TypeError, ValueError):
            dur = 0.0
        try:
            cost = float(payload.get("cost_total", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        stats = payload.get("stats") or {}
        try:
            ops = int(stats.get("attempted", 0) or 0)
        except (TypeError, ValueError):
            ops = 0
        return SessionRecord(
            session_id=sid,
            duration_s=dur,
            cost_usd=cost,
            ops_count=ops,
            stop_reason=str(
                payload.get("stop_reason", "") or "",
            ),
            session_type="manual",
        )

    @staticmethod
    def _upsert_row(
        conn: sqlite3.Connection, rec: SessionRecord,
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO session_index ("
            "session_id, started_at_epoch, ended_at_epoch, "
            "duration_s, outcome, flag_name, cost_usd, "
            "ops_count, runner_attributed, stop_reason, "
            "notes, recorded_by, session_type, schema_version"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.session_id,
                float(rec.started_at_epoch),
                float(rec.ended_at_epoch),
                float(rec.duration_s),
                rec.outcome,
                rec.flag_name,
                float(rec.cost_usd),
                int(rec.ops_count),
                1 if rec.runner_attributed else 0,
                rec.stop_reason[:128],
                rec.notes[:512],
                rec.recorded_by[:64],
                rec.session_type,
                rec.schema_version,
            ),
        )

    @staticmethod
    def _merge_row(
        conn: sqlite3.Connection, rec: SessionRecord,
    ) -> None:
        """COALESCE-style merge: preserve existing non-empty
        fields, fill empty/zero fields from the new record.
        Used when later sources (graduation ledger, summary.json)
        complement an existing live_fire row."""
        cur = conn.execute(
            "SELECT session_id FROM session_index "
            "WHERE session_id = ?",
            (rec.session_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            # No prior row — straight insert.
            SessionArchive._upsert_row(conn, rec)
            return
        # COALESCE: only overwrite empty/zero fields.
        conn.execute(
            "UPDATE session_index SET "
            "started_at_epoch = CASE "
            "  WHEN started_at_epoch <= 0 THEN ? "
            "  ELSE started_at_epoch END, "
            "ended_at_epoch = CASE "
            "  WHEN ended_at_epoch <= 0 THEN ? "
            "  ELSE ended_at_epoch END, "
            "duration_s = CASE "
            "  WHEN duration_s <= 0 THEN ? "
            "  ELSE duration_s END, "
            "cost_usd = CASE "
            "  WHEN cost_usd <= 0 THEN ? "
            "  ELSE cost_usd END, "
            "ops_count = CASE "
            "  WHEN ops_count <= 0 THEN ? "
            "  ELSE ops_count END, "
            "stop_reason = CASE "
            "  WHEN stop_reason = '' THEN ? "
            "  ELSE stop_reason END, "
            "notes = CASE "
            "  WHEN notes = '' THEN ? "
            "  ELSE notes END, "
            "recorded_by = CASE "
            "  WHEN recorded_by = '' THEN ? "
            "  ELSE recorded_by END "
            "WHERE session_id = ?",
            (
                float(rec.started_at_epoch),
                float(rec.ended_at_epoch),
                float(rec.duration_s),
                float(rec.cost_usd),
                int(rec.ops_count),
                rec.stop_reason[:128],
                rec.notes[:512],
                rec.recorded_by[:64],
                rec.session_id,
            ),
        )

    # ------------------------------------------------------------------
    # Query API — pure read
    # ------------------------------------------------------------------

    def find_sessions(
        self,
        *,
        outcome: Optional[str] = None,
        flag: Optional[str] = None,
        since_epoch: Optional[float] = None,
        until_epoch: Optional[float] = None,
        notes_contains: Optional[str] = None,
        limit: int = 50,
    ) -> List[SessionRecord]:
        """Query the index. NEVER raises — returns ``[]`` on
        any failure (master off / DB unavailable / SQL error).

        All filters are optional; combining them yields AND-
        composed predicates. Results sorted by
        ``started_at_epoch DESC`` (newest first). ``limit``
        clamped to [1, 1000]."""
        if not master_enabled():
            return []
        try:
            limit_clamped = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            limit_clamped = 50
        conn = self._ensure_db()
        if conn is None:
            return []
        try:
            sql = (
                "SELECT session_id, started_at_epoch, "
                "ended_at_epoch, duration_s, outcome, "
                "flag_name, cost_usd, ops_count, "
                "runner_attributed, stop_reason, notes, "
                "recorded_by, session_type, schema_version "
                "FROM session_index WHERE 1=1"
            )
            params: List[Any] = []
            if outcome and isinstance(outcome, str):
                sql += " AND outcome = ?"
                params.append(outcome.strip().lower())
            if flag and isinstance(flag, str):
                sql += " AND flag_name = ?"
                params.append(flag.strip())
            if since_epoch is not None:
                try:
                    params.append(float(since_epoch))
                    sql += " AND started_at_epoch >= ?"
                except (TypeError, ValueError):
                    pass
            if until_epoch is not None:
                try:
                    params.append(float(until_epoch))
                    sql += " AND started_at_epoch <= ?"
                except (TypeError, ValueError):
                    pass
            if notes_contains and isinstance(notes_contains, str):
                sql += " AND notes LIKE ?"
                params.append(f"%{notes_contains.strip()}%")
            sql += " ORDER BY started_at_epoch DESC LIMIT ?"
            params.append(limit_clamped)
            cur = conn.execute(sql, tuple(params))
            return [
                self._row_to_record(row)
                for row in cur.fetchall()
            ]
        except sqlite3.Error:
            return []
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — defensive
                pass

    def get_session(
        self, session_id: str,
    ) -> Optional[SessionRecord]:
        """Lookup a single session by ID. Returns ``None`` on
        miss / failure. NEVER raises."""
        if not master_enabled() or not session_id:
            return None
        conn = self._ensure_db()
        if conn is None:
            return None
        try:
            cur = conn.execute(
                "SELECT session_id, started_at_epoch, "
                "ended_at_epoch, duration_s, outcome, "
                "flag_name, cost_usd, ops_count, "
                "runner_attributed, stop_reason, notes, "
                "recorded_by, session_type, schema_version "
                "FROM session_index WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_record(row)
        except sqlite3.Error:
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — defensive
                pass

    def total_count(self) -> int:
        """Operator-visible total session count. NEVER raises."""
        if not master_enabled():
            return 0
        conn = self._ensure_db()
        if conn is None:
            return 0
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM session_index",
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else 0
        except sqlite3.Error:
            return 0
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — defensive
                pass

    @staticmethod
    def _row_to_record(row: Tuple[Any, ...]) -> SessionRecord:
        return SessionRecord(
            session_id=str(row[0]),
            started_at_epoch=float(row[1]),
            ended_at_epoch=float(row[2]),
            duration_s=float(row[3]),
            outcome=str(row[4]),
            flag_name=str(row[5]),
            cost_usd=float(row[6]),
            ops_count=int(row[7]),
            runner_attributed=bool(row[8]),
            stop_reason=str(row[9]),
            notes=str(row[10]),
            recorded_by=str(row[11]),
            session_type=str(row[12]),
            schema_version=str(row[13]),
        )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_DEFAULT_ARCHIVE: Optional[SessionArchive] = None


def get_default_archive() -> SessionArchive:
    """Return the process-wide :class:`SessionArchive` singleton.
    Created lazily on first access. Safe across threads
    (SQLite connections are opened per-call)."""
    global _DEFAULT_ARCHIVE
    if _DEFAULT_ARCHIVE is None:
        _DEFAULT_ARCHIVE = SessionArchive()
    return _DEFAULT_ARCHIVE


def reset_default_archive_for_tests() -> None:
    """Test-only — pinned via naming convention."""
    global _DEFAULT_ARCHIVE
    _DEFAULT_ARCHIVE = None


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the 3 knobs this module reads."""
    try:
        registry.register(
            name="JARVIS_SESSION_ARCHIVE_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for §37 Tier 2 #11 session-"
                "search SQLite archive. Default-FALSE per "
                "§33.1; when off, find_sessions returns []."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "session_archive.py"
            ),
            example="JARVIS_SESSION_ARCHIVE_ENABLED=true",
        )
        registry.register(
            name="JARVIS_SESSION_ARCHIVE_DB_PATH",
            type_="path",
            default=".jarvis/session_archive.db",
            description=(
                "Override path for the SQLite archive. "
                "Defaults to .jarvis/session_archive.db under "
                "the resolved repo root."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "session_archive.py"
            ),
            example=(
                "JARVIS_SESSION_ARCHIVE_DB_PATH=/tmp/sessions.db"
            ),
        )
        registry.register(
            name="JARVIS_SESSION_ARCHIVE_REPO_ROOT",
            type_="path",
            default="(auto-resolved)",
            description=(
                "Override repo root for ledger discovery. "
                "Auto-resolved via this module's location "
                "when unset."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "session_archive.py"
            ),
            example=(
                "JARVIS_SESSION_ARCHIVE_REPO_ROOT="
                "/Users/me/repo"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[SessionArchive] FlagRegistry seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``session_archive_master_flag_default_false`` — §33.1
         producer flag stays default-FALSE.
      2. ``session_archive_authority_asymmetry`` — substrate
         purity (no orchestrator/iron_gate/policy/providers/
         change_engine/semantic_guardian/candidate_generator/
         urgency_router imports).
      3. ``session_archive_composes_canonical_paths`` — single
         source of truth for ledger paths
         (.jarvis/live_fire_graduation_history.jsonl +
         .jarvis/graduation_ledger.jsonl + .ouroboros/
         sessions/); no parallel directories.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/session_archive.py"
    )

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        master_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    master_func = node
                    break
        if master_func is None:
            violations.append("master_enabled() helper missing")
            return tuple(violations)
        empty_guard_returns_false = False
        for sub in ast.walk(master_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            compares: list = []
            for st in ast.walk(test):
                if isinstance(st, ast.Compare):
                    compares.append(st)
            compares_empty_str = False
            for cmp_node in compares:
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        compares_empty_str = True
                        break
                if compares_empty_str:
                    break
            if not compares_empty_str:
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is False
                    ):
                        empty_guard_returns_false = True
                        break
            if empty_guard_returns_false:
                break
        if not empty_guard_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"session_archive.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_paths(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Bytes-pin: source MUST reference the canonical
        ledger filenames — no parallel paths."""
        violations: list = []
        required_substrings = (
            "live_fire_graduation_history.jsonl",
            "graduation_ledger.jsonl",
            "session_archive.db",
        )
        for s in required_substrings:
            if s not in source:
                violations.append(
                    f"canonical path substring missing: {s!r}"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "session_archive_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #11 — §33.1 producer flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "session_archive_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #11 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "session_archive_composes_canonical_paths"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #11 — composes canonical ledger "
                "paths (.jarvis/live_fire_graduation_history "
                ".jsonl + .jarvis/graduation_ledger.jsonl + "
                ".ouroboros/sessions/); no parallel "
                "directories."
            ),
            validate=_validate_composes_canonical_paths,
        ),
    ]


__all__ = [
    "SESSION_ARCHIVE_SCHEMA_VERSION",
    "SessionArchive",
    "SessionRecord",
    "default_db_path",
    "get_default_archive",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_archive_for_tests",
]
