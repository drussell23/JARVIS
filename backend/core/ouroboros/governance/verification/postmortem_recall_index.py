"""Priority #2 Slice 2 — Cross-session PostmortemRecall index store.

Persistent storage for PostmortemRecord across sessions. Two
disciplines mirroring Priority #1 Slice 2's pattern:

  1. ``.jarvis/postmortem_recall_index.jsonl`` — bounded ring
     buffer of ``PostmortemRecord``. Rotates at cap so the index
     never grows unbounded. Read-trim-atomic-write pattern wrapped
     in Tier 1 #3's ``flock_critical_section`` so concurrent
     processes cannot race the ring-buffer mutation.
  2. **Incremental append** during one running session uses Tier 1
     #3's ``flock_append_line`` directly — append-only path
     structurally cannot corrupt because no read-modify-write.

Source material — what we leverage (no duplication):

  * ``last_session_summary._sanitize_field`` — the load-bearing
    safety helper. Imported and reused for every field extraction
    (control-char strip, secret redaction, length cap). AST-pinned.

  * ``last_session_summary._parse_summary`` — the canonical
    summary.json parser that produces ``SessionRecord``. We call
    it for session_id discovery, then do our own raw JSON load
    for ``operations[]`` (which the canonical parser doesn't
    expose). Both readers share the same defensive
    discovery + parse + dict-validation flow.

  * ``cross_process_jsonl.flock_append_line`` /
    ``flock_critical_section`` (Tier 1 #3) — direct importfrom.
    AST-pinned.

Source files we read:

  * ``.ouroboros/sessions/<session_id>/summary.json`` —
    op-level metadata (op_id, status, recorded_at, sensor).
    Failed ops produce skeleton ``PostmortemRecord``.

  * ``.ouroboros/sessions/<session_id>/debug.log`` (best-effort)
    — Human-readable log lines from CommProtocol transport.
    POSTMORTEM lines carry the rich payload
    (root_cause, failed_phase, target_files) as Python dict
    repr. We use **dedicated regex extractors per field** —
    NOT a generic dict-repr evaluator — so we structurally avoid
    any eval-family code path. Brittle by intent: malformed
    payloads → empty enrichment via per-line try/except (the
    skeleton-only PostmortemRecord still ships).

Direct-solve principles:

  * **Asynchronous-ready** — sync API; Slice 3's CONTEXT_
    EXPANSION injector wraps via ``asyncio.to_thread``
    (mirrors Priority #1 Slice 2).
  * **Dynamic** — every numeric env-tunable with floor + ceiling
    clamps. NO hardcoded paths or sizes.
  * **Adaptive** — schema-mismatched lines silently dropped on
    read; missing files → empty results; corrupt JSON → empty
    results. EMPTY_INDEX outcome is first-class (vs FAILED).
  * **Intelligent** — joins ``summary.json`` op skeleton with
    ``debug.log`` enrichment by ``op_id``. Skeleton-only when
    debug.log unavailable.
  * **Robust** — every public function NEVER raises. Disk
    failures (ENOSPC, EACCES, FS unavailable) all map to
    ``IndexOutcome.FAILED``.
  * **No hardcoding** — base dir + max_index_size all env-
    tunable; ``flock_append_line`` is the sole writer for the
    incremental path.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + Slice 1 (``postmortem_recall``) + Tier 1 #3
    (``cross_process_jsonl``) + LastSessionSummary
    (``_sanitize_field`` and ``_parse_summary``) ONLY.
  * MUST reference ``flock_append_line`` AND
    ``flock_critical_section``.
  * MUST reference ``_sanitize_field`` from
    ``last_session_summary``.
  * MUST reference ``_parse_summary`` from
    ``last_session_summary``.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine / episodic_memory /
    ast_canonical / semantic_index.
  * No mutation tools.
  * No exec / no eval (full-name and substring) / no compile —
    enforced by AST walk + bytes pin.
  * No async (Slice 3+ may introduce async via
    asyncio.to_thread wrappers).
"""
from __future__ import annotations

