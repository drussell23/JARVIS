"""M10 Slice 3 — ProposalSynthesizer (LLM-bound, PRD §32.4.2).

The K=3 parallel synthesis stage. Reads an `M10ProposalRecord`
candidate from Slice 2 (`UnhandledPatternMiner`); fans out K
parallel synthesis calls via a caller-injected
:class:`SynthesisProviderProtocol`; AST-canonicalizes each
candidate; checks Quorum consensus (≥majority signature
agreement); enforces **mandatory AST self-pin** (M10 cannot
propose code without a self-pinning invariant per §32.4.4);
returns a frozen :class:`SynthesizedProposal`.

Decisions (operator mandate, AST-pinned at Slice 5):

  * **SP-A1 — Async + parallel** — `asyncio.gather` on K
    Protocol-bound calls. Production wires
    :mod:`candidate_generator` STANDARD route (which auto-
    routes through `urgency_router` + 3-tier failback +
    cost ledger); tests inject mocks via
    :class:`SynthesisProviderProtocol`.
  * **SP-B1 — Cost via composition** — H6 inheritance: each
    provider call returns its `cost_usd` which the
    synthesizer sums into `SynthesizedProposal.cost_usd`. NO
    parallel cost system. NO direct provider import in this
    module — production caller wires the route.
  * **SP-C1 — Quorum via AST canonicalization** — uses
    :func:`compute_ast_signature` (Move 6 substrate) to hash
    each candidate's source body. Majority signature = consensus.
    K=3 default, env-tunable via
    :func:`m10_synthesis_quorum_k`.
  * **SP-D1 — Mandatory self-pin** — generator output MUST
    include an AST invariant name (non-empty
    `ast_pin_name`); rejected with `NO_SELF_PIN` verdict
    otherwise. This is structurally enforced — Slice 4's
    Iron Gate is a second line of defense.
  * **SP-E1 — Risk-tier forced to APPROVAL_REQUIRED** — the
    proposal record returned has its risk-tier semantically
    locked at the highest gate so it can NEVER auto-apply.
    AST-pinned at Slice 5.
  * **SP-F1 — NEVER raises** — all faults map to a
    SynthesizedProposal with the appropriate verdict +
    diagnostic. The caller (Slice 4) reads verdict to dispatch.
  * **SP-G1 — Authority asymmetry** — synthesizer MUST NOT
    import orchestrator / iron_gate / providers /
    candidate_generator / urgency_router / tool_executor /
    auto_action_router / strategic_direction / change_engine
    / subagent_scheduler / semantic_guardian / policy /
    graduation_orchestrator. Pure orchestration over the
    Protocol-supplied provider + Move 6's ast_canonical
    primitive.

Closed taxonomy:

  * :class:`SynthesisVerdict` — 7 values
    (SYNTHESIZED / QUORUM_DISAGREEMENT / NO_SELF_PIN /
    PROVIDER_ERROR / INSUFFICIENT_CONTEXT / DISABLED /
    SKIPPED_KIND).
  * Frozen :class:`SynthesisCandidate` — one provider call
    output. Frozen :class:`SynthesizedProposal` — aggregate
    consensus result.
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any, Awaitable, Callable, Dict, Optional, Protocol,
    Sequence, Tuple,
)

logger = logging.getLogger(__name__)


M10_SYNTHESIZER_SCHEMA_VERSION: str = "m10_proposal_synthesizer.1"


# Forced risk tier for every proposal — operator-pinned. M10
# proposals may NEVER auto-apply; the synthesizer marks the
# output explicitly so downstream consumers (Slice 4 + 5) read
# the constant rather than a config value.
M10_FORCED_RISK_TIER: str = "approval_required"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        f = float(raw)
        if not math.isfinite(f):
            return default
        if f < floor:
            return floor
        if f > ceiling:
            return ceiling
        return f
    except (TypeError, ValueError):
        return default


def m10_synthesis_quorum_k() -> int:
    """``JARVIS_M10_SYNTHESIS_QUORUM_K`` — number of parallel
    synthesis calls for Quorum consensus. Default 3; clamped
    [1, 7]. K=1 disables consensus (single call); K=3 matches
    PRD §32.4.2 default (matches Move 6 K=3); K=5 / K=7 for
    higher-assurance modes."""
    return _read_int_knob(
        "JARVIS_M10_SYNTHESIS_QUORUM_K", 3, 1, 7,
    )


def m10_synthesis_majority_threshold() -> int:
    """``JARVIS_M10_SYNTHESIS_MAJORITY_THRESHOLD`` — minimum
    signature-agreement count required for SYNTHESIZED verdict.
    Default 2 (matches K=3 majority); clamped [1, 7]. Caller
    is responsible for keeping this ≤ K (synthesizer clamps
    defensively)."""
    return _read_int_knob(
        "JARVIS_M10_SYNTHESIS_MAJORITY_THRESHOLD", 2, 1, 7,
    )


def m10_synthesis_per_call_timeout_s() -> float:
    """``JARVIS_M10_SYNTHESIS_PER_CALL_TIMEOUT_S`` — wall-clock
    cap per provider call. Default 60.0; clamped [1.0, 600.0].
    The full K-way fan-out runs under one shared `gather` so
    total wall-clock is bounded near this same value
    (assuming asyncio cooperative scheduling)."""
    return _read_float_knob(
        "JARVIS_M10_SYNTHESIS_PER_CALL_TIMEOUT_S",
        60.0, 1.0, 600.0,
    )


def m10_synthesis_max_evidence_chars() -> int:
    """``JARVIS_M10_SYNTHESIS_MAX_EVIDENCE_CHARS`` — hard cap on
    detection-evidence chars injected into the prompt. Default
    4096; clamped [256, 65536]. Bounds prompt size + cost."""
    return _read_int_knob(
        "JARVIS_M10_SYNTHESIS_MAX_EVIDENCE_CHARS",
        4096, 256, 65536,
    )


# ---------------------------------------------------------------------------
# Closed taxonomy of synthesis verdicts
# ---------------------------------------------------------------------------


class SynthesisVerdict(str, enum.Enum):
    """Closed 7-value taxonomy of synthesis outcomes.
    ``str``-subclass for JSON-friendliness + closed-enum
    dispatch. Slice 4's validation pipeline reads
    ``verdict`` to route — anything other than SYNTHESIZED
    short-circuits to the proposal's terminal failure phase."""

    SYNTHESIZED = "synthesized"
    """K-majority consensus reached + AST self-pin present
    + at least one candidate parsed cleanly. Ready for
    Slice 4 validation pipeline."""

    QUORUM_DISAGREEMENT = "quorum_disagreement"
    """K candidates produced but no signature reached the
    majority threshold (every candidate canonicalized to a
    distinct AST). Indicates the prompt is under-constrained
    OR the underlying pattern doesn't have a stable
    deterministic shape — proposal cannot graduate."""

    NO_SELF_PIN = "no_self_pin"
    """Quorum consensus reached BUT the consensus candidate
    omitted the mandatory AST self-pin. Hard-rejected per
    §32.4.4 — cannot graduate without a self-pinning
    invariant."""

    PROVIDER_ERROR = "provider_error"
    """All K provider calls failed (timeout / exception /
    empty output). Slice 4 logs + retries on a future cycle."""

    INSUFFICIENT_CONTEXT = "insufficient_context"
    """Pattern bundle too sparse to synthesize from
    (empty detection_evidence, missing kind, etc.)."""

    DISABLED = "disabled"
    """Master flag (`JARVIS_M10_ARCH_PROPOSER_ENABLED`) is
    off. Synthesizer returned without invoking any provider."""

    SKIPPED_KIND = "skipped_kind"
    """ProposalKind is DISABLED OR an unsupported kind for
    the synthesis path. Sentinel — caller short-circuits."""


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisCandidate:
    """One model output. Frozen — Slice 3's synthesizer
    aggregates K of these via signature consensus.

    Production providers populate `code_text` + `class_name` +
    `module_path` + `ast_pin_name` from the model's structured
    output; `error` is non-empty when the call failed.
    Tests inject pre-built instances via the
    :class:`SynthesisProviderProtocol`."""

    code_text: str
    """The proposed module source. Empty on provider error.
    AST-canonicalized via :func:`compute_ast_signature` for
    consensus comparison."""

    class_name: str = ""
    """Class name within the module (e.g.,
    ``"NewSensorClass"``). Empty on provider error or when
    the kind doesn't carry a class (e.g., NEW_FLAG_FAMILY)."""

    module_path: str = ""
    """Repository-relative path the synthesizer wrote
    (e.g., ``"backend/core/ouroboros/governance/intake/
    sensors/new_pattern_sensor.py"``). Empty on error."""

    ast_pin_name: str = ""
    """The AST invariant name the synthesizer self-pinned.
    MANDATORY for SYNTHESIZED verdict. Empty signals
    NO_SELF_PIN gate trip."""

    cost_usd: float = 0.0
    """Cost of this single call. Production providers
    populate via candidate_generator's route ledger; tests
    typically zero."""

    error: str = ""
    """Non-empty on provider failure (timeout / exception /
    empty output). Caller treats as "this candidate
    contributes no signature."""


