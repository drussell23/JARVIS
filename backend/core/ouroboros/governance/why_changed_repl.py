"""§37 Slice 3 — `/why_changed` REPL surface composing
:mod:`autonomy.feedback_engine.AutonomyFeedbackEngine`.

Closes Tier 1 #7 from the §37 UX roadmap: surfaces the L1↔L4
outcome-feedback loop reasoning that until now was scoped to
the engine's private state. Per the operator binding "fully
leverage existing files... build cleanly on what already
exists":

  * The ``AutonomyFeedbackEngine`` already tracks every signal
    needed (``_rollback_counts`` per brain, ``_seen_files``
    cursor, ``_brain_hint_threshold``). What was missing was a
    read-side surface for operators.
  * Slice 3 adds: (a) the canonical singleton accessor
    ``get_default_engine()`` matching the §37-pattern from
    Slices 1+2 (``ComponentHealthTracker`` / ``StreamEventBroker``
    singletons); (b) defensive read-only snapshot methods on
    the engine; (c) this REPL surface composing them.
  * No parallel state. No new tracker. The engine remains the
    single source of truth for L2 advisory decisions.

Operator question this surface answers:

  > "Why did O+V just change a brain hint / advance a backlog
  > item / score an attribution? Show me the reasoning."

Architectural locks (operator binding 2026-05-05):

  * **Single pipeline** — read state via
    ``feedback_engine.get_default_engine()`` ONLY. Forbidden to
    construct a new ``AutonomyFeedbackEngine`` here. AST-pinned.
  * **Authority asymmetry / read-only** — REPL NEVER calls
    mutating methods (``consume_curriculum_once`` /
    ``consume_reactor_events_once`` / ``register_event_handlers``
    / ``score_attribution_once`` / etc.). Dashboard observes;
    L2 producers run the engine.
  * **Auto-discovered** — file ends `_repl.py` per §32.11
    Slice 4 naming-cage; verb name ``why_changed`` (underscore
    form — matches Python identifier conventions per the
    cage); dispatcher ``dispatch_why_changed_command(line)``.
  * **Honest empty-state** — when no engine is registered yet
    (cold-start), renders a transparent guidance line rather
    than fabricating data.
  * **NEVER raises** — pure-function dispatch.

Subcommands:

  * ``/why_changed`` (bare)        — overview: brains at
                                     threshold + total rollback
                                     counts + recent processed
                                     files
  * ``/why_changed brains``        — per-brain rollback counts
                                     (sorted by count descending)
  * ``/why_changed at_threshold``  — only brains meeting / over
                                     threshold (next rollback
                                     emits ADJUST_BRAIN_HINT)
  * ``/why_changed files [N]``     — recent processed curriculum
                                     / reactor signal files
  * ``/why_changed config``        — engine config knobs
                                     (threshold, max_backlog,
                                     attribution_interval)
  * ``/why_changed help``          — bypass-master help

Identity preservation: state coloring respects palette
(rollback_count >= threshold = red / 1+ = yellow / 0 = dim).
NO ``bright_green`` in chrome (pinned by Slice 4).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import List, Optional


# ---------------------------------------------------------------------------
# ANSI palette — identity-consistent
# ---------------------------------------------------------------------------


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class WhyChangedReplDispatchResult:
    """Result of a ``/why_changed`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/why_changed`` invocation."""

    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/why_changed — feedback-engine reasoning"
    f"{_RESET}\n"
    f"  {_DIM}Read-only operator view of the L2 outcome-feedback "
    f"loop: which brains accumulated rollbacks, which curriculum "
    f"signals were processed, which advisory hints were emitted."
    f"{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/why_changed{_RESET}                "
    f"{_DIM}overview: brains at threshold + counts{_RESET}\n"
    f"    {_CYAN}/why_changed brains{_RESET}         "
    f"{_DIM}per-brain rollback counts{_RESET}\n"
    f"    {_CYAN}/why_changed at_threshold{_RESET}   "
    f"{_DIM}only brains meeting/over hint threshold{_RESET}\n"
    f"    {_CYAN}/why_changed files [N]{_RESET}      "
    f"{_DIM}recent processed signal files{_RESET}\n"
    f"    {_CYAN}/why_changed config{_RESET}         "
    f"{_DIM}engine config knobs{_RESET}\n"
    f"    {_CYAN}/why_changed help{_RESET}           "
    f"{_DIM}this message{_RESET}\n"
)

_DEFAULT_FILE_LIMIT = 10
_MAX_FILE_LIMIT = 200


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/why_changed"
        or s == "why_changed"
        or s.startswith("/why_changed ")
        or s.startswith("why_changed ")
    )


def _color_for_count(count: int, threshold: int) -> str:
    """Identity-consistent rollback-count coloring."""
    if count >= threshold:
        return _RED
    if count > 0:
        return _YELLOW
    return _DIM


def _engine_unavailable_message() -> str:
    """Honest empty-state — no fabricated data."""
    return (
        f"\n  {_BOLD}{_CYAN}Feedback Engine{_RESET}\n"
        f"  {_DIM}No engine registered yet — the L2 advisory "
        f"layer hasn't constructed an AutonomyFeedbackEngine "
        f"in this process. Engine state will surface here once "
        f"the layer boots.{_RESET}\n"
    )


def _parse_limit(args: List[str]) -> int:
    if not args:
        return _DEFAULT_FILE_LIMIT
    try:
        n = int(args[0])
    except (ValueError, TypeError):
        return _DEFAULT_FILE_LIMIT
    if n < 1:
        return 1
    if n > _MAX_FILE_LIMIT:
        return _MAX_FILE_LIMIT
    return n


# ---------------------------------------------------------------------------
# Renderers — read via canonical singleton ONLY (single-pipeline guardrail)
# ---------------------------------------------------------------------------


def _get_engine() -> Optional[object]:
    """Lazy-import the canonical singleton accessor. NEVER
    raises; returns None on import failure or if no engine has
    been registered yet."""
    try:
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (  # noqa: E501
            get_default_engine,
        )
    except ImportError:
        return None
    try:
        return get_default_engine()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _render_overview() -> str:
    engine = _get_engine()
    if engine is None:
        return _engine_unavailable_message()
    counts = engine.rollback_counts_snapshot()
    threshold = engine.brain_hint_threshold()
    at_threshold = engine.brains_at_threshold()
    seen = engine.seen_files_snapshot()
    n_brains = len(counts)
    n_at_thresh = len(at_threshold)
    n_files = len(seen)
    out = [
        f"\n  {_BOLD}{_CYAN}Feedback Engine{_RESET}",
        f"  brains_tracked={_BOLD}{n_brains}{_RESET}  "
        f"at_threshold={_RED if n_at_thresh else _DIM}"
        f"{n_at_thresh}{_RESET}  "
        f"signal_files_processed={_DIM}{n_files}{_RESET}  "
        f"hint_threshold={_DIM}{threshold}{_RESET}",
        "",
    ]
    if at_threshold:
        out.append(
            f"  {_BOLD}{_RED}Brains at hint threshold{_RESET}  "
            f"{_DIM}(next rollback fires ADJUST_BRAIN_HINT)"
            f"{_RESET}",
        )
        for bid in at_threshold:
            count = counts.get(bid, 0)
            out.append(
                f"    {_RED}● {_BOLD}{bid}{_RESET}  "
                f"{_DIM}rollbacks={count}/"
                f"{threshold}{_RESET}",
            )
        out.append("")
    if not at_threshold and n_brains == 0 and n_files == 0:
        out.append(
            f"  {_DIM}Engine is idle — no rollbacks recorded "
            f"and no curriculum/reactor signals processed "
            f"yet.{_RESET}",
        )
    elif not at_threshold:
        out.append(
            f"  {_GREEN}No brains at hint threshold.{_RESET}",
        )
    out.append("")
    out.append(
        f"  {_DIM}Use /why_changed brains for full per-brain "
        f"detail.{_RESET}",
    )
    return "\n".join(out) + "\n"


def _render_brains() -> str:
    engine = _get_engine()
    if engine is None:
        return _engine_unavailable_message()
    counts = engine.rollback_counts_snapshot()
    threshold = engine.brain_hint_threshold()
    if not counts:
        return (
            f"\n  {_DIM}No rollback counts recorded yet — no "
            f"OP_ROLLED_BACK events have flowed into the "
            f"engine.{_RESET}\n"
        )
    # Sort by count descending, then brain_id ascending.
    sorted_brains = sorted(
        counts.items(),
        key=lambda t: (-t[1], t[0]),
    )
    out = [
        f"\n  {_BOLD}{_CYAN}Per-Brain Rollback Counts{_RESET}  "
        f"{_DIM}({len(sorted_brains)} brains, threshold="
        f"{threshold}){_RESET}",
        "",
    ]
    for bid, count in sorted_brains:
        color = _color_for_count(count, threshold)
        marker = (
            f"{_RED}●{_RESET}" if count >= threshold
            else (f"{_YELLOW}○{_RESET}" if count > 0
                  else f"{_DIM}·{_RESET}")
        )
        out.append(
            f"  {marker} {_BOLD}{bid}{_RESET}  "
            f"{color}rollbacks={count}{_RESET}",
        )
    return "\n".join(out) + "\n"


def _render_at_threshold() -> str:
    engine = _get_engine()
    if engine is None:
        return _engine_unavailable_message()
    at_threshold = engine.brains_at_threshold()
    threshold = engine.brain_hint_threshold()
    if not at_threshold:
        return (
            f"\n  {_GREEN}No brains at hint threshold "
            f"({threshold}). All brains within tolerance."
            f"{_RESET}\n"
        )
    counts = engine.rollback_counts_snapshot()
    out = [
        f"\n  {_BOLD}{_RED}Brains at hint threshold{_RESET}  "
        f"{_DIM}(threshold={threshold}, "
        f"{len(at_threshold)} brains){_RESET}",
        f"  {_DIM}Next rollback for any of these emits an "
        f"ADJUST_BRAIN_HINT advisory command.{_RESET}",
        "",
    ]
    for bid in at_threshold:
        count = counts.get(bid, 0)
        out.append(
            f"  {_RED}●{_RESET} {_BOLD}{bid}{_RESET}  "
            f"{_RED}rollbacks={count}{_RESET}",
        )
    return "\n".join(out) + "\n"


def _render_files(limit: int) -> str:
    engine = _get_engine()
    if engine is None:
        return _engine_unavailable_message()
    seen = engine.seen_files_snapshot()
    if not seen:
        return (
            f"\n  {_DIM}No curriculum/reactor signal files "
            f"processed yet.{_RESET}\n"
        )
    # Most-recent-first via reverse + clamp to limit.
    recent = list(reversed(seen))[:limit]
    out = [
        f"\n  {_BOLD}{_CYAN}Recent Signal Files{_RESET}  "
        f"{_DIM}(showing {len(recent)} most-recent of "
        f"{len(seen)}){_RESET}",
        "",
    ]
    for filename in recent:
        # Type prefix coloring — curriculum_*.json (cyan) vs
        # reactor_*.json (yellow). Matches the existing engine's
        # discrimination.
        if filename.startswith("curriculum_"):
            type_color = _CYAN
        elif filename.startswith("reactor_"):
            type_color = _YELLOW
        else:
            type_color = _DIM
        out.append(
            f"  {type_color}{filename}{_RESET}",
        )
    return "\n".join(out) + "\n"


def _render_config() -> str:
    engine = _get_engine()
    if engine is None:
        return _engine_unavailable_message()
    threshold = engine.brain_hint_threshold()
    # Read engine internal config defensively (don't raise on
    # missing attrs).
    config = getattr(engine, "_config", None)
    out = [
        f"\n  {_BOLD}{_CYAN}Engine Config{_RESET}",
        "",
        f"  {_DIM}brain_hint_threshold:{_RESET}     "
        f"{threshold}",
    ]
    if config is not None:
        max_backlog = getattr(
            config, "max_backlog_entries_per_curriculum",
            "(unset)",
        )
        attribution_interval = getattr(
            config, "attribution_interval_s", "(unset)",
        )
        event_dir = getattr(config, "event_dir", "(unset)")
        state_dir = getattr(config, "state_dir", "(unset)")
        out.extend([
            f"  {_DIM}max_backlog_per_curriculum:{_RESET} "
            f"{max_backlog}",
            f"  {_DIM}attribution_interval_s:{_RESET}   "
            f"{attribution_interval}",
            f"  {_DIM}event_dir:{_RESET}                 "
            f"{event_dir}",
            f"  {_DIM}state_dir:{_RESET}                 "
            f"{state_dir}",
        ])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher (auto-mounted via repl_dispatch_registry)
# ---------------------------------------------------------------------------


def dispatch_why_changed_command(
    line: str,
) -> WhyChangedReplDispatchResult:
    """Parse a ``/why_changed`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return WhyChangedReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return WhyChangedReplDispatchResult(
            ok=False,
            text=f"  /why_changed parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return WhyChangedReplDispatchResult(
            ok=True, text=_HELP,
        )

    try:
        if head == "":
            return WhyChangedReplDispatchResult(
                ok=True, text=_render_overview(),
            )
        if head == "brains":
            return WhyChangedReplDispatchResult(
                ok=True, text=_render_brains(),
            )
        if head == "at_threshold":
            return WhyChangedReplDispatchResult(
                ok=True, text=_render_at_threshold(),
            )
        if head == "files":
            limit = _parse_limit(args[1:])
            return WhyChangedReplDispatchResult(
                ok=True, text=_render_files(limit),
            )
        if head == "config":
            return WhyChangedReplDispatchResult(
                ok=True, text=_render_config(),
            )
        return WhyChangedReplDispatchResult(
            ok=False,
            text=(
                f"  /why_changed: unknown subcommand "
                f"{head!r} — try /why_changed help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return WhyChangedReplDispatchResult(
            ok=False,
            text=(
                f"  /why_changed: error reading engine — "
                f"{exc}. Try again after subsystems boot."
            ),
        )


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    """Auto-discovered by `help_dispatcher`. Registers the
    `/why_changed` verb in the operator-facing /help index."""
    try:
        registry.register(
            verb="why_changed",
            description=(
                "Feedback-engine reasoning — read-only view of "
                "the L2 outcome-feedback loop: per-brain "
                "rollback counts, brains at hint threshold, "
                "recent processed signal files, engine config."
            ),
            posture_relevance="RELEVANT",
            since="§37 Slice 3 (PRD §36.5, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins (auto-discovered via shipped_code_invariants)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``why_changed_repl_composes_canonical_engine`` — module
         reads via `get_default_engine()` ONLY; never constructs
         `AutonomyFeedbackEngine()` directly.
      2. ``why_changed_repl_authority_read_only`` — module NEVER
         calls mutating engine methods (consume_curriculum_once /
         consume_reactor_events_once / register_event_handlers /
         score_attribution_once). Read-only operator surface.
      3. ``why_changed_repl_authority_asymmetry`` — substrate
         purity (no orchestrator / iron_gate / providers imports).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/why_changed_repl.py"
    )

    def _validate_composes_canonical_engine(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "AutonomyFeedbackEngine"
                ):
                    violations.append(
                        "why_changed_repl.py MUST NOT construct "
                        "AutonomyFeedbackEngine() directly — "
                        "compose get_default_engine() (single-"
                        "pipeline guardrail)"
                    )
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "AutonomyFeedbackEngine"
                ):
                    violations.append(
                        "why_changed_repl.py MUST NOT construct "
                        "AutonomyFeedbackEngine() via attribute "
                        "access"
                    )
        return tuple(violations)

    def _validate_authority_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT call mutating engine methods."""
        violations: list = []
        forbidden_methods = (
            "consume_curriculum_once",
            "consume_reactor_events_once",
            "register_event_handlers",
            "score_attribution_once",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in forbidden_methods:
                    continue
                # Heuristic: receiver named "engine" or ends
                # with "_engine".
                receiver = func.value
                if (
                    isinstance(receiver, ast.Name)
                    and (
                        receiver.id == "engine"
                        or receiver.id.endswith("_engine")
                    )
                ):
                    violations.append(
                        f"why_changed_repl.py MUST NOT call "
                        f"engine.{func.attr}(...) — read-only "
                        f"operator surface"
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
                            f"why_changed_repl.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "why_changed_repl_composes_canonical_engine"
            ),
            target_file=target,
            description=(
                "§37 Slice 3 — single-pipeline guardrail: "
                "module composes get_default_engine() "
                "singleton; never constructs "
                "AutonomyFeedbackEngine directly."
            ),
            validate=_validate_composes_canonical_engine,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "why_changed_repl_authority_read_only"
            ),
            target_file=target,
            description=(
                "§37 Slice 3 — read-only operator surface: "
                "module MUST NOT call any of the engine's "
                "mutating methods (consume_curriculum_once / "
                "consume_reactor_events_once / "
                "register_event_handlers / "
                "score_attribution_once)."
            ),
            validate=_validate_authority_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "why_changed_repl_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Slice 3 — substrate purity: no "
                "orchestrator / iron_gate / policy / providers "
                "/ candidate_generator imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "WhyChangedReplDispatchResult",
    "dispatch_why_changed_command",
    "register_shipped_invariants",
    "register_verbs",
]