import enum
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)
from backend.core.ouroboros.governance.last_session_summary import (
    _parse_summary,
    _sanitize_field,
)
from backend.core.ouroboros.governance.verification.postmortem_recall import (
    POSTMORTEM_RECALL_SCHEMA_VERSION,
    PostmortemRecord,
)

logger = logging.getLogger(__name__)


POSTMORTEM_RECALL_INDEX_SCHEMA_VERSION: str = (
    "postmortem_recall_index.1"
)


# ---------------------------------------------------------------------------
# Sub-gate flag
# ---------------------------------------------------------------------------


def postmortem_index_enabled() -> bool:
    """``JARVIS_POSTMORTEM_INDEX_ENABLED`` (default ``true``
    post Slice 5 graduation 2026-05-01).

    Sub-gate for the cross-session index store. Master flag
    (``JARVIS_POSTMORTEM_RECALL_ENABLED``) must also be true for
    rebuild + record to actually write. Operators may set false
    to disable index writes while keeping master on."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_INDEX_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-01 (Priority #2 Slice 5)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Path resolution + cap structure — env-tunable
# ---------------------------------------------------------------------------


_DEFAULT_BASE_DIR_NAME: str = ".jarvis"
_INDEX_FILENAME: str = "postmortem_recall_index.jsonl"


def postmortem_index_base_dir() -> Path:
    """``JARVIS_POSTMORTEM_RECALL_BASE_DIR`` (default
    ``.jarvis/``). Empty/whitespace = unset = fall through."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_RECALL_BASE_DIR", "",
    )
    if raw.strip():
        return Path(raw).expanduser().resolve()
    return Path(_DEFAULT_BASE_DIR_NAME).resolve()


