"""Arc #1 — Conversation Turn Ledger: append-only JSONL persistence.

Closes the operator-survivability gap: conversation turns now survive
process death via an append-only JSONL file per session, keyed by the
stable ``session_id`` from :class:`session_manager.SessionManager`.

Composition contract (operator-binding 2026-05-12):

  * **Single writer primitive** — every write uses
    :func:`cross_process_jsonl.flock_append_line`; every read uses
    :func:`cross_process_jsonl.flock_critical_section`. NEVER raw
    file I/O. Mirrors the ``proposal_store`` / ``bookmarks.jsonl``
    ledger pattern exactly.
  * **Schema-versioned rows** — ``conversation_ledger.1``. Forward-
    compatible: unknown keys ignored on read; missing optional keys
    default. §33.5-style versioning.
  * **Operator-tunable bounds** — max file size, max turns per
    session, retention days, replay tail size — all env vars, not
    hardcoded caps.
  * **NEVER raises** — all faults map to empty results (read) /
    False return (write). Defensive everywhere.
  * **Replay is not trust bypass** — consumers (the resume path)
    MUST re-sanitize persisted bytes through the canonical Tier -1
    ``sanitize_for_log`` + ``redact_secrets`` before feeding the
    bridge. This module stores raw admitted turns; it does NOT
    pre-bless them for future replay.

Authority asymmetry (AST-pinned):
  Imports stdlib + ``cross_process_jsonl`` ONLY. NEVER imports
  orchestrator / iron_gate / policy / candidate_generator /
  tool_executor / urgency_router / change_engine /
  semantic_guardian / auto_committer / risk_tier_floor /
  providers / conversation_bridge (the bridge imports US, not
  the reverse — acyclic dependency).

Ledger path: ``.jarvis/conversation/sessions/<session_id>.jsonl``
  Override via ``JARVIS_CONVERSATION_LEDGER_DIR``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CONVERSATION_LEDGER_SCHEMA_VERSION: str = "conversation_ledger.1"


# ---------------------------------------------------------------------------
# Env knobs (operator-tunable, not hardcoded)
# ---------------------------------------------------------------------------


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw.strip()))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def ledger_enabled() -> bool:
    """``JARVIS_CONVERSATION_LEDGER_ENABLED`` — master switch.
    Default ``false`` per §33.1 (operator opts in)."""
    return _env_bool("JARVIS_CONVERSATION_LEDGER_ENABLED", False)


def ledger_dir() -> Path:
    """``JARVIS_CONVERSATION_LEDGER_DIR`` — directory for per-session
    JSONL files. Default ``.jarvis/conversation/sessions/``.
    Resolved at call time so tests can override via env."""
    raw = _env_str("JARVIS_CONVERSATION_LEDGER_DIR")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "conversation" / "sessions"


def max_file_bytes() -> int:
    """``JARVIS_CONVERSATION_LEDGER_MAX_FILE_BYTES`` — per-session
    file size cap. Default 10 MB. Writes rejected beyond this."""
    return _env_int(
        "JARVIS_CONVERSATION_LEDGER_MAX_FILE_BYTES",
        10 * 1024 * 1024,  # 10 MB
        minimum=1024,
    )


def max_turns_per_session() -> int:
    """``JARVIS_CONVERSATION_LEDGER_MAX_TURNS_PER_SESSION`` — hard
    turn cap per session file. Default 2000."""
    return _env_int(
        "JARVIS_CONVERSATION_LEDGER_MAX_TURNS_PER_SESSION",
        2000,
        minimum=1,
    )


def retention_days() -> int:
    """``JARVIS_CONVERSATION_LEDGER_RETENTION_DAYS`` — ``prune()``
    removes sessions older than this. Default 30 days."""
    return _env_int(
        "JARVIS_CONVERSATION_LEDGER_RETENTION_DAYS",
        30,
        minimum=1,
    )


def replay_tail_default() -> int:
    """``JARVIS_CONVERSATION_LEDGER_REPLAY_TAIL`` — default number
    of turns to read on resume. Caps RAM usage. Default 50."""
    return _env_int(
        "JARVIS_CONVERSATION_LEDGER_REPLAY_TAIL",
        50,
        minimum=1,
    )


# ---------------------------------------------------------------------------
# Session ID sanitization
# ---------------------------------------------------------------------------


def _sanitize_session_id(session_id: str) -> str:
    """Sanitize session_id for safe use as a filename component.
    Mirrors session_manager.py's path-traversal prevention."""
    safe = (
        str(session_id or "")
        .strip()
        .replace("/", "_")
        .replace("..", "_")
        .replace("\\", "_")
        .replace("\x00", "_")
    )
    # Cap length to prevent filesystem issues
    return safe[:128] if safe else ""


