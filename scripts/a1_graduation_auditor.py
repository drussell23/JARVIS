"""A1 Real-Time SSE GraduationAuditor + Absolute Intervention-Lock.
================================================================================

The *live verification half* of the A1 Live-Fire Chaos Harness. A standalone,
$0 (consumer-only) async observer that hooks the EXISTING observability SSE
stream (``GET /observability/stream``) during a live O+V soak and proves the
base autonomy loop fired with the cognitive flags ON, **with zero human
intervention**.

It does NOT mutate the organism. It subscribes, correlates, and renders a
verdict. The only artifacts it writes are its own verdict JSON + report.

What it proves (the A1 success criterion, ``A1_DISPATCH_PROVEN``)
----------------------------------------------------------------
All five must hold:

1. The 5 ``[A1Trace]`` hops observed **in order**:
   ``emit -> ingest -> dequeue -> submit -> accept``.
2. The FSM ran ``CLASSIFY -> ... -> APPLY -> state=applied``.
3. The 12-flag audit (CADENCE_POLICY-derived) PASSED.
4. The Absolute Intervention-Lock never tripped.
5. An autonomous PR / commit signal observed.

Honest verification (no fake-pass)
----------------------------------
Each cognitive flag is graded into one of:
  * ``OBSERVED_EVALUATED`` -- its gate was observably exercised + did NOT
    false-positive-reject.
  * ``REJECTED`` -- its gate caused a rejection/halt (the loop did not stay
    autonomous). This is a FAIL (throws).
  * ``UNVERIFIABLE`` -- no SSE/log signal correlated to the flag. We do NOT
    fake-pass: UNVERIFIABLE is a real verdict. In strict mode (default)
    UNVERIFIABLE -> FAIL; in lenient mode -> WARN.

The Absolute Intervention-Lock (fail-CLOSED autonomy)
-----------------------------------------------------
If ANY human-gate event fires BEFORE the final CRITICAL_ELEVATION merge --
``plan_pending`` / ``APPROVAL_REQUIRED`` / ``ask_human`` /
``CLARIFICATION_REQUEST`` / ``human_ack_gate`` -- the auditor raises
:class:`GraduationFailedException` immediately and signals teardown. The ONLY
permitted human gate is the terminal Sovereign-Law CRITICAL_ELEVATION merge
approval (a worker->Mind/Nerves merge). Any mid-loop operator prompt = autonomy
not proven = fail.

Reuse (no new transport, no hardcoded flag list)
------------------------------------------------
* SSE schema + event vocabulary from ``ide_observability_stream`` (the
  ``{schema_version, event_id, event_type, op_id, timestamp, payload}`` frame).
* The 5 hops + semantics from ``a1_trace`` (emitted at WARNING to the log, so
  the auditor ALSO tails a ``--log-file`` for ``[A1Trace]`` lines).
* The cognitive flag set derived from ``CADENCE_POLICY`` (env-overridable via
  ``JARVIS_A1_AUDIT_FLAGS``) -- NEVER a hardcoded list.

Design constraints: ``from __future__ import annotations``, Python 3.9+
(asyncio), ASCII-only, fail-CLOSED, env-knob driven, no org mutation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)


# ===========================================================================
# Exceptions
# ===========================================================================


class GraduationFailedException(Exception):
    """Raised by the Absolute Intervention-Lock when a human-gate event fires
    BEFORE the terminal CRITICAL_ELEVATION merge -- i.e. autonomy was NOT
    proven. Carries the offending event + the FSM phase + a failure locus so
    the teardown path can name exactly where the loop demanded a human."""

    def __init__(
        self,
        message: str,
        *,
        event_type: str = "",
        fsm_phase: str = "",
        failure_locus: str = "",
        offending_event: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.event_type = event_type
        self.fsm_phase = fsm_phase
        self.failure_locus = failure_locus
        self.offending_event = offending_event or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self),
            "event_type": self.event_type,
            "fsm_phase": self.fsm_phase,
            "failure_locus": self.failure_locus,
            "offending_event": self.offending_event,
        }


# ===========================================================================
# Flag set -- derived from CADENCE_POLICY (NOT hardcoded)
# ===========================================================================


def load_audit_flags() -> List[str]:
    """The cognitive-flag set to audit. Derived from ``CADENCE_POLICY`` (the
    authoritative pinned graduation table), env-overridable via
    ``JARVIS_A1_AUDIT_FLAGS`` (comma-separated).

    NEVER a hardcoded list: if the env override is absent we import
    ``CADENCE_POLICY`` and read its flag names. If the import fails (the
    auditor is a standalone observer that may run outside the repo), we
    fail-CLOSED loudly rather than silently auditing an empty / fabricated
    set.
    """
    override = (os.environ.get("JARVIS_A1_AUDIT_FLAGS", "") or "").strip()
    if override:
        flags = [f.strip() for f in override.split(",") if f.strip()]
        if flags:
            return flags
    # Derive from the canonical CADENCE_POLICY table. The package
    # backend/core/ouroboros/__init__.py imports `backend.core...`, so BOTH the
    # repo root (makes `backend` a package) AND the backend dir (makes `core...`
    # importable) must be on sys.path for a standalone invocation.
    import importlib

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backend = os.path.join(repo_root, "backend")
    for entry in (backend, repo_root):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    cadence = None
    for mod_path in (
        "backend.core.ouroboros.governance.adaptation.graduation_ledger",
        "core.ouroboros.governance.adaptation.graduation_ledger",
    ):
        try:
            cadence = getattr(importlib.import_module(mod_path), "CADENCE_POLICY")
            break
        except Exception:  # noqa: BLE001 -- try the next import path
            continue
    if cadence is None:
        raise GraduationFailedException(
            "FAILURE LOCUS: flag_set_load -- could not import CADENCE_POLICY "
            "and no JARVIS_A1_AUDIT_FLAGS override given",
            failure_locus="flag_set_load",
        )
    return [entry.flag_name for entry in cadence]


# Map each CADENCE_POLICY flag (by substring/family) to the SSE/log signal
# family that would evidence its gate participating. This is a *correlation
# hint table* (family -> matchers), NOT a re-hardcoding of the flag list: the
# flags come from CADENCE_POLICY; this table only says "which observable signal
# proves gate X ran". A flag whose family has no observable matcher is honestly
# graded UNVERIFIABLE.
#
# Each entry: family-key -> (list of substrings that, if present in the flag
# name, bind it to this family).
_FLAG_FAMILY_BINDINGS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("semantic_guardian", ("SEMANTIC_GUARDIAN", "SEMANTIC_GUARD")),
    ("iron_gate", ("EXPLORATION_LEDGER", "IRON_GATE")),
    ("scoped_tool_backend", ("SCOPED_TOOL_BACKEND", "PER_ORDER_BUDGET")),
    ("risk_tier", ("RISK_TIER",)),
    ("hypothesis_probe", ("HYPOTHESIS_PROBE",)),
    ("stale_pattern", ("STALE_PATTERN",)),
    ("meta_governor", ("META_GOVERNOR",)),
    ("decision_trace", ("DECISION_TRACE",)),
    ("confidence_ring", ("LATENT_CONFIDENCE", "CONFIDENCE_RING")),
    ("timeline", ("MULTI_OP_TIMELINE",)),
    ("flag_change", ("FLAG_CHANGE",)),
    ("latency_slo", ("LATENCY_SLO",)),
    ("phase8", ("PHASE8",)),
    ("curiosity", ("CURIOSITY",)),
    ("multi_prior", ("MULTI_PRIOR",)),
    ("execution_monitor", ("EXECUTION_MONITOR",)),
)


def family_for_flag(flag: str) -> Optional[str]:
    """Return the signal-family key a flag binds to, or None (UNVERIFIABLE)."""
    up = flag.upper()
    for family, needles in _FLAG_FAMILY_BINDINGS:
        for needle in needles:
            if needle in up:
                return family
    return None


# Which observable signals (SSE event_type OR log-marker substring) evidence a
# family's gate participating WITHOUT rejecting. The auditor watches the stream
# for any of these and credits the flag's family as observed-evaluated.
#
# family -> {"evaluated": (signals...), "rejected": (signals...)}
# A "rejected" signal means the gate halted/false-rejected -> FAIL.
_FAMILY_SIGNALS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "semantic_guardian": {
        "evaluated": ("[SemanticGuard]", "semantic_guard"),
        "rejected": ("APPROVAL_REQUIRED", "removed_import_still_referenced"),
    },
    "iron_gate": {
        "evaluated": ("tool_exploration_start", "[IronGate]", "exploration"),
        "rejected": ("ExplorationInsufficientError", "exploration_insufficient"),
    },
    "scoped_tool_backend": {
        "evaluated": ("tool_exploration_start", "scoped_tool", "mutation_budget"),
        "rejected": ("mutation_budget_exhausted", "POLICY_DENIED"),
    },
    "risk_tier": {
        "evaluated": ("fsm_phase_changed", "risk_tier", "[RiskTier]"),
        "rejected": ("BLOCKED", "risk_tier_floor_block"),
    },
    "hypothesis_probe": {
        "evaluated": ("hypothesis_probe", "[HypothesisProbe]"),
        "rejected": ("hypothesis_probe_rejected",),
    },
    "stale_pattern": {
        "evaluated": ("stale_pattern", "[StalePattern]"),
        "rejected": ("stale_pattern_block",),
    },
    "meta_governor": {
        "evaluated": ("meta_governor", "[MetaGovernor]"),
        "rejected": ("meta_governor_block",),
    },
    "decision_trace": {
        "evaluated": ("decision_recorded", "decision_trace"),
        "rejected": (),
    },
    "confidence_ring": {
        "evaluated": ("confidence_observed", "model_confidence"),
        "rejected": ("model_confidence_drop",),
    },
    "timeline": {
        "evaluated": ("multi_op", "timeline"),
        "rejected": (),
    },
    "flag_change": {
        "evaluated": ("flag_changed", "flag_registered"),
        "rejected": (),
    },
    "latency_slo": {
        "evaluated": ("slo_breached", "latency_slo"),
        "rejected": ("slo_breached",),
    },
    "phase8": {
        "evaluated": ("decision_recorded", "confidence_observed", "fsm_phase_changed"),
        "rejected": (),
    },
    "curiosity": {
        "evaluated": ("curiosity_changed", "curiosity_question_emitted"),
        "rejected": (),
    },
    "multi_prior": {
        "evaluated": ("multi_prior_dispatch",),
        "rejected": (),
    },
    "execution_monitor": {
        "evaluated": ("operation_terminal", "execution_graph_progress"),
        "rejected": (),
    },
}


# ===========================================================================
# A1Trace hop tracking
# ===========================================================================


A1TRACE_HOPS: Tuple[str, ...] = ("emit", "ingest", "dequeue", "submit", "accept")

# [A1Trace] <hop> goal=<id> [k=v ...]
_A1TRACE_RE = re.compile(r"\[A1Trace\]\s+(?P<hop>\w+)\s+goal=(?P<goal>\S+)")


def parse_a1trace_line(line: str) -> Optional[Tuple[str, str]]:
    """Parse one log line for an ``[A1Trace] <hop> goal=<id>`` breadcrumb.
    Returns ``(hop, goal_id)`` or None. NEVER raises."""
    try:
        m = _A1TRACE_RE.search(line)
        if not m:
            return None
        return m.group("hop"), m.group("goal")
    except Exception:  # noqa: BLE001
        return None


@dataclass
class A1TraceTimeline:
    """Tracks the ordered observation of the 5 A1Trace hops per goal id, plus a
    global ordered-observation log. The criterion "5 hops in order" is met when
    SOME goal id has all 5 hops observed in the canonical order."""

    # goal_id -> list of (hop, ts) in observation order
    per_goal: Dict[str, List[Tuple[str, float]]] = field(default_factory=dict)
    # global ordered list of (hop, goal, ts)
    ordered: List[Tuple[str, str, float]] = field(default_factory=list)

    def observe(self, hop: str, goal_id: str, ts: Optional[float] = None) -> None:
        if hop not in A1TRACE_HOPS:
            return
        ts = time.time() if ts is None else ts
        self.per_goal.setdefault(goal_id, []).append((hop, ts))
        self.ordered.append((hop, goal_id, ts))

    def all_hops_in_order(self) -> bool:
        """True iff some goal observed all 5 hops in canonical order."""
        for goal_id, obs in self.per_goal.items():
            if self._goal_in_order(obs):
                return True
        return False

    @staticmethod
    def _goal_in_order(obs: Sequence[Tuple[str, float]]) -> bool:
        # Walk the canonical hop list; each hop must appear at or after the
        # index of the previous one in the observation sequence.
        seq = [h for h, _ in obs]
        cursor = 0
        for hop in A1TRACE_HOPS:
            found = -1
            for i in range(cursor, len(seq)):
                if seq[i] == hop:
                    found = i
                    break
            if found < 0:
                return False
            cursor = found + 1
        return True

    def winning_goal(self) -> Optional[str]:
        for goal_id, obs in self.per_goal.items():
            if self._goal_in_order(obs):
                return goal_id
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordered": [
                {"hop": h, "goal": g, "ts": ts} for (h, g, ts) in self.ordered
            ],
            "all_hops_in_order": self.all_hops_in_order(),
            "winning_goal": self.winning_goal(),
        }


# ===========================================================================
# Per-flag audit state
# ===========================================================================


class FlagVerdict(str, Enum):
    PENDING = "pending"            # not yet observed
    OBSERVED_EVALUATED = "observed_evaluated"
    REJECTED = "rejected"         # gate false-positive-rejected -> FAIL
    UNVERIFIABLE = "unverifiable"  # no signal correlated -> honest non-pass


@dataclass
class FlagAuditState:
    flag: str
    family: Optional[str]
    expected: bool = True
    observed_evaluated: bool = False
    false_positive_rejected: bool = False
    evidence: List[str] = field(default_factory=list)

    def verdict(self) -> FlagVerdict:
        if self.false_positive_rejected:
            return FlagVerdict.REJECTED
        if self.observed_evaluated:
            return FlagVerdict.OBSERVED_EVALUATED
        if self.family is None:
            # No observable signal family binds this flag.
            return FlagVerdict.UNVERIFIABLE
        # Family exists but never observed in the stream -> UNVERIFIABLE
        # (we do NOT fake-pass: the SSE/log produced no evidence).
        return FlagVerdict.UNVERIFIABLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag": self.flag,
            "family": self.family,
            "expected": self.expected,
            "observed_evaluated": self.observed_evaluated,
            "false_positive_rejected": self.false_positive_rejected,
            "verdict": self.verdict().value,
            "evidence": list(self.evidence[:20]),
        }


# ===========================================================================
# The auditor core (pure; no network) -- feed it events, ask for the verdict.
# ===========================================================================


# Human-gate signals that, observed BEFORE the terminal merge, trip the lock.
_HUMAN_GATE_EVENT_TYPES = frozenset(
    {
        "plan_pending",
        "inline_prompt_pending",
    }
)
# Substring markers (in event_type / payload / log line) that indicate a
# mid-loop human halt.
_HUMAN_GATE_MARKERS: Tuple[str, ...] = (
    "ask_human",
    "CLARIFICATION_REQUEST",
    "clarification_request",
    "human_ack_gate",
    "APPROVAL_REQUIRED",
)

# The terminal Sovereign-Law CRITICAL_ELEVATION merge gate -- the ONLY permitted
# human gate. Distinguished from mid-loop clarifications: it carries the
# cross-repo elevation / critical-elevation merge marker.
_TERMINAL_MERGE_EVENT_TYPES = frozenset(
    {
        "cross_repo_elevation_pending",
    }
)
_TERMINAL_MERGE_MARKERS: Tuple[str, ...] = (
    "CRITICAL_ELEVATION",
    "critical_elevation",
)

# FSM phases (from the 11-phase pipeline). state=applied is the win.
_TERMINAL_APPLIED_STATES = frozenset({"applied"})


@dataclass
class A1Verdict:
    proven: bool
    flags: List[Dict[str, Any]]
    a1trace_timeline: Dict[str, Any]
    criteria: Dict[str, bool]
    failure_locus: str = ""
    graduation_exception: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": "proven" if self.proven else "failed",
            "proven": self.proven,
            "criteria": self.criteria,
            "flags": self.flags,
            "a1trace_timeline": self.a1trace_timeline,
            "failure_locus": self.failure_locus,
            "graduation_exception": self.graduation_exception,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


class A1GraduationAuditor:
    """Pure, network-free audit core. Drive it with :meth:`ingest_event`
    (SSE-style ``(event_type, payload)``) and :meth:`ingest_log_line` (for
    ``[A1Trace]`` + ``[SemanticGuard]`` etc.). Then call :meth:`verdict`.

    The Absolute Intervention-Lock is enforced inline: a mid-loop human-gate
    event raises :class:`GraduationFailedException` from the ingest call (so the
    network shell can tear down immediately)."""

    def __init__(self, *, flags: Optional[Sequence[str]] = None, strict: bool = True) -> None:
        flag_list = list(flags) if flags is not None else load_audit_flags()
        self.strict = strict
        self.flags: Dict[str, FlagAuditState] = {}
        for f in flag_list:
            self.flags[f] = FlagAuditState(flag=f, family=family_for_flag(f))
        # family -> list of flag states (for fast signal fan-out)
        self._by_family: Dict[str, List[FlagAuditState]] = {}
        for st in self.flags.values():
            if st.family is not None:
                self._by_family.setdefault(st.family, []).append(st)

        self.trace = A1TraceTimeline()
        self.fsm_phases_seen: List[str] = []
        self.state_applied = False
        self.pr_signal_observed = False
        self.terminal_merge_reached = False
        self.intervention_tripped = False
        self.tripped_exception: Optional[GraduationFailedException] = None
        self._last_fsm_phase = ""

    # ----- intervention-lock primitive -------------------------------------

    def _check_human_gate(
        self, marker_text: str, event_type: str, payload: Dict[str, Any]
    ) -> None:
        """Raise GraduationFailedException if ``marker_text`` is a mid-loop
        human gate AND the terminal merge has not yet been reached. The
        terminal CRITICAL_ELEVATION merge is the ONLY permitted gate."""
        # First: is this the permitted terminal Sovereign merge gate?
        is_terminal_merge = (
            event_type in _TERMINAL_MERGE_EVENT_TYPES
            or any(m in marker_text for m in _TERMINAL_MERGE_MARKERS)
        )
        if is_terminal_merge:
            self.terminal_merge_reached = True
            return  # permitted -- this is the Sovereign-Law merge approval

        # Otherwise: is it a mid-loop human gate?
        is_human_gate = (
            event_type in _HUMAN_GATE_EVENT_TYPES
            or any(m in marker_text for m in _HUMAN_GATE_MARKERS)
        )
        if not is_human_gate:
            return
        # A human gate BEFORE the terminal merge => autonomy not proven.
        if self.terminal_merge_reached:
            return  # post-merge prompts are out of the autonomy window
        gate_label = event_type or marker_text
        exc = GraduationFailedException(
            "FAILURE LOCUS: intervention_lock -- mid-loop human gate "
            f"'{gate_label}' fired at FSM phase '{self._last_fsm_phase or '?'}' "
            "BEFORE the terminal CRITICAL_ELEVATION merge. Autonomy NOT proven.",
            event_type=event_type or marker_text,
            fsm_phase=self._last_fsm_phase,
            failure_locus="intervention_lock:%s" % (gate_label,),
            offending_event={"event_type": event_type, "payload": payload},
        )
        self.intervention_tripped = True
        self.tripped_exception = exc
        raise exc

    # ----- ingest ----------------------------------------------------------

    def ingest_event(
        self, event_type: Optional[str], payload: Optional[Dict[str, Any]]
    ) -> None:
        """Ingest one SSE-style event. May raise GraduationFailedException
        (intervention-lock). Non-gate parse errors never raise."""
        if not event_type:
            return
        payload = payload if isinstance(payload, dict) else {}
        # Build a flat searchable text of event + payload for marker matching.
        try:
            blob = event_type + " " + json.dumps(payload, default=str)
        except Exception:  # noqa: BLE001
            blob = event_type

        # Intervention-lock FIRST (fail-CLOSED) -- may raise.
        self._check_human_gate(blob, event_type, payload)

        # FSM phase tracking.
        if event_type == "fsm_phase_changed":
            phase = str(payload.get("phase", "")).strip()
            if phase:
                self.fsm_phases_seen.append(phase)
                self._last_fsm_phase = phase

        # Terminal applied / PR signal.
        if event_type == "operation_terminal":
            state = str(payload.get("state", "")).strip().lower()
            if state in _TERMINAL_APPLIED_STATES:
                self.state_applied = True
        if self._is_pr_signal(event_type, payload, blob):
            self.pr_signal_observed = True

        # Flag-family signal correlation.
        self._correlate_flag_signal(blob)

    def ingest_log_line(self, line: str) -> None:
        """Ingest one raw log line -- A1Trace hops + gate-telemetry markers
        (e.g. ``[SemanticGuard]``, ``[IronGate]``). May raise (intervention)."""
        if not line:
            return
        # A1Trace hop?
        parsed = parse_a1trace_line(line)
        if parsed is not None:
            hop, goal = parsed
            self.trace.observe(hop, goal)
            return
        # Intervention-lock on log-surfaced gates (ask_human etc.) -- may raise.
        self._check_human_gate(line, "", {})
        # Flag-family signal correlation from log markers.
        self._correlate_flag_signal(line)
        # PR / commit signal in a log line.
        if self._is_pr_signal("", {}, line):
            self.pr_signal_observed = True

    def _is_pr_signal(
        self, event_type: str, payload: Dict[str, Any], blob: str
    ) -> bool:
        if event_type in ("review_branch_created", "cross_repo_elevation_pending"):
            return True
        markers = (
            "OrangePR", "orange_pr", "pr create", "gh pr create",
            "ouroboros/review/", "[SOVEREIGN GRADUATION]",
            "auto_commit", "AutoCommitter", "O+V signature",
            "pull request", "PR opened", "pr_opened",
        )
        return any(m in blob for m in markers)

    def _correlate_flag_signal(self, text: str) -> None:
        """For each family, credit observed-evaluated OR mark false-reject if a
        rejection marker appears."""
        for family, states in self._by_family.items():
            sig = _FAMILY_SIGNALS.get(family)
            if not sig:
                continue
            rejected_markers = sig.get("rejected", ())
            evaluated_markers = sig.get("evaluated", ())
            hit_reject = any(m and m in text for m in rejected_markers)
            hit_eval = any(m and m in text for m in evaluated_markers)
            if hit_reject:
                for st in states:
                    st.false_positive_rejected = True
                    st.evidence.append("REJECT:%s" % (text[:120],))
            elif hit_eval:
                for st in states:
                    st.observed_evaluated = True
                    st.evidence.append("EVAL:%s" % (text[:120],))

    # ----- verdict ---------------------------------------------------------

    def _flag_audit_passed(self) -> Tuple[bool, str]:
        """Returns (passed, locus). Strict: any UNVERIFIABLE or REJECTED fails.
        Lenient: only REJECTED fails (UNVERIFIABLE warns)."""
        for st in self.flags.values():
            v = st.verdict()
            if v == FlagVerdict.REJECTED:
                return False, "flag_audit:rejected:%s" % (st.flag,)
            if v == FlagVerdict.UNVERIFIABLE and self.strict:
                return False, "flag_audit:unverifiable:%s" % (st.flag,)
        return True, ""

    def _fsm_reached_applied(self) -> bool:
        # CLASSIFY...APPLY present + state=applied observed.
        saw_classify = any(
            p.upper().startswith("CLASSIFY") for p in self.fsm_phases_seen
        )
        saw_apply = any(p.upper().startswith("APPLY") for p in self.fsm_phases_seen)
        return saw_classify and saw_apply and self.state_applied

    def verdict(self) -> A1Verdict:
        """Compute the structured A1 verdict. Honest: each criterion is a real
        bool; A1_DISPATCH_PROVEN requires ALL five."""
        flag_pass, flag_locus = self._flag_audit_passed()
        trace_ok = self.trace.all_hops_in_order()
        fsm_ok = self._fsm_reached_applied()
        no_intervention = not self.intervention_tripped
        pr_ok = self.pr_signal_observed

        criteria = {
            "a1trace_5_hops_in_order": trace_ok,
            "fsm_classify_to_applied": fsm_ok,
            "twelve_flag_audit_passed": flag_pass,
            "intervention_lock_clean": no_intervention,
            "autonomous_pr_observed": pr_ok,
        }
        proven = all(criteria.values())

        # Failure locus: the first failing criterion (deterministic order).
        locus = ""
        if not proven:
            if not no_intervention:
                locus = (
                    self.tripped_exception.failure_locus
                    if self.tripped_exception else "intervention_lock"
                )
            elif not trace_ok:
                missing = self._missing_hops()
                locus = "a1trace:missing_or_out_of_order:%s" % (",".join(missing) or "order",)
            elif not fsm_ok:
                locus = "fsm:did_not_reach_state_applied"
            elif not flag_pass:
                locus = flag_locus
            elif not pr_ok:
                locus = "pr:no_autonomous_pr_or_commit_signal"

        return A1Verdict(
            proven=proven,
            flags=[st.to_dict() for st in self.flags.values()],
            a1trace_timeline=self.trace.to_dict(),
            criteria=criteria,
            failure_locus=locus,
            graduation_exception=(
                self.tripped_exception.to_dict() if self.tripped_exception else None
            ),
        )

    def _missing_hops(self) -> List[str]:
        winning = self.trace.winning_goal()
        if winning is not None:
            return []
        # Report hops never observed for ANY goal.
        seen_hops = {h for (h, _g, _t) in self.trace.ordered}
        return [h for h in A1TRACE_HOPS if h not in seen_hops]


# ===========================================================================
# SSE parsing (reuse the c2 subscriber's proven block parser shape)
# ===========================================================================


def parse_sse_block(block: str) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """Parse one SSE record into ``(event_type, last_event_id, payload)``.
    Heartbeats / comments / malformed -> (event_type, id, None). NEVER raises.

    The engine frames carry the full JSON envelope
    ``{schema_version, event_id, event_type, op_id, timestamp, payload}`` in
    the ``data:`` field; the SSE ``event:`` line repeats event_type and the
    ``id:`` line carries event_id for Last-Event-ID replay."""
    event_type: Optional[str] = None
    event_id: Optional[str] = None
    data_lines: List[str] = []
    for line in block.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return event_type, event_id, None
    try:
        parsed = json.loads("\n".join(data_lines))
    except Exception:  # noqa: BLE001
        return event_type, event_id, None
    return event_type, event_id, parsed


def envelope_to_event(
    sse_event_type: Optional[str], payload: Optional[dict]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Normalize an SSE record into ``(event_type, inner_payload)`` for the
    auditor. The engine wraps the real event in a JSON envelope where the
    authoritative ``event_type`` + ``payload`` live; fall back to the SSE
    ``event:`` line when the envelope is absent."""
    if isinstance(payload, dict):
        et = payload.get("event_type") or sse_event_type
        inner = payload.get("payload")
        if not isinstance(inner, dict):
            # Some frames put fields at top-level; pass the whole dict.
            inner = {k: v for k, v in payload.items() if k != "payload"}
        return (str(et) if et else None), inner
    return sse_event_type, {}


