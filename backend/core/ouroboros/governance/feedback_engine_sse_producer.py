"""FeedbackEngine SSE producer-bridge (§33.2 producer-bridge pattern).

The :class:`AutonomyFeedbackEngine` already runs in production: it
consumes curriculum signals (top_k task-failure aggregates) +
reactor events (model_promoted) and emits :class:`CommandEnvelope`
instances onto the autonomy CommandBus. Until now those signals
were observable only via DEBUG logs and the ``/why-changed`` REPL.
Operators watching the IDE event stream had no SSE projection —
the engine was a silent ledger.

This module is the producer-bridge: it converts FeedbackEngine
state transitions into ``feedback_engine_signal`` SSE frames so
both the IDE tree-view and downstream observability consumers
(VS Code extension, dashboards, audit replay) see the same
events the L1 router sees.

Three transition kinds (closed taxonomy, deterministic dispatch):

  * ``rollback_threshold_crossed`` — fired when a brain's rollback
    count first reaches the configured threshold (and again at
    each multiple thereafter, mirroring the engine's own
    chatter-suppression at
    :meth:`AutonomyFeedbackEngine._on_op_rolled_back`).

  * ``model_promoted`` — fired when the reactor batch processes a
    ``model_promoted`` event AND the resulting backlog command is
    accepted by the bus. One-shot per (model_id, source_event_file)
    — the reactor cursor prevents replay on subsequent batches.

  * ``curriculum_batch_emitted`` — fired when a curriculum file
    scan emits ``N >= 1`` backlog commands. Bounded-rate
    chatter-suppression: empty batches and partial-rejection
    batches with zero acceptances are silent (operator already
    sees an INFO log; no value in SSE noise).

Architectural locks (mirrors
:mod:`curiosity_producer_bridge` + :mod:`confidence_sse_producer`):

  * **Lazy import** — every producer site calls
    ``from .feedback_engine_sse_producer import feed_*`` inside a
    ``try/except`` block. ImportError or any runtime exception →
    silent no-op. Engine continues even if SSE plumbing breaks.

  * **Master-flag-gated at every entry point** — every public
    function calls :func:`producer_enabled` and returns False
    immediately when off. Default-false until Phase 9 cadence
    graduates the flag (3 clean soaks).

  * **NEVER raises** — exception-isolated per public function.
    Engine producers ignore the return value; the bridge's only
    contract is "I ran without breaking your caller."

  * **Authority asymmetry** (AST-pinned by
    :func:`register_shipped_invariants`) — bridge MUST NOT import
    orchestrator / iron_gate / providers / candidate_generator /
    urgency_router / sensor_governor / tool_executor /
    change_engine / strategic_direction. Bridge IS allowed to
    import the FeedbackEngine type (read-only) plus
    ``ide_observability_stream`` for SSE publication.

  * **No parallel state** — the bridge holds zero state of its
    own. Every emission decision is derived from the engine's
    own counters via the canonical
    :func:`autonomy.feedback_engine.get_default_engine` accessor.
    There is no rollback-count mirror here that could drift.

Master flag (Phase 9 cadence default-FALSE):
``JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED``. Asymmetric env
semantics: empty/whitespace = unset = default-false; explicit
truthy/falsy overrides at call time.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


FEEDBACK_ENGINE_SSE_PRODUCER_SCHEMA_VERSION: str = (
    "feedback_engine_sse_producer.1"
)


# ---------------------------------------------------------------------------
# Closed transition-kind taxonomy
# ---------------------------------------------------------------------------


# String constants (NOT an Enum — the SSE schema is string-typed and
# external consumers want stable strings, not enum members). The
# AST pin asserts this 3-element set is frozen.
TRANSITION_ROLLBACK_THRESHOLD_CROSSED: str = (
    "rollback_threshold_crossed"
)
TRANSITION_MODEL_PROMOTED: str = "model_promoted"
TRANSITION_CURRICULUM_BATCH_EMITTED: str = (
    "curriculum_batch_emitted"
)

_VALID_TRANSITION_KINDS: frozenset = frozenset({
    TRANSITION_ROLLBACK_THRESHOLD_CROSSED,
    TRANSITION_MODEL_PROMOTED,
    TRANSITION_CURRICULUM_BATCH_EMITTED,
})


# ---------------------------------------------------------------------------
# Master flag — asymmetric default-false semantics
# ---------------------------------------------------------------------------


def producer_enabled() -> bool:
    """``JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED`` (default
    ``false`` until Phase 9 cadence graduation).

    Asymmetric semantics: empty/whitespace = unset = current
    default; explicit ``0`` / ``false`` / ``no`` / ``off``
    evaluates false; explicit truthy values evaluate true.
    Re-read on every call so flips hot-revert without restart.
    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Internal — best-effort SSE publish
