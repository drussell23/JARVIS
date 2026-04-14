"""
provider_topology — Hard-segmented DoubleWord model mapping (Manifesto §5).

Loads the ``doubleword_topology`` section of ``brain_selection_policy.yaml``
and exposes a deterministic, zero-LLM API for resolving:

  * Which DW model (if any) should handle a given :class:`ProviderRoute`
  * Whether DW is permitted on that route at all
  * Which DW model named callers (semantic_triage, ouroboros_plan) should use

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

Contract
--------
This module is pure and side-effect-free after import. The yaml is
loaded once (cached) and the resolver is called from hot paths. A
missing or malformed ``doubleword_topology`` section is treated as
"topology disabled" — callers fall back to their previous behavior.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


_POLICY_FILENAME = "brain_selection_policy.yaml"


@dataclass(frozen=True)
class RouteTopology:
    """Per-route topology entry.

    ``dw_allowed=False`` is a hard block — callers MUST NOT attempt any
    DoubleWord call for operations on this route. ``dw_model`` is the
    model identifier passed to the DoubleWord API when DW is allowed.
    """

    dw_allowed: bool
    dw_model: Optional[str]
    reason: str


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
    """

    enabled: bool
    routes: Mapping[str, RouteTopology] = field(default_factory=dict)
    callers: Mapping[str, CallerTopology] = field(default_factory=dict)

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


def _parse_topology(raw: Mapping[str, Any]) -> ProviderTopology:
    section = raw.get("doubleword_topology")
    if not isinstance(section, Mapping):
        return _EMPTY_TOPOLOGY

    enabled = bool(section.get("enabled", False))
    if not enabled:
        return _EMPTY_TOPOLOGY

    routes: Dict[str, RouteTopology] = {}
    raw_routes = section.get("routes")
    if isinstance(raw_routes, Mapping):
        for name, body in raw_routes.items():
            if not isinstance(body, Mapping):
                continue
            allowed = bool(body.get("dw_allowed", True))
            model = body.get("dw_model") if allowed else None
            reason = str(body.get("reason", "") or "")
            routes[str(name).strip().lower()] = RouteTopology(
                dw_allowed=allowed,
                dw_model=str(model) if model else None,
                reason=reason,
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

    return ProviderTopology(
        enabled=True,
        routes=routes,
        callers=callers,
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
