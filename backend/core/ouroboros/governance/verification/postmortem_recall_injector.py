"""Priority #2 Slice 3 — CONTEXT_EXPANSION prompt injector.

Composes the ``## Recent Failures (advisory)`` prompt section
that gets injected at the CONTEXT_EXPANSION phase before
GENERATE. The model sees prior-failure context for the symbols
and files this op will touch — biasing first-pass synthesis
toward non-recurrence by construction (without auto-blocking
anything; the model is advised, not commanded).

**Load-bearing requirement: ROBUST DEGRADATION.** Every public
function NEVER raises out. Empty/corrupt/error paths all return
the empty string. The CONTEXT_EXPANSION → GENERATE pipeline
NEVER sees a raise from this module — it gets either a
populated section or ``""``. The standard prompt continues
intact regardless of what happens here.

This is the operational consumer that will activate Priority #1
Slice 4's currently-dormant ``INJECT_POSTMORTEM_RECALL_HINT``
advisory: when a recurrence boost lands (Priority #2 Slice 4
will write this), the recall budget for the matched failure_
class extends — more relevant prior failures surface in the
prompt section.

Source material — what we leverage (no duplication):

  * ``postmortem_recall.recall_postmortems`` (Slice 1) — pure
    ranking. We call it with the index records read by Slice 2
    and a RecallTarget composed from caller-supplied
    files/symbols/failure_class.
  * ``postmortem_recall_index.read_index`` (Slice 2) — index
    reader with age filter + chronological sort + schema-
    tolerance.
  * ``last_session_summary._sanitize_field`` — load-bearing
    safety helper (control-char strip, secret redaction, length
    cap). REUSED for every field rendered into the prompt
    section. AST-pinned via importfrom.

Direct-solve principles:

  * **Asynchronous-ready** — sync API; Slice 5's orchestrator
    integration will wrap via ``asyncio.to_thread`` so the
    CONTEXT_EXPANSION hook doesn't block the loop on file I/O.

  * **Dynamic** — every numeric env-tunable with floor + ceiling
    clamps. NO hardcoded char budgets, top-K caps, or section
    formatting magic.

  * **Adaptive** — different RelevanceLevel values render with
    different markers (HIGH/MEDIUM/LOW). Char-budget triggers
    truncation with explicit ``[truncated]`` marker so the model
    knows context was cut.

  * **Intelligent** — section header includes a one-line
    summary (count + age window) so the model can decide
    whether to attend to the records or move on. Records sorted
    by recency × relevance (Slice 1's score) — most-relevant-
    most-recent first.

  * **Robust** — defensive layered: every step (master flag /
    sub-gate / index read / recall / format) catches its own
    exception and falls back to empty string. The orchestrator
    hook ``compose_for_op_context`` is total — every input maps
    to either a populated string or ``""``.

  * **No hardcoding** — section header text + truncation marker
    are module-level constants (auditable); record format uses
    a structured template; char/result caps env-tunable.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + Slice 1 (``postmortem_recall``) + Slice 2
    (``postmortem_recall_index``) + LastSessionSummary
    (``_sanitize_field``) ONLY.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine / episodic_memory /
    ast_canonical / semantic_index.
  * MUST reference ``_sanitize_field`` (zero-duplication-via-
    reuse contract).
  * MUST reference ``recall_postmortems`` from Slice 1.
  * MUST reference ``read_index`` from Slice 2.
  * No mutation tools.
  * No bare eval-family calls; AST walk + bytes pin enforce.
  * No async (Slice 5's orchestrator integration wraps in
    ``to_thread``; this module stays sync).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

from backend.core.ouroboros.governance.last_session_summary import (
    _sanitize_field,
)
from backend.core.ouroboros.governance.verification.postmortem_recall import (
    PostmortemRecord,
    RecallOutcome,
    RecallTarget,
    RelevanceLevel,
    compute_relevance,
    postmortem_recall_enabled,
    recall_max_age_days,
    recall_postmortems,
    recall_top_k,
)
from backend.core.ouroboros.governance.verification.postmortem_recall_index import (
    IndexOutcome,
    read_index,
)

logger = logging.getLogger(__name__)


POSTMORTEM_RECALL_INJECTOR_SCHEMA_VERSION: str = (
    "postmortem_recall_injector.1"
)


# ---------------------------------------------------------------------------
# Slice 5 — SSE event vocabulary + publisher
# ---------------------------------------------------------------------------


EVENT_TYPE_POSTMORTEM_RECALL_INJECTED: str = (
    "postmortem_recall_injected"
)
"""SSE event fired on every successful (non-empty) injection.
Operators consume via the IDE stream. Master-flag-gated by
``postmortem_recall_enabled()``; broker-missing / publish-error
all return None silently. NEVER raises. Mirrors Move 4/5/6/
Priority#1 lazy-import + best-effort discipline."""