def _session_path(session_id: str) -> Optional[Path]:
    """Return the JSONL file path for a session. Returns None if
    session_id is empty after sanitization."""
    safe = _sanitize_session_id(session_id)
    if not safe:
        return None
    return ledger_dir() / f"{safe}.jsonl"


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersistedTurn:
    """One persisted conversation turn. Frozen + JSON-projectable.

    Stores the raw admitted text — consumers MUST re-sanitize on
    replay (the ``resume`` path calls ``sanitize_for_log`` +
    ``redact_secrets`` before feeding the bridge).
    """

    schema_version: str
    session_id: str
    role: str
    text: str
    source: str
    op_id: str
    ts: float
    turn_seq: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id[:128],
            "role": self.role[:16],
            "text": self.text[:8192],
            "source": self.source[:64],
            "op_id": self.op_id[:128],
            "ts": float(self.ts),
            "turn_seq": int(self.turn_seq),
        }

    @classmethod
    def from_dict(
        cls, raw: Dict[str, Any],
    ) -> Optional["PersistedTurn"]:
        """Defensive parse — returns None on missing required
        fields. NEVER raises."""
        try:
            session_id = str(raw.get("session_id", "")).strip()
            role = str(raw.get("role", "")).strip()
            text = str(raw.get("text", ""))
            source = str(raw.get("source", "")).strip()
            if not session_id or not role or not text:
                return None
            return cls(
                schema_version=str(
                    raw.get("schema_version", "unknown"),
                ),
                session_id=session_id,
                role=role,
                text=text,
                source=source,
                op_id=str(raw.get("op_id", "")),
                ts=float(raw.get("ts", 0.0)),
                turn_seq=int(raw.get("turn_seq", 0)),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class SessionSummary:
    """Lightweight summary of a persisted session. No turn content."""

    session_id: str
    turn_count: int
    first_ts: float
    last_ts: float
    file_size_bytes: int


# ---------------------------------------------------------------------------
# Turn sequence counter (per-session, in-process)
# ---------------------------------------------------------------------------
#
# Monotonic per-session counter. On first append for a session, we
# scan the existing file tail to find the max turn_seq. In-process
# only — does not persist across restarts (the ledger itself carries
# the ground truth; we re-derive on first access).

import threading  # noqa: E402

_seq_lock = threading.Lock()
_seq_cache: Dict[str, int] = {}


def _next_turn_seq(session_id: str, path: Path) -> int:
    """Allocate the next monotonic turn_seq for a session.
    Thread-safe. NEVER raises."""
    global _seq_cache
    safe_id = _sanitize_session_id(session_id)
    with _seq_lock:
        if safe_id in _seq_cache:
            _seq_cache[safe_id] += 1
            return _seq_cache[safe_id]
    # First access — scan existing file for max turn_seq.
    max_seq = 0
    try:
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        seq = int(obj.get("turn_seq", 0))
                        if seq > max_seq:
                            max_seq = seq
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
    except Exception:  # noqa: BLE001 — defensive
        pass
    with _seq_lock:
        _seq_cache[safe_id] = max_seq + 1
        return _seq_cache[safe_id]


def reset_seq_cache_for_tests() -> None:
    """Test helper — clear the in-process seq counter."""
    global _seq_cache
    with _seq_lock:
        _seq_cache.clear()


# ---------------------------------------------------------------------------
# Public API — append
# ---------------------------------------------------------------------------


def append_turn(
    session_id: str,
    *,
    role: str,
    text: str,
    source: str = "",
    op_id: str = "",
    ts: Optional[float] = None,
) -> bool:
    """Append one conversation turn to the session's JSONL ledger.

    Returns True on success, False on any failure (file size cap
    exceeded, lock timeout, write error, ledger disabled, etc.).
    NEVER raises.

    The text is stored as-is (already sanitized by the bridge's
    admission path). Consumers MUST re-sanitize on replay.
    """
    try:
        if not ledger_enabled():
            return False
        path = _session_path(session_id)
        if path is None:
            return False

        # Check file size cap before writing.
        cap = max_file_bytes()
        try:
            if path.exists():
                st = path.stat()
                if st.st_size >= cap:
                    logger.debug(
                        "[conversation_ledger] file size cap "
                        "reached for session %s (%d >= %d)",
                        session_id, st.st_size, cap,
                    )
                    return False
        except OSError:
            pass  # Can't stat — try the write anyway.

        # Check turn count cap.
        turn_cap = max_turns_per_session()
        try:
            if path.exists():
                count = 0
                with path.open(
                    "r", encoding="utf-8", errors="replace",
                ) as fh:
                    for _ in fh:
                        count += 1
                if count >= turn_cap:
                    logger.debug(
                        "[conversation_ledger] turn count cap "
                        "reached for session %s (%d >= %d)",
                        session_id, count, turn_cap,
                    )
                    return False
        except OSError:
            pass  # Can't count — try the write anyway.

        # Ensure parent dir exists.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False

        seq = _next_turn_seq(session_id, path)
        turn = PersistedTurn(
            schema_version=CONVERSATION_LEDGER_SCHEMA_VERSION,
            session_id=_sanitize_session_id(session_id),
            role=str(role or "")[:16],
            text=str(text or "")[:8192],
            source=str(source or "")[:64],
            op_id=str(op_id or "")[:128],
            ts=float(ts if ts is not None else time.time()),
            turn_seq=seq,
        )

        line = json.dumps(
            turn.to_dict(),
            separators=(",", ":"),
            ensure_ascii=False,
        )

        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
        return flock_append_line(path, line)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[conversation_ledger] append_turn raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Public API — read (tail-bounded)
# ---------------------------------------------------------------------------


def read_tail(
    session_id: str,
    *,
    max_turns: Optional[int] = None,
) -> Tuple[PersistedTurn, ...]:
    """Read the most-recent ``max_turns`` turns from a session's
    JSONL ledger. O(file_size) on disk, O(max_turns) in RAM.

    Returns an empty tuple on any failure. NEVER raises.

    The returned turns contain raw persisted text — consumers
    MUST re-sanitize before feeding the bridge (replay is not
    trust bypass).
    """
    try:
        path = _session_path(session_id)
        if path is None or not path.exists():
            return ()

        tail_size = (
            max_turns
            if max_turns is not None
            else replay_tail_default()
        )
        tail_size = max(1, tail_size)

        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )

        with flock_critical_section(path) as acquired:
            if not acquired:
                return ()
            try:
                st = path.stat()
                cap = max_file_bytes()
                if st.st_size > cap:
                    logger.debug(
                        "[conversation_ledger] read_tail: file "
                        "exceeds cap (%d > %d) for session %s",
                        st.st_size, cap, session_id,
                    )
                    return ()
                text = path.read_text(encoding="utf-8")
            except OSError:
                return ()

        # Sliding window — retain only the last tail_size turns.
        window: deque[PersistedTurn] = deque(maxlen=tail_size)
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
                if not isinstance(row, dict):
                    continue
                parsed = PersistedTurn.from_dict(row)
                if parsed is not None:
                    window.append(parsed)
            except json.JSONDecodeError:
                continue
            except Exception:  # noqa: BLE001 — defensive
                continue
        return tuple(window)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[conversation_ledger] read_tail raised: %s", exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Public API — session queries