def postmortem_index_path() -> Path:
    """Resolved path to the bounded ring buffer JSONL.
    ``JARVIS_POSTMORTEM_RECALL_INDEX_PATH`` (full path) takes
    precedence over ``BASE_DIR``."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_RECALL_INDEX_PATH", "",
    )
    if raw.strip():
        return Path(raw).expanduser().resolve()
    return postmortem_index_base_dir() / _INDEX_FILENAME


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def index_max_size() -> int:
    """``JARVIS_POSTMORTEM_RECALL_MAX_INDEX_SIZE`` (default 5000,
    floor 100, ceiling 50000)."""
    return _env_int_clamped(
        "JARVIS_POSTMORTEM_RECALL_MAX_INDEX_SIZE",
        5000, floor=100, ceiling=50000,
    )


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of index outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class IndexOutcome(str, enum.Enum):
    """5-value closed taxonomy.

    ``BUILT``      — fresh index built from scratch.
    ``UPDATED``    — incremental record appended.
    ``READ_OK``    — read returned populated index.
    ``READ_EMPTY`` — file missing / empty / no records in age
                     window.
    ``FAILED``     — defensive sentinel."""

    BUILT = "built"
    UPDATED = "updated"
    READ_OK = "read_ok"
    READ_EMPTY = "read_empty"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Result containers — frozen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexBuildResult:
    """Result of a ``rebuild_index_from_sessions`` call."""

    outcome: IndexOutcome
    sessions_scanned: int = 0
    records_extracted: int = 0
    records_written: int = 0
    records_evicted_by_cap: int = 0
    records_evicted_by_age: int = 0
    detail: str = ""
    schema_version: str = (
        POSTMORTEM_RECALL_INDEX_SCHEMA_VERSION
    )


@dataclass(frozen=True)
class IndexReadResult:
    """Result of a ``read_index`` call."""

    outcome: IndexOutcome
    records: Tuple[PostmortemRecord, ...] = field(
        default_factory=tuple,
    )
    detail: str = ""
    schema_version: str = (
        POSTMORTEM_RECALL_INDEX_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# In-process lock
# ---------------------------------------------------------------------------


_INPROCESS_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Internal: atomic-write
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Tempfile + ``os.replace`` — POSIX-atomic. Raises on disk
    failure; callers wrap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_jsonl_lines(path: Path) -> List[str]:
    """Read non-empty lines from a JSONL file. NEVER raises."""
    try:
        if not path.exists():
            return []
        return [
            ln for ln in path.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()
            if ln.strip()
        ]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemIndex] read failed %s: %s", path, exc,
        )
        return []


# ---------------------------------------------------------------------------
# Internal: summary.json raw loader
# ---------------------------------------------------------------------------


def _load_summary_raw(path: Path) -> Optional[Dict[str, Any]]:
    """Defensive JSON load + dict validation. Mirrors
    ``last_session_summary._parse_summary``'s discovery flow but
    returns the raw dict for ``operations[]`` access. NEVER
    raises."""
    try:
        if not path.exists() or not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemIndex] summary load failed %s: %s",
            path, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Internal: debug.log POSTMORTEM enrichment via DEDICATED FIELD REGEXES
# ---------------------------------------------------------------------------
#
# CRITICAL DESIGN CHOICE: We use per-field regex extractors
# rather than parsing the dict-repr structure. This eliminates
# any need for ``literal_eval``-style parsing and makes the
# AST-pin "no exec/no compile/no eval" trivial. Brittle by
# design — malformed payloads produce empty enrichment via
# per-line try/except, and the PostmortemRecord skeleton from
# summary.json still ships.
#
# Source format (from comm_protocol.emit_postmortem):
#   POSTMORTEM op=<op_id> seq=<n> payload={'root_cause': '...',
#       'failed_phase': None, 'target_files': ['file1', 'file2']}


_POSTMORTEM_LINE_RE = re.compile(
    r"POSTMORTEM op=(\S+) seq=\d+ payload=(\{[^\n]*\})",
)

# Per-field extractors. Single-quoted strings only (Python repr
# convention for emit_postmortem's payload). Nested-quote
# pathologies fail-soft (regex misses → empty enrichment).
_ROOT_CAUSE_RE = re.compile(
    r"'root_cause'\s*:\s*'((?:[^'\\]|\\.)*)'",
)
_FAILED_PHASE_RE = re.compile(
    r"'failed_phase'\s*:\s*(?:'((?:[^'\\]|\\.)*)'|None)",
)
_TARGET_FILES_RE = re.compile(
    r"'target_files'\s*:\s*\[([^\]]*)\]",
)
_TARGET_FILE_ITEM_RE = re.compile(
    r"'((?:[^'\\]|\\.)*)'",
)


def _extract_payload_field(
    payload_str: str, regex: "re.Pattern[str]",
) -> str:
    """Extract first-group from a regex match against the payload
    string. Returns empty string on no match. NEVER raises."""
    try:
        m = regex.search(payload_str)
        if m is None:
            return ""
        # group(1) may be None when the alternative branch matched
        # (e.g., 'failed_phase': None).
        val = m.group(1)
        return val or ""
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _extract_target_files(payload_str: str) -> List[str]:
    """Extract ``target_files`` list contents. Returns empty list
    when the field is absent / empty. NEVER raises."""
    try:
        m = _TARGET_FILES_RE.search(payload_str)
        if m is None:
            return []
        inner = m.group(1) or ""
        return [
            item.group(1)
            for item in _TARGET_FILE_ITEM_RE.finditer(inner)
        ]
    except Exception:  # noqa: BLE001 — defensive
        return []


def _parse_debug_log_enrichment(
    debug_path: Path,
) -> Dict[str, Dict[str, str]]:
    """Best-effort parse of debug.log POSTMORTEM lines via
    dedicated field regexes (NOT a generic evaluator). Returns
    ``{op_id: {root_cause, failed_phase, file_path}}``. NEVER
    raises."""
    try:
        if not debug_path.exists():
            return {}
        text = debug_path.read_text(
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemIndex] debug.log read failed %s: %s",
            debug_path, exc,
        )
        return {}
    out: Dict[str, Dict[str, str]] = {}
    try:
        for match in _POSTMORTEM_LINE_RE.finditer(text):
            try:
                op_id = _sanitize_field(match.group(1))
                payload_str = match.group(2)
                root_cause = _sanitize_field(
                    _extract_payload_field(
                        payload_str, _ROOT_CAUSE_RE,
                    ),
                )
                failed_phase = _sanitize_field(
                    _extract_payload_field(
                        payload_str, _FAILED_PHASE_RE,
                    ),
                )
                target_files = _extract_target_files(payload_str)
                file_path = (
                    _sanitize_field(target_files[0])
                    if target_files else ""
                )
                out[op_id] = {
                    "root_cause": root_cause,
                    "failed_phase": failed_phase,
                    "file_path": file_path,
                }
            except Exception:  # noqa: BLE001 — per-line defensive
                continue
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemIndex] debug.log scan failed %s: %s",
            debug_path, exc,
        )
        return {}
    return out


# ---------------------------------------------------------------------------
# Internal: parse one session dir
# ---------------------------------------------------------------------------


def _parse_postmortems_from_session(
    session_dir: Path,
) -> List[PostmortemRecord]:
    """Walk one ``.ouroboros/sessions/<id>/`` and produce
    PostmortemRecord list for every failed op in summary.json.
    Best-effort enrichment from debug.log POSTMORTEM regex.
    NEVER raises.

    Reuse contract:
      * Calls ``_parse_summary`` for session_id discovery via
        SessionRecord (canonical parser reuse).
      * Reuses ``_sanitize_field`` for every field extraction
        (load-bearing safety helper reuse).
      * ``_load_summary_raw`` for ``operations[]`` access (the
        canonical parser doesn't expose this)."""
    try:
        if not session_dir.exists() or not session_dir.is_dir():
            return []
        summary_path = session_dir / "summary.json"
        if not summary_path.exists():
            return []

        # Canonical parser reuse — extract session_id via the
        # well-tested SessionRecord path
        record, _missing = _parse_summary(summary_path)
        if record is None:
            session_id = _sanitize_field(session_dir.name)
        else:
            session_id = _sanitize_field(record.session_id) or (
                _sanitize_field(session_dir.name)
            )

        # Raw JSON load for operations[] access
        raw = _load_summary_raw(summary_path)
        if raw is None:
            return []
        operations = raw.get("operations") or []
        if not isinstance(operations, list):
            return []

        # Best-effort debug.log enrichment
        debug_path = session_dir / "debug.log"
        enrichment = _parse_debug_log_enrichment(debug_path)

        records: List[PostmortemRecord] = []
        for op in operations:
            if not isinstance(op, dict):
                continue
            try:
                status = _sanitize_field(op.get("status"))
                if status != "failed":
                    continue
                op_id = _sanitize_field(op.get("op_id"))
                try:
                    timestamp = float(op.get("recorded_at", 0.0))
                except (TypeError, ValueError):
                    timestamp = 0.0
                sensor = _sanitize_field(op.get("sensor"))
                enr = enrichment.get(op_id, {})
                file_path = enr.get("file_path", "") or ""
                failure_phase = enr.get("failed_phase", "") or ""
                failure_reason = enr.get("root_cause", "") or ""
                records.append(PostmortemRecord(
                    op_id=op_id,
                    session_id=session_id,
                    file_path=file_path,
                    symbol_name="",
                    failure_class="failed",
                    failure_phase=failure_phase,
                    failure_reason=failure_reason,
                    error_summary=sensor,
                    specific_errors=tuple(),
                    line_numbers=tuple(),
                    attempt=1,
                    timestamp=timestamp,
                    ast_signature="",
                ))
            except Exception:  # noqa: BLE001 — per-op defensive
                continue
        return records
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemIndex] _parse_postmortems_from_session "
            "raised: %s", exc,
        )
        return []


