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
        """
        if not self.enabled:
            return True
        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
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
        entry = self.routes.get((route or "").strip().lower())
        if entry is None:
            return ()
        return entry.effective_dw_models

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


_EMPTY_TOPOLOGY = ProviderTopology(enabled=False)


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


__all__ = [
    "CallerTopology",
    "ProviderTopology",
    "RouteTopology",
    "get_topology",
    "reload_topology",
]