# ===========================================================================
# Async event sources (unified timeline: SSE stream + log-file tail)
# ===========================================================================


async def sse_event_source(
    base: str,
    *,
    on_event: Callable[[Optional[str], Dict[str, Any]], None],
    last_event_id: Optional[str] = None,
    stop: Optional["asyncio.Event"] = None,
    max_reconnect_backoff: float = 30.0,
    log: Callable[[str], None] = print,
) -> None:
    """Subscribe to ``GET {base}/observability/stream``, parse frames live, and
    push normalized ``(event_type, payload)`` to ``on_event``. Bounded
    exp-backoff reconnect + Last-Event-ID replay. Late aiohttp import keeps the
    module unit-testable without the dependency."""
    import aiohttp  # noqa: WPS433 -- late import on purpose

    stream_url = base.rstrip("/") + "/observability/stream"
    backoff = 1.0
    cur_event_id = last_event_id
    log("[A1Auditor] SSE subscribing -> %s" % (stream_url,))
    while not (stop and stop.is_set()):
        try:
            headers = {"Accept": "text/event-stream"}
            if cur_event_id:
                headers["Last-Event-ID"] = cur_event_id
            timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(stream_url, headers=headers) as resp:
                    if resp.status != 200:
                        log("[A1Auditor] SSE HTTP %d -- retrying" % (resp.status,))
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, max_reconnect_backoff)
                        continue
                    backoff = 1.0
                    log("[A1Auditor] SSE connected -- live audit")
                    buf = ""
                    async for chunk in resp.content.iter_any():
                        if stop and stop.is_set():
                            return
                        buf += chunk.decode("utf-8", errors="ignore")
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            et, eid, payload = parse_sse_block(block)
                            if eid:
                                cur_event_id = eid
                            norm_et, inner = envelope_to_event(et, payload)
                            on_event(norm_et, inner)
        except asyncio.CancelledError:
            raise
        except GraduationFailedException:
            raise
        except Exception as exc:  # noqa: BLE001
            log(
                "[A1Auditor] SSE disconnected (%s) -- reconnecting in %.0fs"
                % (type(exc).__name__, backoff)
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_reconnect_backoff)


