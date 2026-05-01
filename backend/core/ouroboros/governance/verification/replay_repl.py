"""/replay REPL dispatcher — Priority #3 Slice 5b operator polish.

Operator surface for inspecting Counterfactual Replay state — the
empirical recurrence-reduction baseline produced by the 4-slice
Priority #3 pipeline.

Subcommands::

    /replay                          status (default)
    /replay status                   summary: total + outcome + last verdict
    /replay history [N]              last N stamped verdicts (default 10)
    /replay baseline                 current ComparisonReport detail
    /replay run <session> <phase> <kind> [--verdict V]
                                     manually run + record one replay
    /replay help                     this text

Authority posture (mirrors /posture + /governor + /coherence):

  * §1 read-only over recorded artifacts — ``run`` triggers Slice 2's
    engine which reads cached ledger + summary (zero LLM cost,
    AST-pinned). NEVER proposes a flag flip.
  * §8 observability — every subcommand produces operator-readable
    output. ``run`` records the verdict via Slice 4's flock'd JSONL
    store + emits the per-verdict SSE event. No new write surfaces
    beyond what Slices 1-4 already shipped.
  * Authority-free — grep-pinned by Slice 5b's regression suite.

Requires ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED=true`` (graduated
default-true post Slice 5). Subcommand sub-gates inherit the
respective Slice's enabled() check.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import textwrap
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReplayREPL")


_COMMANDS = frozenset({"/replay"})


_HELP = textwrap.dedent(
    """
    /replay — Counterfactual Replay inspection
    -------------------------------------------
      /replay                    current status
      /replay status             summary: total + outcome + last verdict
      /replay history [N]        last N stamped verdicts (default 10, max 200)
      /replay baseline           current ComparisonReport detail
      /replay run <session_id> <swap_phase> <swap_kind>
                                 [--verdict approval_required|blocked|auto_apply]
                                 manually run + record one replay
      /replay help               this text

    Override kinds: gate_decision | postmortem_injection |
                    recurrence_boost | quorum_invocation |
                    coherence_observer

    Requires JARVIS_COUNTERFACTUAL_REPLAY_ENABLED=true (graduated
    default-true post Slice 5).
    """
).strip()


@dataclass
class ReplayDispatchResult:
    """REPL dispatch outcome — same shape as posture/governor."""
    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    return line.split(None, 1)[0] in _COMMANDS


def _master_enabled() -> bool:
    """Master flag check via Slice 1 helper (lazy import keeps the
    REPL module authority-free at import time)."""
    try:
        from backend.core.ouroboros.governance.verification.counterfactual_replay import (
            counterfactual_replay_enabled,
        )
    except ImportError:
        return False
    return counterfactual_replay_enabled()


def dispatch_replay_command(
    line: str,
) -> ReplayDispatchResult:
    """Parse ``/replay ...`` and dispatch. NEVER raises — every
    error path produces a friendly text result.

    The dispatcher is sync but ``run`` subcommand wraps Slice 2's
    async engine via ``asyncio.run`` (the REPL caller is the TUI
    thread; the harness async loop is a separate context)."""
    if not _matches(line):
        return ReplayDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ReplayDispatchResult(
            ok=False, text=f"  /replay parse error: {exc}",
        )
    if not tokens:
        return ReplayDispatchResult(ok=False, text="", matched=False)

    args = tokens[1:]
    head = (args[0].lower() if args else "status").strip()

    if head in ("help", "?"):
        return ReplayDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return ReplayDispatchResult(
            ok=False,
            text=(
                "  /replay: master flag disabled — set "
                "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED=true"
            ),
        )

    if head == "status":
        return _status()
    if head == "history":
        limit = 10
        if len(args) >= 2:
            try:
                limit = max(1, min(200, int(args[1])))
            except (TypeError, ValueError):
                return ReplayDispatchResult(
                    ok=False,
                    text=f"  /replay history: invalid N {args[1]!r}",
                )
        return _history(limit)
    if head == "baseline":
        return _baseline()
    if head == "run":
        return _run(args[1:])
    return ReplayDispatchResult(
        ok=False,
        text=f"  /replay: unknown subcommand {head!r}. Try /replay help.",
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _status() -> ReplayDispatchResult:
    """``/replay status`` — concise one-block summary.

    Reads the JSONL ring buffer (flock-safe via Slice 4's reader)
    + computes a fresh ComparisonReport. Total → outcome → quality →
    rec_red% → last verdict timestamp. NEVER raises — degrades to
    empty fields on any I/O fault."""
    try:
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            compare_recent_history,
            read_replay_history,
        )
    except ImportError:
        return ReplayDispatchResult(
            ok=False, text="  /replay: observer module unavailable",
        )

    try:
        history = read_replay_history(limit=200)
    except Exception:  # noqa: BLE001 — defensive
        history = ()
    try:
        report = compare_recent_history()
    except Exception:  # noqa: BLE001 — defensive
        report = None

    lines = ["  /replay status"]
    lines.append(f"  total recorded: {len(history)}")
    if report is not None:
        stats = report.stats
        lines.append(
            f"  outcome:        {report.outcome.value} "
            f"(quality={stats.baseline_quality.value})"
        )
        lines.append(
            f"  rec_red:        "
            f"{stats.recurrence_reduction_pct:.2f}% "
            f"(prev={stats.prevention_count} "
            f"reg={stats.regression_count} "
            f"eq={stats.equivalent_count})"
        )
        if stats.postmortems_in_originals or stats.postmortems_in_counterfactuals:
            lines.append(
                f"  postmortems:    "
                f"orig={stats.postmortems_in_originals} "
                f"cf={stats.postmortems_in_counterfactuals} "
                f"prevented={stats.postmortems_prevented}"
            )
        lines.append(f"  tightening:     {report.tightening}")
    if history:
        last = history[-1]
        verdict = last.verdict
        target = verdict.target
        target_token = ""
        if target is not None:
            target_token = (
                f"{target.session_id} "
                f"{target.swap_at_phase}/"
                f"{target.swap_decision_kind.value}"
            )
        lines.append(
            f"  last verdict:   {verdict.outcome.value}/"
            f"{verdict.verdict.value} "
            f"({target_token})"
        )
    return ReplayDispatchResult(ok=True, text="\n".join(lines))


def _history(limit: int) -> ReplayDispatchResult:
    """``/replay history [N]`` — bounded recent verdicts as one
    line each. Same dense-token shape as LastSessionSummary."""
    try:
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            read_replay_history,
        )
    except ImportError:
        return ReplayDispatchResult(
            ok=False, text="  /replay: observer module unavailable",
        )

    try:
        history = read_replay_history(limit=limit)
    except Exception:  # noqa: BLE001 — defensive
        history = ()

    if not history:
        return ReplayDispatchResult(
            ok=True,
            text=f"  /replay history (last {limit}): <empty>",
        )

    lines = [f"  /replay history (last {len(history)})"]
    for sv in history:
        verdict = sv.verdict
        target = verdict.target
        target_token = ""
        if target is not None:
            target_token = (
                f"{target.session_id}@"
                f"{target.swap_at_phase}/"
                f"{target.swap_decision_kind.value}"
            )
        lines.append(
            f"    {verdict.outcome.value}/"
            f"{verdict.verdict.value} "
            f"{target_token} "
            f"prev_evidence={'Y' if verdict.is_prevention_evidence() else 'N'}"
            + (f" cluster={sv.cluster_kind}" if sv.cluster_kind else "")
        )
    return ReplayDispatchResult(ok=True, text="\n".join(lines))


def _baseline() -> ReplayDispatchResult:
    """``/replay baseline`` — full ComparisonReport detail string.

    Uses Slice 3's ``compose_aggregated_detail`` so operators see
    the same dense tokens that show up in SSE event payloads +
    /observability/replay/baseline GET responses."""
    try:
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            compare_recent_history,
        )
        from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
            compose_aggregated_detail,
        )
    except ImportError:
        return ReplayDispatchResult(
            ok=False, text="  /replay: comparator module unavailable",
        )

    try:
        report = compare_recent_history()
    except Exception:  # noqa: BLE001 — defensive
        return ReplayDispatchResult(
            ok=False,
            text="  /replay baseline: aggregator fault",
        )

    detail = compose_aggregated_detail(report.stats) or "<no data>"
    lines = [
        "  /replay baseline",
        f"  outcome:    {report.outcome.value}",
        f"  tightening: {report.tightening}",
        f"  detail:     {detail}",
    ]
    if report.detail:
        lines.append(f"  reason:     {report.detail}")
    return ReplayDispatchResult(ok=True, text="\n".join(lines))


def _parse_run_args(
    args: Tuple[str, ...],
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], str]:
    """Parse ``run <session> <phase> <kind> [--verdict V]``.

    Returns ``(session_id, swap_phase, swap_kind, verdict, error)``.
    Any None positional → error string set."""
    if len(args) < 3:
        return (
            None, None, None, None,
            "usage: /replay run <session_id> <swap_phase> <swap_kind> "
            "[--verdict V]",
        )
    session_id = args[0].strip()
    swap_phase = args[1].strip()
    swap_kind = args[2].strip().lower()
    verdict: Optional[str] = None
    i = 3
    while i < len(args):
        if args[i] == "--verdict":
            if i + 1 >= len(args):
                return (
                    None, None, None, None,
                    "/replay run: --verdict requires a value",
                )
            verdict = args[i + 1].strip().lower()
            i += 2
        else:
            return (
                None, None, None, None,
                f"/replay run: unknown argument {args[i]!r}",
            )
    if not session_id or not swap_phase or not swap_kind:
        return (
            None, None, None, None,
            "/replay run: empty positional argument",
        )
    return session_id, swap_phase, swap_kind, verdict, ""


def _run(args: Tuple[str, ...]) -> ReplayDispatchResult:
    """``/replay run <session_id> <swap_phase> <swap_kind>
    [--verdict V]`` — manually invoke engine + record verdict.

    Reads cached ledger + summary for the session (zero LLM cost
    by AST-pinned construction). Wraps Slice 2's async engine via
    ``asyncio.run`` since the REPL caller is sync."""
    session_id, swap_phase, swap_kind, verdict_raw, error = _parse_run_args(
        tuple(args),
    )
    if error:
        return ReplayDispatchResult(ok=False, text=f"  {error}")

    # Resolve DecisionOverrideKind from the string. Operators pass
    # canonical lowercase values; we coerce gracefully.
    try:
        from backend.core.ouroboros.governance.verification.counterfactual_replay import (
            DecisionOverrideKind,
            ReplayTarget,
        )
        from backend.core.ouroboros.governance.verification.counterfactual_replay_engine import (
            run_counterfactual_replay,
        )
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            record_replay_verdict,
        )
    except ImportError as exc:
        return ReplayDispatchResult(
            ok=False, text=f"  /replay run: import failed: {exc}",
        )

    try:
        kind_enum = DecisionOverrideKind(swap_kind)
    except ValueError:
        valid = ", ".join(k.value for k in DecisionOverrideKind)
        return ReplayDispatchResult(
            ok=False,
            text=(
                f"  /replay run: unknown swap_kind {swap_kind!r}. "
                f"Valid: {valid}"
            ),
        )

    payload = {}
    if verdict_raw is not None:
        payload = {"verdict": verdict_raw}

    target = ReplayTarget(
        session_id=session_id,
        swap_at_phase=swap_phase,
        swap_decision_kind=kind_enum,
        swap_decision_payload=payload,
    )

    # Run + record. Wraps in asyncio.run since dispatch is sync.
    try:
        result = asyncio.run(run_counterfactual_replay(target))
    except RuntimeError as exc:
        # Already inside an event loop — fallback to a fresh loop.
        if "running event loop" in str(exc):
            try:
                loop = asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(
                        run_counterfactual_replay(target),
                    )
                finally:
                    loop.close()
            except Exception as inner:  # noqa: BLE001 — defensive
                return ReplayDispatchResult(
                    ok=False,
                    text=f"  /replay run: engine failed: {inner}",
                )
        else:
            return ReplayDispatchResult(
                ok=False,
                text=f"  /replay run: engine failed: {exc}",
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ReplayDispatchResult(
            ok=False, text=f"  /replay run: engine raised: {exc}",
        )

    record_outcome = record_replay_verdict(result)

    lines = [
        "  /replay run",
        f"  target:        {session_id} @ {swap_phase}/{swap_kind}",
    ]
    if verdict_raw:
        lines.append(f"  verdict-hint:  {verdict_raw}")
    lines.append(f"  outcome:       {result.outcome.value}")
    lines.append(f"  branch_verdict: {result.verdict.value}")
    lines.append(
        f"  prevention_evidence: "
        f"{'Y' if result.is_prevention_evidence() else 'N'}"
    )
    lines.append(f"  recorded:      {record_outcome.value}")
    if result.detail:
        lines.append(f"  detail:        {result.detail[:160]}")
    return ReplayDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "ReplayDispatchResult",
    "dispatch_replay_command",
]
