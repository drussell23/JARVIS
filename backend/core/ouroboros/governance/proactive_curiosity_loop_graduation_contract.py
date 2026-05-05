"""Move 8 Slice 3 — Proactive Curiosity Loop graduation contract.

Per §33.1 Graduation Contract Pattern. Mirrors
:mod:`phase10_graduation_contract` and
:mod:`cross_op_semantic_budget_graduation_contract` —
canonical shape: pure-function predicate ``is_ready_for_graduation``
+ ``*Verdict`` closed-enum + frozen ``*Report`` + master-flag
helper + ``register_shipped_invariants``.

Gates the Slice 1 master flag (``JARVIS_PROACTIVE_CURIOSITY_
READER_ENABLED``) flip from default-FALSE to default-TRUE on
empirical proof that the loop respects SensorGovernor caps.

Three gates evaluated first-match-wins:

  * **Gate 1: master flag still default-FALSE on the substrate**
    — if Slice 1's flag has already flipped, the contract has
    already done its job; report DISABLED so a callable contract
    doesn't re-trigger evaluation. This is the Move 7 +
    Phase 10 convention.
  * **Gate 2: sufficient emissions observed** — caller-injected
    ``observed_surfaced_emissions`` ≥ ``required_emissions``
    (default 12 — three across each of the four postures, plus
    headroom). Insufficient → report INSUFFICIENT_EMISSIONS.
  * **Gate 3: zero SensorGovernor cap-hits observed** — caller-
    injected ``observed_governor_throttles`` ≤
    ``max_governor_throttles`` (default 0 — the contract is "the
    loop integrates cleanly"; even one cap-hit signals the
    upstream cap discipline is being violated). Excess → report
    EXCESSIVE_THROTTLES.

  * **Pass-through**: all gates clear → READY_FOR_GRADUATION.

Evidence injection (operator-paced wiring):

The empirical numbers come from existing in-tree substrates —
no parallel ledger:

  * ``observed_surfaced_emissions`` from
    :func:`firing_telemetry.read_counter`
    (``"curiosity_driven_envelope_emit"`` — incremented by
    Slice 2's emit path).
  * ``observed_governor_throttles`` from
    :func:`sensor_governor.read_throttle_count` for the
    ``proactive_exploration`` sensor.

Slice 3 substrate is pure-function-testable (caller-injects
both numbers); the wiring lands when the operator runs the
graduation cadence.

Architectural locks (§33.1):

  * Authority asymmetry — the contract is queryable; the
    operator-binding default-FALSE pin lives on Slice 1's master
    flag (the thing being gated). Slice 3's own master flag
    ``JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_ENABLED``
    defaults TRUE (the contract harness is queryable; the
    operator-binding gate is on Slice 1).
  * Pure function — NO I/O on the read path; caller injects
    evidence; NEVER raises.
  * §33.5 versioned report — schema_version + symmetric
    ``to_dict()``.
  * Composes Slice 1 — reads
    :func:`proactive_curiosity_reader.proactive_curiosity_reader_enabled`
    for Gate 1 (the master flag we're gating). Authority-
    asymmetric: substrate-only imports.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_SCHEMA_VERSION: str = (
    "proactive_curiosity_loop_graduation_contract.1"
)


# ---------------------------------------------------------------------------
# Master flag (graduation harness — default-TRUE per §33.1)
# ---------------------------------------------------------------------------


def proactive_curiosity_graduation_contract_enabled() -> bool:
    """``JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_ENABLED``
    — graduation harness master flag. **Default TRUE** per
    §33.1 (the contract is queryable; the operator-binding
    default-FALSE pin lives on Slice 1's master flag).
    Production should leave this on; intended for operator
    troubleshooting only."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_ENABLED",
        "",
    ).strip().lower()
    if raw == "":
        return True  # graduation harness is queryable by default
    return raw in ("1", "true", "yes", "on")


