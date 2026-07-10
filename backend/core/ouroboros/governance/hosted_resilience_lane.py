"""Hosted resilience lane — policy-driven cheap-vendor fallback (Phase 1).

Spec: docs/superpowers/backlog/2026-07-10-longcat-resilience-stub.md

BACKGROUND/SPECULATIVE routes have NO fallback today: a real DW outage kills
them (or, worse, ignites the J-Prime GCE failover). This module gives the
generator a *generic* policy-resolved lane it can consult after Tier-0
exhaustion — the generator never matches on a vendor name, only on the
``hosted_provider_candidates.*.resilience_lane`` shape in
``brain_selection_policy.yaml`` (Mandate 2: no vendor strings in FSM loops).

Design decisions (the four mandates):

* **Root-cause / purity** — the lane provider is a real ``ClaudeProvider``
  whose ``base_url`` is resolved from policy at the instantiation boundary
  (the new constructor param threads to the canonical
  ``aegis_provider_bridge.make_async_anthropic_client`` factory). No URL
  string surgery anywhere.
* **DRY** — because the lane IS a ``ClaudeProvider``, it inherits the
  session-budget preflight (``SessionBudgetPreflightRefused`` raised inside
  ``generate()`` before any network dispatch), so lane budget exhaustion
  flows through the exact Slice 4 T2 ``is_budget_refusal`` axis with zero
  duplicated state-checking. Serialization, breaker, backoff: all inherited.
* **Bulletproof** — every entry point is fail-soft: a missing key, unpassed
  Phase 0 gate, active Aegis, unparseable policy, or provider-construction
  error DISARMS the lane (reason string, logged once) and the caller falls
  through to legacy behavior. Nothing here can raise into generator init.

DOUBLE-DARK by default: the policy entry ships ``enabled: false`` AND the
master env (named IN policy, default false) must be set. The Phase 0 verdict
artifact must read the required verdict before the lane will arm — an
unverified dialect never receives production traffic.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).parent / "brain_selection_policy.yaml"
# Repo root for resolving the (repo-relative) phase0 verdict artifact path.
_REPO_ROOT = Path(__file__).resolve().parents[4]

_TRUTHY = ("true", "1", "yes", "on")


@dataclass
class LaneConfig:
    """One hosted candidate's resilience-lane shape, parsed from policy."""

    name: str
    dialect: str
    endpoint: str
    api_key_env: str
    lane_model: str
    routes: Tuple[str, ...]
    policy_enabled: bool
    master_env: str
    verdict_artifact: str
    required_verdict: str
    pricing_per_mtok: Dict[str, float] = field(default_factory=dict)


def load_lane_configs(policy_path: Optional[Path] = None) -> List[LaneConfig]:
    """Parse every ``hosted_provider_candidates.*`` entry that declares a
    ``resilience_lane`` block. Generic — no candidate names appear in code.

    NEVER raises: an unreadable/unparseable policy returns ``[]`` (the lane
    simply stays dark — Mandate 4)."""
    try:
        import yaml

        policy = yaml.safe_load(
            (policy_path or _POLICY_PATH).read_text(encoding="utf-8")
        )
        candidates = (policy or {}).get("hosted_provider_candidates") or {}
    except Exception as exc:  # noqa: BLE001 — fail-dark, never fail-boot
        logger.warning(
            "[ResilienceLane] policy unreadable (%s) — lane stays dark", exc,
        )
        return []

    configs: List[LaneConfig] = []
    for name, cand in candidates.items():
        try:
            lane = cand.get("resilience_lane")
            if not lane:
                continue
            models = {
                m.get("model_name"): m for m in (cand.get("models") or [])
            }
            lane_model = lane.get("lane_model", "")
            configs.append(LaneConfig(
                name=str(name),
                dialect=str(cand.get("dialect", "")).strip().lower(),
                endpoint=str(cand.get("endpoint", "")).strip().rstrip("/"),
                api_key_env=str(cand.get("api_key_env", "")).strip(),
                lane_model=str(lane_model),
                routes=tuple(
                    str(r).strip().lower() for r in (lane.get("routes") or [])
                ),
                policy_enabled=bool(lane.get("enabled", False)),
                master_env=str(lane.get("master_env", "")).strip(),
                verdict_artifact=str(lane.get("phase0_verdict_artifact", "")),
                required_verdict=str(lane.get("required_verdict", "")),
                pricing_per_mtok=dict(
                    (models.get(lane_model) or {}).get("pricing_per_mtok")
                    or {}
                ),
            ))
        except Exception as exc:  # noqa: BLE001 — one bad entry ≠ dark lane
            logger.warning(
                "[ResilienceLane] candidate %r unparseable (%s) — skipped",
                name, exc,
            )
    return configs


