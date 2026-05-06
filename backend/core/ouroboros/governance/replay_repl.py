"""§37 Tier 2 #10 — `/replay` REPL surface composing canonical
deterministic-replay substrate.

Closes Tier 2 #10 from the §37 UX roadmap (PRD §36.4 Priority #2
Temporal Observability spine — the biggest cognitive-depth
multiplier per the brutal review). Operators can:

  * Browse recorded sessions with replay-eligible state
  * Inspect distinct phases per session (fork-boundary candidates)
  * Resolve a (session, phase) pair to its canonical fork-point
    record_id — the same input the existing `--rerun-from
    <record-id>` CLI flag accepts
  * Render the replay plan that would execute (without firing it)

The actual `--rerun-from <session>:<phase>` re-execution lives at
the harness CLI level (`scripts/ouroboros_battle_test.py`); this
REPL is the BROWSE + INSPECT surface that lets operators
navigate the DAG before committing to a re-execution.

Per the operator binding "fully leverage existing files... no
duplication":

  * Composes `verification/causality_dag.build_dag(session_id)`
    + the new `nodes_for_phase` / `first_record_in_phase` /
    `distinct_phases` helpers (PRD §37 Tier 2 #10 extension)
  * Composes `verification/replay_from_record.prepare_replay_from_record`
    for plan rendering — same machinery the harness CLI uses
  * NO new state, NO parallel registry, NO duplicate DAG
    construction

Architectural locks:

  * Single pipeline (canonical DAG + replay machinery only;
    AST-pinned no parallel construction)
  * Authority asymmetry / read-only — REPL NEVER triggers a
    re-execution; that's the harness's job. AST-pinned (no
    `apply_replay_from_record_env` calls in this module).
  * Auto-discovered via §32.11 Slice 4 naming-cage convention:
    `replay_repl.py` → verb `/replay` → dispatcher
    `dispatch_replay_command(line)`.
  * Honest empty-state — sessions without DAG data render an
    accurate "no replay-eligible state" message.
  * NEVER raises.

Subcommands:

  * ``/replay`` (bare)              — list sessions with
                                      replay-eligible DAG data
  * ``/replay sessions``            — same as bare
  * ``/replay phases <session>``    — distinct phases recorded
                                      in this session (fork
                                      candidates)
  * ``/replay show <session>:<phase>`` — resolve to
                                          first-record-in-phase +
                                          render fork plan
  * ``/replay show <session>``      — render full session DAG
                                      summary (record count + phase
                                      list + edge count)
  * ``/replay help``                — bypass-master help

Identity preservation: NO `bright_green` in chrome (§37.9
invariant #3, Slice 4 lint pin). Phase coloring uses the same
palette discipline as `/show_plan` (cyan for canonical,
yellow for warnings).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


# ---------------------------------------------------------------------------
# Frozen result envelope (mirrors §37 Slice 1-3 / 6 pattern)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/replay — deterministic-replay browser"
    f"{_RESET}\n"
    f"  {_DIM}Read-only operator surface for the canonical "
    f"causality DAG. Resolves (session, phase) pairs to "
    f"fork-boundary record_ids — the input format the harness "
    f"CLI's `--rerun-from <record-id>` flag accepts.{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/replay{_RESET}                       "
    f"{_DIM}list sessions with replay-eligible DAG{_RESET}\n"
    f"    {_CYAN}/replay sessions{_RESET}              "
    f"{_DIM}same as bare{_RESET}\n"
    f"    {_CYAN}/replay phases <session>{_RESET}      "
    f"{_DIM}distinct phases in this session{_RESET}\n"
    f"    {_CYAN}/replay show <session>{_RESET}        "
    f"{_DIM}full DAG summary for one session{_RESET}\n"
    f"    {_CYAN}/replay show <session>:<phase>{_RESET} "
    f"{_DIM}fork-point record_id for (session, phase){_RESET}\n"
    f"    {_CYAN}/replay help{_RESET}                  "
    f"{_DIM}this message{_RESET}\n"
    f"\n"
    f"  {_BOLD}Workflow:{_RESET}\n"
    f"    {_DIM}1. /replay sessions          — find a session"
    f"{_RESET}\n"
    f"    {_DIM}2. /replay phases <session>  — pick fork phase"
    f"{_RESET}\n"
    f"    {_DIM}3. /replay show <session>:<phase>  — copy "
    f"record_id{_RESET}\n"
    f"    {_DIM}4. cd <repo> && python3 scripts/"
    f"ouroboros_battle_test.py --rerun <session> "
    f"--rerun-from <record_id>{_RESET}\n"
)


# Canonical (session, phase) form — matches `<session>:<phase>`.
# Session IDs are typically `bt-YYYY-MM-DD-HHMMSS`; phases are
# UPPERCASE (CLASSIFY / ROUTE / GENERATE / VALIDATE / etc.).
# Pattern is permissive: anything before the colon is session;
# anything after is phase.
_SESSION_PHASE_RE = re.compile(r"^([^:]+):([^:]+)$")


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/replay"
        or s == "replay"
        or s.startswith("/replay ")
        or s.startswith("replay ")
    )


def _color_for_phase(phase: str) -> str:
    """Identity-consistent phase coloring (cyan default; yellow
    for warning phases like VALIDATE/GATE; dim for trivial/
    boundary phases)."""
    p = (phase or "").upper()
    if p in ("ERROR", "FAILED", "ROLLBACK", "ABORTED"):
        return _RED
    if p in ("VALIDATE", "GATE", "APPROVE"):
        return _YELLOW
    if p in ("CLASSIFY", "ROUTE", "COMPLETE", ""):
        return _DIM
    return _CYAN


# ---------------------------------------------------------------------------
# Session discovery — read .ouroboros/sessions/ + decisions ledger filter
# ---------------------------------------------------------------------------


def _discover_replay_eligible_sessions() -> List[str]:
    """Return session IDs that have decisions.jsonl ledger
    files (replay-eligible). NEVER raises.

    Composes the canonical session storage layout (
    `.ouroboros/sessions/<session_id>/decisions.jsonl`) without
    duplicating path resolution. Empty list on missing dir."""
    try:
        sessions_root = (
            Path.cwd() / ".ouroboros" / "sessions"
        )
        if not sessions_root.exists():
            return []
        eligible: List[str] = []
        for entry in sorted(
            sessions_root.iterdir(), reverse=True,
        ):
            try:
                if not entry.is_dir():
                    continue
                ledger = entry / "decisions.jsonl"
                if ledger.exists() and ledger.stat().st_size > 0:
                    eligible.append(entry.name)
            except OSError:
                continue
        return eligible
    except Exception:  # noqa: BLE001 — defensive
        return []


def _build_dag_for_session(session_id: str) -> Any:
    """Compose canonical DAG builder. NEVER raises — returns
    None on any error so caller renders honest empty-state."""
    try:
        from backend.core.ouroboros.governance.verification.causality_dag import (  # noqa: E501
            build_dag,
        )
        return build_dag(session_id=session_id)
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_sessions() -> str:
    sessions = _discover_replay_eligible_sessions()
    if not sessions:
        return (
            f"\n  {_BOLD}{_CYAN}Replay-Eligible Sessions"
            f"{_RESET}\n"
            f"  {_DIM}No sessions with `decisions.jsonl` "
            f"ledgers found in `.ouroboros/sessions/`. The "
            f"DecisionTraceLedger flag (graduated default-TRUE "
            f"2026-05-05) populates this surface as ops fire."
            f"{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Replay-Eligible Sessions{_RESET}  "
        f"{_DIM}(showing {len(sessions)} most recent){_RESET}",
        "",
    ]
    for sid in sessions[:50]:
        out.append(f"  {_CYAN}{sid}{_RESET}")
    if len(sessions) > 50:
        out.append(
            f"  {_DIM}... +{len(sessions) - 50} more{_RESET}"
        )
    out.append("")
    out.append(
        f"  {_DIM}Use /replay phases <session> to inspect "
        f"available fork-boundary phases.{_RESET}"
    )
    return "\n".join(out) + "\n"


def _render_phases(session_id: str) -> str:
    dag = _build_dag_for_session(session_id)
    if dag is None or dag.is_empty:
        return (
            f"\n  {_RED}No DAG data for session "
            f"{session_id!r}.{_RESET}\n"
            f"  {_DIM}Verify the session has a populated "
            f"`decisions.jsonl` ledger; the master flag "
            f"`JARVIS_DECISION_TRACE_LEDGER_ENABLED` must be "
            f"on (graduated default-TRUE 2026-05-05).{_RESET}\n"
        )
    phases = dag.distinct_phases()
    if not phases:
        return (
            f"\n  {_DIM}Session {session_id!r} has DAG records "
            f"but no `phase` field set on any record.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Distinct phases — {session_id}"
        f"{_RESET}  {_DIM}({len(phases)} phases, "
        f"{dag.node_count} records){_RESET}",
        "",
    ]
    for phase in phases:
        n_records = len(dag.nodes_for_phase(phase))
        out.append(
            f"  {_color_for_phase(phase)}{phase}{_RESET}  "
            f"{_DIM}({n_records} records){_RESET}"
        )
    out.append("")
    out.append(
        f"  {_DIM}Use /replay show {session_id}:<phase> for "
        f"the fork-boundary record_id.{_RESET}"
    )
    return "\n".join(out) + "\n"


def _render_show_session(session_id: str) -> str:
    dag = _build_dag_for_session(session_id)
    if dag is None or dag.is_empty:
        return (
            f"\n  {_RED}No DAG data for session "
            f"{session_id!r}.{_RESET}\n"
        )
    phases = dag.distinct_phases()
    out = [
        f"\n  {_BOLD}{_CYAN}Session DAG — {session_id}"
        f"{_RESET}",
        "",
        f"  {_DIM}records:  {_RESET}{dag.node_count}",
        f"  {_DIM}edges:    {_RESET}{dag.edge_count}",
        f"  {_DIM}phases:   {_RESET}{len(phases)}",
        "",
        f"  {_BOLD}Phases (in order):{_RESET}",
    ]
    for phase in phases:
        n_records = len(dag.nodes_for_phase(phase))
        out.append(
            f"    {_color_for_phase(phase)}{phase}{_RESET}  "
            f"{_DIM}({n_records} records){_RESET}"
        )
    return "\n".join(out) + "\n"


def _render_show_session_phase(
    session_id: str, phase: str,
) -> str:
    dag = _build_dag_for_session(session_id)
    if dag is None or dag.is_empty:
        return (
            f"\n  {_RED}No DAG data for session "
            f"{session_id!r}.{_RESET}\n"
        )
    record = dag.first_record_in_phase(phase)
    if record is None:
        available = dag.distinct_phases()
        return (
            f"\n  {_RED}No records found in phase {phase!r} "
            f"for session {session_id!r}.{_RESET}\n"
            f"  {_DIM}Available phases: "
            f"{', '.join(available) if available else '(none)'}"
            f"{_RESET}\n"
        )
    record_id = getattr(record, "record_id", None) or "(unknown)"
    op_id = getattr(record, "op_id", "") or "(no op_id)"
    kind = getattr(record, "kind", "") or "(no kind)"
    parents = getattr(record, "parent_record_ids", None) or ()
    out = [
        f"\n  {_BOLD}{_CYAN}Fork Boundary — {session_id}:"
        f"{phase}{_RESET}",
        "",
        f"  {_DIM}record_id:{_RESET}      "
        f"{_BOLD}{record_id}{_RESET}",
        f"  {_DIM}op_id:{_RESET}          {op_id}",
        f"  {_DIM}kind:{_RESET}           {kind}",
        f"  {_DIM}phase:{_RESET}          "
        f"{_color_for_phase(phase)}{phase}{_RESET}",
        f"  {_DIM}parent_records:{_RESET} "
        f"{len(parents)}",
        "",
        f"  {_BOLD}Replay command:{_RESET}",
        f"    {_DIM}python3 scripts/ouroboros_battle_test.py "
        f"\\{_RESET}",
        f"    {_DIM}    --rerun {session_id} \\{_RESET}",
        f"    {_DIM}    --rerun-from {record_id}{_RESET}",
        "",
        f"  {_DIM}This re-executes from the fork point onward "
        f"under the canonical RECORD/REPLAY/VERIFY env block."
        f"{_RESET}",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_replay_command(
    line: str,
) -> ReplayReplDispatchResult:
    """Parse a ``/replay`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ReplayReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ReplayReplDispatchResult(
            ok=False,
            text=f"  /replay parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return ReplayReplDispatchResult(
            ok=True, text=_HELP,
        )

    try:
        if head == "" or head == "sessions":
            return ReplayReplDispatchResult(
                ok=True, text=_render_sessions(),
            )
        if head == "phases":
            if len(args) < 2:
                return ReplayReplDispatchResult(
                    ok=False,
                    text=(
                        "  /replay phases <session> — "
                        "session_id required"
                    ),
                )
            return ReplayReplDispatchResult(
                ok=True, text=_render_phases(args[1]),
            )
        if head == "show":
            if len(args) < 2:
                return ReplayReplDispatchResult(
                    ok=False,
                    text=(
                        "  /replay show <session>[:<phase>] "
                        "— argument required"
                    ),
                )
            target = args[1]
            # Try `<session>:<phase>` parse first.
            match = _SESSION_PHASE_RE.match(target)
            if match:
                session, phase = match.group(1), match.group(2)
                return ReplayReplDispatchResult(
                    ok=True,
                    text=_render_show_session_phase(
                        session, phase,
                    ),
                )
            # Fallback: bare session
            return ReplayReplDispatchResult(
                ok=True,
                text=_render_show_session(target),
            )
        return ReplayReplDispatchResult(
            ok=False,
            text=(
                f"  /replay: unknown subcommand "
                f"{head!r} — try /replay help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ReplayReplDispatchResult(
            ok=False,
            text=(
                f"  /replay: error — {exc}. Try again."
            ),
        )


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="replay",
            description=(
                "Deterministic-replay browser — list sessions, "
                "inspect phases, resolve (session, phase) pairs "
                "to fork-boundary record_ids for the harness "
                "CLI's `--rerun-from` flag. Read-only."
            ),
            posture_relevance="RELEVANT",
            since="§37 Tier 2 #10 (PRD §36.4 Priority #2, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``replay_repl_composes_canonical_dag`` — module reads
         via `verification.causality_dag.build_dag` ONLY; never
         constructs `CausalityDAG` directly.
      2. ``replay_repl_authority_read_only`` — module NEVER
         calls `apply_replay_from_record_env` /
         `prepare_replay_from_record` mutating helpers (browses
         only; harness CLI executes).
      3. ``replay_repl_authority_asymmetry`` — substrate purity
         (no orchestrator / iron_gate / providers imports).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/replay_repl.py"
    )

    def _validate_composes_canonical_dag(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "CausalityDAG"
                ):
                    violations.append(
                        "replay_repl.py MUST NOT construct "
                        "CausalityDAG() directly — compose "
                        "build_dag() (single-pipeline)"
                    )
        return tuple(violations)

    def _validate_authority_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT call mutating replay helpers."""
        violations: list = []
        forbidden_calls = (
            "apply_replay_from_record_env",
            "prepare_replay_from_record",
            "setup_replay_from_cli",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    if func.id in forbidden_calls:
                        violations.append(
                            f"replay_repl.py MUST NOT call "
                            f"{func.id}() — read-only browser "
                            f"surface; harness CLI executes"
                        )
                if isinstance(func, ast.Attribute):
                    if func.attr in forbidden_calls:
                        violations.append(
                            f"replay_repl.py MUST NOT call "
                            f"<obj>.{func.attr}() — read-only"
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
                            f"replay_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "replay_repl_composes_canonical_dag"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #10 — single-pipeline guardrail."
            ),
            validate=_validate_composes_canonical_dag,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "replay_repl_authority_read_only"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #10 — read-only operator surface."
            ),
            validate=_validate_authority_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "replay_repl_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #10 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "ReplayReplDispatchResult",
    "dispatch_replay_command",
    "register_shipped_invariants",
    "register_verbs",
]