def required_emissions() -> int:
    """``JARVIS_PROACTIVE_CURIOSITY_REQUIRED_EMISSIONS`` —
    minimum surfaced emissions before READY_FOR_GRADUATION.
    Default 12 (3× across each of 4 postures, plus headroom).
    Clamped [3, 1000]."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_REQUIRED_EMISSIONS", "",
    ).strip()
    try:
        n = int(raw) if raw else 12
        if n < 3:
            return 3
        if n > 1000:
            return 1000
        return n
    except (TypeError, ValueError):
        return 12


def max_governor_throttles() -> int:
    """``JARVIS_PROACTIVE_CURIOSITY_MAX_GOVERNOR_THROTTLES`` —
    maximum SensorGovernor cap-hits tolerable. Default 0
    (the contract is 'the loop integrates cleanly'). Clamped
    [0, 100]."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_MAX_GOVERNOR_THROTTLES", "",
    ).strip()
    try:
        n = int(raw) if raw else 0
        if n < 0:
            return 0
        if n > 100:
            return 100
        return n
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class CuriosityGraduationVerdict(str, enum.Enum):
    """Closed taxonomy: 5-value verdict ladder, AST-pinned."""

    READY_FOR_GRADUATION = "ready_for_graduation"
    """All gates cleared — operator may flip Slice 1's master
    flag default-TRUE."""

    INSUFFICIENT_EMISSIONS = "insufficient_emissions"
    """Gate 2 fail — fewer than :func:`required_emissions`
    surfaced emissions observed."""

    EXCESSIVE_THROTTLES = "excessive_throttles"
    """Gate 3 fail — SensorGovernor throttled the loop more
    than :func:`max_governor_throttles`."""

    ALREADY_GRADUATED = "already_graduated"
    """Gate 1 pass-through — Slice 1's master flag has already
    flipped to TRUE; the contract has done its job. NOT an
    error."""

    DISABLED = "disabled"
    """Graduation harness master flag is off; computation
    skipped."""


# ---------------------------------------------------------------------------
# Frozen report (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuriosityGraduationReport:
    """Frozen verdict envelope. ``to_dict()`` for SSE / REPL
    serialization; from_dict deferred until empirical wiring
    lands."""

    verdict: CuriosityGraduationVerdict

    observed_surfaced_emissions: int
    required_emissions: int

    observed_governor_throttles: int
    max_governor_throttles: int

    elapsed_s: float
    diagnostics: str
    schema_version: str = field(
        default=(
            PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_SCHEMA_VERSION
        ),
    )

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "observed_surfaced_emissions": int(
                self.observed_surfaced_emissions,
            ),
            "required_emissions": int(self.required_emissions),
            "observed_governor_throttles": int(
                self.observed_governor_throttles,
            ),
            "max_governor_throttles": int(
                self.max_governor_throttles,
            ),
            "elapsed_s": float(self.elapsed_s),
            "diagnostics": str(self.diagnostics),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Pure-function predicate
# ---------------------------------------------------------------------------


