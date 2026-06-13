"""
provider_topology — Hard-segmented DoubleWord model mapping (Manifesto §5).

Loads the ``doubleword_topology`` section of ``brain_selection_policy.yaml``
and exposes a deterministic, zero-LLM API for resolving:

  * Which DW model (if any) should handle a given :class:`ProviderRoute`
  * Whether DW is permitted on that route at all
  * Which DW model named callers (semantic_triage, ouroboros_plan) should use
  * **(v2 schema, 2026-04-27)** Per-route ranked ``dw_models:`` lists so
    the AsyncTopologySentinel can walk multiple DW model candidates
    before any Claude cascade.

The module is consumed by three downstream sites:

  * ``candidate_generator.py`` — hard-blocks DW from routes where
    ``dw_allowed`` is ``false`` (IMMEDIATE + COMPLEX in the default config).
  * ``doubleword_provider.py`` — resolves the effective model per-call from
    the operation's ``provider_route``, overriding the instance's default
    ``self._model`` without requiring multiple provider instances.
  * ``semantic_triage.py`` — picks its triage model from the
    ``callers.semantic_triage.dw_model`` override at init time.

Calibration history
-------------------
Live-fire ``bbpst3ebf`` (2026-04-14) proved that BOTH DW 397B and Gemma
4 31B time out on the 120s Tier 0 RT budget for architectural COMPLEX
GENERATE. Rather than extend the timeout (which violates the temporal
physics of the pipeline), the topology excludes DW from the Prefrontal
Cortex entirely. Gemma 4 31B is still a strong fit for high-volume,
structured-JSON tasks — it is retained as the Basal Ganglia engine for
BACKGROUND / SPECULATIVE / semantic_triage / ouroboros_plan.

Schema versions
---------------
``topology.1`` (legacy, current production): per-route ``dw_allowed`` +
single ``dw_model`` + ``block_mode``. Single-model-per-route — when the
configured DW model stream-stalls, the route hard-fails to its
``block_mode`` (cascade or skip_and_queue).

``topology.2`` (additive, 2026-04-27, gated by Slice 2 of Phase 10):
introduces three additive concepts:

  * Per-route ``dw_models:`` — ordered list of DW model_ids; the
    AsyncTopologySentinel walks the list trying each healthy model.
    The first model whose breaker is CLOSED (or HALF_OPEN per recovery
    semantics) wins. Only after exhausting all DW models does the route
    fall through to ``fallback_tolerance``.
  * ``fallback_tolerance:`` — explicit cost-contract. ``cascade_to_claude``
    routes the op to Claude on full DW failure; ``queue`` raises a
    ``RuntimeError`` that the orchestrator's existing accept-failure
    branch handles. Replaces v1's ``block_mode`` with sharper semantics.
  * ``monitor:`` — sentinel tunables (probe intervals, severed threshold,
    ramp schedule). Routes the AsyncTopologySentinel's own configuration
    through the same yaml that defines the routes.

**Backward compatibility**: v1 keys remain authoritative when v2 keys
are absent. ``dw_models`` defaults to a single-element list of
``[dw_model]`` when v2 omitted; ``fallback_tolerance`` derives from
``block_mode`` (``skip_and_queue`` → ``queue``, else ``cascade_to_claude``).
Existing call-sites continue to read v1 methods (``dw_allowed_for_route``,
``block_mode_for_route``) without behavior change. Slice 3 will wire
new consumers to the v2 methods (``dw_models_for_route``,
``fallback_tolerance_for_route``).

Contract
--------
This module is pure and side-effect-free after import. The yaml is
loaded once (cached) and the resolver is called from hot paths. A
missing or malformed ``doubleword_topology`` section is treated as
"topology disabled" — callers fall back to their previous behavior.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


# Schema-version sentinels emitted by the YAML reader. Consumers can
# inspect ``ProviderTopology.schema_version`` to branch behavior.
SCHEMA_VERSION_V1 = "topology.1"
SCHEMA_VERSION_V2 = "topology.2"


# Valid values for ``fallback_tolerance``. Operator-typed strings outside
# this set get coerced to the "cascade_to_claude" default rather than
# raising — same fail-open posture as the rest of this module.
_VALID_FALLBACK_TOLERANCES = frozenset(
    ("cascade_to_claude", "queue"),
)

logger = logging.getLogger(__name__)


_POLICY_FILENAME = "brain_selection_policy.yaml"


# ---------------------------------------------------------------------------
# Dev / verification env override (off by default)
# ---------------------------------------------------------------------------
#
# ``JARVIS_TOPOLOGY_BG_CASCADE_ENABLED`` forces ``background`` and
# ``speculative`` route ``block_mode`` from ``skip_and_queue`` to
# ``cascade_to_claude`` — i.e. when DW stalls on those routes, the op
# cascades to Claude instead of being skipped-and-queued.
#
# Purpose: verification-only escape hatch. The default topology
# (skip_and_queue for BG/SPEC) exists to protect unit economics — don't
# burn Claude compute on low-urgency background chores. But when DW
# health is degraded for a sustained period, operators need a way to
# drive a single verification session through to APPLY/VERIFY/commit so
# v1.1a ``ops_digest`` can be observed live end-to-end.
#
# Enable with ``JARVIS_TOPOLOGY_BG_CASCADE_ENABLED=true`` for a single
# session, collect the ``ops_digest`` artifact, then revert. The
# override logs a WARN on first use per boot so the audit trail shows
# exactly when unit-economics protection was suspended.
#
# This env does NOT disable ``dw_allowed`` — DW is still tried first on
# BG/SPEC. The override only changes what happens when DW fails: skip
# (default) vs cascade (override).


def _bg_cascade_override_enabled() -> bool:
    raw = os.environ.get("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


_BG_OVERRIDE_WARNED = False


def _warn_once_bg_override() -> None:
    """One-time WARN when the BG cascade override fires, per process."""
    global _BG_OVERRIDE_WARNED
    if _BG_OVERRIDE_WARNED:
        return
    _BG_OVERRIDE_WARNED = True
    logger.warning(
        "[ProviderTopology] JARVIS_TOPOLOGY_BG_CASCADE_ENABLED=true — "
        "BACKGROUND / SPECULATIVE routes will cascade to Claude when DW "
        "fails (overrides skip_and_queue default). Unit-economics protection "
        "suspended for this process. Disable the env once verification is done."
    )


@dataclass(frozen=True)
class RouteTopology:
    """Per-route topology entry.

    ``dw_allowed=False`` is a hard block — callers MUST NOT attempt any
    DoubleWord call for operations on this route. ``dw_model`` is the
    single model identifier passed to the DoubleWord API when DW is
    allowed under the v1 schema.

    ``block_mode`` controls what happens when DW is disallowed:

    * ``"cascade_to_claude"`` — route the op straight to Claude via the
      Prefrontal Cortex path. Used for IMMEDIATE and COMPLEX where Claude
      is the intended brain.
    * ``"skip_and_queue"`` — do NOT cascade. Raise a skip-and-queue
      sentinel the orchestrator already knows how to accept gracefully
      (background_dw_blocked_by_topology / speculative_deferred). Used
      for BACKGROUND and SPECULATIVE where routing to Claude would
      violate the unit economics of scalable autonomy.

    Default is ``"cascade_to_claude"`` so existing Prefrontal Cortex
    blocks keep working when the yaml is from before this field existed.

    **v2 schema additions** (additive, optional):

    * ``dw_models`` — ordered tuple of DW model_ids. When non-empty, the
      AsyncTopologySentinel walks the list trying each healthy model
      before falling through to ``fallback_tolerance``. Empty (default)
      means the route is single-model under the v1 schema; the
      ``dw_model`` field is the canonical (and only) model.
    * ``fallback_tolerance`` — explicit cost-contract.
      ``"cascade_to_claude"`` (default for v1 routes whose ``block_mode``
      is ``cascade_to_claude``) or ``"queue"`` (default derived from
      ``block_mode="skip_and_queue"``). Slice 3 (`candidate_generator`
      consumer wiring) is the first call site that branches on this.
    """

    dw_allowed: bool
    dw_model: Optional[str]
    reason: str
    block_mode: str = "cascade_to_claude"
    # v2 additive — empty tuple when reading v1-only yaml.
    dw_models: Tuple[str, ...] = field(default_factory=tuple)
    # v2 additive — defaults derive from block_mode at construction time.
    fallback_tolerance: str = "cascade_to_claude"

    @property
    def effective_dw_models(self) -> Tuple[str, ...]:
        """Returns the model rank order to walk, with v1 backward-compat.

        v2 yaml: returns the explicit ``dw_models`` list.
        v1 yaml: returns ``(dw_model,)`` — single-element tuple — when DW
        is allowed AND ``dw_model`` is set; empty tuple otherwise. Same
        semantics as v1 callers' single-model behavior, but exposed
        through the v2-shaped accessor so Slice 3+ consumers can use
        one code path."""
        if self.dw_models:
            return self.dw_models
        if self.dw_allowed and self.dw_model:
            return (self.dw_model,)
        return ()


@dataclass(frozen=True)
class MonitorConfig:
    """Sentinel tunables loaded from yaml v2 ``monitor:`` block.

    All fields are optional with sensible defaults. The
    AsyncTopologySentinel reads these via :meth:`ProviderTopology.monitor_config`
    on boot and uses them for probe scheduling, the severed-threshold
    weighted streak, and the slow-start ramp schedule.

    When yaml v2 ``monitor:`` is absent, every field is ``None`` and
    the sentinel falls back to its env-knob defaults (whose names match
    the keys here, prefixed with ``JARVIS_TOPOLOGY_``).
    """

    probe_interval_healthy_s: Optional[float] = None
    probe_backoff_base_s: Optional[float] = None
    probe_backoff_cap_s: Optional[float] = None
    severed_threshold_weighted: Optional[float] = None
    heavy_probe_ratio: Optional[float] = None
    ramp_schedule_csv: Optional[str] = None  # "0:1.0,10:2.0,..." form
    schema_version: str = SCHEMA_VERSION_V2


@dataclass(frozen=True)
class CallerTopology:
    """Per-caller topology entry (semantic_triage, ouroboros_plan, ...)."""

    dw_model: str
    reason: str


@dataclass(frozen=True)
class ProviderTopology:
    """Resolved view of the ``doubleword_topology`` yaml section.

    ``enabled=False`` means the yaml section is missing or disabled;
    callers should fall back to their pre-topology defaults.

    ``schema_version`` reflects which yaml schema produced this topology
    object. Slice 3+ consumers can branch on it (e.g. AsyncTopologySentinel
    only walks ``dw_models`` lists when v2 — under v1 it stays observer-
    only against the single ``dw_model``).
    """

    enabled: bool
    routes: Mapping[str, RouteTopology] = field(default_factory=dict)
    callers: Mapping[str, CallerTopology] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION_V1
    monitor: Optional[MonitorConfig] = None

    def dw_allowed_for_route(self, route: str) -> bool:
        """Return True if DW may be attempted on *route*.

        Unknown routes and missing topology both default to True so that
        the legacy DW cascade keeps working when the yaml is absent. The
        whole point of the hard-block is that it is **explicit** — new
        routes are opt-in to the cortex, not opt-out by accident.

        Slice 10B-ii (2026-05-26) — when the YAML returns ``dw_allowed=
        false`` for a non-IMMEDIATE route, consult
        :func:`_trusted_seed_dw_models_for_route` for operator-attested
        models from PromotionLedger that pass the route's per-route
        eligibility gates. If non-empty, the runtime bypass returns
        True so the candidate_generator topology block doesn't fire
        and DW dispatch can proceed via the trusted seeds. IMMEDIATE
        is excluded from the bypass per Manifesto §5 (Claude-direct
        by design — speed permanently supersedes cost optimization).

        The YAML ``dw_allowed: false`` stays the SAFETY CONTRACT; the
        ledger bypass is the RUNTIME AUTHORITY when the operator has
        explicitly attested specific models via JARVIS_DW_TRUSTED_MODELS.
        """
        if not self.enabled:
            return True
        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
            return True
        if entry.dw_allowed:
            return True
        # ── Slice 10B-ii — trusted-seed runtime bypass ──
        # YAML says DW blocked for this route; check whether
        # operator-attested PromotionLedger seeds qualify.
        if _trusted_seed_dw_models_for_route(route):
            return True
        return entry.dw_allowed

    def model_for_route(self, route: str) -> Optional[str]:
        """Return the effective DW model for *route*, or None.

        None indicates the caller should use the DoubleWord provider's
        default model (``self._model``). A string indicates a per-route
        override that MUST be used for this generation call.
        """
        if not self.enabled:
            return None
        entry = self.routes.get((route or "").strip().lower())
        if entry is None or not entry.dw_allowed:
            return None
        return entry.dw_model

    def reason_for_route(self, route: str) -> str:
        """Return the human-readable rationale for the route's policy.

        Used in log lines so operators can see *why* DW is blocked
        without reading the yaml.
        """
        if not self.enabled:
            return "topology_disabled"
        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
            return "route_unmapped"
        return entry.reason

    def block_mode_for_route(self, route: str) -> str:
        """Return the block behavior for a DW-disallowed route.

        Only meaningful when :meth:`dw_allowed_for_route` is False.
        Returns ``"cascade_to_claude"`` by default so legacy IMMEDIATE/
        COMPLEX blocks keep routing to Claude. BACKGROUND/SPECULATIVE
        entries in the yaml declare ``block_mode: skip_and_queue`` to
        opt out of the cascade.

        Dev/verification override: when
        ``JARVIS_TOPOLOGY_BG_CASCADE_ENABLED=true``, any route whose yaml
        declared ``skip_and_queue`` is flipped to ``cascade_to_claude``
        at read-time (YAML not modified). The override emits exactly one
        WARN per process the first time it rewrites a decision.
        """
        if not self.enabled:
            return "cascade_to_claude"
        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
            return "cascade_to_claude"
        effective = entry.block_mode or "cascade_to_claude"
        if effective == "skip_and_queue" and _bg_cascade_override_enabled():
            _warn_once_bg_override()
            return "cascade_to_claude"
        return effective

    def model_for_caller(self, caller: str) -> Optional[str]:
        """Return the DW model override for a named caller, or None.

        Callers are logical identifiers (``semantic_triage``,
        ``ouroboros_plan``) that may need a different model from the
        route-based default. Returning None means the caller should use
        whatever default applies at its site (env var or
        ``self._model``).
        """
        if not self.enabled:
            return None
        entry = self.callers.get((caller or "").strip().lower())
        if entry is None:
            return None
        return entry.dw_model

    # ------------------------------------------------------------------
    # v2 schema accessors (additive — Slice 3+ consumes these)
    # ------------------------------------------------------------------

    def dw_models_for_route(self, route: str) -> Tuple[str, ...]:
        """Return the v2 ranked ``dw_models`` list for *route*.

        Phase 12 Slice D — when ``JARVIS_DW_CATALOG_AUTHORITATIVE=true``
        AND the dynamic catalog holder is fresh AND the route has a
        non-empty assignment, the holder is consulted FIRST. YAML
        remains the fallback for every other path:

          * authoritative flag off → YAML
          * holder is None (no discovery cycle has populated it) → YAML
          * holder is stale (older than ``JARVIS_DW_CATALOG_MAX_AGE_S``,
            default 7200s = 2h) → YAML
          * route missing from holder → YAML
          * route present but empty list → YAML

        ``fallback_tolerance``, ``block_mode``, ``dw_allowed``, and the
        ``reason`` strings stay YAML-authored throughout — those encode
        cost-contract policy, not catalog facts. Only the ``dw_models``
        ranked list is dynamically substituted. BG/SPEC routes still
        respect ``fallback_tolerance: queue`` after sentinel exhaustion
        regardless of which models populated them.

        Backward-compat: when the yaml is v1 (no ``dw_models`` key for
        the route), returns a single-element tuple ``(dw_model,)`` if DW
        is allowed AND a ``dw_model`` is configured; empty tuple
        otherwise. This lets Slice 3 consumers always iterate the
        result without branching on schema_version.

        Returns the empty tuple when topology is disabled OR the route
        is unmapped — Slice 3's cascade-matrix interprets this as
        "no DW path, use fallback_tolerance directly."
        """
        if not self.enabled:
            return ()

        # Phase 12 Slice D — catalog-first read with YAML fallback
        if catalog_authoritative_enabled():
            holder = get_dynamic_catalog()
            if holder is not None and _holder_is_fresh(holder):
                key = (route or "").strip().lower()
                ranked = holder.assignments_by_route.get(key, ())
                if ranked:
                    return ranked
            # Fall through to YAML — cold-start, stale, route missing,
            # or route empty in catalog. YAML stays authoritative for
            # all four fallback conditions

        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
            return ()
        effective = entry.effective_dw_models
        if effective:
            return effective
        # ── Slice 10B-ii — trusted-seed runtime bypass ──
        # YAML + dynamic catalog both returned empty; consult
        # PromotionLedger for operator-attested trusted seeds that
        # pass the route's per-route eligibility gates. See
        # :func:`_trusted_seed_dw_models_for_route` for the gate
        # composition (re-uses dw_catalog_classifier.gate_for_route
        # so the same param/price thresholds apply).
        return _trusted_seed_dw_models_for_route(route)

    def fallback_tolerance_for_route(self, route: str) -> str:
        """Return the v2 ``fallback_tolerance`` for *route*.

        Backward-compat:
          * v1 yaml with ``block_mode: skip_and_queue`` → ``"queue"``
          * v1 yaml with ``block_mode: cascade_to_claude`` (or default)
            → ``"cascade_to_claude"``
          * v2 yaml with explicit ``fallback_tolerance`` key → that value

        Same dev-override behavior as :meth:`block_mode_for_route`:
        ``JARVIS_TOPOLOGY_BG_CASCADE_ENABLED=true`` flips ``"queue"``
        to ``"cascade_to_claude"`` at read-time so a single
        verification session can cascade BG/SPEC ops to Claude.
        """
        if not self.enabled:
            return "cascade_to_claude"
        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
            return "cascade_to_claude"
        effective = entry.fallback_tolerance or "cascade_to_claude"
        if effective == "queue" and _bg_cascade_override_enabled():
            _warn_once_bg_override()
            return "cascade_to_claude"
        return effective

    def monitor_config(self) -> Optional[MonitorConfig]:
        """Return the v2 ``monitor:`` block, or None.

        AsyncTopologySentinel reads this once at boot to override its
        env-knob defaults with yaml-driven values."""
        return self.monitor

    # ------------------------------------------------------------------
    # Phase 10 Slice 5a — Deletion-side unified helpers
    # ------------------------------------------------------------------
    #
    # Operator binding 2026-05-07 (verbatim): "Slice 5 deletion-side
    # substrate (delete redundant `dw_allowed: false` + `block_mode:`
    # lines from yaml; migrate readers to topology.2-only methods)".
    #
    # The yaml deletion is gated on `phase10_graduation_contract.
    # is_ready_for_purge() == READY_FOR_PURGE` (operator-paced;
    # requires 3 forced-clean once-proofs). Until then, callers
    # MUST route through the unified helpers below — they branch
    # on `JARVIS_TOPOLOGY_SENTINEL_ENABLED` to derive from v2 when
    # the master is on (yaml v1 fields irrelevant) and v1 when the
    # master is off (current production behavior preserved
    # byte-identical).
    #
    # AST-pinned via `phase10_v1_topology_methods_routed_through_helper`
    # (see :func:`register_shipped_invariants` below): production
    # code outside this module + tests MUST call the unified
    # helpers, not the v1 methods directly. New v1-method call
    # sites are forbidden by construction.

    def is_dw_blocked_for_route(
        self, route: str,
    ) -> Tuple[bool, str, str]:
        """Phase 10 Slice 5a deletion-side unified helper.

        Returns ``(is_blocked, reason, block_mode_v1_vocab)`` for
        *route*, branching on
        ``JARVIS_TOPOLOGY_SENTINEL_ENABLED``:

          * Master ON → derives from v2 methods
            (:meth:`dw_models_for_route` +
            :meth:`fallback_tolerance_for_route`). Yaml v1
            fields (``dw_allowed``, ``block_mode``) become
            irrelevant runtime inputs and can be deleted safely
            in Slice 5b after :func:`is_ready_for_purge` returns
            ``READY_FOR_PURGE``.
          * Master OFF → derives from v1 methods
            (:meth:`dw_allowed_for_route` +
            :meth:`block_mode_for_route`). Byte-identical to
            pre-Phase-10 production behavior.

        ``block_mode`` is returned in the **v1 vocabulary**
        (``"cascade_to_claude"`` / ``"skip_and_queue"``) so
        existing call-site string matches keep working across
        the migration. The v2→v1 translation:

          * v2 ``"queue"`` → v1 ``"skip_and_queue"``
          * v2 ``"cascade_to_claude"`` (or anything else) → v1
            ``"cascade_to_claude"``

        Operator binding 2026-05-07: "no second parallel retry
        loop with divergent env knobs without consolidating
        names" — single env knob
        ``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` gates the
        migration. NEVER raises.
        """
        if not self.enabled:
            return (False, "topology_disabled", "cascade_to_claude")

        sentinel_on = os.environ.get(
            "JARVIS_TOPOLOGY_SENTINEL_ENABLED", "",
        ).strip().lower() in ("1", "true", "yes", "on")

        reason = self.reason_for_route(route)

        if sentinel_on:
            # v2 path — yaml v1 fields irrelevant. Slice 5b
            # deletion is safe under this branch.
            try:
                has_dw = bool(self.dw_models_for_route(route))
            except Exception:  # noqa: BLE001 — defensive
                # On v2 read failure, fail OPEN (DW allowed)
                # to mirror legacy fail-open posture (line 268
                # docstring: "Unknown routes and missing
                # topology both default to True so that the
                # legacy DW cascade keeps working").
                return (False, reason, "cascade_to_claude")
            if has_dw:
                return (False, reason, "cascade_to_claude")
            # No DW path — derive block_mode from v2
            # fallback_tolerance, translate to v1 vocab.
            try:
                fallback = self.fallback_tolerance_for_route(route)
            except Exception:  # noqa: BLE001 — defensive
                fallback = "cascade_to_claude"
            block_mode = (
                "skip_and_queue" if fallback == "queue"
                else "cascade_to_claude"
            )
            return (True, reason, block_mode)

        # v1 path — current production behavior, byte-identical.
        try:
            allowed = self.dw_allowed_for_route(route)
        except Exception:  # noqa: BLE001 — defensive
            return (False, reason, "cascade_to_claude")
        if allowed:
            return (False, reason, "cascade_to_claude")
        try:
            block_mode = self.block_mode_for_route(route)
        except Exception:  # noqa: BLE001 — defensive
            block_mode = "cascade_to_claude"
        return (True, reason, block_mode)

    def model_for_route_unified(
        self, route: str,
    ) -> Optional[str]:
        """Phase 10 Slice 5a deletion-side unified per-route
        model resolver.

        Branches on ``JARVIS_TOPOLOGY_SENTINEL_ENABLED``:

          * Master ON → first element of
            :meth:`dw_models_for_route` (v2 catalog-first →
            yaml fallback). Empty list → ``None``.
          * Master OFF → :meth:`model_for_route` (v1
            yaml-direct).

        NEVER raises."""
        if not self.enabled:
            return None
        sentinel_on = os.environ.get(
            "JARVIS_TOPOLOGY_SENTINEL_ENABLED", "",
        ).strip().lower() in ("1", "true", "yes", "on")
        if sentinel_on:
            try:
                models = self.dw_models_for_route(route)
            except Exception:  # noqa: BLE001 — defensive
                return None
            return models[0] if models else None
        try:
            return self.model_for_route(route)
        except Exception:  # noqa: BLE001 — defensive
            return None


_EMPTY_TOPOLOGY = ProviderTopology(enabled=False)


def route_elevation_enabled() -> bool:
    """Slice 229 master — exploration-floor driven route elevation. Default
    **TRUE**: an op that must satisfy the Iron Gate exploration floor gets the
    COMPLEX route's (agentic-elite, active-param-ranked) model pool prepended to
    its own, so tool-loop work is never starved onto low-active models that
    cannot drive it (the live GOAL-001::file-00 layer-5 wedge: elites all
    promoted but unreachable from STANDARD). OFF = byte-identical legacy pool.
    NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_ROUTE_ELEVATION_ENABLED", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def elevate_pool_for_exploration(
    ranked_models: Tuple[str, ...],
    elite_models: Tuple[str, ...],
    *,
    demands_tools: bool,
) -> Tuple[str, ...]:
    """Slice 229 — pure pool-expansion invariant.

    When ``demands_tools`` (the Slice-226 ``exploration_gate_demands_tools``
    predicate, resolved by the caller) and the master is on, return
    ``elite_models + (ranked_models - elite_models)``: the COMPLEX pool —
    already ranked agentic-first by active-param scoring + family preference —
    leads, the route's own models follow, deduped, order-stable. Elites-first is
    deliberate: a gated op on a weak model burns its GENERATE attempts and fails
    anyway, so one capable run is both cheaper and the point. No model names in
    code — the elite pool is whatever the classifier ranked into COMPLEX.
    Identity when the demand is absent / master off / elite pool empty.
    NEVER raises — falls back to the unmodified pool."""
    try:
        base = tuple(ranked_models or ())
        if not demands_tools or not route_elevation_enabled():
            return base
        elites = tuple(elite_models or ())
        if not elites:
            return base
        elite_set = set(elites)
        return elites + tuple(m for m in base if m not in elite_set)
    except Exception:  # noqa: BLE001 — defensive: never perturb dispatch
        try:
            return tuple(ranked_models or ())
        except Exception:  # noqa: BLE001
            return ()


def _locate_policy_yaml() -> Optional[Path]:
    """Return the path to ``brain_selection_policy.yaml`` or None.

    Walks from this module's directory (``governance/``) because the
    yaml lives next to the governance code. No repo-root heuristics —
    if the file is moved, the caller must update the path here.
    """
    here = Path(__file__).resolve().parent
    candidate = here / _POLICY_FILENAME
    if candidate.is_file():
        return candidate
    logger.warning(
        "[ProviderTopology] %s not found next to provider_topology.py "
        "(searched %s) — topology disabled, DW cascade falls back to legacy",
        _POLICY_FILENAME, candidate,
    )
    return None


def _parse_dw_models(raw_value: Any) -> Tuple[str, ...]:
    """Parse a v2 ``dw_models:`` list — accepts list/tuple of strings.

    Malformed entries (non-strings, empty strings) are silently skipped
    (fail-open posture). Result is ``Tuple`` for hashability inside the
    frozen dataclass.
    """
    if raw_value is None:
        return ()
    if not isinstance(raw_value, (list, tuple)):
        return ()
    out: List[str] = []
    for item in raw_value:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                out.append(cleaned)
    return tuple(out)


def _resolve_fallback_tolerance(
    explicit_value: Any,
    block_mode: str,
) -> str:
    """Pick the v2 ``fallback_tolerance`` string with v1-derivation
    fallback.

    Precedence:
      1. Explicit ``fallback_tolerance`` key in yaml → validated against
         the allowlist; invalid values fall through to derivation.
      2. Derive from ``block_mode``: ``"skip_and_queue"`` → ``"queue"``;
         everything else → ``"cascade_to_claude"``.
    """
    if isinstance(explicit_value, str):
        cleaned = explicit_value.strip().lower()
        if cleaned in _VALID_FALLBACK_TOLERANCES:
            return cleaned
    return "queue" if block_mode == "skip_and_queue" else "cascade_to_claude"


def _parse_monitor_block(
    raw_value: Any,
) -> Optional[MonitorConfig]:
    """Parse the v2 ``monitor:`` block into a MonitorConfig.

    Returns None when the block is missing or malformed. Each field is
    individually parsed — a typo in one field doesn't disable the rest.
    """
    if not isinstance(raw_value, Mapping):
        return None

    def _opt_float(key: str) -> Optional[float]:
        v = raw_value.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _opt_str(key: str) -> Optional[str]:
        v = raw_value.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        cleaned = v.strip()
        return cleaned or None

    return MonitorConfig(
        probe_interval_healthy_s=_opt_float("probe_interval_healthy_s"),
        probe_backoff_base_s=_opt_float("probe_backoff_base_s"),
        probe_backoff_cap_s=_opt_float("probe_backoff_cap_s"),
        severed_threshold_weighted=_opt_float(
            "severed_threshold_weighted",
        ),
        heavy_probe_ratio=_opt_float("heavy_probe_ratio"),
        ramp_schedule_csv=_opt_str("ramp_schedule_csv"),
    )


def _parse_topology(raw: Mapping[str, Any]) -> ProviderTopology:
    section = raw.get("doubleword_topology")
    if not isinstance(section, Mapping):
        return _EMPTY_TOPOLOGY

    enabled = bool(section.get("enabled", False))
    if not enabled:
        return _EMPTY_TOPOLOGY

    # Schema-version detection. v2 yaml MUST set ``schema_version:
    # "topology.2"`` to opt in. Any other value (including missing)
    # parses as v1 — additive v2 keys are still parsed when present
    # so an operator can stage v2 keys in the file before flipping
    # ``schema_version``.
    raw_schema = section.get("schema_version")
    if isinstance(raw_schema, str) and raw_schema.strip() == SCHEMA_VERSION_V2:
        schema_version = SCHEMA_VERSION_V2
    else:
        schema_version = SCHEMA_VERSION_V1

    routes: Dict[str, RouteTopology] = {}
    raw_routes = section.get("routes")
    if isinstance(raw_routes, Mapping):
        for name, body in raw_routes.items():
            if not isinstance(body, Mapping):
                continue
            allowed = bool(body.get("dw_allowed", True))
            model = body.get("dw_model") if allowed else None
            reason = str(body.get("reason", "") or "")
            block_mode_raw = str(
                body.get("block_mode", "cascade_to_claude") or "cascade_to_claude"
            ).strip().lower()
            if block_mode_raw not in {"cascade_to_claude", "skip_and_queue"}:
                block_mode_raw = "cascade_to_claude"
            # v2 additive — both keys are read regardless of
            # schema_version so operators can stage them in advance.
            dw_models = _parse_dw_models(body.get("dw_models"))
            fallback_tolerance = _resolve_fallback_tolerance(
                body.get("fallback_tolerance"),
                block_mode_raw,
            )
            routes[str(name).strip().lower()] = RouteTopology(
                dw_allowed=allowed,
                dw_model=str(model) if model else None,
                reason=reason,
                block_mode=block_mode_raw,
                dw_models=dw_models,
                fallback_tolerance=fallback_tolerance,
            )

    callers: Dict[str, CallerTopology] = {}
    raw_callers = section.get("callers")
    if isinstance(raw_callers, Mapping):
        for name, body in raw_callers.items():
            if not isinstance(body, Mapping):
                continue
            model = body.get("dw_model")
            if not model:
                continue
            reason = str(body.get("reason", "") or "")
            callers[str(name).strip().lower()] = CallerTopology(
                dw_model=str(model),
                reason=reason,
            )

    monitor_cfg = _parse_monitor_block(section.get("monitor"))

    return ProviderTopology(
        enabled=True,
        routes=routes,
        callers=callers,
        schema_version=schema_version,
        monitor=monitor_cfg,
    )


_CACHED_TOPOLOGY: Optional[ProviderTopology] = None


def _load_topology() -> ProviderTopology:
    """Load and parse the yaml once. Returns the disabled sentinel on any error.

    Parsing failures are logged at WARNING and never raise — the
    governance stack must keep working even if the yaml is corrupt. The
    warning gives operators enough detail to fix the file without having
    to read source.
    """
    path = _locate_policy_yaml()
    if path is None:
        return _EMPTY_TOPOLOGY

    try:
        import yaml  # pyyaml is already a project dep
    except ImportError:
        logger.warning(
            "[ProviderTopology] PyYAML not importable — topology disabled. "
            "Install pyyaml to enable strict cognitive segmentation.",
        )
        return _EMPTY_TOPOLOGY

    try:
        raw_text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text) or {}
    except Exception as exc:
        logger.warning(
            "[ProviderTopology] Failed to parse %s (%s: %s) — "
            "topology disabled, legacy cascade in effect",
            path, type(exc).__name__, exc,
        )
        return _EMPTY_TOPOLOGY

    if not isinstance(raw, Mapping):
        logger.warning(
            "[ProviderTopology] %s root is not a mapping — topology disabled",
            path,
        )
        return _EMPTY_TOPOLOGY

    topo = _parse_topology(raw)
    if not topo.enabled:
        logger.info(
            "[ProviderTopology] doubleword_topology section missing or disabled "
            "in %s — legacy DW cascade active", path,
        )
    else:
        logger.info(
            "[ProviderTopology] Loaded from %s — %d routes, %d callers",
            path, len(topo.routes), len(topo.callers),
        )
    return topo


def get_topology() -> ProviderTopology:
    """Return the process-wide cached topology.

    Loaded lazily on first call and memoized for the life of the
    process. Tests that need to exercise a different yaml can call
    :func:`reload_topology` after setting up the environment.
    """
    global _CACHED_TOPOLOGY
    if _CACHED_TOPOLOGY is None:
        _CACHED_TOPOLOGY = _load_topology()
    return _CACHED_TOPOLOGY


def reload_topology() -> ProviderTopology:
    """Force a reload of the yaml — tests and hot-reload scenarios only.

    Clears the cache and re-parses the yaml. Returns the new topology
    object. Do not call this from hot paths in production; the yaml is
    deliberately read once per process.
    """
    global _CACHED_TOPOLOGY
    _CACHED_TOPOLOGY = None
    return get_topology()


# ---------------------------------------------------------------------------
# Phase 12 Slice D — Authoritative-mode flag + freshness helper
# ---------------------------------------------------------------------------
#
# Two-flag separation (operator-controllable independently):
#
#   JARVIS_DW_CATALOG_DISCOVERY_ENABLED  (Slice A) — does discovery RUN?
#                                                    fetch + classify + populate
#                                                    holder. Master flag for the
#                                                    end-to-end pipeline.
#   JARVIS_DW_CATALOG_AUTHORITATIVE      (Slice D) — does dw_models_for_route
#                                                    READ the holder first?
#                                                    Slice C kept holder pure
#                                                    observation; Slice D flips
#                                                    the read order.
#
# Flag matrix (post-Slice-D, pre-Slice-E graduation):
#
#   discovery=off, authoritative=off  → legacy YAML-only (current main as of A)
#   discovery=on,  authoritative=off  → shadow mode (Slice C — holder populated,
#                                       dispatcher still reads YAML, operator
#                                       can audit yaml_diff diagnostic)
#   discovery=off, authoritative=on   → no-op in practice — holder stays empty,
#                                       dw_models_for_route falls through to YAML
#                                       on every call. Cost-safe by construction.
#   discovery=on,  authoritative=on   → full Phase 12 (Slice E target). Catalog
#                                       reads first, YAML is the fallback layer.


def catalog_authoritative_enabled() -> bool:
    """``JARVIS_DW_CATALOG_AUTHORITATIVE`` (default ``true`` —
    graduated in Phase 12 Slice E alongside DISCOVERY_ENABLED).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_DW_CATALOG_AUTHORITATIVE=false`` returns dispatcher to
    YAML-authored dw_models lists. Note: at graduation, YAML's
    dw_models arrays were purged — so the hot-revert lands on
    ``effective_dw_models = ()`` per route, which means the
    dispatcher cascades per ``fallback_tolerance``. BG/SPEC stay
    queue-only (no Claude burn); STANDARD/COMPLEX cascade to
    Claude until DISCOVERY is re-enabled."""
    raw = os.environ.get(
        "JARVIS_DW_CATALOG_AUTHORITATIVE", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _catalog_max_age_s() -> float:
    """Re-read ``JARVIS_DW_CATALOG_MAX_AGE_S`` (default 7200s = 2h)
    at call time. Used by ``_holder_is_fresh``. Mirrors the same env
    knob in ``dw_catalog_client.py`` so a single operator-tuned value
    governs both the client cache + the dispatcher freshness gate."""
    try:
        return float(
            os.environ.get("JARVIS_DW_CATALOG_MAX_AGE_S", "7200").strip(),
        )
    except (ValueError, TypeError):
        return 7200.0


def _holder_is_fresh(holder: "_DynamicCatalogHolder") -> bool:
    """``True`` when the holder was populated within
    ``_catalog_max_age_s`` seconds ago. Stale holder → caller falls
    back to YAML. NEVER raises."""
    import time as _time
    try:
        return (_time.time() - holder.fetched_at_unix) < _catalog_max_age_s()
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Phase 12 Slice C — Dynamic catalog holder (shadow mode)
# ---------------------------------------------------------------------------
#
# In shadow mode (default for Slice C), the catalog discovery pipeline
# fetches DW's /models endpoint, runs the classifier, and populates the
# in-memory holder below. The dispatcher continues consuming the YAML
# ranked lists via :meth:`ProviderTopology.dw_models_for_route` — the
# dynamic catalog is OBSERVATION-ONLY this slice. Slice D flips
# ``dw_models_for_route`` to read the dynamic holder first.
#
# The holder is a module-level singleton (not a ProviderTopology field)
# because ProviderTopology is ``@dataclass(frozen=True)``. Keeping the
# mutable runtime state separate from the YAML-derived immutable view
# preserves the contract that ``get_topology()`` returns the same
# bit-for-bit object across calls.

import threading as _threading


@dataclass(frozen=True)
class _DynamicCatalogHolder:
    """Frozen view of the latest discovery cycle's output. Replaced
    atomically — never mutated in place — so concurrent readers always
    see a consistent snapshot."""
    assignments_by_route: Mapping[str, Tuple[str, ...]]  # route → ranked model_ids
    fetched_at_unix: float
    fetch_failure_reason: Optional[str] = None
    schema_version: str = "dynamic_catalog.1"


_DYNAMIC_CATALOG: Optional[_DynamicCatalogHolder] = None
_DYNAMIC_CATALOG_LOCK = _threading.Lock()


def set_dynamic_catalog(
    assignments_by_route: Mapping[str, Tuple[str, ...]],
    *,
    fetched_at_unix: float,
    fetch_failure_reason: Optional[str] = None,
) -> None:
    """Replace the in-memory dynamic catalog. Called by the discovery
    runner (Phase 12 Slice C) after a successful or failed catalog
    fetch + classification.

    Atomic-replace contract: the holder is never partially updated; a
    concurrent ``get_dynamic_catalog()`` either sees the prior holder
    or the new one, never an in-progress mutation.

    NEVER raises on bad input — coerces to a defensive frozen mapping."""
    global _DYNAMIC_CATALOG
    safe: Dict[str, Tuple[str, ...]] = {}
    if isinstance(assignments_by_route, Mapping):
        for k, v in assignments_by_route.items():
            try:
                key = str(k).strip().lower()
                if not key:
                    continue
                if isinstance(v, (list, tuple)):
                    safe[key] = tuple(str(m) for m in v if m)
            except Exception:  # noqa: BLE001 — defensive
                continue
    holder = _DynamicCatalogHolder(
        assignments_by_route=safe,
        fetched_at_unix=float(fetched_at_unix),
        fetch_failure_reason=fetch_failure_reason,
    )
    with _DYNAMIC_CATALOG_LOCK:
        _DYNAMIC_CATALOG = holder


def get_dynamic_catalog() -> Optional[_DynamicCatalogHolder]:
    """Return the most recent dynamic catalog snapshot, or None when
    discovery has never run / has been cleared. NEVER raises."""
    with _DYNAMIC_CATALOG_LOCK:
        return _DYNAMIC_CATALOG


def clear_dynamic_catalog() -> None:
    """Reset the holder to None — for tests + explicit operator
    invalidation. NEVER raises."""
    global _DYNAMIC_CATALOG
    with _DYNAMIC_CATALOG_LOCK:
        _DYNAMIC_CATALOG = None


# ──────────────────────────────────────────────────────────────────────
# Slice 10B-ii (2026-05-26) — PromotionLedger trusted-seed bypass
#
# Closes the two-registry disconnect surfaced by bt-2026-05-26-000630
# (v11 DW-PRIMARY soak). Slice 10B seeded ``JARVIS_DW_TRUSTED_MODELS``
# into PromotionLedger as ``promoted=True`` with origin=trusted_seed,
# but the topology check (``dw_allowed_for_route`` / ``dw_models_for_
# route``) never consulted PromotionLedger — it only read
# ``brain_selection_policy.yaml`` (where ``dw_allowed: false`` +
# ``dw_models: []`` for STANDARD/COMPLEX/BG/SPEC remained the
# Phase-12 frozen contract awaiting catalog discovery). Result:
# operator-attested DW models were structurally invisible to the
# topology gate; every STANDARD-routed op cascaded to Claude despite
# the trusted seed.
#
# This module is the cooperative bridge between the YAML safety
# contract (immutable, encodes cost-policy) and the operator-attested
# runtime authority (mutable, encodes "this model is known-good").
# Compose discipline: re-use ``dw_promotion_ledger.PromotionLedger``
# + ``dw_catalog_classifier.gate_for_route`` (same per-route param /
# price thresholds that the classifier applies during normal discovery).
# No parallel state, no new env knob; ``JARVIS_DW_TRUSTED_MODELS``
# stays the single operator-facing surface.
# ──────────────────────────────────────────────────────────────────────


# Routes that MUST NEVER receive a trusted-seed bypass even when seeds
# exist. Manifesto §5: IMMEDIATE is Claude-direct by design — "survival
# and execution speed permanently supersede cost optimization." The
# operator's stated preference for cheap DW does NOT override §5's
# human-reflex-routing contract.
_TRUSTED_SEED_BYPASS_FORBIDDEN_ROUTES = frozenset({"immediate"})


def _trusted_seed_dw_models_for_route(route: str) -> Tuple[str, ...]:
    """Slice 10B-ii — return PromotionLedger trusted-seed model_ids
    that pass the route's per-route eligibility gates, in stable
    insertion order. Returns empty tuple when:

      * route is IMMEDIATE (Manifesto §5 — bypass forbidden);
      * PromotionLedger import / read raises (defensive fail-empty);
      * no promoted models exist in the ledger;
      * promoted models exist but none have ModelCard metadata
        that passes the route's ``EligibilityGate`` (per-route
        min_params_b + max_out_price_per_m thresholds);
      * any ledger / catalog interaction surfaces an exception
        (NEVER raises — topology must not crash on bridge errors).

    Composes ``dw_promotion_ledger.PromotionLedger`` (already-existing
    Slice 10B substrate) + ``dw_catalog_classifier.gate_for_route``
    (already-existing per-route gate definitions). No parallel state.

    Trusted seeds without catalog ModelCard metadata (the common case
    — operator attestation is "this model_id is good" without metadata)
    PASS the gate by construction: per-route gates skip checks when the
    relevant field is None (see EligibilityGate.admits — checks fire
    ONLY when both threshold AND card field are non-None). This is
    intentional — the OPERATOR'S attestation is the warrant, not
    metadata. Future enhancement: cross-check against
    dw_catalog_client snapshot if available.
    """
    route_lc = (route or "").strip().lower()
    if not route_lc or route_lc in _TRUSTED_SEED_BYPASS_FORBIDDEN_ROUTES:
        return ()
    try:
        # Lazy imports — keep provider_topology bootable without
        # forcing the entire promotion-ledger / classifier graph
        # to load when topology is consulted standalone (e.g., test).
        from backend.core.ouroboros.governance.dw_promotion_ledger import (  # noqa: E501
            PromotionLedger,
        )
        from backend.core.ouroboros.governance.dw_catalog_classifier import (  # noqa: E501
            gate_for_route,
        )
    except Exception:  # noqa: BLE001 — defensive (fail-closed → empty)
        return ()
    try:
        ledger = PromotionLedger()
        ledger.load()
        promoted = ledger.promoted_models()
    except Exception:  # noqa: BLE001 — defensive
        return ()
    if not promoted:
        return ()
    try:
        gate = gate_for_route(route_lc)
    except Exception:  # noqa: BLE001 — defensive
        return ()
    # When we lack ModelCard metadata for a promoted model, the
    # EligibilityGate.admits() check on a synthetic minimal card
    # passes by construction (every gate check skips when the
    # relevant field is None). Build a minimal synthetic ModelCard
    # for each promoted model_id and let the gate decide.
    try:
        from backend.core.ouroboros.governance.dw_catalog_client import (  # noqa: E501
            ModelCard,
            parse_parameter_count,
            parse_family,
        )
    except Exception:  # noqa: BLE001 — defensive
        # If ModelCard unavailable, fall back to returning ALL
        # promoted seeds (defense-in-depth: operator attested them).
        return tuple(promoted)
    admitted: list = []
    for model_id in promoted:
        try:
            mid_str = str(model_id)
            # Parse parameter count from the model_id (e.g.,
            # "doubleword-397b" → 397.0). The existing
            # parse_parameter_count helper handles "-NNNb",
            # "-NN.Mb", and "_NNNb" suffixes; returns None when
            # no recognizable size token is present.
            #
            # When parsing fails (no size in model_id), we set
            # the field to None — the per-route EligibilityGate
            # will reject if min_params_b > 0 (STANDARD=14B,
            # COMPLEX=30B). This is the CORRECT behavior: a
            # model_id without a parseable size hasn't proven it
            # meets the per-route cost-shape contract. BG/SPEC
            # routes (no min_params_b) admit the model regardless.
            parsed_params = parse_parameter_count(mid_str)
            parsed_family = parse_family(mid_str)
            synthetic = ModelCard(
                model_id=mid_str,
                family=parsed_family or "unknown",
                parameter_count_b=parsed_params,
                context_window=None,
                pricing_in_per_m_usd=None,
                pricing_out_per_m_usd=None,
                supports_streaming=True,
                raw_metadata_json="{}",
            )
            if gate.admits(synthetic):
                admitted.append(mid_str)
        except Exception:  # noqa: BLE001 — per-model defensive
            continue
    # Slice 83 — capability-priority sort. Previously this returned the
    # promoted models in INSERTION order, which buried the strong agentic
    # coders (GLM-5.1 754B, Kimi-K2.6 / DeepSeek-V4-Pro 1000B) behind the
    # baseline Qwen (35/397B): the dispatch tried Qwen first, it hit
    # live_transport, and Slice 73 severed the lane before the coders were
    # ever reached (the Sweep-#6 mis-dispatch). Reuse the classifier's existing
    # `_score` (params-weighted +1 for COMPLEX/STANDARD, pricing-weighted for
    # BG/SPEC) so the highest-CAPABILITY model leads — metadata-driven, not a
    # hardcoded order. Fail-soft: any scoring error → original insertion order.
    try:
        from backend.core.ouroboros.governance.dw_catalog_classifier import (  # noqa: E501
            _score as _cap_score_fn,
            _ranking_weights,
            _family_preference,
        )
        from backend.core.ouroboros.governance.pricing_oracle import (
            resolve_pricing,
        )
        _weights = _ranking_weights()
        _fam = _family_preference()
        _prefer_cheap = route_lc in ("background", "speculative")

        def _capability_score(mid: str) -> float:
            try:
                _pr = resolve_pricing(mid) or (None, None)
                _card = ModelCard(
                    model_id=mid, family=parse_family(mid),
                    parameter_count_b=parse_parameter_count(mid),
                    context_window=None, pricing_in_per_m_usd=_pr[0],
                    pricing_out_per_m_usd=_pr[1], supports_streaming=True,
                    raw_metadata_json="{}",
                )
                return float(_cap_score_fn(
                    _card, _weights, _fam, prefer_cheap=_prefer_cheap,
                ))
            except Exception:  # noqa: BLE001
                return 0.0

        admitted.sort(key=lambda _m: (-_capability_score(_m), _m))
    except Exception:  # noqa: BLE001 — never break dispatch on ranking error
        pass
    return tuple(admitted)


@dataclass(frozen=True)
class RouteDiff:
    """Per-route comparison of YAML-authored vs. dynamically-discovered
    ranked model lists. Surfaced by ``compute_yaml_diff`` for operator
    review during shadow-mode rollout.

    ``yaml_only`` — model_ids YAML wires that the catalog doesn't
        expose (likely renamed / removed upstream)
    ``catalog_only`` — model_ids the catalog exposes but YAML doesn't
        wire (the 14 missing models from the 22-model catalog)
    ``both`` — model_ids that overlap. Order may still differ; the
        ``yaml_order`` and ``catalog_order`` tuples preserve the
        ranking from each source for comparison.
    """
    route: str
    yaml_only: Tuple[str, ...]
    catalog_only: Tuple[str, ...]
    both: Tuple[str, ...]
    yaml_order: Tuple[str, ...]
    catalog_order: Tuple[str, ...]


def compute_yaml_diff(
    *,
    catalog_assignments: Mapping[str, Tuple[str, ...]],
    yaml_topology: Optional[ProviderTopology] = None,
) -> Dict[str, RouteDiff]:
    """Compare dynamic catalog assignments against the YAML topology's
    ranked lists, per route. Returns a ``Dict[route, RouteDiff]``.

    Pure function — does NOT mutate state. Used by the discovery runner
    to surface diagnostic strings for the sentinel preflight result,
    and by future operator tooling for shadow-mode review.

    Routes considered: ``standard``, ``complex``, ``background``,
    ``speculative``. IMMEDIATE is intentionally excluded (Claude-direct
    by Manifesto §5; the catalog never assigns models to it)."""
    if yaml_topology is None:
        yaml_topology = get_topology()
    out: Dict[str, RouteDiff] = {}
    for route in ("standard", "complex", "background", "speculative"):
        try:
            yaml_list = yaml_topology.dw_models_for_route(route)
        except Exception:  # noqa: BLE001 — defensive
            yaml_list = ()
        catalog_list = tuple(catalog_assignments.get(route, ()))
        yaml_set = set(yaml_list)
        catalog_set = set(catalog_list)
        out[route] = RouteDiff(
            route=route,
            yaml_only=tuple(sorted(yaml_set - catalog_set)),
            catalog_only=tuple(sorted(catalog_set - yaml_set)),
            both=tuple(sorted(yaml_set & catalog_set)),
            yaml_order=tuple(yaml_list),
            catalog_order=catalog_list,
        )
    return out


# ---------------------------------------------------------------------------
# Phase 10 Slice 5a — AST pin: forbid new v1-method call sites
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins the deletion-side migration:

      ``phase10_v1_topology_methods_routed_through_helper`` —
      production code under ``backend/core/ouroboros/governance/``
      MUST NOT call the v1 methods (:meth:`dw_allowed_for_route` /
      :meth:`block_mode_for_route` / :meth:`model_for_route`)
      directly. Use :meth:`is_dw_blocked_for_route` /
      :meth:`model_for_route_unified` instead.

      Operator binding 2026-05-07: the unified helpers branch on
      ``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` so v1 yaml fields can
      be deleted safely once :func:`is_ready_for_purge` returns
      ``READY_FOR_PURGE``. New v1-method call sites would
      reintroduce hard yaml dependency and block the purge.

      Exemptions: this module itself (the v1 methods are defined
      here + the unified helpers call them in the master-OFF
      branch) + the ``tests/`` tree.
    """
    import ast
    from pathlib import Path

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "provider_topology.py"
    )

    # v1 method names — bytes-pinned. Forbidden as method calls
    # outside this module + tests.
    _FORBIDDEN_V1_METHODS = (
        "dw_allowed_for_route",
        "block_mode_for_route",
        "model_for_route",
    )

    def _validate_v1_methods_routed_through_helper(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Walk every .py under governance/ excluding this module
        + tests + provider_topology test files. Forbid calls to
        v1 method names. NEVER raises."""
        violations: list = []
        try:
            governance_root = Path(__file__).resolve().parent
        except Exception:  # noqa: BLE001 — defensive
            return ()
        try:
            files = list(governance_root.rglob("*.py"))
        except Exception:  # noqa: BLE001 — defensive
            return ()
        # Self-exempt: this module defines + dispatches v1 methods.
        self_path = governance_root / "provider_topology.py"
        for py_path in files:
            try:
                if "__pycache__" in py_path.parts:
                    continue
                if py_path == self_path:
                    continue
                # Allow tests/ tree (defensive — governance/
                # shouldn't have tests but check just in case).
                if any(
                    p in {"tests", "test"}
                    for p in py_path.parts
                ):
                    continue
            except Exception:  # noqa: BLE001 — defensive
                continue
            try:
                src = py_path.read_text(encoding="utf-8")
                t = ast.parse(src)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(t):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in _FORBIDDEN_V1_METHODS:
                    continue
                try:
                    rel = py_path.relative_to(
                        governance_root,
                    ).as_posix()
                except ValueError:
                    rel = str(py_path)
                violations.append(
                    f"{rel}:{node.lineno} forbidden v1 "
                    f"method call .{func.attr}() — use "
                    f"is_dw_blocked_for_route() or "
                    f"model_for_route_unified() instead "
                    f"(Phase 10 Slice 5a deletion-side)"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_v1_topology_methods_"
                "routed_through_helper"
            ),
            target_file=target,
            description=(
                "Phase 10 Slice 5a — production code MUST "
                "NOT call v1 methods (dw_allowed_for_route "
                "/ block_mode_for_route / model_for_route) "
                "directly. Use is_dw_blocked_for_route() / "
                "model_for_route_unified() so the migration "
                "to topology.2-only methods stays single-"
                "source-of-truth and the yaml v1 fields can "
                "be deleted in Slice 5b after contract "
                "green."
            ),
            validate=_validate_v1_methods_routed_through_helper,
        ),
    ]


__all__ = [
    "CallerTopology",
    "ProviderTopology",
    "RouteTopology",
    "RouteDiff",
    "catalog_authoritative_enabled",
    "compute_yaml_diff",
    "get_topology",
    "reload_topology",
    "register_shipped_invariants",
    "set_dynamic_catalog",
    "get_dynamic_catalog",
    "clear_dynamic_catalog",
]