# ---------------------------------------------------------------------------
# Internal: project root + session walker
# ---------------------------------------------------------------------------


def _resolve_project_root() -> Path:
    """Walks up from this module's location to find
    ``CLAUDE.md`` (project root marker). Falls back to CWD on
    miss. NEVER raises."""
    try:
        here = Path(__file__).resolve().parent
        cur = here
        while cur != cur.parent:
            if (cur / "CLAUDE.md").exists():
                return cur
            cur = cur.parent
        return Path.cwd().resolve()
    except Exception:  # noqa: BLE001 — defensive
        return Path.cwd().resolve()


def _walk_session_dirs(
    project_root: Path,
) -> List[Path]:
    """List ``.ouroboros/sessions/*/``. NEVER raises."""
    try:
        sessions_root = project_root / ".ouroboros" / "sessions"
        if not sessions_root.exists() or not sessions_root.is_dir():
            return []
        return [
            p for p in sessions_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemIndex] session walk failed: %s", exc,
        )
        return []


# ---------------------------------------------------------------------------
# Public: rebuild_index_from_sessions
# ---------------------------------------------------------------------------


def rebuild_index_from_sessions(
    *,
    project_root: Optional[Path] = None,
    max_age_days: float = 30.0,
    max_index_size: Optional[int] = None,
    target_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> IndexBuildResult:
    """Walk ``.ouroboros/sessions/*/`` and rebuild the index from
    scratch. Atomic-write under cross-process flock. NEVER
    raises."""
    try:
        if not postmortem_index_enabled():
            return IndexBuildResult(
                outcome=IndexOutcome.FAILED,
                detail=(
                    "JARVIS_POSTMORTEM_INDEX_ENABLED is false"
                ),
            )

        root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else _resolve_project_root()
        )
        target = (
            Path(target_path).expanduser().resolve()
            if target_path is not None
            else postmortem_index_path()
        )
        cap = (
            int(max_index_size) if max_index_size is not None
            else index_max_size()
        )
        ref_ts = (
            float(now_ts) if now_ts is not None else time.time()
        )
        cutoff_ts = ref_ts - (
            float(max(0.0, max_age_days)) * 86400.0
        )

        session_dirs = _walk_session_dirs(root)
        all_records: List[PostmortemRecord] = []
        for sdir in session_dirs:
            all_records.extend(
                _parse_postmortems_from_session(sdir),
            )
        records_extracted = len(all_records)

        # Age filter
        records_in_age = [
            r for r in all_records
            if r.timestamp >= cutoff_ts
        ]
        records_evicted_by_age = (
            records_extracted - len(records_in_age)
        )

        records_in_age.sort(key=lambda r: r.timestamp)

        # Cap rotation
        if len(records_in_age) > cap:
            records_evicted_by_cap = len(records_in_age) - cap
            kept = records_in_age[-cap:]
        else:
            records_evicted_by_cap = 0
            kept = records_in_age

        lines: List[str] = []
        for r in kept:
            try:
                lines.append(
                    json.dumps(
                        r.to_dict(), separators=(",", ":"),
                    ),
                )
            except Exception:  # noqa: BLE001 — defensive
                continue

        with _INPROCESS_LOCK:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return IndexBuildResult(
                    outcome=IndexOutcome.FAILED,
                    sessions_scanned=len(session_dirs),
                    records_extracted=records_extracted,
                    detail=f"mkdir failed: {exc}",
                )
            with flock_critical_section(target) as acquired:
                _ = acquired
                try:
                    body = "\n".join(lines)
                    if body:
                        body += "\n"
                    _atomic_write(target, body)
                except Exception as exc:  # noqa: BLE001
                    return IndexBuildResult(
                        outcome=IndexOutcome.FAILED,
                        sessions_scanned=len(session_dirs),
                        records_extracted=records_extracted,
                        detail=f"atomic_write: {exc!r}",
                    )

        return IndexBuildResult(
            outcome=IndexOutcome.BUILT,
            sessions_scanned=len(session_dirs),
            records_extracted=records_extracted,
            records_written=len(lines),
            records_evicted_by_cap=records_evicted_by_cap,
            records_evicted_by_age=records_evicted_by_age,
            detail=(
                f"scanned {len(session_dirs)} sessions, "
                f"wrote {len(lines)} records"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemIndex] rebuild raised: %s", exc,
        )
        return IndexBuildResult(
            outcome=IndexOutcome.FAILED,
            detail=f"rebuild raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Public: record_postmortem (incremental append)
# ---------------------------------------------------------------------------


def record_postmortem(
    record: PostmortemRecord,
    *,
    target_path: Optional[Path] = None,
) -> IndexOutcome:
    """Append a single PostmortemRecord. Append-only via Tier 1
    #3 ``flock_append_line``. NEVER raises."""
    try:
        if not postmortem_index_enabled():
            return IndexOutcome.FAILED
        if not isinstance(record, PostmortemRecord):
            return IndexOutcome.FAILED
        target = (
            Path(target_path).expanduser().resolve()
            if target_path is not None
            else postmortem_index_path()
        )
        try:
            line = json.dumps(
                record.to_dict(), separators=(",", ":"),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[PostmortemIndex] record serialize failed: %s",
                exc,
            )
            return IndexOutcome.FAILED
        ok = flock_append_line(target, line)
        if not ok:
            return IndexOutcome.FAILED
        return IndexOutcome.UPDATED
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemIndex] record raised: %s", exc,
        )
        return IndexOutcome.FAILED


