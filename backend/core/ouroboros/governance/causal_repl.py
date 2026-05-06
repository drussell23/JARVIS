"""§31 U2 empirical wiring — Slice 4 ``/causal`` REPL.

Operator-facing read-only browser for an op's causal lineage.
Auto-discovered by the §32.11 Slice 4
``repl_dispatch_registry`` via the canonical naming-cage
convention (file ends ``_repl.py`` → verb ``/causal`` →
dispatcher ``dispatch_causal_command(line)``).

Subcommands:

  * ``/causal``                      — render the singleton
                                       observer's recently-
                                       observed transitions
  * ``/causal show <session>:<rec>`` — feature digest for one op
  * ``/causal show <session> <rec>`` — same, space-separated
  * ``/causal help``                 — bypass-master help

Architectural locks (mirrors :mod:`replay_repl`):

  * **Read-only** — REPL NEVER calls
    :func:`apply_replay_from_record_env` or any other mutating
    surface (AST-pinned).
  * **Authority asymmetry** — substrate purity (no orchestrator
    / iron_gate / policy / providers / candidate_generator
    imports; AST-pinned).
  * **Composes Slice 1** — REPL reads via
    :func:`compute_op_causal_features` only; never duplicates
    feature extraction (AST-pinned).
  * **NEVER raises** — every dispatch path returns a structured
    result instead.

Identity preservation: cyan default, yellow advisory, red
critical, dim metadata. NO ``bright_green`` (§37.9 invariant
#3 + Slice 4 lint pin).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class CausalReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/causal — causal-lineage browser"
    f"{_RESET}\n"
    f"  {_DIM}Read-only operator surface for the §31 U2 "
    f"empirical-wiring substrate. Composes the canonical "
    f"CausalityDAG via compute_op_causal_features.{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/causal{_RESET}                       "
    f"{_DIM}observer recent transitions{_RESET}\n"
    f"    {_CYAN}/causal show <session>:<rec>{_RESET}  "
    f"{_DIM}feature digest for one op{_RESET}\n"
    f"    {_CYAN}/causal show <session> <rec>{_RESET}  "
    f"{_DIM}space-separated form{_RESET}\n"
    f"    {_CYAN}/causal help{_RESET}                  "
    f"{_DIM}this message{_RESET}\n"
    f"\n"
    f"  {_BOLD}Master flag:{_RESET}\n"
    f"    {_DIM}JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED{_RESET}\n"
    f"  {_BOLD}Observer flag:{_RESET}\n"
    f"    {_DIM}JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED{_RESET}\n"
)


_SESSION_REC_RE = re.compile(r"^([^:\s]+):([^:\s]+)$")


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/causal"
        or s == "causal"
        or s.startswith("/causal ")
        or s.startswith("causal ")
    )


def _color_for_advice(advice: str) -> str:
    a = (advice or "").lower()
    if a in ("recurrence_warning", "deep_lineage_harden"):
        return _YELLOW
    if a in ("disabled",):
        return _DIM
    if a in ("sibling_dedup",):
        return _CYAN
    return _CYAN


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_observer_state() -> str:
    """Render the singleton observer's per-key advice state map."""
    try:
        from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
            get_default_observer, is_observer_enabled,
        )
    except ImportError:
        return (
            f"\n  {_RED}causal_advisory_observer substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        observer = get_default_observer()
    except Exception:  # noqa: BLE001 — defensive
        return (
            f"\n  {_RED}observer construction failed{_RESET}\n"
        )
    state = dict(getattr(observer, "_last_advice", {}) or {})
    out = [
        f"\n  {_BOLD}{_CYAN}/causal — observer state{_RESET}  "
        f"{_DIM}(master={'on' if is_observer_enabled() else 'off'}){_RESET}",
        "",
    ]
    if not state:
        out.append(
            f"  {_DIM}No advice transitions recorded yet. "
            f"Observer fires on cross-advice transitions only "
            f"(chatter-suppressed){_RESET}"
        )
    else:
        out.append(
            f"  {_DIM}Last observed advice per "
            f"(session, record):{_RESET}"
        )
        out.append("")
        for (sid, rid), advice in sorted(state.items())[:50]:
            out.append(
                f"    {_DIM}{sid[:24]}:{rid[:24]}{_RESET}  "
                f"{_color_for_advice(advice)}{advice}{_RESET}"
            )
    out.append("")
    return "\n".join(out) + "\n"


def _render_show_features(
    session_id: str, record_id: str,
) -> str:
    try:
        from backend.core.ouroboros.governance.causality_consumer import (  # noqa: E501
            compute_op_causal_features,
            is_consumer_enabled,
        )
    except ImportError:
        return (
            f"\n  {_RED}causality_consumer substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        features = compute_op_causal_features(
            session_id=session_id, record_id=record_id,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return (
            f"\n  {_RED}compute raised: {exc}{_RESET}\n"
        )
    advice_value = features.advice.value
    out = [
        f"\n  {_BOLD}{_CYAN}/causal {session_id}:{record_id}"
        f"{_RESET}  {_DIM}(consumer="
        f"{'on' if is_consumer_enabled() else 'off'}){_RESET}",
        "",
        f"  {_DIM}schema_version:{_RESET}      "
        f"{features.schema_version}",
        f"  {_DIM}advice:{_RESET}              "
        f"{_color_for_advice(advice_value)}{advice_value}"
        f"{_RESET}",
        f"  {_DIM}ancestor_count:{_RESET}      "
        f"{features.ancestor_count}",
        f"  {_DIM}sibling_count:{_RESET}       "
        f"{features.sibling_count}",
        f"  {_DIM}recurrence_score:{_RESET}    "
        f"{features.recurrence_score:.2f}",
        f"  {_DIM}distinct_phases:{_RESET}     "
        f"{', '.join(features.distinct_phases_in_lineage) or '(none)'}",
    ]
    if features.parent_decisions_summary:
        out.append(
            f"  {_DIM}parents:{_RESET}             "
            f"{features.parent_decisions_summary[:120]}"
        )
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_causal_command(
    line: str,
) -> CausalReplDispatchResult:
    if not _matches(line):
        return CausalReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CausalReplDispatchResult(
            ok=False, text=f"  /causal parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return CausalReplDispatchResult(ok=True, text=_HELP)

    try:
        if head == "":
            return CausalReplDispatchResult(
                ok=True, text=_render_observer_state(),
            )
        if head == "show":
            if len(args) < 2:
                return CausalReplDispatchResult(
                    ok=False,
                    text=(
                        "  /causal show <session>:<record_id> "
                        "— argument required"
                    ),
                )
            target = args[1]
            match = _SESSION_REC_RE.match(target)
            if match:
                session, rec = (
                    match.group(1), match.group(2),
                )
                return CausalReplDispatchResult(
                    ok=True,
                    text=_render_show_features(session, rec),
                )
            # Space-separated form: show <session> <rec>
            if len(args) >= 3:
                return CausalReplDispatchResult(
                    ok=True,
                    text=_render_show_features(
                        args[1], args[2],
                    ),
                )
            return CausalReplDispatchResult(
                ok=False,
                text=(
                    "  /causal show: pass <session>:<rec> "
                    "or <session> <rec>"
                ),
            )
        return CausalReplDispatchResult(
            ok=False,
            text=(
                f"  /causal: unknown subcommand "
                f"{head!r} — try /causal help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return CausalReplDispatchResult(
            ok=False,
            text=f"  /causal: error — {exc}. Try again.",
        )


# ---------------------------------------------------------------------------
# Help dispatcher hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="causal",
            description=(
                "Causal-lineage browser — composes the §31 U2 "
                "empirical-wiring substrate. Read-only."
            ),
            posture_relevance="RELEVANT",
            since="§31 U2 Slice 4 (PRD §31.3, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``causal_repl_authority_read_only`` — REPL is read-only.
         Forbids ``apply_replay_from_record_env`` /
         ``DecisionRuntime.record`` mutating calls.
      2. ``causal_repl_authority_asymmetry`` — substrate purity.
         Forbids orchestrator+iron_gate+policy+providers+
         candidate_generator+urgency_router+change_engine+
         semantic_guardian imports.
      3. ``causal_repl_composes_slice_1`` — REPL composes
         compute_op_causal_features ONLY; no parallel feature
         extraction.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/causal_repl.py"
    )

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_calls = (
            "apply_replay_from_record_env",
            "prepare_replay_from_record",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    if fn.id in forbidden_calls:
                        violations.append(
                            f"causal_repl.py MUST NOT call "
                            f"{fn.id}() — read-only browser"
                        )
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "record"
                    and isinstance(fn.value, ast.Name)
                ):
                    rcv = fn.value.id.lower()
                    if (
                        "decision" in rcv
                        or "runtime" in rcv
                        or "ledger" in rcv
                    ):
                        violations.append(
                            "causal_repl.py is read-only; "
                            "MUST NOT call .record() on a "
                            "decision/runtime/ledger receiver"
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
                            f"causal_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_slice_1(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        found_compose = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "causality_consumer" in node.module
                ):
                    for alias in node.names:
                        if (
                            alias.name
                            == "compute_op_causal_features"
                        ):
                            found_compose = True
        if not found_compose:
            violations.append(
                "causal_repl.py MUST compose "
                "causality_consumer.compute_op_causal_features "
                "(no parallel feature extraction)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="causal_repl_authority_read_only",
            target_file=target,
            description=(
                "§31 U2 Slice 4 — REPL is read-only browser."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="causal_repl_authority_asymmetry",
            target_file=target,
            description=(
                "§31 U2 Slice 4 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="causal_repl_composes_slice_1",
            target_file=target,
            description=(
                "§31 U2 Slice 4 — single pipeline; composes "
                "Slice 1 substrate only."
            ),
            validate=_validate_composes_slice_1,
        ),
    ]


__all__ = [
    "CausalReplDispatchResult",
    "dispatch_causal_command",
    "register_shipped_invariants",
    "register_verbs",
]
