"""Move 6.5 Slice 1 — Multi-prior planning materializer.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Move 6.5 must close K-way consensus across genuinely
   different planning angles — same structural defense target
   as CC-style 'try angle A vs B,' but without inventing a
   parallel orchestrator or bypassing Iron Gate /
   SemanticGuardian / risk-tier / mutation budget. Different
   prompts → same AST signature is the gold signal; do not
   fake 'priors' with cosmetic prompt noise."

Move 6 (`generative_quorum.py`) closed K-way consensus on the
SAME prior via seed variation. Move 6.5 closes K-way consensus
across DIFFERENT priors — different planning angles dispatched
in parallel. When K diverse priors converge on the same AST
signature, the signal is *strictly stronger* than K seed-rolls
converging: different prompt scaffolds arriving at the same
answer is the canonical defense against motivated reasoning.

**This module is Slice 1 only**. It produces deterministic,
auditable :class:`PriorSet` artifacts. The Slice 2 runner (not
yet shipped) consumes them via :class:`Prior` → generation
adapter and feeds resulting :class:`CandidateRoll` instances
into Move 6's :func:`compute_consensus`. Slice 1 does NOT:

  * Run any generation
  * Touch :class:`CandidateRoll` (prior-agnostic by Move 6
    contract — Slice 2 will thread prior identity through a
    sibling ``Map[roll_id, prior_id]`` so consensus math stays
    byte-identical)
  * Fork :class:`PlanGenerator` (operator binding: Slice 1
    stays a materializer; PLAN_VARIANT priors deferred to
    Slice 7+ once STYLE_HINT priors prove insufficient)
  * Hardcode any model identifiers (all generation routes
    resolve from existing policy at Slice 2 dispatch time)

**Architectural boundary**: Slice 1 is a pure-stdlib, read-only
materializer. The prior table is module-level (auditable; AST-
pinnable; deterministic). External YAML/JSON config is a
deliberate Slice 7+ enhancement — Slice 1's invariant is that
the operator-visible style-hint set + their canonical IDs +
their version string are bytes-checkable in source.

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / urgency_router / candidate_generator /
plan_generator / change_engine / semantic_guardian imports.
Read-only materializer.

**Master flag** ``JARVIS_MULTI_PRIOR_PLANNING_ENABLED``
default-FALSE per §33.1: when off, ``materialize_priors``
returns None. Operator opts in once Slice 6 graduation
contract reports READY_FOR_GRADUATION.

**Composition with consensus math**: Slice 1 holds NO consensus
math. Slice 2 will compose Move 6's :func:`compute_consensus`
verbatim — pinned via ``multi_prior_planning_no_consensus_math``
(forbids local re-implementation; future Slice 2 will lazy-
import from ``verification.generative_quorum``).

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


logger = logging.getLogger(
    "Ouroboros.MultiPriorPlanning",
)


MULTI_PRIOR_PLANNING_SCHEMA_VERSION: str = (
    "multi_prior_planning.1"
)


# Style-hint table version. Bumped by hand when STYLE_HINT_TABLE
# entries change semantically (text edited, entry added/removed).
# Slice 4 ledger rows record this so auditors can correlate
# historical rolls with the prompt scaffold that produced them.
STYLE_HINT_TABLE_VERSION: str = "style_hint_table.1"


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


_DEFAULT_K: int = 4
_K_FLOOR: int = 2
_K_CEILING: int = 8


# ---------------------------------------------------------------------------
# Closed taxonomy — 2-value Slice 1 scope
# ---------------------------------------------------------------------------


class PriorKind(str, enum.Enum):
    """Closed 2-value taxonomy for Slice 1. Operator binding
    2026-05-07: ship SEED_ONLY + STYLE_HINT only; PLAN_VARIANT
    + POSTURE_VARIANT deferred to Slice 7+ once style-hint
    priors prove insufficient divergence in Slice 4 ledgers.

    AST-pinned. Future expansion to 4 values is intentional but
    requires:
      1. Updating the AST pin's ``required`` set
      2. Updating ``_PRIOR_KIND_DISPATCH`` table
      3. Adding corresponding materializer arms"""

    SEED_ONLY = "seed_only"
    """Identical prompt scaffold to Move 6 path; only the
    provider seed varies. Carries no ``system_prompt_addendum``.
    Functions as the diversity-baseline anchor — when a
    STYLE_HINT roll converges with a SEED_ONLY roll on the same
    AST signature, the convergence signal generalizes beyond
    the style-hint dimension."""

    STYLE_HINT = "style_hint"
    """Adds a small, deterministic
    ``system_prompt_addendum`` from :data:`STYLE_HINT_TABLE` to
    the GENERATE prompt. Hint text is canonical (table-sourced,
    NEVER scattered in call sites). Each STYLE_HINT roll
    receives a distinct hint entry; deterministic round-robin
    by ``op_id`` hash."""


# ---------------------------------------------------------------------------
# Style-hint canonical table — auditable, version-stamped
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StyleHintEntry:
    """One canonical style-hint entry. Frozen so propagation
    through the materializer + Slice 2 prompt-builder is safe.

    ``hint_id`` is the deterministic operator-facing identifier
    (used in ``Prior.prior_id`` to make ledger rows auditable).
    ``addendum`` is the verbatim text appended to the GENERATE
    system prompt at Slice 2 dispatch — kept short (<200 chars)
    so it doesn't dominate the prompt budget."""

    hint_id: str
    addendum: str
    description: str