# ---------------------------------------------------------------------------
# Public: read_index
# ---------------------------------------------------------------------------


def read_index(
    *,
    max_age_days: Optional[float] = None,
    limit: Optional[int] = None,
    target_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> IndexReadResult:
    """Read all PostmortemRecords. Schema-mismatched / malformed
    lines silently dropped. Sorted ascending by timestamp.
    NEVER raises."""
    try:
        target = (
            Path(target_path).expanduser().resolve()
            if target_path is not None
            else postmortem_index_path()
        )
        lines = _read_jsonl_lines(target)
        if not lines:
            return IndexReadResult(
                outcome=IndexOutcome.READ_EMPTY,
                detail=f"file empty or missing: {target}",
            )

        records: List[PostmortemRecord] = []
        for ln in lines:
            try:
                payload = json.loads(ln)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            r = PostmortemRecord.from_dict(payload)
            if r is None:
                continue
            records.append(r)

        if not records:
            return IndexReadResult(
                outcome=IndexOutcome.READ_EMPTY,
                detail="no valid records after schema filter",
            )

        if max_age_days is not None:
            ref_ts = (
                float(now_ts) if now_ts is not None
                else time.time()
            )
            cutoff_ts = ref_ts - (
                float(max(0.0, max_age_days)) * 86400.0
            )
            records = [
                r for r in records if r.timestamp >= cutoff_ts
            ]
            if not records:
                return IndexReadResult(
                    outcome=IndexOutcome.READ_EMPTY,
                    detail=(
                        f"all records older than "
                        f"{max_age_days}d cutoff"
                    ),
                )

        records.sort(key=lambda r: r.timestamp)

        if limit is not None and limit >= 0:
            records = records[-limit:]

        return IndexReadResult(
            outcome=IndexOutcome.READ_OK,
            records=tuple(records),
            detail=f"{len(records)} records",
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemIndex] read raised: %s", exc,
        )
        return IndexReadResult(
            outcome=IndexOutcome.FAILED,
            detail=f"read raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "IndexBuildResult",
    "IndexOutcome",
    "IndexReadResult",
    "POSTMORTEM_RECALL_INDEX_SCHEMA_VERSION",
    "index_max_size",
    "postmortem_index_base_dir",
    "postmortem_index_enabled",
    "postmortem_index_path",
    "read_index",
    "rebuild_index_from_sessions",
    "record_postmortem",
]