# ---------------------------------------------------------------------------


def _publish(
    *,
    transition_kind: str,
    payload: Dict[str, Any],
) -> Optional[str]:
    """Best-effort SSE publish via canonical
    :func:`publish_feedback_engine_signal_event`. NEVER raises.

    Returns the published event_id (string) on success, ``None``
    on master-off / publish failure / unknown transition_kind.
    The closed-set check on transition_kind is structural: the
    only way to add a new kind is to extend the constants above
    AND update the AST pin. Drift produces a None return + DEBUG
    log, never a raise."""
    if transition_kind not in _VALID_TRANSITION_KINDS:
        logger.debug(
            "[feedback_engine_sse_producer] unknown "
            "transition_kind=%r; dropping",
            transition_kind,
        )
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_feedback_engine_signal_event,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[feedback_engine_sse_producer] publish helper "
            "import failed: %s", exc,
        )
        return None
    try:
        return publish_feedback_engine_signal_event(
            transition_kind=transition_kind,
            payload=dict(payload),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[feedback_engine_sse_producer] publish raised: %s",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public producer API — three entry points (one per transition kind)
# ---------------------------------------------------------------------------


def feed_rollback_threshold(
    *,
    brain_id: str,
    rollback_count: int,
    threshold: int,
    weight_delta: float = -0.1,
) -> bool:
    """Producer entry: brain rollback count crossed threshold.

    Called from :meth:`AutonomyFeedbackEngine._on_op_rolled_back`
    AFTER ``CommandBus.try_put`` returns True (i.e. the
    ADJUST_BRAIN_HINT command was successfully enqueued).

    The engine already implements its own multiple-of-threshold
    chatter-suppression (count >= threshold AND count % threshold
    == 0) — the bridge does not re-impose its own rate limit. One
    enqueue, one SSE frame.

    Returns True if the SSE frame published (event_id captured),
    False on master-off / publish failure. NEVER raises."""
    if not producer_enabled():
        return False
    try:
        bid = str(brain_id or "").strip()
        if not bid:
            return False
        count = int(rollback_count)
        thresh = max(1, int(threshold))
        delta = float(weight_delta)
    except (TypeError, ValueError):
        return False
    event_id = _publish(
        transition_kind=TRANSITION_ROLLBACK_THRESHOLD_CROSSED,
        payload={
            "brain_id": bid,
            "rollback_count": count,
            "threshold": thresh,
            "weight_delta": delta,
        },
    )
    return event_id is not None


def feed_model_promoted(
    *,
    model_id: str,
    previous_model_id: str = "",
    source_event_file: str = "",
    repo: str = "",
) -> bool:
    """Producer entry: reactor batch processed a model_promoted
    event AND the resulting backlog command was accepted.

    Called from :meth:`AutonomyFeedbackEngine._handle_model_promoted`
    AFTER ``CommandBus.try_put`` returns True. The reactor file
    cursor (persisted to disk) prevents replay on subsequent
    batches, so this is one-shot per (model_id, source_event_file).

    Returns True if the SSE frame published, False on master-off
    or empty model_id. NEVER raises."""
    if not producer_enabled():
        return False
    try:
        mid = str(model_id or "").strip()
        if not mid:
            return False
        prev = str(previous_model_id or "").strip()
        src = str(source_event_file or "").strip()
        repo_s = str(repo or "").strip()
    except (TypeError, ValueError):
        return False
    event_id = _publish(
        transition_kind=TRANSITION_MODEL_PROMOTED,
        payload={
            "model_id": mid,
            "previous_model_id": prev,
            "source_event_file": src,
            "repo": repo_s,
        },
    )
    return event_id is not None


def feed_curriculum_batch(
    *,
    source_curriculum_id: str,
    emitted_count: int,
    rejected_count: int = 0,
) -> bool:
    """Producer entry: curriculum file scan finished AND at least
    one backlog command was accepted by the bus.

    Called from
    :meth:`AutonomyFeedbackEngine.consume_curriculum_once` AFTER
    each file is processed. Empty batches (``emitted_count == 0``)
    are silent — operator already sees that case via the engine's
    own DEBUG log; no SSE noise.

    Returns True if the SSE frame published, False on master-off /
    empty batch / empty curriculum_id. NEVER raises."""
    if not producer_enabled():
        return False
    try:
        cid = str(source_curriculum_id or "").strip()
        if not cid:
            return False
        emitted = int(emitted_count)
        if emitted < 1:
            return False  # chatter suppression — empty batches silent
        rejected = max(0, int(rejected_count))
    except (TypeError, ValueError):
        return False
    event_id = _publish(
        transition_kind=TRANSITION_CURRICULUM_BATCH_EMITTED,
        payload={
            "source_curriculum_id": cid,
            "emitted_count": emitted,
            "rejected_count": rejected,
        },
    )
    return event_id is not None


# ---------------------------------------------------------------------------
# Shipped-code AST invariants (§33 register_shipped_invariants pattern)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned shipped-code AST pins.

    Three invariants — the producer MUST stay frozen across
    refactors so consumers (VS Code extension, dashboards, audit
    replay) can rely on the contract:

    1. ``producer_default_false`` — master flag remains
       default-false until graduation. Reverting to ``"true"``
       breaks the Phase 9 cadence.
    2. ``transition_kinds_frozen`` — exactly the three string
       constants are valid; adding a fourth without updating the
       pin is a structural bug.
    3. ``no_authority_imports`` — bridge MUST NOT import
       orchestrator / iron_gate / providers / candidate_generator
       / urgency_router / sensor_governor / tool_executor /
       change_engine / strategic_direction.

    NEVER raises. Returns ``[]`` if the invariants substrate is
    not available (test-only graceful degradation)."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_default_false(_tree, source) -> tuple:
        marker = (
            'os.environ.get(\n'
            '        "JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED", "",'
        )
        if marker not in source:
            return (
                "feedback_engine_sse_producer.producer_enabled "
                "must read JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_"
                "ENABLED env with default-false fallback (Phase 9 "
                "cadence contract).",
            )
        # Also require the explicit `return False` default branch.
        if "return False  # default-false until graduation" not in source:
            return (
                "feedback_engine_sse_producer.producer_enabled "
                "must explicitly comment + return False on the "
                "default branch (graduation contract).",
            )
        return ()

    def _validate_transition_kinds_frozen(_tree, source) -> tuple:
        required_constants = (
            'TRANSITION_ROLLBACK_THRESHOLD_CROSSED: str = (',
            'TRANSITION_MODEL_PROMOTED: str = "model_promoted"',
            'TRANSITION_CURRICULUM_BATCH_EMITTED: str = (',
        )
        for needle in required_constants:
            if needle not in source:
                return (
                    f"feedback_engine_sse_producer transition-kind "
                    f"taxonomy frozen: missing constant {needle!r}.",
                )
        # Frozenset pin — must contain exactly the 3 constants.
        if "_VALID_TRANSITION_KINDS: frozenset = frozenset({" not in source:
            return (
                "feedback_engine_sse_producer "
                "_VALID_TRANSITION_KINDS must be a frozenset over "
                "the three constants.",
            )
        return ()

    def _validate_no_authority_imports(tree, _source) -> tuple:
        # AST-walk-based check (not substring match) so the
        # validator's OWN forbidden list doesn't self-match.
        forbidden_modules = frozenset({
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.sensor_governor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.strategic_direction",
        })
        try:
            import ast as _ast
        except ImportError:  # pragma: no cover — stdlib always present
            return ()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                # node.module is the dotted source ('backend.x.y.z').
                # Drop trailing component checks — we only forbid
                # the exact forbidden modules, not their submodules
                # like governance.orchestrator.helpers (which don't
                # exist anyway, but the discipline is "no orchestrator
                # import root").
                mod = node.module or ""
                if mod in forbidden_modules:
                    return (
                        f"feedback_engine_sse_producer authority "
                        f"asymmetry violated: imports forbidden "
                        f"module {mod!r}.",
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    mod = alias.name or ""
                    if mod in forbidden_modules:
                        return (
                            f"feedback_engine_sse_producer authority "
                            f"asymmetry violated: imports forbidden "
                            f"module {mod!r}.",
                        )
        return ()

    target = (
        "backend/core/ouroboros/governance/"
        "feedback_engine_sse_producer.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "feedback_engine_sse_producer_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_"
                "ENABLED must default-false until Phase 9 cadence "
                "graduates it (3 clean soaks)."
            ),
            validate=_validate_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "feedback_engine_sse_producer_transition_kinds_frozen"
            ),
            target_file=target,
            description=(
                "Three transition-kind string constants frozen + "
                "covered by _VALID_TRANSITION_KINDS frozenset; "
                "external SSE consumers depend on the closed set."
            ),
            validate=_validate_transition_kinds_frozen,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "feedback_engine_sse_producer_no_authority_imports"
            ),
            target_file=target,
            description=(
                "Producer-bridge authority asymmetry: must not "
                "import orchestrator / iron_gate / providers / "
                "candidate_generator / urgency_router / "
                "sensor_governor / tool_executor / change_engine / "
                "strategic_direction (read-only projection only)."
            ),
            validate=_validate_no_authority_imports,
        ),
    ]


