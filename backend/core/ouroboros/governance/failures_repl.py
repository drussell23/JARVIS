"""Upgrade 3 Slice 5 — ``/failures`` REPL dispatcher (PRD §31.4).

Operator-facing CLI surface mirroring ``/probe`` / ``/coherence`` /
``/quorum`` (Slice 5b E pattern). Consumes the SAME readers that the
HTTP routes consume so CLI + HTTP outputs are byte-equivalent for
the same query.

Subcommands:

  * ``/failures``                — alias for ``/failures top``
  * ``/failures top [N]``        — last N records by recency,
    diversity-deduped (default 20)
  * ``/failures for <signature>`` — single record by signature
    hash (full or 12+ char prefix)
  * ``/failures clear``          — truncate the JSONL store
    (operator-triggered maintenance)
  * ``/failures help``           — usage listing (always
    available; bypasses master-flag gate)

Master gate: :func:`failure_mode_memory_enabled`. Auto-discovered
by :func:`help_dispatcher._discover_module_provided_verbs`. NEVER
raises.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + ``failure_mode_memory`` ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor.
  * Read-only EXCEPT for ``clear`` subcommand (operator-
    explicit maintenance).
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

from backend.core.ouroboros.governance.failure_mode_memory import (
    FAILURE_MODE_MEMORY_SCHEMA_VERSION,
    FailureModeRecord,
    clear_failure_mode_history,
    dedup_window_days,
    failure_mode_memory_enabled,
    failure_mode_min_weight,
    failure_mode_recency_halflife_days,
    failure_mode_top_k,
    find_failure_mode_by_signature,
    history_max_records,
    history_path,
    read_failure_mode_history,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/failures — Failure-Mode Memory (Upgrade 3 / PRD §31.4)\n"
    "\n"
    "Subcommands:\n"
    "  /failures                  alias for /failures top\n"
    "  /failures top [N]          last N records (default 20)\n"
    "  /failures for <signature>  single record by sig (12+ chars)\n"
    "  /failures clear            truncate the JSONL store\n"
    "  /failures help             this text\n"
    "\n"
    "Master flag: JARVIS_FAILURE_MODE_MEMORY_ENABLED (graduated\n"
    "default-TRUE post Slice 5; flip to false for instant revert)\n"
    "Live HTTP surface: GET /observability/failure-modes[/...]\n"
    "Live SSE event:    failure_mode_recalled_at_generate\n"
)

_DEFAULT_TOP_LIMIT: int = 20
_MAX_TOP_LIMIT: int = 200
_MIN_SIG_PREFIX: int = 12


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailuresDispatchResult:
    """Result of a ``/failures`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/failures`` invocation at all (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return s == "/failures" or s == "failures" or (
        s.startswith("/failures ") or s.startswith("failures ")
    )


def _parse_limit(args: List[str]) -> int:
    if len(args) < 2:
        return _DEFAULT_TOP_LIMIT
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > _MAX_TOP_LIMIT:
            return _MAX_TOP_LIMIT
        return n
    except (TypeError, ValueError):
        return _DEFAULT_TOP_LIMIT


def dispatch_failures_command(line: str) -> FailuresDispatchResult:
    """Parse a ``/failures`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return FailuresDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return FailuresDispatchResult(
            ok=False, text=f"  /failures parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "top")

    if head in ("help", "?"):
        return FailuresDispatchResult(ok=True, text=_HELP)

    if not failure_mode_memory_enabled():
        return FailuresDispatchResult(
            ok=False,
            text=(
                "  /failures: FailureModeMemory disabled — set "
                "JARVIS_FAILURE_MODE_MEMORY_ENABLED=true"
            ),
        )

    if head == "top":
        return _render_top(_parse_limit(args))
    if head == "for":
        if len(args) < 2:
            return FailuresDispatchResult(
                ok=False,
                text=(
                    "  /failures for <signature>: "
                    "missing signature argument."
                ),
            )
        return _render_for_signature(args[1])
    if head == "clear":
        return _render_clear()
    if head == "config":
        return _render_config()
    return FailuresDispatchResult(
        ok=False,
        text=(
            f"  /failures: unknown subcommand {head!r}. "
            f"Try /failures help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_record_summary(rec: FailureModeRecord) -> str:
    sig_short = (rec.signature_hash or "")[:12]
    sit = rec.situation_kind.value
    attempt = rec.attempted_action_kind or "unspecified"
    mode = rec.failure_mode_kind.value
    return (
        f"  • {sit:<28} `{attempt:<22}` "
        f"{mode:<24} weight={rec.weight} sig=`{sig_short}` "
        f"op={(rec.op_id or '')[:16]}"
    )


def _render_top(limit: int) -> FailuresDispatchResult:
    try:
        history = read_failure_mode_history(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[failures_repl] _render_top raised: %s", exc,
        )
        history = ()
    lines: List[str] = [
        f"/failures top — last {limit} records "
        f"({len(history)} found)",
        "",
    ]
    if not history:
        lines.append("  (no records yet)")
        lines.append("")
        return FailuresDispatchResult(
            ok=True, text="\n".join(lines),
        )
    # Display in reverse-chronological so most-recent is first
    for rec in reversed(history):
        try:
            lines.append(_format_record_summary(rec))
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  • (corrupt record — skipped)")
    lines.append("")
    return FailuresDispatchResult(ok=True, text="\n".join(lines))


def _render_for_signature(sig_arg: str) -> FailuresDispatchResult:
    sig = (sig_arg or "").strip().lower()
    if len(sig) < _MIN_SIG_PREFIX:
        return FailuresDispatchResult(
            ok=False,
            text=(
                f"  /failures for: signature must be at least "
                f"{_MIN_SIG_PREFIX} characters (got "
                f"{len(sig)})."
            ),
        )
    # find_failure_mode_by_signature does exact match; but we
    # accept prefix matches by walking history when the input is
    # shorter than the full sha256 hex (64 chars). Exact match
    # first; fall back to prefix scan.
    try:
        if len(sig) == 64:
            rec = find_failure_mode_by_signature(sig)
        else:
            history = read_failure_mode_history()
            rec = next(
                (
                    r for r in history
                    if (r.signature_hash or "").lower().startswith(
                        sig,
                    )
                ),
                None,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[failures_repl] _render_for_signature raised: %s",
            exc,
        )
        rec = None

    if rec is None:
        return FailuresDispatchResult(
            ok=True,
            text=(
                f"/failures for `{sig}` — no record found.\n"
            ),
        )
    lines: List[str] = [
        f"/failures for `{(rec.signature_hash or '')[:16]}…`",
        "",
        f"  situation_kind         {rec.situation_kind.value}",
        f"  attempted_action_kind  {rec.attempted_action_kind}",
        f"  failure_mode_kind      {rec.failure_mode_kind.value}",
        f"  weight                 {rec.weight}",
        f"  observed_at_unix       {rec.observed_at_unix}",
        f"  op_id                  {rec.op_id}",
        f"  signature_hash         {rec.signature_hash}",
        "",
        "  Mitigation:",
        f"    {rec.mitigation_summary}",
        "",
    ]
    return FailuresDispatchResult(ok=True, text="\n".join(lines))


def _render_clear() -> FailuresDispatchResult:
    try:
        ok = clear_failure_mode_history()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[failures_repl] _render_clear raised: %s", exc,
        )
        ok = False
    if ok:
        return FailuresDispatchResult(
            ok=True,
            text=(
                f"/failures clear — store truncated at "
                f"{history_path()}\n"
            ),
        )
    return FailuresDispatchResult(
        ok=False,
        text=(
            "/failures clear — failed (master off, "
            "flock contention, or disk fault).\n"
        ),
    )


def _render_config() -> FailuresDispatchResult:
    """Convenience subcommand — surfaces the env-knob snapshot."""
    lines: List[str] = [
        "/failures config — env-knob snapshot",
        "",
        f"  schema_version              "
        f"{FAILURE_MODE_MEMORY_SCHEMA_VERSION}",
        f"  master_enabled              "
        f"{failure_mode_memory_enabled()}",
        f"  history_path                {history_path()}",
        f"  history_max_records         {history_max_records()}",
        f"  dedup_window_days           {dedup_window_days()}",
        f"  retrieval_top_k             {failure_mode_top_k()}",
        f"  retrieval_min_weight        "
        f"{failure_mode_min_weight()}",
        f"  retrieval_halflife_days     "
        f"{failure_mode_recency_halflife_days()}",
        "",
    ]
    return FailuresDispatchResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/failures`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/failures",
            one_line=(
                "Failure-Mode Memory: top recurrence records, "
                "single-signature lookup, store maintenance "
                "(Upgrade 3 / PRD §31.4)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[failures_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "FailuresDispatchResult",
    "dispatch_failures_command",
    "register_verbs",
]