async def log_tail_source(
    path: str,
    *,
    on_line: Callable[[str], None],
    stop: Optional["asyncio.Event"] = None,
    poll_interval: float = 0.25,
    log: Callable[[str], None] = print,
) -> None:
    """Tail a soak log file for ``[A1Trace]`` (and gate-telemetry) lines, then
    feed each line to ``on_line``. Follows the file as it grows (and survives
    the file not yet existing). Pure stdlib."""
    log("[A1Auditor] log tail -> %s" % (path,))
    pos = 0
    while not (stop and stop.is_set()):
        try:
            if not os.path.exists(path):
                await asyncio.sleep(poll_interval)
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                fh.seek(pos)
                for line in fh:
                    if line.endswith("\n"):
                        on_line(line.rstrip("\n"))
                    else:
                        # Partial trailing line -- rewind so we re-read it whole.
                        fh.seek(fh.tell() - len(line.encode("utf-8")))
                        break
                pos = fh.tell()
        except GraduationFailedException:
            raise
        except Exception as exc:  # noqa: BLE001
            log("[A1Auditor] log tail error (%s)" % (type(exc).__name__,))
        await asyncio.sleep(poll_interval)


# ===========================================================================
# Watch orchestration
# ===========================================================================


async def run_watch(
    auditor: A1GraduationAuditor,
    *,
    base: Optional[str],
    log_file: Optional[str],
    timeout_s: float,
    log: Callable[[str], None] = print,
) -> A1Verdict:
    """Run the live audit until A1_DISPATCH_PROVEN, an intervention trip, or a
    timeout. Returns the final verdict. Fail-CLOSED: a GraduationFailedException
    from any source terminates and yields a failed verdict (with the exception
    recorded)."""
    stop = asyncio.Event()

    def _on_event(et: Optional[str], payload: Dict[str, Any]) -> None:
        auditor.ingest_event(et, payload)
        if auditor.verdict().proven:
            stop.set()

    def _on_line(line: str) -> None:
        auditor.ingest_log_line(line)
        if auditor.verdict().proven:
            stop.set()

    tasks: List["asyncio.Task[Any]"] = []
    if base:
        tasks.append(
            asyncio.ensure_future(
                sse_event_source(base, on_event=_on_event, stop=stop, log=log)
            )
        )
    if log_file:
        tasks.append(
            asyncio.ensure_future(
                log_tail_source(log_file, on_line=_on_line, stop=stop, log=log)
            )
        )
    if not tasks:
        log("[A1Auditor] no event source (need --base and/or --log-file)")
        return auditor.verdict()

    async def _deadline() -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            log("[A1Auditor] timeout after %.0fs -- emitting partial verdict" % (timeout_s,))
            stop.set()

    deadline_task = asyncio.ensure_future(_deadline())
    tasks.append(deadline_task)

    try:
        # Wait until stop is set OR a source raises (intervention).
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )
        for t in done:
            exc = t.exception()
            if isinstance(exc, GraduationFailedException):
                log("[A1Auditor] %s" % (str(exc),))
                stop.set()
                break
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                log("[A1Auditor] source error: %r" % (exc,))
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return auditor.verdict()


