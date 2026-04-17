"""``/resume`` — re-enqueue an orphaned in-flight op from the ledger.

Closes UX Priority trust-and-productivity gap: when a battle test dies
mid-op (SIGKILL, crash, idle timeout during GENERATE), the operator
had to remember what O+V was working on and hand-submit the same
request again. ``/resume`` scans the per-op ledger, finds orphans (no
terminal state reached), and re-enqueues the original intent as a
fresh IntentEnvelope — the CC-equivalent of "pick up where you left off"
at the *conversation* level (the model regenerates; we don't resume
mid-L2-iteration).

Scope honesty (V1):
  * **Re-enqueues the intent** (goal + target_files + source) — not the
    in-flight candidate, validation result, or L2 iteration state. Those
    are ephemeral and would require a much larger orchestrator refactor.
  * **Workspace is NOT restored** — the on-disk checkpoint files are
    metadata-only ({'entries': [], 'timestamp': ...}), the in-memory
    WorkspaceCheckpointManager does not persist across process death.
    Deferred to V1.1 when checkpoint persistence lands. Operators see
    an explicit warning on every resume output.
  * **Close-to-done advisory**: if the orphan's last recorded phase was
    GATING or APPLYING, the warning escalates because the file edits
    may already be on disk without the final ledger terminal entry —
    operator should check ``git status`` / ``git log`` before resuming.

Architecture:
  * :class:`ResumeScanner` — read-only ledger walker. Classifies each
    ``op-*.jsonl`` file as orphan (no terminal marker) or terminal
    (APPLIED / FAILED / BLOCKED / ROLLED_BACK). Extracts the PLANNED
    entry for intent reconstruction (goal, target_file[s], risk_tier).
  * :class:`ResumePlan` — frozen list of :class:`ResumeTarget`s with
    per-target safety verdict + human-readable warnings.
  * :class:`ResumeExecutor` — given a safe plan, synthesizes a fresh
    IntentEnvelope via ``make_envelope`` with ``source="resume"`` and
    ``evidence.parent_op_id=<orig>``, calls
    ``UnifiedIntakeRouter.ingest``, and appends a final lineage entry
    to the orphan's ledger so the genealogy stays auditable.

Authority invariant: operator-triggered only. The module reads the
ledger + submits signals; it never touches Iron Gate, UrgencyRouter,
risk tier, policy engine, FORBIDDEN_PATH, or ToolExecutor. The
re-enqueued signal flows through every safety gate the original did.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.Resume")

_ENV_ENABLED = "JARVIS_RESUME_ENABLED"
_ENV_MAX_AGE = "JARVIS_RESUME_MAX_AGE_S"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEFAULT_MAX_AGE_S = 86400  # 24h

# Terminal ledger states — presence means the op completed (success or
# failure) and MUST NOT be considered orphan. From OperationState enum
# in governance/ledger.py: the forward-progress terminal is APPLIED;
# abnormal terminations are FAILED / BLOCKED / ROLLED_BACK. Every other
# state (planned, sandboxing, validating, gating, applying) is transient
# and signals "still in flight".
_TERMINAL_STATES = frozenset({"applied", "failed", "blocked", "rolled_back"})

# Phases close to APPLY completion where partial disk edits may exist
# even though the ledger lacks a terminal entry. Operator should check
# ``git status`` / ``git log`` before resuming.
_NEAR_TERMINAL_STATES = frozenset({"gating", "applying"})


def resume_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def max_age_s() -> int:
    try:
        val = int(os.environ.get(_ENV_MAX_AGE, str(_DEFAULT_MAX_AGE_S)))
    except (TypeError, ValueError):
        val = _DEFAULT_MAX_AGE_S
    return max(60, val)  # never allow <60s


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrphanEntry:
    """One ledger-derived orphan record."""

    op_id: str
    ledger_path: Path
    last_state: str            # most recent non-terminal state seen
    last_wall_time: float       # wall_time of the last ledger entry
    # Intent reconstruction (from PLANNED entry, may be empty for
    # malformed ledgers — executor refuses those with a clear reason).
    goal: str = ""
    target_files: Tuple[str, ...] = ()
    risk_tier: str = ""
    states_observed: Tuple[str, ...] = ()   # full history, oldest→newest

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.last_wall_time)

    @property
    def short_op_id(self) -> str:
        if not self.op_id:
            return "?"
        # ``op-019d9368-...-cau`` → ``019d9368``
        core = self.op_id.split("-", 1)[1] if self.op_id.count("-") >= 1 else self.op_id
        return core.split("-", 1)[0] if "-" in core else core[:10]

    @property
    def is_near_terminal(self) -> bool:
        """True if the orphan was in GATING or APPLYING when it died —
        partial disk edits may exist without a terminal ledger entry."""
        return self.last_state in _NEAR_TERMINAL_STATES


@dataclass(frozen=True)
class ResumeTarget:
    """One orphan the plan proposes to resume, with per-target verdict."""

    orphan: OrphanEntry
    resumable: bool
    reasons: Tuple[str, ...] = ()    # why NOT (empty when resumable)
    warnings: Tuple[str, ...] = ()   # advisory (e.g. near-terminal)


@dataclass
class ResumePlan:
    """Output of :meth:`ResumeScanner.plan` — read-only description."""

    targets: Tuple[ResumeTarget, ...] = ()
    mode: str = "latest"             # "latest" | "list" | "specific" | "all"
    global_errors: Tuple[str, ...] = ()

    @property
    def has_global_errors(self) -> bool:
        return bool(self.global_errors)

    @property
    def resumable_targets(self) -> Tuple[ResumeTarget, ...]:
        return tuple(t for t in self.targets if t.resumable)

    @property
    def orphan_count(self) -> int:
        return len(self.targets)

    @property
    def resumable_count(self) -> int:
        return len(self.resumable_targets)


@dataclass
class ResumeResult:
    """Output of :meth:`ResumeExecutor.execute`."""

    executed: bool = False
    resumed_op_ids: Tuple[str, ...] = ()      # NEW op ids (one per target)
    parent_op_ids: Tuple[str, ...] = ()       # the orphans they came from
    skipped_reasons: Tuple[Tuple[str, str], ...] = ()  # [(parent_op, reason)]
    error: str = ""


# ---------------------------------------------------------------------------
# Scanner — read-only ledger walker
# ---------------------------------------------------------------------------


class ResumeScanner:
    """Scans the per-op ledger directory for orphaned operations.

    The ledger directory is expected at
    ``<repo>/.ouroboros/state/ouroboros/ledger/``. Each file
    ``op-*.jsonl`` contains one operation's state-transition events
    (one JSON object per line). The scanner classifies each file and
    builds orphan records.
    """

    def __init__(
        self,
        *,
        ledger_root: Path,
        governed_loop_service: Any = None,
    ) -> None:
        self._ledger_root = Path(ledger_root)
        self._gls = governed_loop_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_orphans(self) -> List[OrphanEntry]:
        """Walk the ledger dir; return orphans (no terminal state), newest first."""
        out: List[OrphanEntry] = []
        if not self._ledger_root.is_dir():
            logger.debug(
                "[Resume] ledger dir missing: %s", self._ledger_root,
            )
            return out
        # Iterate all op-*.jsonl files. Using glob avoids sorted() on
        # the directory contents (filesystem order differs across OS).
        for path in sorted(self._ledger_root.glob("op-*.jsonl")):
            orphan = self._classify_one(path)
            if orphan is not None:
                out.append(orphan)
        # Newest orphan first (by last wall_time).
        out.sort(key=lambda o: o.last_wall_time, reverse=True)
        return out

    def plan(
        self,
        *,
        mode: str = "latest",
        op_id_prefix: str = "",
    ) -> ResumePlan:
        """Build a :class:`ResumePlan` for the requested mode.

        Modes:
          ``latest``    — resume the single most recent orphan
          ``list``      — describe every orphan (safety-checked but no execute)
          ``specific``  — resume the orphan whose op_id starts with ``op_id_prefix``
          ``all``       — resume every qualifying orphan in one batch
        """
        errors: List[str] = []
        if not resume_enabled():
            errors.append(
                f"Resume disabled by env — set {_ENV_ENABLED}=1 to enable"
            )
        if mode not in {"latest", "list", "specific", "all"}:
            errors.append(f"Unknown resume mode '{mode}'")
        if mode == "specific" and not op_id_prefix:
            errors.append("`specific` mode requires an op-id prefix")

        orphans = self.scan_orphans() if not errors else []

        # Restrict by mode.
        if mode == "latest":
            orphans = orphans[:1]
        elif mode == "specific":
            matches = [o for o in orphans if o.op_id.startswith(op_id_prefix)
                       or o.short_op_id.startswith(op_id_prefix)]
            if not matches:
                errors.append(
                    f"No orphan matches prefix '{op_id_prefix}'"
                )
            orphans = matches

        # Per-target safety check.
        targets: List[ResumeTarget] = []
        active_ids = self._active_op_ids()
        cutoff = max_age_s()
        for o in orphans:
            reasons: List[str] = []
            warnings: List[str] = []

            if o.op_id in active_ids:
                reasons.append(
                    "op is actively running in the current session"
                )
            if o.age_s > cutoff:
                reasons.append(
                    f"age={int(o.age_s)}s exceeds cutoff "
                    f"{_ENV_MAX_AGE}={cutoff}s"
                )
            if not o.goal:
                reasons.append(
                    "no PLANNED entry — intent cannot be reconstructed"
                )
            if not o.target_files:
                reasons.append("no target files recorded in ledger")

            # Near-terminal advisory — partial disk edits may exist.
            if o.is_near_terminal:
                warnings.append(
                    f"⚠ last phase was {o.last_state} — file edits may "
                    f"already be on disk. Check `git status` / `git log` "
                    f"before resuming."
                )

            targets.append(ResumeTarget(
                orphan=o,
                resumable=not reasons,
                reasons=tuple(reasons),
                warnings=tuple(warnings),
            ))

        return ResumePlan(
            targets=tuple(targets),
            mode=mode,
            global_errors=tuple(errors),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _classify_one(self, path: Path) -> Optional[OrphanEntry]:
        """Parse one ledger file. Return OrphanEntry if NOT terminal, else None.

        Resilient to malformed lines — any ``JSONDecodeError`` is
        skipped; a file with zero valid lines returns None.
        """
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            logger.debug("[Resume] failed to read %s", path, exc_info=True)
            return None
        entries: List[Dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not entries:
            return None

        # Detect terminal state.
        states_observed: List[str] = []
        terminal_seen = False
        for e in entries:
            st = str(e.get("state", "")).lower()
            if st:
                states_observed.append(st)
            if st in _TERMINAL_STATES:
                terminal_seen = True
        if terminal_seen:
            return None

        # Extract intent from a PLANNED entry if present.
        goal = ""
        target_files: Tuple[str, ...] = ()
        risk_tier = ""
        for e in entries:
            if str(e.get("state", "")).lower() == "planned":
                data = e.get("data") or {}
                if isinstance(data, dict):
                    g = data.get("goal")
                    if isinstance(g, str):
                        goal = g
                    # ledger may store singular or plural; accept either.
                    tf = data.get("target_files")
                    if isinstance(tf, list) and tf:
                        target_files = tuple(str(x) for x in tf if isinstance(x, str))
                    elif not target_files:
                        single = data.get("target_file")
                        if isinstance(single, str) and single:
                            target_files = (single,)
                    rt = data.get("risk_tier")
                    if isinstance(rt, str):
                        risk_tier = rt
                break

        op_id = str(entries[-1].get("op_id", "")) or path.stem
        last_entry = entries[-1]
        last_state = str(last_entry.get("state", "")).lower() or "unknown"
        last_wall = float(last_entry.get("wall_time", 0.0) or 0.0)

        return OrphanEntry(
            op_id=op_id,
            ledger_path=path,
            last_state=last_state,
            last_wall_time=last_wall,
            goal=goal,
            target_files=target_files,
            risk_tier=risk_tier,
            states_observed=tuple(states_observed),
        )

    def _active_op_ids(self) -> set:
        """Op ids currently live in the running session — never resume
        these (resume would double-enqueue)."""
        gls = self._gls
        if gls is None:
            return set()
        try:
            return set(getattr(gls, "_active_ops", set()) or set())
        except Exception:  # noqa: BLE001
            return set()


# ---------------------------------------------------------------------------
# Executor — re-enqueue via IntakeRouter
# ---------------------------------------------------------------------------


class ResumeExecutor:
    """Re-enqueues qualifying orphans as fresh IntentEnvelopes.

    The new envelope carries ``source="resume"`` and an
    ``evidence.parent_op_id=<orig>`` pointer so downstream callers (and
    the session recorder) can trace the lineage.
    """

    def __init__(
        self,
        *,
        repo_name: str,
        intake_router: Any,
        comm: Any = None,
    ) -> None:
        self._repo_name = repo_name
        self._router = intake_router
        self._comm = comm

    async def execute(self, plan: ResumePlan) -> ResumeResult:
        """Re-enqueue every resumable target; skip the rest with reasons."""
        if plan.has_global_errors:
            return ResumeResult(
                executed=False,
                error="; ".join(plan.global_errors),
            )
        if plan.mode == "list":
            # List mode is a pure report — no mutation.
            return ResumeResult(
                executed=False, error="list mode is read-only",
            )
        if self._router is None:
            return ResumeResult(
                executed=False,
                error="intake router unavailable — cannot re-enqueue",
            )

        resumed_new: List[str] = []
        resumed_parents: List[str] = []
        skipped: List[Tuple[str, str]] = []

        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
        except Exception as exc:  # noqa: BLE001
            return ResumeResult(
                executed=False,
                error=f"intake envelope factory unavailable: {exc}",
            )

        for target in plan.targets:
            orph = target.orphan
            if not target.resumable:
                skipped.append((orph.op_id, "; ".join(target.reasons)))
                continue

            # Synthesize the fresh envelope.
            try:
                envelope = make_envelope(
                    source="resume",
                    description=orph.goal,
                    target_files=orph.target_files,
                    repo=self._repo_name,
                    confidence=0.9,
                    urgency="normal",
                    evidence={
                        "resume_of_op": orph.op_id,
                        "resume_orig_phase": orph.last_state,
                        "resume_orig_age_s": int(orph.age_s),
                        "resume_orig_risk_tier": orph.risk_tier,
                    },
                    requires_human_ack=False,
                )
            except Exception as exc:  # noqa: BLE001
                skipped.append((orph.op_id, f"envelope build failed: {exc}"))
                continue

            # Submit via the router's ingest method.
            try:
                verdict = await self._router.ingest(envelope)
            except Exception as exc:  # noqa: BLE001
                skipped.append((orph.op_id, f"ingest raised: {exc}"))
                continue
            if verdict not in {"enqueued", "queued_behind", "pending_ack"}:
                skipped.append((orph.op_id, f"ingest rejected: {verdict}"))
                continue

            new_id = getattr(envelope, "causal_id", "") or getattr(
                envelope, "signal_id", "",
            )
            resumed_new.append(new_id)
            resumed_parents.append(orph.op_id)

            # Lineage trailer on the orphan's ledger so genealogy is
            # auditable without needing to walk active ops.
            self._append_lineage_entry(
                ledger_path=orph.ledger_path,
                orig_op_id=orph.op_id,
                new_envelope_id=new_id,
            )

            logger.info(
                "[Resume] op=%s orig_phase=%s age_s=%d new_env_id=%s "
                "workspace_restored=false",
                orph.op_id, orph.last_state, int(orph.age_s), new_id,
            )

            # Emit CommProtocol DECISION (best-effort).
            await self._emit_decision(
                new_env_id=new_id,
                parent_op_id=orph.op_id,
                orig_phase=orph.last_state,
                target_files=orph.target_files,
            )

        return ResumeResult(
            executed=bool(resumed_new),
            resumed_op_ids=tuple(resumed_new),
            parent_op_ids=tuple(resumed_parents),
            skipped_reasons=tuple(skipped),
        )

    # ------------------------------------------------------------------
    # Internals — ledger lineage append + DECISION emit
    # ------------------------------------------------------------------

    def _append_lineage_entry(
        self,
        *,
        ledger_path: Path,
        orig_op_id: str,
        new_envelope_id: str,
    ) -> None:
        """Append a structured line to the orphan's ledger noting the resume.

        Uses the same JSONL shape as live entries but with a non-enum
        ``state`` value (``"resumed"``) so existing parsers either skip
        it as unknown or surface it as metadata. Never raises.
        """
        payload = {
            "op_id": orig_op_id,
            "state": "resumed",
            "data": {
                "resumed_to_env_id": new_envelope_id,
                "resumed_at": time.time(),
                "note": (
                    "operator-triggered /resume — intent re-enqueued as "
                    "fresh envelope; candidate + validation + L2 state lost"
                ),
            },
            "timestamp": time.monotonic(),
            "wall_time": time.time(),
        }
        try:
            with ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Resume] lineage append failed for %s",
                ledger_path, exc_info=True,
            )

    async def _emit_decision(
        self,
        *,
        new_env_id: str,
        parent_op_id: str,
        orig_phase: str,
        target_files: Tuple[str, ...],
    ) -> None:
        if self._comm is None:
            return
        emit = getattr(self._comm, "emit_decision", None)
        if emit is None:
            return
        try:
            await emit(
                op_id=new_env_id or f"resume-{parent_op_id[:10]}",
                outcome="resumed",
                reason_code=f"resume_of={parent_op_id} phase={orig_phase}",
                target_files=list(target_files),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Resume] emit_decision failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def render_plan(plan: ResumePlan) -> Any:
    """Rich renderable summarising the plan for the operator.

    Used by ``/resume list`` (read-only) and as the pre-execute banner
    for ``/resume``, ``/resume <op_id>``, ``/resume all``. Falls back
    to plain string when Rich is missing.
    """
    try:
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except Exception:  # noqa: BLE001
        return _render_plan_plain(plan)

    title = Text()
    title.append("/resume", style="bold")
    title.append(f"  mode={plan.mode}", style="cyan")
    title.append(
        f"  orphans={plan.orphan_count}"
        f"  resumable={plan.resumable_count}",
    )

    lines: List[Any] = [title]

    if plan.global_errors:
        err_txt = Text()
        for e in plan.global_errors:
            err_txt.append(f"✖ {e}\n", style="bold red")
        lines.append(err_txt)

    if plan.targets:
        table = Table(
            show_header=True, header_style="bold cyan",
            padding=(0, 1),
        )
        table.add_column("#", justify="right")
        table.add_column("op", no_wrap=True)
        table.add_column("last phase")
        table.add_column("age", justify="right")
        table.add_column("files", justify="right")
        table.add_column("ok?", justify="center")
        table.add_column("goal / reason")
        for idx, t in enumerate(plan.targets, 1):
            o = t.orphan
            ok_txt = (
                Text("✓", style="green") if t.resumable
                else Text("✗", style="red")
            )
            phase_style = "yellow" if o.is_near_terminal else "white"
            age_s = int(o.age_s)
            age_txt = (
                f"{age_s}s" if age_s < 60
                else f"{age_s // 60}m" if age_s < 3600
                else f"{age_s // 3600}h"
            )
            goal_or_reason = (
                (o.goal[:70] if o.goal else "(no goal)")
                if t.resumable
                else "; ".join(t.reasons)
            )
            table.add_row(
                str(idx),
                o.short_op_id,
                Text(o.last_state, style=phase_style),
                age_txt,
                str(len(o.target_files)),
                ok_txt,
                goal_or_reason,
            )
        lines.append(table)

    # Aggregate warnings.
    any_warnings: List[str] = []
    for t in plan.targets:
        for w in t.warnings:
            any_warnings.append(f"{t.orphan.short_op_id}: {w}")
    if any_warnings:
        warn_txt = Text()
        for w in any_warnings:
            warn_txt.append(f"⚠ {w}\n", style="yellow")
        lines.append(warn_txt)

    # V1 honesty banner.
    honesty = Text()
    honesty.append(
        "Note: candidate + validation + L2 iteration state NOT preserved "
        "(fresh intent re-enqueue only). Workspace edits from mid-op are "
        "lost in V1 — checkpoint persistence lands in V1.1.",
        style="dim italic",
    )
    lines.append(honesty)

    return Panel(
        Group(*lines),
        title="[bold]Resume Plan[/bold]",
        border_style="cyan",
        padding=(1, 2),
    )


def _render_plan_plain(plan: ResumePlan) -> str:
    out: List[str] = []
    out.append(
        f"/resume  mode={plan.mode}  "
        f"orphans={plan.orphan_count}  resumable={plan.resumable_count}"
    )
    for e in plan.global_errors:
        out.append(f"  ERROR: {e}")
    for idx, t in enumerate(plan.targets, 1):
        o = t.orphan
        status = "OK" if t.resumable else "SKIP"
        out.append(
            f"  {idx:>2}. {o.short_op_id} [{o.last_state}] age={int(o.age_s)}s "
            f"files={len(o.target_files)} {status} "
            f"{'goal=' + o.goal[:60] if t.resumable else 'reason=' + '; '.join(t.reasons)}"
        )
        for w in t.warnings:
            out.append(f"      warn: {w}")
    out.append(
        "  note: V1 re-enqueues intent only — candidate/L2 state not preserved"
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Argument parser — ``/resume [list|all|<op_prefix>]``
# ---------------------------------------------------------------------------


def parse_resume_args(raw: str) -> Tuple[str, str, Optional[str]]:
    """Parse ``/resume`` tail into ``(mode, op_prefix, error)``.

    Examples
    --------
    ``/resume``            → ``("latest", "", None)``
    ``/resume list``       → ``("list", "", None)``
    ``/resume all``        → ``("all", "", None)``
    ``/resume 019d9368``   → ``("specific", "019d9368", None)``
    """
    import shlex
    tokens = shlex.split((raw or "").strip())
    if tokens and tokens[0].lstrip("/") == "resume":
        tokens = tokens[1:]

    if not tokens:
        return ("latest", "", None)
    head = tokens[0].lower()
    if head == "list":
        return ("list", "", None)
    if head == "all":
        return ("all", "", None)
    if head == "latest":
        return ("latest", "", None)
    # Anything else is treated as an op-id prefix.
    return ("specific", tokens[0], None)