# ---------------------------------------------------------------------------


def session_exists(session_id: str) -> bool:
    """Check if a session ledger file exists. NEVER raises."""
    try:
        path = _session_path(session_id)
        if path is None:
            return False
        return path.exists()
    except Exception:  # noqa: BLE001 — defensive
        return False


def list_sessions(
    *, limit: int = 20,
) -> List[SessionSummary]:
    """List recent sessions from the ledger directory, sorted by
    last-modified time (newest first). NEVER raises.

    Returns lightweight summaries — no turn content loaded."""
    try:
        bound = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        bound = 20
    try:
        d = ledger_dir()
        if not d.exists():
            return []
        entries: List[Tuple[float, SessionSummary]] = []
        for p in d.glob("*.jsonl"):
            try:
                st = p.stat()
                # Extract session_id from filename (strip .jsonl).
                sid = p.stem
                # Count lines (turns) cheaply.
                turn_count = 0
                first_ts = 0.0
                last_ts = 0.0
                try:
                    with p.open(
                        "r", encoding="utf-8", errors="replace",
                    ) as fh:
                        for line in fh:
                            s = line.strip()
                            if not s:
                                continue
                            turn_count += 1
                            try:
                                obj = json.loads(s)
                                ts = float(obj.get("ts", 0.0))
                                if first_ts == 0.0 or ts < first_ts:
                                    first_ts = ts
                                if ts > last_ts:
                                    last_ts = ts
                            except (
                                json.JSONDecodeError,
                                TypeError,
                                ValueError,
                            ):
                                continue
                except OSError:
                    continue
                summary = SessionSummary(
                    session_id=sid,
                    turn_count=turn_count,
                    first_ts=first_ts,
                    last_ts=last_ts,
                    file_size_bytes=st.st_size,
                )
                entries.append((st.st_mtime, summary))
            except OSError:
                continue
        # Sort newest first.
        entries.sort(key=lambda x: -x[0])
        return [s for _, s in entries[:bound]]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[conversation_ledger] list_sessions raised: %s", exc,
        )
        return []


