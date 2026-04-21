"""
SessionRecord — Slice 1 of the Session History Browser arc.
============================================================

Read-only frozen metadata for ONE past Ouroboros session. Parses:

* ``<sessions_root>/<session-id>/summary.json`` — canonical key/value
  per-session summary (matches the :mod:`last_session_summary`
  conventions already in use).
* ``<sessions_root>/<session-id>/debug.log`` header — first few
  lines for a timestamp anchor (optional, best-effort).
* Filesystem metadata — mtime, size-on-disk, replay HTML presence.

Slice 1 scope
-------------

* Pure-code parsing; no LLM, no network, no subprocess.
* Fail-closed. Corrupt ``summary.json`` → record marked
  ``parse_error=true``, entry still listable. Missing file → record
  with ``summary_missing=true``.
* Immutable. :class:`SessionRecord` is a frozen dataclass; projection
  helpers never mutate the record.

What this module is NOT
-----------------------

* A scanner. Slice 2's :class:`SessionIndex` walks the root.
* A browser. Slice 3's :class:`SessionBrowser` handles navigation +
  bookmarks + replay HTML lookup.
* A REPL. Slice 4 ships ``/session``.

Manifesto alignment
-------------------

* §1 read-only. A record is a view; it can't mutate the session it
  describes.
* §4 Privacy. Never echoes raw debug.log bodies — only structured
  fields + bounded previews.
* §7 fail-closed. Never raises on malformed inputs; always produces
  a record with some sentinel fields.
* §8 observable. Every parse error path logs a WARNING with the
  session-id and the reason, so operators can grep their browse
  history for silent data loss.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.SessionRecord")


SESSION_RECORD_SCHEMA_VERSION: str = "session_record.v1"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


_DEFAULT_SESSIONS_ROOT = Path(".ouroboros/sessions")
_SUMMARY_FILENAME = "summary.json"
_DEBUG_LOG_FILENAME = "debug.log"
_REPLAY_HTML_NAMES = ("replay.html", "session_replay.html", "playback.html")


def default_sessions_root() -> Path:
    """Env-tunable root. Defaults to ``.ouroboros/sessions`` under CWD."""
    env = os.environ.get("JARVIS_OUROBOROS_SESSIONS_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(".ouroboros/sessions").resolve()


# ---------------------------------------------------------------------------
# Sanitisation helpers — mirror last_session_summary conventions
# ---------------------------------------------------------------------------


_SESSION_ID_RX = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


def _looks_like_session_dir(path: Path) -> bool:
    """Minimal shape check — directory + name matching the session regex."""
    if not path.is_dir():
        return False
    name = path.name
    return bool(_SESSION_ID_RX.match(name))


def _sanitise_str(value: Any, *, max_len: int = 500) -> str:
    """Coerce to string + truncate. Never raises."""
    if value is None:
        return ""
    try:
        s = str(value)
    except Exception:  # noqa: BLE001
        return ""
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _sanitise_int(value: Any, *, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _sanitise_float(value: Any, *, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# SessionRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """Metadata projection of one session on disk.

    Every field has a safe default so a record can be built even for
    a session directory with no / corrupt summary.
    """

    # --- identity --------------------------------------------------------
    session_id: str = ""
    path: str = ""

    # --- provenance flags -----------------------------------------------
    summary_found: bool = False
    parse_error: bool = False
    parse_error_reason: str = ""

    # --- summary-derived ------------------------------------------------
    stop_reason: str = ""
    started_at_iso: str = ""
    ended_at_iso: str = ""
    duration_s: float = 0.0

    ops_total: int = 0
    ops_applied: int = 0
    ops_verified_pass: int = 0
    ops_verified_total: int = 0

    cost_spent_usd: float = 0.0
    cost_budget_usd: Optional[float] = None

    commit_hash: str = ""           # short (10 char) if present
    schema_version_summary: str = ""  # summary.json's own version

    # --- filesystem metadata --------------------------------------------
    on_disk_bytes: int = 0
    mtime_ts: float = 0.0
    mtime_iso: str = ""
    has_debug_log: bool = False
    has_replay_html: bool = False
    replay_html_path: str = ""

    # --- debug.log preview ----------------------------------------------
    debug_log_head_lines: Tuple[str, ...] = ()

    schema_version: str = SESSION_RECORD_SCHEMA_VERSION

    # --- convenience ----------------------------------------------------

    @property
    def short_session_id(self) -> str:
        if len(self.session_id) <= 16:
            return self.session_id
        return self.session_id[:16] + "…"

    @property
    def ok_outcome(self) -> bool:
        """Heuristic: session *probably* ran cleanly."""
        return (
            self.summary_found
            and not self.parse_error
            and self.ops_total > 0
            and (
                self.stop_reason in ("", "idle_timeout", "cost_cap", "complete")
            )
        )

    # --- projection (SSE / IDE) ----------------------------------------

    def project(self) -> Dict[str, Any]:
        """JSON-safe bounded projection."""
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "short_session_id": self.short_session_id,
            "path": self.path,
            "summary_found": self.summary_found,
            "parse_error": self.parse_error,
            "parse_error_reason": self.parse_error_reason,
            "stop_reason": self.stop_reason,
            "started_at_iso": self.started_at_iso,
            "ended_at_iso": self.ended_at_iso,
            "duration_s": self.duration_s,
            "ops_total": self.ops_total,
            "ops_applied": self.ops_applied,
            "ops_verified_pass": self.ops_verified_pass,
            "ops_verified_total": self.ops_verified_total,
            "cost_spent_usd": self.cost_spent_usd,
            "cost_budget_usd": self.cost_budget_usd,
            "commit_hash": self.commit_hash[:10],
            "on_disk_bytes": self.on_disk_bytes,
            "mtime_iso": self.mtime_iso,
            "has_debug_log": self.has_debug_log,
            "has_replay_html": self.has_replay_html,
            "ok_outcome": self.ok_outcome,
            "debug_log_head_lines_count": len(self.debug_log_head_lines),
        }

    # --- narrative ------------------------------------------------------

    def one_line_summary(self) -> str:
        """Compact single-line rendering for list output.

        Example::

            bt-2026-04-21-100000  ok  ops=12 applied=5 verify=5/5 $0.243 (2m)
        """
        if not self.session_id:
            return "<unnamed session>"
        flags: List[str] = []
        if self.parse_error:
            flags.append("PARSE-ERROR")
        elif not self.summary_found:
            flags.append("no-summary")
        elif self.ok_outcome:
            flags.append("ok")
        else:
            flags.append(self.stop_reason or "unknown")
        parts: List[str] = [self.short_session_id, "|".join(flags)]
        if self.ops_total:
            verify_str = (
                f"{self.ops_verified_pass}/{self.ops_verified_total}"
                if self.ops_verified_total else "-"
            )
            parts.append(
                f"ops={self.ops_total} applied={self.ops_applied} "
                f"verify={verify_str}"
            )
        if self.cost_spent_usd > 0 or self.cost_budget_usd is not None:
            budget_str = (
                f" / ${self.cost_budget_usd:.2f}"
                if self.cost_budget_usd is not None else ""
            )
            parts.append(f"${self.cost_spent_usd:.3f}{budget_str}")
        if self.duration_s > 0:
            parts.append(f"({_fmt_duration(self.duration_s)})")
        if self.has_replay_html:
            parts.append("replay")
        return "  ".join(parts)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class SessionParseError(Exception):
    """Raised by the PRIVATE parser helpers; callers return sentinels."""


def parse_session_dir(
    session_dir: Path,
    *,
    debug_log_head_lines: int = 3,
) -> SessionRecord:
    """Parse one session directory into a :class:`SessionRecord`.

    Never raises. Malformed / missing inputs → record fields become
    sentinels (``summary_found=False`` / ``parse_error=True``).
    """
    session_dir = Path(session_dir)
    if not session_dir.exists() or not session_dir.is_dir():
        logger.warning(
            "[SessionRecord] not a directory: %s", session_dir,
        )
        return SessionRecord(
            session_id=session_dir.name if session_dir.name else "",
            path=str(session_dir),
            parse_error=True,
            parse_error_reason="not_a_directory",
        )

    session_id = session_dir.name
    if not _SESSION_ID_RX.match(session_id):
        logger.warning(
            "[SessionRecord] session id fails regex: %r", session_id,
        )
        return SessionRecord(
            session_id=session_id,
            path=str(session_dir),
            parse_error=True,
            parse_error_reason="bad_session_id_format",
        )

    summary_path = session_dir / _SUMMARY_FILENAME
    debug_log_path = session_dir / _DEBUG_LOG_FILENAME
    has_debug_log = debug_log_path.exists()

    # filesystem metadata
    try:
        stat = session_dir.stat()
        mtime_ts = stat.st_mtime
        mtime_iso = datetime.fromtimestamp(
            mtime_ts, tz=timezone.utc,
        ).replace(microsecond=0).isoformat()
    except OSError:
        mtime_ts = 0.0
        mtime_iso = ""

    on_disk_bytes = _dir_size(session_dir)

    # replay html
    replay_html_path = ""
    has_replay_html = False
    for name in _REPLAY_HTML_NAMES:
        candidate = session_dir / name
        if candidate.exists():
            has_replay_html = True
            replay_html_path = str(candidate)
            break

    # summary.json
    summary: Optional[Mapping[str, Any]] = None
    summary_found = False
    parse_error = False
    parse_error_reason = ""
    if summary_path.exists():
        summary_found = True
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            parse_error = True
            parse_error_reason = f"json_decode_error:{exc.msg[:100]}"
            logger.warning(
                "[SessionRecord] %s summary.json decode error: %s",
                session_id, exc,
            )
        except OSError as exc:
            parse_error = True
            parse_error_reason = f"os_error:{exc.errno}"
            logger.warning(
                "[SessionRecord] %s summary.json os error: %s",
                session_id, exc,
            )
        if summary is not None and not isinstance(summary, Mapping):
            parse_error = True
            parse_error_reason = (
                f"summary_not_a_mapping:{type(summary).__name__}"
            )
            summary = None

    # stats extraction — tolerant of missing keys / wrong types
    stop_reason = ""
    started_at_iso = ""
    ended_at_iso = ""
    duration_s = 0.0
    ops_total = 0
    ops_applied = 0
    ops_verified_pass = 0
    ops_verified_total = 0
    cost_spent_usd = 0.0
    cost_budget_usd: Optional[float] = None
    commit_hash = ""
    schema_version_summary = ""

    if summary is not None:
        stop_reason = _sanitise_str(summary.get("stop_reason"), max_len=120)
        started_at_iso = _sanitise_str(
            summary.get("started_at") or summary.get("started_at_iso"),
            max_len=64,
        )
        ended_at_iso = _sanitise_str(
            summary.get("ended_at") or summary.get("ended_at_iso"),
            max_len=64,
        )
        duration_s = _sanitise_float(summary.get("duration_s"))
        stats = summary.get("stats") or {}
        if isinstance(stats, Mapping):
            ops_total = _sanitise_int(stats.get("ops_total"))
            ops_applied = _sanitise_int(stats.get("ops_applied"))
            verify = stats.get("verify") or {}
            if isinstance(verify, Mapping):
                ops_verified_pass = _sanitise_int(verify.get("pass"))
                ops_verified_total = _sanitise_int(verify.get("total"))
            cost = stats.get("cost") or {}
            if isinstance(cost, Mapping):
                cost_spent_usd = _sanitise_float(cost.get("spent_usd"))
                budget = cost.get("budget_usd")
                if budget is not None:
                    cost_budget_usd = _sanitise_float(budget)
        # Top-level fallbacks
        if ops_total == 0:
            ops_total = _sanitise_int(summary.get("ops_total"))
        if ops_applied == 0:
            ops_applied = _sanitise_int(summary.get("ops_applied"))
        if cost_spent_usd == 0.0:
            cost_spent_usd = _sanitise_float(summary.get("cost_spent_usd"))
        commit_hash = _sanitise_str(
            summary.get("commit_hash") or summary.get("commit"),
            max_len=64,
        )
        schema_version_summary = _sanitise_str(
            summary.get("schema_version"), max_len=64,
        )

    # debug.log head (best-effort)
    head_lines: Tuple[str, ...] = ()
    if has_debug_log and debug_log_head_lines > 0:
        try:
            with debug_log_path.open("r", encoding="utf-8", errors="replace") as f:
                lines: List[str] = []
                for _ in range(debug_log_head_lines):
                    line = f.readline()
                    if not line:
                        break
                    lines.append(line.rstrip()[:500])
                head_lines = tuple(lines)
        except OSError as exc:
            logger.debug(
                "[SessionRecord] %s debug.log read error: %s",
                session_id, exc,
            )

    return SessionRecord(
        session_id=session_id,
        path=str(session_dir),
        summary_found=summary_found,
        parse_error=parse_error,
        parse_error_reason=parse_error_reason,
        stop_reason=stop_reason,
        started_at_iso=started_at_iso,
        ended_at_iso=ended_at_iso,
        duration_s=duration_s,
        ops_total=ops_total,
        ops_applied=ops_applied,
        ops_verified_pass=ops_verified_pass,
        ops_verified_total=ops_verified_total,
        cost_spent_usd=cost_spent_usd,
        cost_budget_usd=cost_budget_usd,
        commit_hash=commit_hash,
        schema_version_summary=schema_version_summary,
        on_disk_bytes=on_disk_bytes,
        mtime_ts=mtime_ts,
        mtime_iso=mtime_iso,
        has_debug_log=has_debug_log,
        has_replay_html=has_replay_html,
        replay_html_path=replay_html_path,
        debug_log_head_lines=head_lines,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _dir_size(path: Path) -> int:
    """Best-effort directory size in bytes. Silent on errors."""
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


__all__ = [
    "SESSION_RECORD_SCHEMA_VERSION",
    "SessionParseError",
    "SessionRecord",
    "default_sessions_root",
    "parse_session_dir",
]

_ = (asdict, FrozenSet, field)  # silence unused-import guards