# ===========================================================================
# CLI
# ===========================================================================


def _write_verdict(verdict: A1Verdict, path: str, log: Callable[[str], None] = print) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(verdict.to_json())
        log("[A1Auditor] verdict written -> %s" % (path,))
    except Exception as exc:  # noqa: BLE001
        log("[A1Auditor] FAILED to write verdict (%s)" % (type(exc).__name__,))


def _render_verdict(verdict: A1Verdict, log: Callable[[str], None] = print) -> None:
    if verdict.proven:
        log("[A1Auditor] VERDICT: A1_DISPATCH_PROVEN")
    else:
        log("[A1Auditor] VERDICT: FAILED")
        log("[A1Auditor] FAILURE LOCUS: %s" % (verdict.failure_locus or "unknown",))
    for name, ok in verdict.criteria.items():
        log("[A1Auditor]   %-32s %s" % (name, "PASS" if ok else "FAIL"))
    # Flag table.
    for f in verdict.flags:
        log(
            "[A1Auditor]   flag %-50s %s"
            % (f["flag"][:50], f["verdict"])
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "A1 Real-Time SSE GraduationAuditor + Absolute Intervention-Lock "
            "-- live 12-flag (CADENCE_POLICY-derived) audit + 5-hop A1Trace "
            "observation, fail-CLOSED autonomy verification."
        )
    )
    ap.add_argument(
        "--watch", action="store_true",
        help="Run the live audit against the SSE stream + log file.",
    )
    ap.add_argument(
        "--base", default=os.environ.get("JARVIS_OBSERVABILITY_BASE", "http://localhost:8099"),
        help="Observability base URL (default loopback / env JARVIS_OBSERVABILITY_BASE).",
    )
    ap.add_argument("--log-file", default=None, help="Soak log file to tail for [A1Trace] lines.")
    ap.add_argument(
        "--timeout", type=float,
        default=float(os.environ.get("JARVIS_A1_AUDIT_TIMEOUT_S", "3600") or 3600),
        help="Max wall-clock seconds to watch before emitting a partial verdict.",
    )
    strict_default = (
        os.environ.get("JARVIS_A1_AUDIT_STRICT", "true").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    ap.add_argument("--strict", dest="strict", action="store_true", default=strict_default,
                    help="UNVERIFIABLE flags fail the audit (default).")
    ap.add_argument("--lenient", dest="strict", action="store_false",
                    help="UNVERIFIABLE flags only warn (do not fail).")
    ap.add_argument(
        "--verdict-out", default="a1_verdict.json",
        help="Path to write the structured verdict JSON.",
    )
    args = ap.parse_args(argv)

    try:
        auditor = A1GraduationAuditor(strict=args.strict)
    except GraduationFailedException as exc:
        print("[A1Auditor] %s" % (str(exc),))
        return 2

    print(
        "[A1Auditor] flags=%d strict=%s (set derived from %s)"
        % (
            len(auditor.flags),
            args.strict,
            "JARVIS_A1_AUDIT_FLAGS" if os.environ.get("JARVIS_A1_AUDIT_FLAGS") else "CADENCE_POLICY",
        )
    )

    if not args.watch:
        print("[A1Auditor] no --watch given; nothing to do (see --help).")
        return 0

    try:
        verdict = asyncio.run(
            run_watch(
                auditor,
                base=args.base,
                log_file=args.log_file,
                timeout_s=args.timeout,
            )
        )
    except GraduationFailedException as exc:
        # Fail-CLOSED: surface the partial verdict with the exception recorded.
        verdict = auditor.verdict()
        print("[A1Auditor] GraduationFailedException: %s" % (str(exc),))

    _render_verdict(verdict)
    _write_verdict(verdict, args.verdict_out)
    return 0 if verdict.proven else 1


if __name__ == "__main__":
    raise SystemExit(main())
