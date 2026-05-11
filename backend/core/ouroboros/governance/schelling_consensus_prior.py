"""
Schelling-Point Consensus Prior
================================

Closes §40 Wave 4 #12 — the third Wave 4 (Tier 3 calibration
learning) arc. Per the operator binding:

  "Multi-prior speculative execution dispatches K rolls;
   consensus computed via structural-signature voting. Add a
   'most-trusted historical prior' weight to break ties when
   consensus is split."

This substrate is a **pure-function tie-breaker** that composes
the existing Move 6.5 ``generative_quorum.ConsensusVerdict``
surface with a §33.4 JSONL history of per-prior accept-rate
outcomes. When the verdict comes back ``DISAGREEMENT`` — no
cluster met the consensus threshold — the substrate ranks the
competing rolls by historical trust score and emits a frozen
:class:`SchellingTieBreakReport` proposing the most-trusted
prior's roll as the canonical pick.

The tie-break is **deterministic** — same prior history corpus +
same candidate roll set → same selection. No LLM call. The
selection is *proposed*; the caller (dispatcher) decides whether
to act on it. ``DISABLED`` / ``NO_RECORD`` / ``NO_TIE`` outcomes
are no-ops (substrate doesn't claim authority to override
consensus when it had something to say).

Composition contract — thin pure-function tie-breaker over
canonical substrates:

* :class:`generative_quorum.ConsensusVerdict` (Move 6.5 frozen
  artifact) — the input. The substrate NEVER recomputes
  consensus; it only reads the verdict and decides whether the
  outcome warrants a tie-break attempt.
* :class:`generative_quorum.CandidateRoll` (Move 6.5 frozen
  artifact) — the per-roll diff + signature surface.
* :func:`cross_process_jsonl.flock_append_line` — §33.4 prior
  history ledger at ``.jarvis/schelling_prior_history.jsonl``.
  One record per (prior_kind, op_id, was_accepted) outcome
  observation.

NEVER raises. Empty history / ledger unreachable / malformed
records all degrade to ``NO_RECORD`` or ``DISABLED``, not
exception.

Closed 4-value :class:`SchellingDecision`:

  NO_TIE          ✓ verdict was CONSENSUS or MAJORITY_CONSENSUS
                    — substrate is a no-op.
  TIE_BROKEN      🎯 verdict was DISAGREEMENT AND at least one
                    competing prior has historical records —
                    most-trusted prior's roll proposed as pick.
  NO_RECORD       · verdict was DISAGREEMENT but no historical
                    records for any competing prior — caller
                    falls through to existing DISAGREEMENT path
                    (BLOCKED escalation).
  DISABLED        ◌ master flag off OR substrate unavailable.

Closed 4-value :class:`PriorTrustLevel`:

  UNKNOWN         sample_count == 0
  LOW             accept_rate < 0.25
  MEDIUM          0.25 ≤ accept_rate < 0.75
  HIGH            accept_rate ≥ 0.75

§33.1 cognitive substrate ``JARVIS_SCHELLING_PRIOR_ENABLED``
default-**FALSE** — operator-paced opt-in. Sub-flag
``JARVIS_SCHELLING_PRIOR_PERSIST_ENABLED`` gates §33.4 writes
(default TRUE when master on).

Authority asymmetry (AST-pinned): imports stdlib +
``cross_process_jsonl`` ONLY. Does NOT import orchestrator /
iron_gate / policy / providers / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor. Does NOT import
``multi_prior_dispatch`` / ``multi_prior_runner`` either — the
substrate is a *recommender* read by the dispatcher; reverse
dependency keeps the cage one-way.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


SCHELLING_PRIOR_SCHEMA_VERSION: str = "schelling_prior.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_SCHELLING_PRIOR_ENABLED"
_ENV_PERSIST = "JARVIS_SCHELLING_PRIOR_PERSIST_ENABLED"
_ENV_MAX_RECORDS = "JARVIS_SCHELLING_PRIOR_MAX_RECORDS"
_ENV_MIN_SAMPLE = "JARVIS_SCHELLING_PRIOR_MIN_SAMPLE"
_ENV_LEDGER_PATH = "JARVIS_SCHELLING_PRIOR_LEDGER_PATH"

_DEFAULT_MAX_RECORDS = 500
_DEFAULT_MIN_SAMPLE = 3
_MIN_RECORDS = 1
_MAX_RECORDS = 100_000
_MIN_SAMPLE_LO = 1
_MIN_SAMPLE_HI = 1_000

_DEFAULT_LEDGER_REL = ".jarvis/schelling_prior_history.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Tie-break attempts return DISABLED
    when off. Flip ``JARVIS_SCHELLING_PRIOR_ENABLED=true`` to
    begin accumulating per-prior accept-rate history + breaking
    ties on multi-prior DISAGREEMENT.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate §33.4 JSONL writes. Default TRUE when
    master on. Operator may set False for eval-only mode."""
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_records() -> int:
    """Maximum history rows read per evaluation. Clamped to
    [1, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_RECORDS,
        _DEFAULT_MAX_RECORDS,
        _MIN_RECORDS,
        _MAX_RECORDS,
    )


def min_sample_size() -> int:
    """Minimum number of historical records required for a
    prior's trust score to be considered actionable. Defaults
    to 3 — lower yields under-sampled noise; higher yields slow
    cold-start. Clamped to [1, 1_000]."""
    return _read_clamped_int(
        _ENV_MIN_SAMPLE,
        _DEFAULT_MIN_SAMPLE,
        _MIN_SAMPLE_LO,
        _MIN_SAMPLE_HI,
    )


def ledger_path() -> Path:
    """History ledger path. Defaults to
    ``.jarvis/schelling_prior_history.jsonl`` relative to CWD;
    operator may override via
    ``JARVIS_SCHELLING_PRIOR_LEDGER_PATH``.
    """
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class SchellingDecision(str, enum.Enum):
    """Closed 4-value top-level decision — bytes-pinned via AST."""

    NO_TIE = "no_tie"
    TIE_BROKEN = "tie_broken"
    NO_RECORD = "no_record"
    DISABLED = "disabled"


class PriorTrustLevel(str, enum.Enum):
    """Closed 4-value trust-level taxonomy — bytes-pinned via AST."""

    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_DECISION_GLYPH: Dict[str, str] = {
    SchellingDecision.NO_TIE.value: "✓",
    SchellingDecision.TIE_BROKEN.value: "🎯",
    SchellingDecision.NO_RECORD.value: "·",
    SchellingDecision.DISABLED.value: "◌",
}


_TRUST_GLYPH: Dict[str, str] = {
    PriorTrustLevel.UNKNOWN.value: "○",
    PriorTrustLevel.LOW.value: "▽",
    PriorTrustLevel.MEDIUM.value: "◊",
    PriorTrustLevel.HIGH.value: "▲",
}


def decision_glyph(decision: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(decision, "value"):
            return _DECISION_GLYPH.get(str(decision.value), "?")
        return _DECISION_GLYPH.get(
            str(decision or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def trust_glyph(level: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(level, "value"):
            return _TRUST_GLYPH.get(str(level.value), "?")
        return _TRUST_GLYPH.get(
            str(level or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _trust_level_for(accept_rate: float, sample_count: int) -> PriorTrustLevel:
    if sample_count < min_sample_size():
        return PriorTrustLevel.UNKNOWN
    if accept_rate < 0.25:
        return PriorTrustLevel.LOW
    if accept_rate < 0.75:
        return PriorTrustLevel.MEDIUM
    return PriorTrustLevel.HIGH


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class PriorOutcomeRecord:
    """One historical accept-rate observation for a prior."""

    prior_kind: str
    op_id: str
    ast_signature: str
    was_accepted: bool
    observed_at_unix: float
    schema_version: str = SCHELLING_PRIOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "prior_outcome",
            "prior_kind": self.prior_kind[:64],
            "op_id": self.op_id[:128],
            "ast_signature": self.ast_signature[:64],
            "was_accepted": bool(self.was_accepted),
            "observed_at_unix": float(self.observed_at_unix),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PriorTrustReport:
    """Per-prior trust aggregation."""

    prior_kind: str
    sample_count: int
    accept_count: int
    accept_rate: float
    trust_level: PriorTrustLevel
    schema_version: str = SCHELLING_PRIOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prior_kind": self.prior_kind[:64],
            "sample_count": int(self.sample_count),
            "accept_count": int(self.accept_count),
            "accept_rate": float(self.accept_rate),
            "trust_level": self.trust_level.value,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SchellingTieBreakReport:
    """Aggregate tie-break report."""

    evaluated_at_unix: float
    master_enabled: bool
    decision: SchellingDecision
    consensus_outcome: str
    chosen_prior_kind: str
    chosen_roll_id: str
    trust_table: Tuple[PriorTrustReport, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = SCHELLING_PRIOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "decision": self.decision.value,
            "consensus_outcome": self.consensus_outcome,
            "chosen_prior_kind": self.chosen_prior_kind[:64],
            "chosen_roll_id": self.chosen_roll_id[:128],
            "trust_table": [t.to_dict() for t in self.trust_table],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces
# ===========================================================================


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 JSONL write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_history(
    *,
    max_total: Optional[int] = None,
    path_override: Optional[Path] = None,
) -> Tuple[Dict[str, Any], ...]:
    """Plain stdlib read-back of the append-only JSONL history.
    Corrupted lines silently skipped. NEVER raises."""
    cap = max_records() if max_total is None else int(max_total)
    target = path_override or ledger_path()
    rows: List[Dict[str, Any]] = []
    try:
        if not target.exists():
            return ()
        with target.open("r", encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                rows.append(obj)
                if len(rows) >= cap:
                    break
    except Exception:  # noqa: BLE001
        return tuple(rows)
    return tuple(rows)


# ===========================================================================
# Producer-bridge — record_prior_outcome
# ===========================================================================


def record_prior_outcome(
    prior_kind: str,
    op_id: str,
    ast_signature: str,
    was_accepted: bool,
    *,
    now_unix: Optional[float] = None,
) -> Optional[PriorOutcomeRecord]:
    """Producer-bridge — record one historical outcome row.
    NEVER raises. Returns frozen artifact even master-off
    (caller can route the record elsewhere)."""
    try:
        pk = str(prior_kind or "").strip()
        op = str(op_id or "").strip()
        sig = str(ast_signature or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not pk:
        return None
    now = time.time() if now_unix is None else float(now_unix)
    rec = PriorOutcomeRecord(
        prior_kind=pk,
        op_id=op,
        ast_signature=sig,
        was_accepted=bool(was_accepted),
        observed_at_unix=now,
    )
    _flock_append(rec.to_dict())
    return rec


# ===========================================================================
# Pure trust aggregation
# ===========================================================================


def _index_history(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Mapping[str, Any]]]:
    out: Dict[str, List[Mapping[str, Any]]] = {}
    for r in rows:
        if r.get("kind") != "prior_outcome":
            continue
        pk = str(r.get("prior_kind") or "").strip()
        if not pk:
            continue
        out.setdefault(pk, []).append(r)
    return out


def compute_prior_trust(
    prior_kind: str,
    *,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> PriorTrustReport:
    """Pure trust aggregation for one prior. NEVER raises."""
    pk = str(prior_kind or "").strip()
    history = rows if rows is not None else _load_history()
    matching = [
        r for r in history
        if r.get("kind") == "prior_outcome"
        and str(r.get("prior_kind") or "").strip() == pk
    ]
    sample = len(matching)
    accepts = sum(
        1 for r in matching if bool(r.get("was_accepted"))
    )
    rate = (accepts / sample) if sample > 0 else 0.0
    return PriorTrustReport(
        prior_kind=pk,
        sample_count=sample,
        accept_count=accepts,
        accept_rate=rate,
        trust_level=_trust_level_for(rate, sample),
    )


def _build_trust_table(
    candidate_priors: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[PriorTrustReport, ...]:
    """For each (roll_id → prior_kind) entry, return one
    PriorTrustReport per distinct prior_kind. Deterministic
    sort order (descending accept_rate, then ascending name)."""
    distinct_priors = sorted(
        {str(pk or "").strip() for pk in candidate_priors.values() if pk},
    )
    reports = [
        compute_prior_trust(pk, rows=rows)
        for pk in distinct_priors
    ]
    return tuple(
        sorted(
            reports,
            key=lambda r: (-r.accept_rate, r.prior_kind),
        )
    )


# ===========================================================================
# Top-level tie-break
# ===========================================================================


def break_tie(
    consensus_verdict: Any,
    candidate_priors_by_roll: Mapping[str, str],
    *,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> SchellingTieBreakReport:
    """Pure tie-break. NEVER raises.

    Parameters
    ----------
    consensus_verdict:
        The Move 6.5 :class:`ConsensusVerdict` returned by
        ``generative_quorum.compute_consensus``. Substrate
        accesses ``.outcome.value`` defensively.
    candidate_priors_by_roll:
        Mapping ``{roll_id: prior_kind}`` — one entry per
        competing roll.
    rows:
        Caller-injectable history (testing seam). Defaults to
        :func:`_load_history`.
    """
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return SchellingTieBreakReport(
            evaluated_at_unix=started,
            master_enabled=False,
            decision=SchellingDecision.DISABLED,
            consensus_outcome="",
            chosen_prior_kind="",
            chosen_roll_id="",
            trust_table=(),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )

    # Defensive outcome extraction. Move 6.5 verdict has
    # ``.outcome.value``; we tolerate raw strings + missing
    # attrs without raising.
    try:
        if hasattr(consensus_verdict, "outcome"):
            raw_outcome = getattr(
                consensus_verdict.outcome, "value", consensus_verdict.outcome,
            )
        else:
            raw_outcome = consensus_verdict
        consensus_outcome = str(raw_outcome or "").strip().lower()
    except Exception:  # noqa: BLE001
        consensus_outcome = ""

    # NO_TIE shortcut — consensus already had an actionable answer.
    if consensus_outcome in ("consensus", "majority_consensus"):
        return SchellingTieBreakReport(
            evaluated_at_unix=started,
            master_enabled=True,
            decision=SchellingDecision.NO_TIE,
            consensus_outcome=consensus_outcome,
            chosen_prior_kind="",
            chosen_roll_id="",
            trust_table=(),
            diagnostic=(
                f"consensus={consensus_outcome} — substrate "
                "is a no-op"
            ),
            elapsed_s=max(0.0, time.time() - started),
        )

    # Load history + build trust table for competing priors.
    history = rows if rows is not None else _load_history()
    by_kind = _index_history(history)
    trust_table = _build_trust_table(candidate_priors_by_roll, history)

    # Need at least one prior with sample_count >= min_sample to
    # break the tie. Otherwise fall through.
    actionable = [
        t for t in trust_table
        if t.trust_level is not PriorTrustLevel.UNKNOWN
    ]
    if not actionable:
        return SchellingTieBreakReport(
            evaluated_at_unix=started,
            master_enabled=True,
            decision=SchellingDecision.NO_RECORD,
            consensus_outcome=consensus_outcome,
            chosen_prior_kind="",
            chosen_roll_id="",
            trust_table=trust_table,
            diagnostic=(
                "no historical records meet min_sample "
                f"({min_sample_size()}) for any competing "
                f"prior; {len(candidate_priors_by_roll)} "
                "candidate roll(s) — fall through to existing "
                "DISAGREEMENT path"
            ),
            elapsed_s=max(0.0, time.time() - started),
        )

    # Highest-trust prior wins (already sorted by accept_rate desc).
    winner = actionable[0]
    chosen_roll_id = ""
    for roll_id, pk in candidate_priors_by_roll.items():
        if str(pk or "").strip() == winner.prior_kind:
            chosen_roll_id = str(roll_id)
            break

    report = SchellingTieBreakReport(
        evaluated_at_unix=started,
        master_enabled=True,
        decision=SchellingDecision.TIE_BROKEN,
        consensus_outcome=consensus_outcome,
        chosen_prior_kind=winner.prior_kind,
        chosen_roll_id=chosen_roll_id,
        trust_table=trust_table,
        diagnostic=(
            f"tie broken: prior={winner.prior_kind} trust="
            f"{winner.trust_level.value} "
            f"({winner.accept_count}/{winner.sample_count} "
            f"= {winner.accept_rate:.2f})"
        ),
        elapsed_s=max(0.0, time.time() - started),
    )
    _publish_tiebreak_event(report)
    return report


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_tiebreak_event(
    report: SchellingTieBreakReport,
) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.decision is not SchellingDecision.TIE_BROKEN:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SCHELLING_TIE_BROKEN,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_SCHELLING_TIE_BROKEN,
            (
                f"system::schelling_prior::"
                f"{report.schema_version}"
            ),
            {
                "decision": report.decision.value,
                "consensus_outcome": report.consensus_outcome,
                "chosen_prior_kind": report.chosen_prior_kind,
                "chosen_roll_id": report.chosen_roll_id,
                "trust_table_size": len(report.trust_table),
                "evaluated_at_unix": report.evaluated_at_unix,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_tiebreak_panel(
    report: Optional[SchellingTieBreakReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"schelling tie-break: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "schelling tie-break: no report"
    if not report.master_enabled:
        return (
            f"schelling tie-break: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = decision_glyph(report.decision)
    lines = [
        f"🎯 Schelling Tie-Break  {glyph} {report.decision.value}",
        f"  consensus_outcome   : {report.consensus_outcome or '?'}",
    ]
    if report.decision is SchellingDecision.TIE_BROKEN:
        lines.extend([
            f"  chosen_prior_kind   : {report.chosen_prior_kind}",
            f"  chosen_roll_id      : {report.chosen_roll_id}",
        ])
    if report.trust_table:
        lines.append("  trust_table:")
        for t in report.trust_table[:5]:
            tg = trust_glyph(t.trust_level)
            lines.append(
                f"    {tg} {t.prior_kind:<24} "
                f"{t.accept_count}/{t.sample_count} = "
                f"{t.accept_rate:.2f} ({t.trust_level.value})"
            )
        if len(report.trust_table) > 5:
            lines.append(
                f"    ... (+{len(report.trust_table) - 5} more)"
            )
    lines.append(f"  diagnostic          : {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "schelling_consensus_prior.py"
    )

    _EXPECTED_DECISIONS = {
        "no_tie", "tie_broken", "no_record", "disabled",
    }
    _EXPECTED_TRUST = {
        "unknown", "low", "medium", "high",
    }

    def _validate_decision_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "SchellingDecision"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_DECISIONS - found
                extra = found - _EXPECTED_DECISIONS
                if missing:
                    return (
                        f"SchellingDecision missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"SchellingDecision drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("SchellingDecision class not found",)

    def _validate_trust_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PriorTrustLevel"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_TRUST - found
                extra = found - _EXPECTED_TRUST
                if missing:
                    return (
                        f"PriorTrustLevel missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"PriorTrustLevel drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("PriorTrustLevel class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            (
                "backend.core.ouroboros.governance.verification."
                "multi_prior_dispatch"
            ),
            (
                "backend.core.ouroboros.governance.verification."
                "multi_prior_runner"
            ),
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose canonical cross_process_jsonl "
                "(no parallel JSONL writer)",
            )
        if "flock_append_line" not in source:
            violations.append(
                "must use flock_append_line for §33.4 writes",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "schelling_prior_decision_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "SchellingDecision 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_decision_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "schelling_prior_trust_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "PriorTrustLevel 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_trust_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="schelling_prior_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — pure tie-breaker. MUST "
                "NOT import orchestrator / iron_gate / policy "
                "/ providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / multi_prior_dispatch / "
                "multi_prior_runner."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "schelling_prior_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="schelling_prior_composes_canonical",
            target_file=target,
            description=(
                "Substrate composes cross_process_jsonl "
                "(flock_append_line for §33.4 writes)."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "schelling_consensus_prior.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Schelling-point consensus prior master "
                "switch. §33.1 cognitive substrate "
                "default-FALSE. When on, the substrate "
                "accumulates per-prior accept-rate history "
                "via record_prior_outcome and breaks "
                "consensus ties on multi-prior DISAGREEMENT "
                "by selecting the most-trusted historical "
                "prior. Composes Move 6.5 generative_quorum "
                "ConsensusVerdict (read-only) + "
                "cross_process_jsonl (§33.4 ledger). Closes "
                "§40 Wave 4 #12 (PRD v2.99+)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate §33.4 JSONL audit writes. "
                "Default True when master on. Operator may "
                "set False for ephemeral eval-only mode."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_MAX_RECORDS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_RECORDS,
            description=(
                "Maximum history rows read per evaluation. "
                "Clamped to [1, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_RECORDS}=1000",
        ),
        FlagSpec(
            name=_ENV_MIN_SAMPLE,
            type=FlagType.INT,
            default=_DEFAULT_MIN_SAMPLE,
            description=(
                "Minimum historical sample count required "
                "for a prior's trust score to be actionable. "
                "Defaults to 3. Clamped to [1, 1_000]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MIN_SAMPLE}=10",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "SCHELLING_PRIOR_SCHEMA_VERSION",
    "SchellingDecision",
    "PriorTrustLevel",
    "PriorOutcomeRecord",
    "PriorTrustReport",
    "SchellingTieBreakReport",
    "master_enabled",
    "persistence_enabled",
    "max_records",
    "min_sample_size",
    "ledger_path",
    "decision_glyph",
    "trust_glyph",
    "record_prior_outcome",
    "compute_prior_trust",
    "break_tie",
    "format_tiebreak_panel",
    "register_shipped_invariants",
    "register_flags",
]