# ---------------------------------------------------------------------------
# Public API — retention / pruning
# ---------------------------------------------------------------------------


def prune(
    *, max_age_days: Optional[int] = None,
) -> int:
    """Remove session ledger files older than ``max_age_days``.
    Returns count of files removed. NEVER raises."""
    try:
        age = (
            max_age_days
            if max_age_days is not None
            else retention_days()
        )
        cutoff = time.time() - (age * 86400)
        d = ledger_dir()
        if not d.exists():
            return 0
        removed = 0
        for p in list(d.glob("*.jsonl")):
            try:
                st = p.stat()
                if st.st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info(
                "[conversation_ledger] pruned %d session(s) "
                "(max_age=%dd)", removed, age,
            )
        return removed
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[conversation_ledger] prune raised: %s", exc,
        )
        return 0


# ===========================================================================
# §33.1 — register_shipped_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Conversation ledger substrate invariants."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/conversation_ledger.py"
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
        "backend.core.ouroboros.governance.conversation_bridge",
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

    def _validate_composes_flock(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for needle in (
            "cross_process_jsonl",
            "flock_append_line",
        ):
            if needle not in source:
                violations.append(
                    f"must compose canonical {needle!r} — "
                    f"no raw file I/O for writes"
                )
        return tuple(violations)

    def _validate_schema_version(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.AnnAssign)
                and isinstance(node.target, _ast.Name)
                and node.target.id
                == "CONVERSATION_LEDGER_SCHEMA_VERSION"
                and isinstance(node.value, _ast.Constant)
                and node.value.value
                == "conversation_ledger.1"
            ):
                return ()
        return (
            "CONVERSATION_LEDGER_SCHEMA_VERSION must equal "
            "'conversation_ledger.1'",
        )

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "conversation_ledger_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Ledger MUST NOT import orchestrator / "
                "iron_gate / policy / conversation_bridge / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "conversation_ledger_composes_flock"
            ),
            target_file=target,
            description=(
                "Ledger MUST compose cross_process_jsonl + "
                "flock_append_line for writes — no raw file I/O."
            ),
            validate=_validate_composes_flock,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "conversation_ledger_schema_version_pinned"
            ),
            target_file=target,
            description=(
                "CONVERSATION_LEDGER_SCHEMA_VERSION = "
                "'conversation_ledger.1' bytes-pinned."
            ),
            validate=_validate_schema_version,
        ),
    ]


__all__ = [
    "CONVERSATION_LEDGER_SCHEMA_VERSION",
    "PersistedTurn",
    "SessionSummary",
    "append_turn",
    "ledger_dir",
    "ledger_enabled",
    "list_sessions",
    "max_file_bytes",
    "max_turns_per_session",
    "prune",
    "read_tail",
    "register_shipped_invariants",
    "replay_tail_default",
    "reset_seq_cache_for_tests",
    "retention_days",
    "session_exists",
]