def publish_postmortem_recall_injection(
    *,
    op_id: str = "",
    section_chars: int = 0,
    record_count: int = 0,
    max_relevance: str = "",
) -> Optional[str]:
    """Fire ``EVENT_TYPE_POSTMORTEM_RECALL_INJECTED`` SSE event.
    Lazy ``ide_observability_stream`` import + best-effort
    publish + never-raise contract.

    Returns broker frame_id on publish, ``None`` on suppression
    /failure (master-off / broker-missing / publish-error)."""
    if not postmortem_recall_enabled():
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_POSTMORTEM_RECALL_INJECTED,
            op_id=str(op_id or ""),
            payload={
                "schema_version": (
                    POSTMORTEM_RECALL_INJECTOR_SCHEMA_VERSION
                ),
                "op_id": str(op_id or ""),
                "section_chars": int(section_chars),
                "record_count": int(record_count),
                "max_relevance": str(max_relevance or ""),
            },
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemInjector] SSE publish swallowed",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Sub-gate flag
# ---------------------------------------------------------------------------


def postmortem_injection_enabled() -> bool:
    """``JARVIS_POSTMORTEM_INJECTION_ENABLED`` (default
    ``true`` post Slice 5 graduation 2026-05-01).

    Sub-gate for the CONTEXT_EXPANSION injection. Master flag
    (``JARVIS_POSTMORTEM_RECALL_ENABLED``) must also be true for
    the section to actually render. Operators may set false to
    disable the injection while keeping the underlying index
    fresh."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_INJECTION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-01 (Priority #2 Slice 5)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped
# ---------------------------------------------------------------------------


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


def max_prompt_chars() -> int:
    """``JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS`` (default
    2000, floor 500, ceiling 8000).

    Hard cap on the rendered section's total length. When the
    full record list would exceed this, the section truncates
    with an explicit truncation marker. Bounded floor (500)
    prevents pathologically small budgets that render nothing
    useful; ceiling (8000) prevents prompt-bloat starving
    GENERATE."""
    return _env_int_clamped(
        "JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS",
        2000, floor=500, ceiling=8000,
    )


def max_chars_per_record() -> int:
    """``JARVIS_POSTMORTEM_RECALL_MAX_CHARS_PER_RECORD`` (default
    400, floor 100, ceiling 2000).

    Per-record char cap. Prevents one pathologically-long
    failure_reason from consuming the entire section budget."""
    return _env_int_clamped(
        "JARVIS_POSTMORTEM_RECALL_MAX_CHARS_PER_RECORD",
        400, floor=100, ceiling=2000,
    )


# ---------------------------------------------------------------------------
# Format constants — module-level for auditability
# ---------------------------------------------------------------------------


_SECTION_HEADER: str = "## Recent Failures (advisory)"
_SECTION_FOOTER: str = (
    "These are advisory context — the model should weigh them "
    "but is not bound by them."
)
_TRUNCATION_MARKER: str = "  … [truncated]"
_RELEVANCE_MARKERS: dict = {
    RelevanceLevel.HIGH: "**HIGH**",
    RelevanceLevel.MEDIUM: "**MEDIUM**",
    RelevanceLevel.LOW: "**LOW**",
    RelevanceLevel.NONE: "",
}


# ---------------------------------------------------------------------------
# Internal: human-friendly age formatter
# ---------------------------------------------------------------------------


def _format_age_human(age_days: float) -> str:
    """Format age_days as human-readable string. NEVER raises."""
    try:
        if age_days != age_days:  # NaN
            return "unknown"
        if age_days < 0:
            return "0s ago"
        if age_days < 1.0 / 24.0:  # < 1 hour
            mins = max(1, int(age_days * 24 * 60))
            return f"{mins}m ago"
        if age_days < 1.0:
            hours = max(1, int(age_days * 24))
            return f"{hours}h ago"
        if age_days < 30:
            days = max(1, int(age_days))
            return f"{days}d ago"
        months = max(1, int(age_days / 30))
        return f"{months}mo ago"
    except Exception:  # noqa: BLE001 — defensive
        return "unknown"


# ---------------------------------------------------------------------------
# Internal: per-record renderer
# ---------------------------------------------------------------------------


def _render_record(
    record: PostmortemRecord,
    *,
    relevance: RelevanceLevel,
    rank: int,
    max_chars: int,
    now_ts: Optional[float] = None,
) -> str:
    """Render one record into a structured 1–3 line block.
    Bounded by ``max_chars``; truncates the failure_reason
    line first if oversized. NEVER raises — returns empty
    string on any failure."""
    try:
        marker = _RELEVANCE_MARKERS.get(relevance, "")
        # Sanitize EVERY field with the canonical helper —
        # zero-duplication-via-reuse contract
        op_id = _sanitize_field(record.op_id)[:20]
        session_id = _sanitize_field(record.session_id)[:32]
        file_path = _sanitize_field(record.file_path)
        symbol_name = _sanitize_field(record.symbol_name)
        failure_class = _sanitize_field(record.failure_class)
        failure_phase = _sanitize_field(record.failure_phase)
        failure_reason = _sanitize_field(record.failure_reason)
        age_str = _format_age_human(
            record.age_days(now_ts=now_ts),
        )

        # Title line — file:symbol or just file or just symbol
        if file_path and symbol_name:
            target_str = f"`{file_path}:{symbol_name}`"
        elif file_path:
            target_str = f"`{file_path}`"
        elif symbol_name:
            target_str = f"`{symbol_name}`"
        else:
            target_str = (
                f"failure_class={failure_class}"
                if failure_class else f"op {op_id}"
            )

        phase_str = (
            f" in {failure_phase}" if failure_phase else ""
        )
        marker_prefix = f"{marker} " if marker else ""
        title = (
            f"{rank}. {marker_prefix}{target_str} failed"
            f"{phase_str} {age_str}"
        )

        lines = [title]
        if failure_reason:
            lines.append(f"   Reason: {failure_reason}")
        if session_id:
            lines.append(f"   Source: {session_id}")

        block = "\n".join(lines)

        # Per-record char cap with truncation
        if len(block) > max_chars:
            block = (
                block[: max(1, max_chars - len(_TRUNCATION_MARKER))]
                + _TRUNCATION_MARKER
            )
        return block
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemInjector] _render_record raised: %s",
            exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Internal: full section renderer with char budget
# ---------------------------------------------------------------------------


def _render_section(
    records_with_relevance: list,
    *,
    total_index_size: int,
    max_age_days: float,
    char_budget: int,
    now_ts: Optional[float] = None,
) -> str:
    """Compose the full prompt section. Bounded by
    ``char_budget`` with explicit truncation marker. NEVER
    raises."""
    try:
        if not records_with_relevance:
            return ""

        n_matched = len(records_with_relevance)
        per_rec_cap = max_chars_per_record()

        # Header + summary line
        summary_line = (
            f"In the last {int(max_age_days)} days, "
            f"{n_matched} prior failures matched. "
            f"Showing top {n_matched} by recency × relevance:"
        )
        header_block = f"{_SECTION_HEADER}\n\n{summary_line}\n"

        # Render each record
        record_blocks = []
        for idx, (record, rel) in enumerate(
            records_with_relevance, start=1,
        ):
            block = _render_record(
                record, relevance=rel, rank=idx,
                max_chars=per_rec_cap, now_ts=now_ts,
            )
            if block:
                record_blocks.append(block)

        if not record_blocks:
            return ""

        records_str = "\n\n".join(record_blocks)
        full = (
            f"{header_block}\n{records_str}\n\n{_SECTION_FOOTER}"
        )

        # Char-budget truncation
        if len(full) > char_budget:
            cutoff = max(
                len(header_block),
                char_budget - len(_TRUNCATION_MARKER),
            )
            full = full[:cutoff] + _TRUNCATION_MARKER

        return full
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostmortemInjector] _render_section raised: %s",
            exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Public: render_postmortem_recall_section
# ---------------------------------------------------------------------------


def render_postmortem_recall_section(
    *,
    target_files: Iterable[str] = (),
    target_symbols: Iterable[str] = (),
    target_failure_class: Optional[str] = None,
    max_results: Optional[int] = None,
    max_chars: Optional[int] = None,
    enabled_override: Optional[bool] = None,
    target_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> str:
    """Compose the full ``## Recent Failures (advisory)`` prompt
    section.

    **ROBUST DEGRADATION CONTRACT** — every degraded path
    returns the empty string ``""``. NEVER raises:
      * Master flag off → ``""``
      * Sub-gate flag off → ``""``
      * Index read fails / empty → ``""``
      * Recall returns DISABLED / EMPTY_INDEX / MISS / FAILED
        → ``""``
      * Render fails → ``""``
      * Any uncaught exception → ``""`` (last-resort defensive)

    The orchestrator hook ``compose_for_op_context`` calls this
    and the prompt assembler appends the returned string. Empty
    string = no injection (standard prompt continues unaffected).

    Inputs:
      * ``target_files`` — paths the op will touch
      * ``target_symbols`` — function/class names being modified
      * ``target_failure_class`` — Optional hard filter (None =
        match-any failure_class)
      * ``max_results`` — top-K records (defaults to
        ``recall_top_k()``)
      * ``max_chars`` — section char budget (defaults to
        ``max_prompt_chars()``)"""
    try:
        # Step 1: master flag
        is_master_on = (
            enabled_override if enabled_override is not None
            else postmortem_recall_enabled()
        )
        if not is_master_on:
            return ""

        # Step 2: sub-gate
        if not postmortem_injection_enabled():
            return ""

        # Step 3: read index — schema-tolerant + age filter
        max_age = recall_max_age_days()
        try:
            read_result = read_index(
                target_path=target_path,
                max_age_days=max_age,
                now_ts=now_ts,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PostmortemInjector] read_index raised: %s",
                exc,
            )
            return ""

        if read_result.outcome is not IndexOutcome.READ_OK:
            return ""
        if not read_result.records:
            return ""

        # Step 4: build target + recall
        try:
            files_set = frozenset(
                _sanitize_field(f) for f in target_files
                if _sanitize_field(f)
            )
            symbols_set = frozenset(
                _sanitize_field(s) for s in target_symbols
                if _sanitize_field(s)
            )
            target = RecallTarget(
                target_files=files_set,
                target_symbols=symbols_set,
                target_failure_class=(
                    target_failure_class
                    if target_failure_class else None
                ),
                max_age_days=float(max_age),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PostmortemInjector] target build raised: %s",
                exc,
            )
            return ""

        # Slice 1's recall_postmortems honors the master flag via
        # its own enabled check. We pass enabled_override=True
        # because we already validated the flags upstream — this
        # avoids env-flip races between flag-check and recall.
        try:
            verdict = recall_postmortems(
                read_result.records,
                target,
                max_results=(
                    max_results
                    if max_results is not None
                    else recall_top_k()
                ),
                enabled_override=True,
                now_ts=now_ts,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PostmortemInjector] recall raised: %s", exc,
            )
            return ""

        if verdict.outcome is not RecallOutcome.HIT:
            return ""
        if not verdict.records:
            return ""

        # Step 5: re-evaluate per-record relevance (verdict
        # carries max_relevance only; we need each record's
        # individual level for the marker)
        try:
            records_with_relevance = [
                (r, compute_relevance(r, target))
                for r in verdict.records
            ]
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PostmortemInjector] re-relevance raised: %s",
                exc,
            )
            return ""

        # Step 6: render section
        char_budget = (
            int(max_chars) if max_chars is not None
            else max_prompt_chars()
        )
        section = _render_section(
            records_with_relevance,
            total_index_size=verdict.total_index_size,
            max_age_days=max_age,
            char_budget=char_budget,
            now_ts=now_ts,
        )

        # Step 7 (Slice 5) — fire SSE event on successful
        # injection. Best-effort; never raises. Empty section
        # = silenced.
        if section:
            try:
                publish_postmortem_recall_injection(
                    op_id="",
                    section_chars=len(section),
                    record_count=len(records_with_relevance),
                    max_relevance=verdict.max_relevance.value,
                )
            except Exception:  # noqa: BLE001 — defensive
                pass

        return section
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemInjector] render_section raised: %s",
            exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Public: orchestrator hook
# ---------------------------------------------------------------------------


def compose_for_op_context(
    *,
    op_id: str = "",
    target_files: Iterable[str] = (),
    target_symbols: Iterable[str] = (),
    target_failure_class: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> str:
    """Orchestrator-facing entry point. Single signature for the
    CONTEXT_EXPANSION integration hook (Slice 5 wires this).

    NEVER raises. Returns either the populated section or
    ``""`` — orchestrator appends the returned string to its
    prompt and continues. Empty string = no injection
    (standard prompt continues unaffected).

    ``op_id`` is currently unused for matching but reserved for
    Slice 4's recurrence-boost lookup (boost cache keyed by
    op_id will extend the recall budget for next-N-ops on the
    matched failure_class)."""
    try:
        _ = op_id  # reserved for Slice 4 wiring
        return render_postmortem_recall_section(
            target_files=target_files,
            target_symbols=target_symbols,
            target_failure_class=target_failure_class,
            max_chars=max_chars,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemInjector] compose hook raised: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EVENT_TYPE_POSTMORTEM_RECALL_INJECTED",
    "POSTMORTEM_RECALL_INJECTOR_SCHEMA_VERSION",
    "compose_for_op_context",
    "max_chars_per_record",
    "max_prompt_chars",
    "postmortem_injection_enabled",
    "publish_postmortem_recall_injection",
    "render_postmortem_recall_section",
]