def is_ready_for_graduation(
    *,
    observed_surfaced_emissions: int,
    observed_governor_throttles: int,
    required_emissions_override: Optional[int] = None,
    max_governor_throttles_override: Optional[int] = None,
    enabled_override: Optional[bool] = None,
    slice1_already_flipped_override: Optional[bool] = None,
    now_unix: Optional[float] = None,
) -> CuriosityGraduationReport:
    """§33.1 graduation predicate. Pure-function — caller
    injects evidence (testing); NEVER raises.

    Three gates first-match-wins:

      Gate 1: contract harness flag on AND Slice 1's master
              flag still default-FALSE → continue
      Gate 2: ≥ required_emissions → continue
      Gate 3: ≤ max_governor_throttles → READY

    Any failed gate reports its specific verdict; pass-through
    reports READY_FOR_GRADUATION.
    """
    started = (
        float(now_unix) if now_unix is not None else time.time()
    )

    # Gate 0: harness master flag.
    is_enabled = (
        enabled_override
        if enabled_override is not None
        else proactive_curiosity_graduation_contract_enabled()
    )
    if not is_enabled:
        return _build_report(
            verdict=CuriosityGraduationVerdict.DISABLED,
            observed_surfaced_emissions=int(
                observed_surfaced_emissions,
            ),
            required_emissions_n=(
                int(required_emissions_override)
                if required_emissions_override is not None
                else required_emissions()
            ),
            observed_governor_throttles=int(
                observed_governor_throttles,
            ),
            max_governor_throttles_n=(
                int(max_governor_throttles_override)
                if max_governor_throttles_override is not None
                else max_governor_throttles()
            ),
            started=started,
            diagnostics=(
                "graduation harness master flag off "
                "(JARVIS_PROACTIVE_CURIOSITY_GRADUATION_"
                "CONTRACT_ENABLED=false)"
            ),
        )

    # Gate 1: Slice 1's master flag already flipped?
    if slice1_already_flipped_override is not None:
        slice1_flipped = bool(slice1_already_flipped_override)
    else:
        try:
            from backend.core.ouroboros.governance.proactive_curiosity_reader import (  # noqa: E501
                proactive_curiosity_reader_enabled,
            )
            slice1_flipped = proactive_curiosity_reader_enabled()
        except Exception:  # noqa: BLE001 -- defensive
            slice1_flipped = False

    req_n = (
        int(required_emissions_override)
        if required_emissions_override is not None
        else required_emissions()
    )
    max_n = (
        int(max_governor_throttles_override)
        if max_governor_throttles_override is not None
        else max_governor_throttles()
    )

    if slice1_flipped:
        return _build_report(
            verdict=CuriosityGraduationVerdict.ALREADY_GRADUATED,
            observed_surfaced_emissions=int(
                observed_surfaced_emissions,
            ),
            required_emissions_n=req_n,
            observed_governor_throttles=int(
                observed_governor_throttles,
            ),
            max_governor_throttles_n=max_n,
            started=started,
            diagnostics=(
                "Slice 1 master flag has already flipped — "
                "contract no-op (NOT an error)"
            ),
        )

    # Gate 2: sufficient emissions?
    if int(observed_surfaced_emissions) < req_n:
        return _build_report(
            verdict=(
                CuriosityGraduationVerdict.INSUFFICIENT_EMISSIONS
            ),
            observed_surfaced_emissions=int(
                observed_surfaced_emissions,
            ),
            required_emissions_n=req_n,
            observed_governor_throttles=int(
                observed_governor_throttles,
            ),
            max_governor_throttles_n=max_n,
            started=started,
            diagnostics=(
                f"only {int(observed_surfaced_emissions)} "
                f"surfaced emissions observed; need ≥ {req_n} "
                f"per JARVIS_PROACTIVE_CURIOSITY_REQUIRED_"
                f"EMISSIONS"
            ),
        )

    # Gate 3: zero (or low) governor throttles?
    if int(observed_governor_throttles) > max_n:
        return _build_report(
            verdict=CuriosityGraduationVerdict.EXCESSIVE_THROTTLES,
            observed_surfaced_emissions=int(
                observed_surfaced_emissions,
            ),
            required_emissions_n=req_n,
            observed_governor_throttles=int(
                observed_governor_throttles,
            ),
            max_governor_throttles_n=max_n,
            started=started,
            diagnostics=(
                f"observed {int(observed_governor_throttles)} "
                f"SensorGovernor throttle events; max allowed "
                f"is {max_n}. The loop is being capped — "
                f"reduce TOP_K or increase the cap before "
                f"graduating"
            ),
        )

    return _build_report(
        verdict=CuriosityGraduationVerdict.READY_FOR_GRADUATION,
        observed_surfaced_emissions=int(
            observed_surfaced_emissions,
        ),
        required_emissions_n=req_n,
        observed_governor_throttles=int(
            observed_governor_throttles,
        ),
        max_governor_throttles_n=max_n,
        started=started,
        diagnostics=(
            "all gates clear — operator may flip "
            "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED to true"
        ),
    )