def register_flags(registry: Any) -> int:
    """Module-owned FlagSpec declaration (§33 naming-cage —
    auto-discovered by ``flag_registry_seed._discover_module_provided_flags``).

    Single flag: ``JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED``,
    default-FALSE, OBSERVABILITY category, posture-relevant under
    HARDEN/CONSOLIDATE (operator wants to see autonomy state in
    those postures; less interesting in EXPLORE/MAINTAIN).

    NEVER raises. Returns count installed (0 or 1)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except ImportError:
        return 0
    try:
        spec = FlagSpec(
            name="JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED",
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for FeedbackEngine SSE "
                "producer-bridge (Phase B4). When true, three "
                "AutonomyFeedbackEngine state transitions publish "
                "to the IDE event stream as "
                "``feedback_engine_signal`` frames: "
                "rollback_threshold_crossed (brain weight-delta "
                "advisory), model_promoted (reactor batch), and "
                "curriculum_batch_emitted (per-file batch result). "
                "Default-false until Phase 9 cadence graduation "
                "(3 clean soaks). Producer-bridge §33.2 — engine "
                "state mirroring forbidden; lazy-import contract."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "feedback_engine_sse_producer.py"
            ),
            example="false",
            since="Phase B4 (2026-05-10)",
            posture_relevance={
                "HARDEN": Relevance.RELEVANT,
                "CONSOLIDATE": Relevance.RELEVANT,
            },
        )
        registry.register(spec, override=True)
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


__all__ = [
    "FEEDBACK_ENGINE_SSE_PRODUCER_SCHEMA_VERSION",
    "TRANSITION_ROLLBACK_THRESHOLD_CROSSED",
    "TRANSITION_MODEL_PROMOTED",
    "TRANSITION_CURRICULUM_BATCH_EMITTED",
    "producer_enabled",
    "feed_rollback_threshold",
    "feed_model_promoted",
    "feed_curriculum_batch",
    "register_flags",
    "register_shipped_invariants",
]