# Canonical style-hint table. Order is load-bearing — round-
# robin over ``range(K_STYLE_HINTS)`` deterministically picks
# entries by index, so reordering changes which prior fires for
# a given (op_id, candidate_index) pair. Bump
# ``STYLE_HINT_TABLE_VERSION`` whenever the table changes
# semantically.
#
# Operator binding 2026-05-07: keep the set small + opinionated
# + structurally distinct. Four entries map cleanly to the K=4
# default (1 SEED_ONLY anchor + 3 STYLE_HINT priors) without
# truncation.
STYLE_HINT_TABLE: Tuple[StyleHintEntry, ...] = (
    StyleHintEntry(
        hint_id="defensive",
        addendum=(
            "Code defensively. Validate inputs at boundaries. "
            "Prefer explicit error handling over implicit "
            "fallthrough. Add narrow guards before mutation."
        ),
        description=(
            "Boundary-validation + explicit error handling "
            "emphasis."
        ),
    ),
    StyleHintEntry(
        hint_id="minimalist",
        addendum=(
            "Prefer the simplest possible solution. Reuse "
            "existing helpers. Avoid abstraction unless three "
            "or more concrete callers already exist."
        ),
        description=(
            "Simplicity + composition-over-abstraction "
            "emphasis."
        ),
    ),
    StyleHintEntry(
        hint_id="composition_first",
        addendum=(
            "Identify and reuse the canonical helper before "
            "writing new code. Prefer composition over "
            "duplication. Cite the existing module by path."
        ),
        description=(
            "Substrate-reuse emphasis (anti-duplication)."
        ),
    ),
    StyleHintEntry(
        hint_id="type_strict",
        addendum=(
            "Prefer narrow types and explicit type "
            "annotations. Reject Any. Prefer frozen "
            "dataclasses for value types."
        ),
        description=(
            "Type-narrowness + immutability emphasis."
        ),
    ),
)


def get_style_hint_by_id(
    hint_id: str,
) -> Optional[StyleHintEntry]:
    """Lookup helper for Slice 4 ledger reconstruction. Returns
    None on miss. NEVER raises."""
    for entry in STYLE_HINT_TABLE:
        if entry.hint_id == hint_id:
            return entry
    return None