def preflight(cfg: LaneConfig, *,
              repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    """Arm/disarm decision for one lane. Returns ``(armed, reason)`` and
    NEVER raises (Mandate 4). Gate order — cheapest first, each reason is a
    distinct telemetry class:

    1. master env (named in policy, default false)
    2. policy ``enabled`` flag (double-dark)
    3. dialect must be ``anthropic`` — the only dialect the ClaudeProvider
       seam speaks; openai-dialect candidates are a Path B / DW-seam build
    4. Aegis must be inactive (factory would swap the api_key for the
       daemon placeholder → guaranteed auth-fail at the vendor)
    5. credential present at the policy-named env var
    6. Phase 0 contract-validation artifact carries the required verdict
    """
    if not cfg.master_env or os.environ.get(
        cfg.master_env, "",
    ).strip().lower() not in _TRUTHY:
        return False, "master_env_off"
    if not cfg.policy_enabled:
        return False, "policy_disabled"
    if not cfg.routes:
        return False, "no_routes_declared"
    if cfg.dialect != "anthropic":
        return False, f"dialect_unsupported:{cfg.dialect or '?'}:pivot_path_b"
    if not cfg.endpoint:
        return False, "no_endpoint"
    try:
        from backend.core.ouroboros.aegis import client as _aegis_client
        if _aegis_client.is_enabled():
            return False, "aegis_active_would_placeholder_key"
    except Exception:  # noqa: BLE001 — no aegis module = no aegis hazard
        pass
    if not cfg.api_key_env or not os.environ.get(
        cfg.api_key_env, "",
    ).strip():
        return False, f"no_credentials:{cfg.api_key_env or '?'}"
    verdict_ok, verdict_reason = _phase0_verdict_ok(cfg, repo_root=repo_root)
    if not verdict_ok:
        return False, verdict_reason
    return True, "armed"


def _phase0_verdict_ok(cfg: LaneConfig, *,
                       repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    """The Mandate 4 contract-validation gate: the live Phase 0 probe must
    have stamped the required verdict. Missing/stale/failed artifact =
    disarmed, never an exception."""
    if not cfg.verdict_artifact or not cfg.required_verdict:
        return False, "phase0_gate_unconfigured"
    try:
        import json

        path = Path(cfg.verdict_artifact)
        if not path.is_absolute():
            path = (repo_root or _REPO_ROOT) / path
        report = json.loads(path.read_text(encoding="utf-8"))
        verdict = str(report.get("verdict", ""))
        if verdict != cfg.required_verdict:
            return False, f"phase0_verdict:{verdict or 'absent'}"
        return True, "phase0_verified"
    except FileNotFoundError:
        return False, "phase0_artifact_missing"
    except Exception as exc:  # noqa: BLE001 — corrupt artifact = not passed
        return False, f"phase0_artifact_unreadable:{type(exc).__name__}"


class HostedResilienceLane:
    """Facade the generator consults after Tier-0 exhaustion.

    Caches per-route provider resolution; logs each disarm reason ONCE per
    (candidate, reason) so a dark lane costs one debug line, not log spam.
    Construction itself is cheap and cannot raise past __init__ (policy
    parse is fail-dark)."""

    def __init__(self, *, policy_path: Optional[Path] = None,
                 repo_root: Optional[Path] = None) -> None:
        self._configs = load_lane_configs(policy_path)
        self._repo_root = repo_root
        self._providers: Dict[str, Any] = {}
        self._logged_reasons: set = set()

    def provider_for_route(self, route: str) -> Tuple[Optional[Any], str]:
        """Resolve an ARMED provider for *route* (e.g. ``"background"``).

        Returns ``(provider, "armed:<name>")`` or ``(None, reason)``.
        Never raises. Preflight is re-evaluated per call (env/artifact state
        may change mid-session — e.g. an operator exporting the key); the
        constructed provider object itself is cached per candidate."""
        route = (route or "").strip().lower()
        for cfg in self._configs:
            if route not in cfg.routes:
                continue
            armed, reason = preflight(cfg, repo_root=self._repo_root)
            if not armed:
                self._log_once(cfg.name, reason)
                continue
            provider = self._providers.get(cfg.name)
            if provider is None:
                provider = self._build_provider(cfg)
                if provider is None:
                    continue
                self._providers[cfg.name] = provider
            return provider, f"armed:{cfg.name}"
        return None, "no_armed_lane"

    def _build_provider(self, cfg: LaneConfig) -> Optional[Any]:
        """Construct the lane's ClaudeProvider (Anthropic dialect, policy
        endpoint at the instantiation boundary). Fail-soft: any construction
        error disarms this candidate for the session (Mandate 4 — never an
        unhandled init exception into the caller)."""
        try:
            from backend.core.ouroboros.governance.providers import (
                ClaudeProvider,
            )
            provider = ClaudeProvider(
                api_key=os.environ.get(cfg.api_key_env, "").strip(),
                model=cfg.lane_model,
                base_url=cfg.endpoint,
                # BG/SPEC economics: modest per-op cap; env-tunable like
                # every other knob (defaults are deliberately tight — this
                # lane exists to be cheap).
                max_cost_per_op=float(os.environ.get(
                    "JARVIS_RESILIENCE_LANE_MAX_COST_PER_OP", "0.05",
                )),
                daily_budget=float(os.environ.get(
                    "JARVIS_RESILIENCE_LANE_DAILY_BUDGET", "1.00",
                )),
                # Venom tool loop stays OFF for BG/SPEC (matches the
                # existing route cost-optimization: those routes skip the
                # tool loop on every provider).
                tools_enabled=False,
            )
            logger.info(
                "[ResilienceLane] provider armed: candidate=%s model=%s "
                "endpoint=%s routes=%s (budget preflight + breaker + "
                "serialization inherited from ClaudeProvider)",
                cfg.name, cfg.lane_model, cfg.endpoint, list(cfg.routes),
            )
            return provider
        except Exception as exc:  # noqa: BLE001 — disarm, don't detonate
            self._log_once(cfg.name, f"provider_build_failed:{exc}")
            self._providers.pop(cfg.name, None)
            return None

    def _log_once(self, name: str, reason: str) -> None:
        key = f"{name}:{reason.split(':', 1)[0]}"
        if key not in self._logged_reasons:
            self._logged_reasons.add(key)
            logger.debug(
                "[ResilienceLane] candidate=%s disarmed (%s)", name, reason,
            )