def _build_report(
    *,
    verdict: CuriosityGraduationVerdict,
    observed_surfaced_emissions: int,
    required_emissions_n: int,
    observed_governor_throttles: int,
    max_governor_throttles_n: int,
    started: float,
    diagnostics: str,
) -> CuriosityGraduationReport:
    return CuriosityGraduationReport(
        verdict=verdict,
        observed_surfaced_emissions=(
            observed_surfaced_emissions
        ),
        required_emissions=required_emissions_n,
        observed_governor_throttles=(
            observed_governor_throttles
        ),
        max_governor_throttles=max_governor_throttles_n,
        elapsed_s=max(0.0, time.time() - started),
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# AST pins (§32.11 Slice 2 / shipped_code_invariants)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``proactive_curiosity_graduation_contract_authority_asymmetry``
         — substrate purity (no orchestrator / iron_gate / etc.)
      2. ``proactive_curiosity_graduation_contract_verdict_taxonomy_5_values``
         — closed-enum integrity.
      3. ``proactive_curiosity_graduation_contract_composes_substrate``
         — composes Slice 1's master flag via the canonical
         helper, never reads ``os.environ`` directly for that
         flag.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "proactive_curiosity_loop_graduation_contract.py"
    )

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
                            f"proactive_curiosity_loop_"
                            f"graduation_contract.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_verdict_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "READY_FOR_GRADUATION", "INSUFFICIENT_EMISSIONS",
            "EXCESSIVE_THROTTLES", "ALREADY_GRADUATED",
            "DISABLED",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "CuriosityGraduationVerdict":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    extra = seen - required
                    missing = required - seen
                    if extra:
                        violations.append(
                            f"CuriosityGraduationVerdict has "
                            f"extra values {sorted(extra)}"
                        )
                    if missing:
                        violations.append(
                            f"CuriosityGraduationVerdict missing "
                            f"required values {sorted(missing)}"
                        )
        return tuple(violations)

    def _validate_composes_slice1(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Contract MUST NOT read
        JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED directly via
        os.environ — it MUST compose
        proactive_curiosity_reader_enabled() to keep a single
        source of truth for the master flag.

        AST-precise: only fires on
        ``os.environ.get("JARVIS_PROACTIVE_CURIOSITY_READER_
        ENABLED", ...)`` style call patterns, not literal string
        mentions in docstrings or diagnostics."""
        violations: list = []
        flag_name = "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # os.environ.get("FLAG", ...) shape
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == flag_name
                ):
                    violations.append(
                        "graduation contract MUST NOT call "
                        "os.environ.get on "
                        "JARVIS_PROACTIVE_CURIOSITY_READER_"
                        "ENABLED — compose "
                        "proactive_curiosity_reader_enabled() "
                        "instead"
                    )
                # os.environ["FLAG"] indexing
                if (
                    isinstance(func, ast.Subscript)
                    and isinstance(func.slice, ast.Constant)
                    and func.slice.value == flag_name
                ):
                    violations.append(
                        "graduation contract MUST NOT index "
                        "os.environ on "
                        "JARVIS_PROACTIVE_CURIOSITY_READER_"
                        "ENABLED"
                    )
        # And the composing import MUST be present (proves
        # we didn't simply remove the gate).
        has_composes = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "proactive_curiosity_reader" in module:
                    for alias in node.names:
                        if (
                            alias.name
                            == "proactive_curiosity_reader_enabled"
                        ):
                            has_composes = True
        if not has_composes:
            violations.append(
                "graduation contract MUST import "
                "proactive_curiosity_reader_enabled to compose "
                "Slice 1's master flag (Gate 1 evidence)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_graduation_contract_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 3 — substrate purity: contract "
                "MUST NOT import orchestrator / iron_gate / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_graduation_contract_"
                "verdict_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 3 — CuriosityGraduationVerdict is "
                "a 5-value closed enum."
            ),
            validate=_validate_verdict_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_graduation_contract_"
                "composes_substrate"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 3 — composes Slice 1's master "
                "flag via the canonical helper; no parallel "
                "env read."
            ),
            validate=_validate_composes_slice1,
        ),
    ]


__all__ = [
    "CuriosityGraduationReport",
    "CuriosityGraduationVerdict",
    "PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_SCHEMA_VERSION",
    "is_ready_for_graduation",
    "max_governor_throttles",
    "proactive_curiosity_graduation_contract_enabled",
    "register_shipped_invariants",
    "required_emissions",
]
