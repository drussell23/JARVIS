"""Item #4 — `/graduate` REPL dispatcher.

Operator-facing surface for the graduation cadence ledger:

  * ``/graduate list`` — show all known flags + their progress
  * ``/graduate status <flag>`` — show one flag's detailed progress
  * ``/graduate record <flag> <session_id> <outcome> [reason]`` —
    record one session outcome (clean/infra/runner/migration)
  * ``/graduate eligible`` — show flags ready to flip
  * ``/graduate help`` — show usage

Read-side commands (list / status / eligible / help) work even
when the ledger master flag is off (discoverability per the
adopted-across-Pass-A graduations policy). Write-side
(record) requires the master flag.

## Default-off

``JARVIS_GRADUATE_REPL_ENABLED`` (default false). When off, every
subcommand except ``help`` returns DISABLED status.
"""
from __future__ import annotations

import enum
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
    CADENCE_POLICY,
    GraduationLedger,
    SessionOutcome,
    get_default_ledger,
    get_policy,
    is_ledger_enabled,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


GRADUATE_REPL_SCHEMA_VERSION: str = "graduate.repl.1"

MAX_REASON_CHARS_DISPATCH: int = 500


def is_repl_enabled() -> bool:
    """Master flag — ``JARVIS_GRADUATE_REPL_ENABLED`` (default false)."""
    return os.environ.get(
        "JARVIS_GRADUATE_REPL_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Status enum + result shape
# ---------------------------------------------------------------------------


class DispatchStatus(str, enum.Enum):
    OK = "ok"
    DISABLED = "disabled"
    LEDGER_DISABLED = "ledger_disabled"
    UNKNOWN_SUBCOMMAND = "unknown_subcommand"
    INVALID_ARGS = "invalid_args"
    UNKNOWN_FLAG = "unknown_flag"
    INVALID_OUTCOME = "invalid_outcome"
    EMPTY_SESSION_ID = "empty_session_id"
    LEDGER_REJECTED = "ledger_rejected"


@dataclass(frozen=True)
class DispatchResult:
    schema_version: str
    subcommand: str
    status: DispatchStatus
    output: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Renderers (pure)
# ---------------------------------------------------------------------------


def render_help() -> str:
    return (
        "/graduate — track per-loader graduation cadences\n"
        "\n"
        "  list                              — show all known flags\n"
        "  status <flag>                     — show one flag's progress\n"
        "  record <flag> <session_id> <outcome> [reason]\n"
        "                                    — record one session outcome\n"
        "                                      (outcome: clean|infra|runner|migration)\n"
        "  eligible                          — show flags ready to flip\n"
        "  help                              — this message\n"
        "\n"
        "Master flag: JARVIS_GRADUATE_REPL_ENABLED\n"
        "Ledger flag: JARVIS_GRADUATION_LEDGER_ENABLED"
    )


def render_progress_row(
    flag_name: str, progress: dict,
) -> str:
    policy = get_policy(flag_name)
    cls = policy.cadence_class.value if policy else "?"
    return (
        f"  {flag_name}\n"
        f"    cadence_class={cls} required={progress['required']}\n"
        f"    clean={progress['clean']} infra={progress['infra']} "
        f"runner={progress['runner']} migration={progress['migration']}\n"
        f"    unique_sessions={progress['unique_sessions']}"
    )


def render_list(ledger: GraduationLedger) -> str:
    parts = ["# /graduate list — all known flags"]
    all_progress = ledger.all_progress()
    for flag in sorted(all_progress):
        progress = all_progress[flag]
        parts.append(render_progress_row(flag, progress))
    parts.append(f"\nTotal flags: {len(all_progress)}")
    return "\n".join(parts)


def render_status(ledger: GraduationLedger, flag_name: str) -> str:
    policy = get_policy(flag_name)
    if policy is None:
        return f"unknown flag: {flag_name}"
    progress = ledger.progress(flag_name)
    eligible = ledger.is_eligible(flag_name)
    parts = [
        f"# /graduate status {flag_name}",
        f"description: {policy.description}",
        f"cadence_class: {policy.cadence_class.value}",
        f"required_clean_sessions: {progress['required']}",
        f"actual_clean_sessions: {progress['clean']}",
        f"infra_failures: {progress['infra']}",
        f"runner_failures: {progress['runner']}",
        f"migration_skips: {progress['migration']}",
        f"unique_sessions: {progress['unique_sessions']}",
        f"eligible_to_flip: {eligible}",
    ]
    return "\n".join(parts)


def render_eligible(ledger: GraduationLedger) -> str:
    eligible = ledger.eligible_flags()
    parts = [
        f"# /graduate eligible — {len(eligible)} flag(s) ready to flip"
    ]
    for f in eligible:
        parts.append(f"  {f}")
    if not eligible:
        parts.append("  (none — record more clean sessions)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def parse_argv(line: str) -> List[str]:
    return shlex.split(line)


def dispatch_graduate(
    argv: List[str],
    *,
    ledger: Optional[GraduationLedger] = None,
    operator: str = "operator",
) -> DispatchResult:
    """Dispatch a `/graduate` subcommand. NEVER raises.

    Read-side subcommands (list / status / eligible / help) work
    even when the REPL master flag is off (discoverability
    convention adopted across Pass A graduations).
    """
    if not argv:
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand="",
            status=DispatchStatus.INVALID_ARGS,
            detail="empty_argv",
        )
    subcmd = argv[0].strip().lower()

    # Help is always available.
    if subcmd == "help":
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.OK,
            output=render_help(),
        )

    # Read-side subcommands: short-circuit if REPL master off.
    if not is_repl_enabled():
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.DISABLED,
            detail="JARVIS_GRADUATE_REPL_ENABLED unset/false",
        )

    if ledger is None:
        ledger = get_default_ledger()

    if subcmd == "list":
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.OK,
            output=render_list(ledger),
        )

    if subcmd == "status":
        if len(argv) < 2:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.INVALID_ARGS,
                detail="usage: status <flag>",
            )
        flag = argv[1].strip()
        if get_policy(flag) is None:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.UNKNOWN_FLAG,
                detail=f"unknown flag: {flag}",
            )
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.OK,
            output=render_status(ledger, flag),
        )

    if subcmd == "eligible":
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.OK,
            output=render_eligible(ledger),
        )

    if subcmd == "record":
        if len(argv) < 4:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.INVALID_ARGS,
                detail=(
                    "usage: record <flag> <session_id> <outcome> [reason]"
                ),
            )
        flag = argv[1].strip()
        session_id = argv[2].strip()
        outcome_raw = argv[3].strip().lower()
        reason = " ".join(argv[4:]).strip()[:MAX_REASON_CHARS_DISPATCH]
        if get_policy(flag) is None:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.UNKNOWN_FLAG,
                detail=f"unknown flag: {flag}",
            )
        if not session_id:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.EMPTY_SESSION_ID,
                detail="session_id is required",
            )
        try:
            outcome = SessionOutcome(outcome_raw)
        except ValueError:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.INVALID_OUTCOME,
                detail=(
                    f"unknown outcome: {outcome_raw!r}; "
                    "must be one of: clean, infra, runner, migration"
                ),
            )
        if not is_ledger_enabled():
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.LEDGER_DISABLED,
                detail="JARVIS_GRADUATION_LEDGER_ENABLED unset/false",
            )
        ok, detail = ledger.record_session(
            flag_name=flag,
            session_id=session_id,
            outcome=outcome,
            recorded_by=operator,
            notes=reason,
        )
        if not ok:
            return DispatchResult(
                schema_version=GRADUATE_REPL_SCHEMA_VERSION,
                subcommand=subcmd,
                status=DispatchStatus.LEDGER_REJECTED,
                detail=detail,
            )
        progress = ledger.progress(flag)
        return DispatchResult(
            schema_version=GRADUATE_REPL_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.OK,
            output=(
                f"recorded {flag} session={session_id} outcome={outcome.value} "
                f"by={operator}\n"
                f"progress: clean={progress['clean']}/{progress['required']}"
                f" runner={progress['runner']}"
                f" eligible={ledger.is_eligible(flag)}"
            ),
        )

    return DispatchResult(
        schema_version=GRADUATE_REPL_SCHEMA_VERSION,
        subcommand=subcmd,
        status=DispatchStatus.UNKNOWN_SUBCOMMAND,
        detail=f"unknown subcommand: {subcmd!r}",
    )


__all__ = [
    "DispatchResult",
    "DispatchStatus",
    "GRADUATE_REPL_SCHEMA_VERSION",
    "MAX_REASON_CHARS_DISPATCH",
    "dispatch_graduate",
    "is_repl_enabled",
    "parse_argv",
    "render_help",
]