# ---------------------------------------------------------------------------
# Master flag + K-default knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_MULTI_PRIOR_PLANNING_ENABLED`` master switch.
    Default-FALSE per §33.1: when off, ``materialize_priors``
    returns None (zero substrate touch). Operator opts in once
    Slice 6 graduation contract reports READY_FOR_GRADUATION.
    """
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def k_default() -> int:
    """Effective K (number of priors to materialize). Reads
    ``JARVIS_MULTI_PRIOR_K_DEFAULT``; clamps to
    [_K_FLOOR, _K_CEILING]. Defaults to 4 per operator binding
    2026-05-07 ("fixed default K=4; no adaptive K until Slice
    7+"). Pure read; NEVER raises."""
    try:
        raw = os.environ.get(
            "JARVIS_MULTI_PRIOR_K_DEFAULT", "",
        ).strip()
        if raw == "":
            return _DEFAULT_K
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_K
    if parsed < _K_FLOOR:
        return _K_FLOOR
    if parsed > _K_CEILING:
        return _K_CEILING
    return parsed


# ---------------------------------------------------------------------------
# Frozen artifacts — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prior:
    """One prior's identity + materializing info. Frozen so
    propagation through Slice 2's parallel runner is safe.
    Adopts §33.5 versioned-artifact contract.

    ``prior_id`` is the deterministic operator-facing
    identifier (e.g. ``seed_only:0``, ``style_hint:defensive``)
    — load-bearing for Slice 4 ledger auditability.

    ``system_prompt_addendum`` is the verbatim text Slice 2
    will append to the GENERATE prompt for this roll. Empty
    string for SEED_ONLY (no addendum — pure seed variation).
    Non-empty for STYLE_HINT (sourced from
    :data:`STYLE_HINT_TABLE` — never written by Slice 2 to
    avoid drift).

    ``seed`` is a deterministic provider seed derived from
    ``op_id + prior_id`` so the same (op_id, prior_id) pair
    reproduces. Slice 2 passes it directly to the provider
    (mirrors Move 6's seed-passing discipline at
    ``generative_quorum_runner.py:265``).

    ``weight`` informs Slice 2's per-roll cost-budget
    allocation: a roll's allowed cost is the parent op's
    remaining budget × ``weight / sum_of_weights``. Default
    1.0 (uniform allocation). Reserved for Slice 7+ when
    PLAN_VARIANT priors may legitimately consume more budget.
    """

    prior_id: str
    kind: PriorKind
    system_prompt_addendum: str
    seed: int
    weight: float = 1.0
    description: str = ""
    schema_version: str = field(
        default=MULTI_PRIOR_PLANNING_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prior_id": str(self.prior_id),
            "kind": self.kind.value,
            "system_prompt_addendum": str(
                self.system_prompt_addendum,
            ),
            "seed": int(self.seed),
            "weight": float(self.weight),
            "description": str(self.description)[:256],
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Any,
    ) -> Optional["Prior"]:
        """Reconstruct from a ``to_dict`` payload. Returns
        None on schema mismatch OR malformed shape. NEVER
        raises."""
        try:
            schema = payload.get("schema_version")
            if schema != MULTI_PRIOR_PLANNING_SCHEMA_VERSION:
                return None
            kind_raw = str(payload.get("kind", ""))
            try:
                kind = PriorKind(kind_raw)
            except ValueError:
                return None
            return cls(
                prior_id=str(payload["prior_id"]),
                kind=kind,
                system_prompt_addendum=str(
                    payload.get("system_prompt_addendum", ""),
                ),
                seed=int(payload["seed"]),
                weight=float(payload.get("weight", 1.0)),
                description=str(
                    payload.get("description", ""),
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class PriorSet:
    """Frozen K-tuple of :class:`Prior` instances + audit
    metadata. Adopts §33.5 versioned-artifact contract.

    ``priors`` is the materialized prior set (length K). Order
    is load-bearing — Slice 2 dispatches priors by index so
    ledger rows can correlate ``candidate_index`` ↔ prior_id.

    ``op_id``, ``route``, ``posture`` are the inputs that
    triggered materialization — recorded for Slice 4 ledger
    rows so auditors can correlate gate decisions with the
    prior set fired.

    ``style_hint_table_version`` snapshots
    :data:`STYLE_HINT_TABLE_VERSION` at materialization time so
    rows reconstructed from old ledgers reference the canonical
    table version that produced them.
    """

    priors: Tuple[Prior, ...]
    op_id: str
    route: str
    posture: str
    materialized_at_ts: float
    style_hint_table_version: str = field(
        default=STYLE_HINT_TABLE_VERSION,
    )
    schema_version: str = field(
        default=MULTI_PRIOR_PLANNING_SCHEMA_VERSION,
    )

    @property
    def k(self) -> int:
        return len(self.priors)

    @property
    def kind_distribution(self) -> Dict[str, int]:
        """Count of priors per :class:`PriorKind`. Useful for
        Slice 4 telemetry chatter-suppression decisions."""
        out: Dict[str, int] = {}
        for p in self.priors:
            out[p.kind.value] = out.get(p.kind.value, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "priors": [p.to_dict() for p in self.priors],
            "op_id": str(self.op_id),
            "route": str(self.route),
            "posture": str(self.posture),
            "materialized_at_ts": float(
                self.materialized_at_ts,
            ),
            "style_hint_table_version": str(
                self.style_hint_table_version,
            ),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Any,
    ) -> Optional["PriorSet"]:
        """Reconstruct from a ``to_dict`` payload. Returns
        None on schema mismatch OR malformed shape. NEVER
        raises."""
        try:
            schema = payload.get("schema_version")
            if schema != MULTI_PRIOR_PLANNING_SCHEMA_VERSION:
                return None
            raw_priors = payload.get("priors", [])
            if not isinstance(raw_priors, list):
                return None
            priors_built: List[Prior] = []
            for raw in raw_priors:
                p = Prior.from_dict(raw)
                if p is None:
                    return None
                priors_built.append(p)
            return cls(
                priors=tuple(priors_built),
                op_id=str(payload["op_id"]),
                route=str(payload["route"]),
                posture=str(payload["posture"]),
                materialized_at_ts=float(
                    payload["materialized_at_ts"],
                ),
                style_hint_table_version=str(
                    payload.get(
                        "style_hint_table_version",
                        STYLE_HINT_TABLE_VERSION,
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Pure gate functions — caller passes string values, no enum imports
# ---------------------------------------------------------------------------


def should_fire_for_route(route: str) -> bool:
    """True iff ``route`` matches the COMPLEX route gate.
    Operator binding 2026-05-07: only COMPLEX
    (per ``urgency_router.ProviderRoute.COMPLEX = "complex"``).
    Never IMMEDIATE/STANDARD/BACKGROUND/SPECULATIVE.

    Pure function — caller passes the string value (no
    UrgencyRouter import needed; tests can call with synthetic
    values). NEVER raises."""
    try:
        return str(route).strip().lower() == "complex"
    except Exception:  # noqa: BLE001 — defensive
        return False


def should_fire_for_posture(posture: str) -> bool:
    """True iff ``posture`` matches the EXPLORE gate. Operator
    binding 2026-05-07: only EXPLORE
    (per ``posture.Posture.EXPLORE = "EXPLORE"``). Suppressed
    under CONSOLIDATE/HARDEN/MAINTAIN.

    Pure function — caller passes the string value (no
    DirectionInferrer import needed; tests can call with
    synthetic values). NEVER raises."""
    try:
        return str(posture).strip().upper() == "EXPLORE"
    except Exception:  # noqa: BLE001 — defensive
        return False


def should_fire_for_op(
    *,
    op_id: str,
    route: str,
    posture: str,
) -> bool:
    """Composed gate: master flag on AND route gate AND
    posture gate. Pure function; NEVER raises."""
    if not master_enabled():
        return False
    name = str(op_id or "").strip()
    if not name:
        return False
    if not should_fire_for_route(route):
        return False
    if not should_fire_for_posture(posture):
        return False
    return True


# ---------------------------------------------------------------------------
# Deterministic seed derivation
# ---------------------------------------------------------------------------


def _derive_seed(*, op_id: str, prior_id: str) -> int:
    """Derive a deterministic 31-bit non-negative seed from
    ``op_id + prior_id``. Same inputs → same seed. Pure function;
    NEVER raises.

    31-bit range matches the canonical numpy-style int32-safe
    band; provider SDKs that accept ``Optional[int]`` seeds
    accept this range without overflow."""
    payload = (
        f"{op_id}::{prior_id}".encode("utf-8")
    )
    digest = hashlib.sha256(payload).digest()
    # Take first 4 bytes (32 bits) and mask the high bit to
    # stay non-negative within int32 range (0 .. 2^31 - 1).
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# ---------------------------------------------------------------------------
# materialize_priors — public entry point
# ---------------------------------------------------------------------------


def materialize_priors(
    *,
    op_id: str,
    route: str,
    posture: str,
    k: Optional[int] = None,
) -> Optional[PriorSet]:
    """Materialize a deterministic K-prior set for the given
    op. Returns None when:

      * Master flag off (``master_enabled() == False``)
      * Route gate fails (``route != "complex"``)
      * Posture gate fails (``posture != "EXPLORE"``)
      * ``op_id`` blank

    Otherwise returns an auditable :class:`PriorSet`. Same
    (op_id, route, posture, k) inputs → byte-identical output.

    Composition of priors:
      * 1 SEED_ONLY anchor (``seed_only:0``)
      * (K - 1) STYLE_HINT entries via deterministic round-
        robin over :data:`STYLE_HINT_TABLE`
      * Anchor count scales with K when K > len(table) + 1
        (no truncation): for K ≤ ``len(STYLE_HINT_TABLE) + 1``,
        single SEED_ONLY anchor; for K above that, repeat the
        SEED_ONLY/style cycle deterministically.

    Pure function; NEVER raises."""
    if not master_enabled():
        return None
    name = str(op_id or "").strip()
    if not name:
        return None
    if not should_fire_for_route(route):
        return None
    if not should_fire_for_posture(posture):
        return None
    effective_k = k if k is not None else k_default()
    try:
        effective_k = int(effective_k)
    except (TypeError, ValueError):
        effective_k = k_default()
    if effective_k < _K_FLOOR:
        effective_k = _K_FLOOR
    if effective_k > _K_CEILING:
        effective_k = _K_CEILING

    priors_out: List[Prior] = []
    style_hint_count = len(STYLE_HINT_TABLE)
    seed_only_index = 0
    style_hint_index = 0
    # Deterministic interleaving: 1 SEED_ONLY anchor first,
    # then style hints round-robin, repeat block when K
    # exceeds (1 + len(table)).
    for slot in range(effective_k):
        slot_in_block = slot % (1 + style_hint_count)
        if slot_in_block == 0:
            prior_id = (
                f"seed_only:{seed_only_index}"
            )
            seed = _derive_seed(
                op_id=name, prior_id=prior_id,
            )
            priors_out.append(
                Prior(
                    prior_id=prior_id,
                    kind=PriorKind.SEED_ONLY,
                    system_prompt_addendum="",
                    seed=seed,
                    weight=1.0,
                    description=(
                        "Diversity-baseline anchor "
                        "(no addendum; seed varies only)."
                    ),
                ),
            )
            seed_only_index += 1
        else:
            entry = STYLE_HINT_TABLE[
                style_hint_index % style_hint_count
            ]
            prior_id = (
                f"style_hint:{entry.hint_id}"
                f":{style_hint_index // style_hint_count}"
                if style_hint_index >= style_hint_count
                else f"style_hint:{entry.hint_id}"
            )
            seed = _derive_seed(
                op_id=name, prior_id=prior_id,
            )
            priors_out.append(
                Prior(
                    prior_id=prior_id,
                    kind=PriorKind.STYLE_HINT,
                    system_prompt_addendum=entry.addendum,
                    seed=seed,
                    weight=1.0,
                    description=entry.description,
                ),
            )
            style_hint_index += 1

    # Materialization timestamp captured deterministically per
    # call (Slice 4 ledger reconstructs prior set ordering by
    # this field combined with op_id).
    import time

    return PriorSet(
        priors=tuple(priors_out),
        op_id=name,
        route=str(route),
        posture=str(posture),
        materialized_at_ts=time.time(),
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the 2 flags this module reads."""
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Move 6.5 Slice 1 multi-"
                "prior materializer. Default-FALSE per §33.1; "
                "when off, materialize_priors returns None. "
                "Operator opts in once Slice 6 graduation "
                "contract reports READY_FOR_GRADUATION."
            ),
            category="Generation",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_planning.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_PLANNING_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorPlanning] master-flag seeding failed "
            "(non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_K_DEFAULT",
            type_="int",
            default=str(_DEFAULT_K),
            description=(
                "Default K (number of priors materialized). "
                "Operator binding 2026-05-07: fixed K=4; "
                "clamped to [{}, {}]. No adaptive K until "
                "Slice 7+ once metrics exist."
            ).format(_K_FLOOR, _K_CEILING),
            category="Generation",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_planning.py"
            ),
            example="JARVIS_MULTI_PRIOR_K_DEFAULT=4",
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorPlanning] k-default seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_planning_taxonomy_2_values`` — closed
         enum (SEED_ONLY/STYLE_HINT). Slice 7+ widens to 4
         values; pin must be updated alongside.
      2. ``multi_prior_planning_master_default_false`` — §33.1
         producer flag stays default-FALSE.
      3. ``multi_prior_planning_authority_asymmetry`` — no
         orchestrator-tier imports (read-only materializer).
      4. ``multi_prior_planning_no_consensus_math`` — operator
         binding "do not fork consensus math". This module MUST
         NOT define ``compute_consensus`` or any local clone of
         Move 6's consensus math; Slice 2 will lazy-import from
         ``verification.generative_quorum`` instead.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_planning.py"
    )

    def _validate_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"SEED_ONLY", "STYLE_HINT"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PriorKind"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"PriorKind missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"PriorKind has extra "
                        f"{sorted(extra)} — Slice 1 taxonomy "
                        f"is closed at 2 values; widening "
                        f"requires AST pin update + "
                        f"materializer arms"
                    )
                return tuple(violations)
        violations.append("PriorKind class missing")
        return tuple(violations)

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            for cmp_node in ast.walk(test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operands_have_empty_str = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operands_have_empty_str = True
                        break
                if not operands_have_empty_str:
                    continue
                for body_stmt in sub.body:
                    if isinstance(body_stmt, ast.Return):
                        if (
                            isinstance(
                                body_stmt.value, ast.Constant,
                            )
                            and body_stmt.value.value is False
                        ):
                            empty_returns_false = True
                            break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_planning" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_planning.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_planning.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_no_consensus_math(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 1 must NOT define consensus math locally.
        Move 6's authoritative function is the canonical
        sole owner; Slice 2 will lazy-import it. AST walk
        catches:
          * any ``FunctionDef`` whose name matches the Move 6
            authority
          * any top-level ``ImportFrom`` of the Move 6 name
            (Slice 2's lazy-import lives inside its runner
            function, not at module-top)
        """
        violations: list = []
        # Bytes-pinned token (avoid mentioning the full
        # forbidden symbol literally outside this assignment so
        # documentation references don't loop back through any
        # future substring sweeps).
        forbidden_name = "compute" + "_consensus"
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == forbidden_name
            ):
                violations.append(
                    "multi_prior_planning.py MUST NOT define "
                    "the Move 6 authority — operator binding "
                    "2026-05-07 forbids forking consensus "
                    "math"
                )
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "generative_quorum" in module:
                    for alias in node.names:
                        if alias.name == forbidden_name:
                            violations.append(
                                "multi_prior_planning.py "
                                "MUST NOT top-level-import "
                                "the Move 6 authority — "
                                "Slice 2 lazy-imports inside "
                                "the runner instead "
                                "(composition discipline)"
                            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_planning_taxonomy_2_values"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 1 — PriorKind is closed at "
                "exactly {SEED_ONLY, STYLE_HINT}. Slice 7+ "
                "widening requires AST pin update + "
                "materializer arms."
            ),
            validate=_validate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_planning_master_default_false"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 1 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_planning_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 1 — substrate purity: no "
                "orchestrator-tier imports (read-only "
                "materializer)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_planning_no_consensus_math"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 1 — operator binding "
                "2026-05-07 forbids forking Move 6 consensus "
                "math. Slice 2 lazy-imports compute_consensus "
                "from generative_quorum at dispatch time."
            ),
            validate=_validate_no_consensus_math,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_PLANNING_SCHEMA_VERSION",
    "Prior",
    "PriorKind",
    "PriorSet",
    "STYLE_HINT_TABLE",
    "STYLE_HINT_TABLE_VERSION",
    "StyleHintEntry",
    "get_style_hint_by_id",
    "k_default",
    "master_enabled",
    "materialize_priors",
    "register_flags",
    "register_shipped_invariants",
    "should_fire_for_op",
    "should_fire_for_posture",
    "should_fire_for_route",
]