@dataclass(frozen=True)
class SynthesizedProposal:
    """Aggregate consensus result. Frozen — Slice 4 reads to
    dispatch through validation; Slice 5 observability
    projects via `to_dict()`."""

    proposal_id: str
    """Carries the originating M10ProposalRecord.proposal_id
    so Slice 4 can correlate."""

    kind: Any
    """The :class:`ProposalKind` value from the originating
    record. Held as Any to avoid cross-module type hint
    coupling at module load (lazy import discipline)."""

    verdict: SynthesisVerdict
    """Closed-enum dispatch for Slice 4."""

    code_text: str = ""
    """Consensus candidate's source body. Empty unless
    verdict is SYNTHESIZED."""

    class_name: str = ""
    module_path: str = ""
    ast_pin_name: str = ""
    """Mandatory for SYNTHESIZED verdict."""

    consensus_signature: str = ""
    """64-char SHA-256 of the AST-canonicalized consensus
    candidate. Used by Slice 4 for cross-cycle dedup +
    provenance tracking."""

    candidate_count: int = 0
    """Number of provider calls actually attempted. <= K."""

    candidate_signatures: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    """All K signatures (empty string for failed candidates).
    Operator-explainability — Slice 5 REPL renders these so
    operators see the K-way diversity."""

    cost_usd: float = 0.0
    """Sum of all candidate cost_usd. H6 inheritance —
    accumulates via the route ledger; this field is the
    proposal-level total."""

    forced_risk_tier: str = M10_FORCED_RISK_TIER
    """Always equals :data:`M10_FORCED_RISK_TIER`. Pinned
    at Slice 5 — proposals can NEVER auto-apply."""

    elapsed_s: float = 0.0
    diagnostic: str = ""
    schema_version: str = field(
        default=M10_SYNTHESIZER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe projection. NEVER raises."""
        try:
            kind_value = (
                self.kind.value
                if hasattr(self.kind, "value")
                else str(self.kind)
            )
        except Exception:  # noqa: BLE001 — defensive
            kind_value = "unknown"
        try:
            return {
                "schema_version": self.schema_version,
                "proposal_id": self.proposal_id,
                "kind": kind_value,
                "verdict": self.verdict.value,
                "code_text_len": len(self.code_text or ""),
                "class_name": self.class_name,
                "module_path": self.module_path,
                "ast_pin_name": self.ast_pin_name,
                "consensus_signature": (
                    self.consensus_signature
                ),
                "candidate_count": int(self.candidate_count),
                "candidate_signatures": list(
                    self.candidate_signatures,
                ),
                "cost_usd": float(self.cost_usd),
                "forced_risk_tier": self.forced_risk_tier,
                "elapsed_s": float(self.elapsed_s),
                "diagnostic": self.diagnostic,
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "schema_version": self.schema_version,
                "proposal_id": self.proposal_id,
                "verdict": self.verdict.value,
                "error": "projection_failed",
            }


# ---------------------------------------------------------------------------
# SynthesisProviderProtocol — caller-injected for testability
# ---------------------------------------------------------------------------


class SynthesisProviderProtocol(Protocol):
    """Caller-injected provider for the K parallel synthesis
    calls. Production wires :mod:`candidate_generator`'s
    STANDARD route (auto-routes through urgency_router +
    3-tier failback + cost ledger); tests inject in-memory
    mocks.

    Each call MUST return a :class:`SynthesisCandidate`. The
    provider is responsible for invoking the model + parsing
    the structured output (code_text + class_name +
    module_path + ast_pin_name fields). The synthesizer
    NEVER calls the model directly — pure orchestration."""

    async def synthesize_one(
        self,
        *,
        prompt: str,
        kind: Any,
        proposal_id: str,
    ) -> SynthesisCandidate: ...  # pragma: no cover — Protocol


# ---------------------------------------------------------------------------
# Prompt construction (deterministic, no LLM calls)
# ---------------------------------------------------------------------------


def _build_synthesis_prompt(
    record: Any,
    *,
    max_evidence_chars: int,
) -> str:
    """Construct the structured synthesis prompt. Pure +
    deterministic. NEVER raises — defaults to a minimal-
    viable prompt on any error."""
    try:
        kind_value = (
            record.kind.value
            if hasattr(record.kind, "value")
            else str(record.kind)
        )
        evidence_lines = list(
            getattr(record, "detection_evidence", ()) or (),
        )
        # Bound evidence to budget
        evidence_text = "\n".join(
            f"  - {line}" for line in evidence_lines
        )
        if len(evidence_text) > max_evidence_chars:
            evidence_text = (
                evidence_text[:max_evidence_chars]
                + "\n  - <truncated for budget>"
            )
        return _PROMPT_TEMPLATE.format(
            kind=kind_value,
            pattern_signature=getattr(
                record, "pattern_signature", "",
            ),
            proposal_id=getattr(record, "proposal_id", ""),
            evidence_text=evidence_text or "  - (none)",
            forced_risk_tier=M10_FORCED_RISK_TIER,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[m10_synthesizer] prompt build raised: %s", exc,
        )
        return _MINIMAL_PROMPT


_PROMPT_TEMPLATE: str = """\
M10 ArchitectureProposer — synthesis request

Proposal: {proposal_id}
Kind: {kind}
Pattern signature: {pattern_signature}

Detection evidence (recurring pattern that no existing sensor
catches OR a coherence-auditor RECURRENCE_DRIFT cluster):
{evidence_text}

Output requirements:
  1. A complete Python module body (code_text) — stdlib + the
     existing IntakeSensor / Observer / phase-runner Protocol
     contracts. Do NOT import orchestrator / iron_gate /
     providers — pure substrate.
  2. A class_name + module_path indicating where the module
     should live in the repo.
  3. **MANDATORY**: an ast_pin_name — a unique invariant name
     that will be added to ``meta/shipped_code_invariants.py``
     to lock the new module's contract. Proposals without
     self-pins are rejected at Iron Gate.
  4. Risk tier is forced to: {forced_risk_tier}. The proposal
     CANNOT auto-apply — a human reviews via GitHub PR.

Return: structured JSON with code_text, class_name,
module_path, ast_pin_name fields.
"""


_MINIMAL_PROMPT: str = (
    "M10 ArchitectureProposer synthesis request — "
    "context unavailable; produce a minimal scaffold."
)


# ---------------------------------------------------------------------------
# ProposalSynthesizer — load-bearing orchestrator
# ---------------------------------------------------------------------------


class ProposalSynthesizer:
    """K-way parallel synthesis orchestrator. Stateless —
    every call is independent. NEVER raises.

    Production: lazy-singleton via :func:`get_default_synthesizer`.
    Tests: construct fresh + inject
    :class:`SynthesisProviderProtocol` mock."""

    async def synthesize(
        self,
        record: Any,
        *,
        provider: SynthesisProviderProtocol,
    ) -> SynthesizedProposal:
        """**Authoritative entry point.** Synthesize K
        candidates in parallel, check consensus, enforce
        AST self-pin, return frozen
        :class:`SynthesizedProposal`. NEVER raises."""
        started = time.monotonic()
        proposal_id = getattr(record, "proposal_id", "") or ""
        # Default kind for diagnostic projection on early-exit
        record_kind = getattr(record, "kind", None)

        # Master-flag check
        try:
            from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
                m10_arch_proposer_enabled,
                ProposalKind,
            )
        except Exception:  # noqa: BLE001 — defensive
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.DISABLED,
                elapsed_s=time.monotonic() - started,
                diagnostic="primitives import failed",
            )
        if not m10_arch_proposer_enabled():
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.DISABLED,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    "JARVIS_M10_ARCH_PROPOSER_ENABLED is false"
                ),
            )

        # Kind sanity — DISABLED short-circuits structurally
        try:
            if record_kind is ProposalKind.DISABLED:
                return SynthesizedProposal(
                    proposal_id=proposal_id,
                    kind=record_kind,
                    verdict=SynthesisVerdict.SKIPPED_KIND,
                    elapsed_s=(
                        time.monotonic() - started
                    ),
                    diagnostic=(
                        "ProposalKind.DISABLED — synthesizer "
                        "skipped"
                    ),
                )
        except Exception:  # noqa: BLE001 — defensive
            pass

        # Evidence sanity — refuse to synthesize on empty
        # detection bundle (Decision SP-D1 prerequisite).
        evidence = getattr(record, "detection_evidence", ()) or ()
        if not evidence:
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.INSUFFICIENT_CONTEXT,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    "detection_evidence is empty — refuse to "
                    "synthesize without context"
                ),
            )

        if provider is None:
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.PROVIDER_ERROR,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    "no provider injected — caller wired "
                    "synthesizer with provider=None"
                ),
            )

        k = m10_synthesis_quorum_k()
        majority = min(
            k, max(1, m10_synthesis_majority_threshold()),
        )
        timeout = m10_synthesis_per_call_timeout_s()
        max_evidence = m10_synthesis_max_evidence_chars()

        prompt = _build_synthesis_prompt(
            record, max_evidence_chars=max_evidence,
        )

        # Fan out K parallel calls
        async def _one_call(idx: int) -> SynthesisCandidate:
            try:
                cand = await asyncio.wait_for(
                    provider.synthesize_one(
                        prompt=prompt,
                        kind=record_kind,
                        proposal_id=(
                            f"{proposal_id}-cand-{idx}"
                        ),
                    ),
                    timeout=timeout,
                )
                if not isinstance(cand, SynthesisCandidate):
                    return SynthesisCandidate(
                        code_text="",
                        error=(
                            "provider returned non-Synthesis"
                            "Candidate"
                        ),
                    )
                return cand
            except asyncio.TimeoutError:
                return SynthesisCandidate(
                    code_text="",
                    error=f"timeout after {timeout}s",
                )
            except Exception as exc:  # noqa: BLE001
                return SynthesisCandidate(
                    code_text="",
                    error=(
                        f"provider raised: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

        try:
            candidates: Sequence[SynthesisCandidate] = (
                await asyncio.gather(
                    *(_one_call(i) for i in range(k)),
                    return_exceptions=False,
                )
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.PROVIDER_ERROR,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    f"asyncio.gather raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        # Aggregate cost
        total_cost: float = 0.0
        for c in candidates:
            try:
                total_cost += float(c.cost_usd or 0.0)
            except (TypeError, ValueError):
                continue

        # Compute signatures via Move 6's ast_canonical primitive
        try:
            from backend.core.ouroboros.governance.verification.ast_canonical import (  # noqa: E501
                compute_ast_signature,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.PROVIDER_ERROR,
                cost_usd=total_cost,
                candidate_count=len(candidates),
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    f"ast_canonical import failed: "
                    f"{type(exc).__name__}"
                ),
            )

        signatures: Tuple[str, ...] = tuple(
            compute_ast_signature(c.code_text or "")
            for c in candidates
        )

        # Detect provider-error case: every candidate failed OR
        # all signatures are empty
        non_empty = [s for s in signatures if s]
        if not non_empty:
            errors = [
                c.error or "empty_output"
                for c in candidates
            ]
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.PROVIDER_ERROR,
                candidate_count=len(candidates),
                candidate_signatures=signatures,
                cost_usd=total_cost,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    f"all {len(candidates)} candidates failed: "
                    f"{errors[:3]}"
                ),
            )

        # Tally signatures + find consensus
        tally: Dict[str, int] = {}
        for sig in signatures:
            if sig:
                tally[sig] = tally.get(sig, 0) + 1
        # Sorted descending — most-frequent first
        ranked = sorted(
            tally.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
        top_sig, top_count = ranked[0]

        if top_count < majority:
            # No consensus reached
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.QUORUM_DISAGREEMENT,
                candidate_count=len(candidates),
                candidate_signatures=signatures,
                cost_usd=total_cost,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    f"top signature count {top_count} < "
                    f"majority {majority} (K={k}). Tally: "
                    f"{ranked[:3]}"
                ),
            )

        # Pick the consensus candidate (first one matching
        # the top signature)
        consensus_idx: Optional[int] = None
        for i, sig in enumerate(signatures):
            if sig == top_sig:
                consensus_idx = i
                break
        if consensus_idx is None:
            # Defensive — shouldn't reach (top_count > 0 above)
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.QUORUM_DISAGREEMENT,
                candidate_count=len(candidates),
                candidate_signatures=signatures,
                cost_usd=total_cost,
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    "consensus signature not found in "
                    "candidate list (defensive)"
                ),
            )

        consensus = candidates[consensus_idx]

        # Decision SP-D1: MANDATORY AST self-pin
        ast_pin = (consensus.ast_pin_name or "").strip()
        if not ast_pin:
            return SynthesizedProposal(
                proposal_id=proposal_id,
                kind=record_kind,
                verdict=SynthesisVerdict.NO_SELF_PIN,
                candidate_count=len(candidates),
                candidate_signatures=signatures,
                consensus_signature=top_sig,
                cost_usd=total_cost,
                code_text=consensus.code_text or "",
                class_name=consensus.class_name or "",
                module_path=consensus.module_path or "",
                elapsed_s=time.monotonic() - started,
                diagnostic=(
                    "Quorum consensus reached but consensus "
                    "candidate omitted mandatory ast_pin_name "
                    "— rejected per §32.4.4"
                ),
            )

        return SynthesizedProposal(
            proposal_id=proposal_id,
            kind=record_kind,
            verdict=SynthesisVerdict.SYNTHESIZED,
            code_text=consensus.code_text or "",
            class_name=consensus.class_name or "",
            module_path=consensus.module_path or "",
            ast_pin_name=ast_pin,
            consensus_signature=top_sig,
            candidate_count=len(candidates),
            candidate_signatures=signatures,
            cost_usd=total_cost,
            forced_risk_tier=M10_FORCED_RISK_TIER,
            elapsed_s=time.monotonic() - started,
            diagnostic=(
                f"consensus={top_count}/{k} K-majority"
            ),
        )


# ---------------------------------------------------------------------------
# Process-singleton
# ---------------------------------------------------------------------------


_DEFAULT_SYNTHESIZER: Optional[ProposalSynthesizer] = None


def get_default_synthesizer() -> ProposalSynthesizer:
    """Lazy-constructed process singleton. NEVER raises."""
    global _DEFAULT_SYNTHESIZER  # noqa: PLW0603
    if _DEFAULT_SYNTHESIZER is None:
        _DEFAULT_SYNTHESIZER = ProposalSynthesizer()
    return _DEFAULT_SYNTHESIZER


def reset_default_synthesizer_for_tests() -> None:
    """Test-only — drop the default. Production NEVER calls."""
    global _DEFAULT_SYNTHESIZER  # noqa: PLW0603
    _DEFAULT_SYNTHESIZER = None


__all__ = [
    "M10_FORCED_RISK_TIER",
    "M10_SYNTHESIZER_SCHEMA_VERSION",
    "ProposalSynthesizer",
    "SynthesisCandidate",
    "SynthesisProviderProtocol",
    "SynthesisVerdict",
    "SynthesizedProposal",
    "get_default_synthesizer",
    "m10_synthesis_majority_threshold",
    "m10_synthesis_max_evidence_chars",
    "m10_synthesis_per_call_timeout_s",
    "m10_synthesis_quorum_k",
    "reset_default_synthesizer_for_tests",
]
