"""§37 Tier 2 #11 — `/history` REPL verb (Slice 2).

Operator-facing surface for the SessionArchive (Slice 1).
Makes the Phase 9 evidence ledger queryable: ``/history flag
<name>``, ``/history since <days>``, ``/history search
<text>``. Auto-discovered via §32.11 Slice 4 naming-cage:
file ``history_repl.py`` → verb ``/history`` → dispatcher
``dispatch_history_command(line)``.

**Subcommands**:

  * ``/history`` (bare) — recent 20 sessions.
  * ``/history recent [N]`` — N most-recent sessions
    (clamped to [1, 200]).
  * ``/history flag <name>`` — sessions testing flag <name>.
  * ``/history since <days>`` — sessions started in the last N
    days.
  * ``/history outcome <status>`` — filter by clean / runner /
    infra / spec / timeout.
  * ``/history session <id>`` — detail card for one session.
  * ``/history search <text>`` — grep ``notes`` field.
  * ``/history backfill`` — re-hydrate archive from canonical
    ledgers (idempotent; safe to call repeatedly).
  * ``/history help`` — usage.

**Read-only browser** (mirrors ``replay_repl`` authority
asymmetry): operator queries the archive but never mutates
orchestrator / risk-tier / iron-gate state. Backfill is the
only write surface and writes only to the SQLite index.

**Composition**:
  * Single source of truth — :func:`get_default_archive` from
    Slice 1; no parallel SessionArchive construction.
  * Master flag composes Slice 1's
    ``JARVIS_SESSION_ARCHIVE_ENABLED`` (no separate REPL
    flag — single source of truth).

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import logging
import shlex
import time
from dataclasses import dataclass
from typing import Any, List, Optional


logger = logging.getLogger("Ouroboros.HistoryREPL")


_VERBS = ("/history",)
_VALID_SUBCOMMANDS = {
    "recent", "flag", "since", "outcome", "session",
    "search", "backfill", "help",
}


@dataclass
class HistoryDispatchResult:
    """Mirrors sibling REPL dispatch shape — auto-discovery
    convention requires ``ok``, ``text``, ``matched``."""
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _VERBS


def dispatch_history_command(line: str) -> HistoryDispatchResult:
    """Parse a ``/history`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return HistoryDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return HistoryDispatchResult(
            ok=False, text=f"/history: parse error — {exc}",
        )
    args = tokens[1:] if len(tokens) > 1 else []
    if not args:
        return _render_recent(20)
    sub = args[0].lower()
    if sub not in _VALID_SUBCOMMANDS:
        return HistoryDispatchResult(
            ok=False,
            text=(
                f"/history: unknown subcommand {sub!r}. "
                f"Try /history help."
            ),
        )
    if sub == "help":
        return _render_help()
    if sub == "recent":
        n = 20
        if len(args) >= 2:
            try:
                n = int(args[1])
            except (TypeError, ValueError):
                return HistoryDispatchResult(
                    ok=False,
                    text=(
                        "/history recent: N must be an integer "
                        "(e.g., /history recent 50)"
                    ),
                )
        return _render_recent(n)
    if sub == "flag":
        if len(args) < 2:
            return HistoryDispatchResult(
                ok=False,
                text=(
                    "/history flag: missing flag name. Usage: "
                    "/history flag <FLAG_NAME>"
                ),
            )
        return _render_flag(args[1])
    if sub == "since":
        if len(args) < 2:
            return HistoryDispatchResult(
                ok=False,
                text=(
                    "/history since: missing days. Usage: "
                    "/history since <N>"
                ),
            )
        try:
            days = float(args[1])
        except (TypeError, ValueError):
            return HistoryDispatchResult(
                ok=False,
                text="/history since: days must be numeric",
            )
        return _render_since(days)
    if sub == "outcome":
        if len(args) < 2:
            return HistoryDispatchResult(
                ok=False,
                text=(
                    "/history outcome: missing status. Usage: "
                    "/history outcome <clean|runner|infra|"
                    "spec|timeout>"
                ),
            )
        return _render_outcome(args[1])
    if sub == "session":
        if len(args) < 2:
            return HistoryDispatchResult(
                ok=False,
                text=(
                    "/history session: missing session id. "
                    "Usage: /history session <id>"
                ),
            )
        return _render_session(args[1])
    if sub == "search":
        if len(args) < 2:
            return HistoryDispatchResult(
                ok=False,
                text=(
                    "/history search: missing search text. "
                    "Usage: /history search <text>"
                ),
            )
        return _render_search(" ".join(args[1:]))
    if sub == "backfill":
        return _handle_backfill()
    return HistoryDispatchResult(
        ok=False,
        text=f"/history: unhandled subcommand {sub!r}",
    )


def _render_help() -> HistoryDispatchResult:
    text = (
        "/history — Phase 9 session-search "
        "(§37 Tier 2 #11)\n"
        "\n"
        "  /history                       recent 20 sessions\n"
        "  /history recent [N]            N most-recent\n"
        "                                 (clamped 1-200)\n"
        "  /history flag <FLAG>           sessions testing "
        "<FLAG>\n"
        "  /history since <days>          sessions in last "
        "<days>\n"
        "  /history outcome <status>      filter "
        "clean/runner/infra/spec/timeout\n"
        "  /history session <id>          detail card\n"
        "  /history search <text>         grep notes field\n"
        "  /history backfill              re-hydrate from "
        "canonical ledgers\n"
        "  /history help                  this message\n"
        "\n"
        "Master flag: JARVIS_SESSION_ARCHIVE_ENABLED "
        "(default-FALSE per §33.1)\n"
        "Sources: .jarvis/live_fire_graduation_history.jsonl + "
        ".jarvis/graduation_ledger.jsonl +\n"
        "         .ouroboros/sessions/<id>/summary.json"
    )
    return HistoryDispatchResult(ok=True, text=text)


def _render_recent(n: int) -> HistoryDispatchResult:
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    try:
        records = archive.find_sessions(limit=n)
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            "/history recent: archive read failed (non-fatal)",
        )
    return _render_record_list(
        records, header=f"recent {len(records)} sessions",
    )


def _render_flag(flag: str) -> HistoryDispatchResult:
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    flag = flag.strip()
    try:
        records = archive.find_sessions(flag=flag, limit=200)
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            f"/history flag: archive read failed (non-fatal)",
        )
    return _render_record_list(
        records, header=f"flag={flag} ({len(records)} sessions)",
    )


def _render_since(days: float) -> HistoryDispatchResult:
    if days <= 0:
        return HistoryDispatchResult(
            ok=False, text="/history since: days must be > 0",
        )
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    since_epoch = time.time() - (days * 86400.0)
    try:
        records = archive.find_sessions(
            since_epoch=since_epoch, limit=200,
        )
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            "/history since: archive read failed (non-fatal)",
        )
    return _render_record_list(
        records,
        header=(
            f"last {days:.1f} days "
            f"({len(records)} sessions)"
        ),
    )


def _render_outcome(status: str) -> HistoryDispatchResult:
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    norm = status.strip().lower()
    try:
        records = archive.find_sessions(
            outcome=norm, limit=200,
        )
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            "/history outcome: archive read failed "
            "(non-fatal)",
        )
    return _render_record_list(
        records,
        header=f"outcome={norm} ({len(records)} sessions)",
    )


def _render_session(session_id: str) -> HistoryDispatchResult:
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    sid = session_id.strip()
    try:
        rec = archive.get_session(sid)
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            "/history session: archive read failed "
            "(non-fatal)",
        )
    if rec is None:
        return HistoryDispatchResult(
            ok=False,
            text=f"/history session: no record for {sid!r}",
        )
    lines = [
        f"session: {rec.session_id}",
        f"  type        = {rec.session_type}",
        f"  outcome     = {rec.outcome}",
        f"  flag        = {rec.flag_name or '(none)'}",
        f"  duration_s  = {rec.duration_s:.1f}",
        f"  cost_usd    = ${rec.cost_usd:.4f}",
        f"  ops_count   = {rec.ops_count}",
        f"  runner_attr = {rec.runner_attributed}",
        f"  stop_reason = {rec.stop_reason or '(none)'}",
        f"  recorded_by = {rec.recorded_by or '(none)'}",
        f"  notes       = {rec.notes or '(none)'}",
    ]
    return HistoryDispatchResult(ok=True, text="\n".join(lines))


def _render_search(text: str) -> HistoryDispatchResult:
    text = text.strip()
    if not text:
        return HistoryDispatchResult(
            ok=False,
            text="/history search: empty search text",
        )
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    try:
        records = archive.find_sessions(
            notes_contains=text, limit=200,
        )
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            "/history search: archive read failed "
            "(non-fatal)",
        )
    return _render_record_list(
        records,
        header=(
            f"search={text!r} ({len(records)} matches)"
        ),
    )


def _handle_backfill() -> HistoryDispatchResult:
    archive = _archive_or_disabled()
    if archive is None:
        return _disabled_result()
    try:
        rows = archive.backfill()
    except Exception:  # noqa: BLE001 — defensive
        return _empty_result(
            "/history backfill: backfill failed (non-fatal)",
        )
    total = 0
    try:
        total = archive.total_count()
    except Exception:  # noqa: BLE001 — defensive
        total = 0
    return HistoryDispatchResult(
        ok=True,
        text=(
            f"/history backfill: {rows} rows upserted; "
            f"archive now contains {total} sessions."
        ),
    )


def _render_record_list(
    records: List[Any], *, header: str,
) -> HistoryDispatchResult:
    if not records:
        return HistoryDispatchResult(
            ok=True,
            text=(
                f"/history: no sessions match ({header}). "
                f"Run /history backfill if archive may be "
                f"stale, or check master flag "
                f"JARVIS_SESSION_ARCHIVE_ENABLED."
            ),
        )
    lines = [f"/history {header}:"]
    for rec in records:
        try:
            ts = (
                time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(rec.started_at_epoch),
                ) if rec.started_at_epoch > 0
                else "unknown-time"
            )
        except Exception:  # noqa: BLE001 — defensive
            ts = "unknown-time"
        lines.append(
            f"  {rec.session_id:<28} {ts:<17} "
            f"outcome={rec.outcome:<8} "
            f"flag={rec.flag_name[:38] or '(none)':<38} "
            f"cost=${rec.cost_usd:.4f}"
        )
    return HistoryDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _archive_or_disabled() -> Optional[Any]:
    try:
        from backend.core.ouroboros.governance.session_archive import (
            get_default_archive,
            master_enabled,
        )
    except ImportError:
        return None
    try:
        if not master_enabled():
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        return get_default_archive()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _disabled_result() -> HistoryDispatchResult:
    return HistoryDispatchResult(
        ok=True,
        text=(
            "/history: SessionArchive disabled. Set "
            "JARVIS_SESSION_ARCHIVE_ENABLED=true to enable, "
            "then run /history backfill."
        ),
    )


def _empty_result(text: str) -> HistoryDispatchResult:
    return HistoryDispatchResult(ok=False, text=text)


__all__ = [
    "HistoryDispatchResult",
    "dispatch_history_command",
]
