"""Pricing surface — read-only composition of existing JARVIS price config.

Per Slice Aegis-1 binding correction #4: do NOT duplicate the pricing
table. Compose what already exists.

Resolution order (first hit wins) for ``cost_per_token_usd(route, model)``:

  1. ``brain_selection_policy.yaml`` ``s2_pricing.routes.<route>.<model>``
     — the authoritative per-route per-model table maintained by §11.
  2. ``brain_selection_policy.yaml`` ``s2_pricing.default_per_token_usd``
     — conservative fallback for unknown route/model combos.
  3. Per-provider env vars already in use by the existing providers
     (``DOUBLEWORD_INPUT_COST_PER_M`` / ``DOUBLEWORD_OUTPUT_COST_PER_M``
     for Qwen models; ``providers.py`` constants for Claude). Composed
     via known-model substring match — no new env vocabulary introduced.
  4. Floor defaults — only used if the yaml is missing AND env is unset.
     Strictly conservative (overestimates so the budget cap fires
     earlier, not later).

This module:
  - Never writes anywhere.
  - Never raises (lookup failure → floor defaults with a DEBUG log).
  - Lazy-loads the yaml on first call; caches the parsed dict per
    process.
  - Is async-safe via a simple ``asyncio.Lock`` around the first-load
    race window.

The pricing data is denominated in **USD per token** to align with the
yaml schema (NOT USD per 1M tokens — caller does no arithmetic).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


PRICING_SCHEMA_VERSION: str = "aegis_pricing.1"

# Conservative floor defaults — used only when yaml + env both unavailable.
# These are the absolute fallback so Aegis never silently treats a call
# as "free" — better to over-account than under-account.
_FLOOR_INPUT_USD_PER_TOKEN: float = 3.0e-6   # $3 / M (Claude Sonnet)
_FLOOR_OUTPUT_USD_PER_TOKEN: float = 1.5e-5  # $15 / M (Claude Sonnet)

# Default path to the policy yaml — composes the well-known location.
# Override via env for sandboxed envs / test isolation.
_DEFAULT_POLICY_YAML_REL: str = "backend/core/ouroboros/governance/brain_selection_policy.yaml"

ENV_AEGIS_POLICY_YAML_PATH: str = "JARVIS_AEGIS_POLICY_YAML_PATH"


@dataclass(frozen=True)
class TokenPrice:
    """USD-per-token pair. Frozen, hashable."""

    input_usd_per_token: float
    output_usd_per_token: float

    def cost_for(self, *, input_tokens: int, output_tokens: int) -> float:
        return (
            float(input_tokens) * self.input_usd_per_token
            + float(output_tokens) * self.output_usd_per_token
        )


def _floor_price() -> TokenPrice:
    return TokenPrice(
        input_usd_per_token=_FLOOR_INPUT_USD_PER_TOKEN,
        output_usd_per_token=_FLOOR_OUTPUT_USD_PER_TOKEN,
    )


# ---------------------------------------------------------------------------
# Env-fallback table — composes existing JARVIS env conventions.
# Each entry is (model_substring → (input_env, output_env, divisor)).
# divisor is 1_000_000 because the existing env vars are USD per MILLION
# tokens (matches DOUBLEWORD_INPUT_COST_PER_M, etc.).
# ---------------------------------------------------------------------------


_ENV_FALLBACK_TABLE: Tuple[Tuple[str, str, str, float], ...] = (
    # (model_substring, input_env, output_env, divisor)
    ("qwen", "DOUBLEWORD_INPUT_COST_PER_M", "DOUBLEWORD_OUTPUT_COST_PER_M", 1_000_000.0),
    ("doubleword", "DOUBLEWORD_INPUT_COST_PER_M", "DOUBLEWORD_OUTPUT_COST_PER_M", 1_000_000.0),
)


def _env_fallback_price(model: str) -> Optional[TokenPrice]:
    model_lower = model.lower()
    for substr, in_env, out_env, divisor in _ENV_FALLBACK_TABLE:
        if substr in model_lower:
            in_raw = os.environ.get(in_env, "").strip()
            out_raw = os.environ.get(out_env, "").strip()
            if not in_raw or not out_raw:
                continue
            try:
                in_usd = float(in_raw) / divisor
                out_usd = float(out_raw) / divisor
            except (TypeError, ValueError):
                continue
            return TokenPrice(in_usd, out_usd)
    return None


# ---------------------------------------------------------------------------
# Lazy yaml loader
# ---------------------------------------------------------------------------


_yaml_cache: Optional[Dict[str, Any]] = None
_yaml_cache_lock = asyncio.Lock()
_yaml_load_attempted: bool = False


def _resolve_policy_yaml_path() -> Path:
    raw = os.environ.get(ENV_AEGIS_POLICY_YAML_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    # Compose project-root convention.
    return Path(_DEFAULT_POLICY_YAML_REL)


def _load_yaml_sync(path: Path) -> Optional[Dict[str, Any]]:
    """Load the policy yaml synchronously. Returns None on any failure.

    Uses yaml.safe_load (no arbitrary object instantiation). PyYAML is
    already a project dep (used widely by the harness).
    """
    if not path.exists():
        logger.debug("[AegisPricing] policy yaml not found at %s", path)
        return None
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("[AegisPricing] PyYAML not available; env fallback only")
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            logger.debug("[AegisPricing] yaml is not a dict; ignoring")
            return None
        return data
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[AegisPricing] yaml load failed: %s", exc)
        return None


async def _ensure_yaml_loaded() -> Optional[Dict[str, Any]]:
    """Lazy-load + cache the policy yaml. Async-safe."""
    global _yaml_cache, _yaml_load_attempted
    if _yaml_load_attempted:
        return _yaml_cache
    async with _yaml_cache_lock:
        if _yaml_load_attempted:
            return _yaml_cache
        # The actual file read is sync I/O; offload so the event loop
        # is never blocked by a slow disk.
        _yaml_cache = await asyncio.to_thread(
            _load_yaml_sync, _resolve_policy_yaml_path(),
        )
        _yaml_load_attempted = True
    return _yaml_cache


def reset_cache_for_tests() -> None:
    """Test isolation helper. Drops the yaml cache + retry flag so each
    test starts fresh."""
    global _yaml_cache, _yaml_load_attempted
    _yaml_cache = None
    _yaml_load_attempted = False


# ---------------------------------------------------------------------------
# Public lookup surface
# ---------------------------------------------------------------------------


def _resolve_from_yaml(
    data: Dict[str, Any], *, route: str, model: str,
) -> Optional[TokenPrice]:
    """Walk s2_pricing.routes.<route>.<model>; fall back to
    s2_pricing.default_per_token_usd if route/model missing."""
    s2 = data.get("s2_pricing")
    if not isinstance(s2, dict):
        return None
    routes = s2.get("routes")
    route_key = route.lower()
    if isinstance(routes, dict):
        route_block = routes.get(route_key)
        if isinstance(route_block, dict):
            model_block = route_block.get(model)
            if isinstance(model_block, dict):
                try:
                    return TokenPrice(
                        input_usd_per_token=float(model_block["input"]),
                        output_usd_per_token=float(model_block["output"]),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
    # Fallback inside the yaml: default_per_token_usd
    default = s2.get("default_per_token_usd")
    if isinstance(default, dict):
        try:
            return TokenPrice(
                input_usd_per_token=float(default["input"]),
                output_usd_per_token=float(default["output"]),
            )
        except (KeyError, TypeError, ValueError):
            pass
    return None


async def cost_per_token_usd(*, route: str, model: str) -> TokenPrice:
    """Look up per-token price (USD) for ``(route, model)``.

    Async because we lazy-load the yaml the first time. Subsequent
    calls hit the cache and return synchronously (still wrapped in
    awaitable for caller-API symmetry).

    Never raises. On total miss (yaml absent + env unset), returns
    the floor defaults so the budget machinery is always working with
    SOMETHING (over-accounting is safer than under-accounting).
    """
    data = await _ensure_yaml_loaded()
    if data is not None:
        price = _resolve_from_yaml(data, route=route, model=model)
        if price is not None:
            return price

    env_price = _env_fallback_price(model)
    if env_price is not None:
        return env_price

    logger.debug(
        "[AegisPricing] no price found for route=%s model=%s; using floor",
        route, model,
    )
    return _floor_price()


def cost_per_token_usd_sync(*, route: str, model: str) -> TokenPrice:
    """Sync convenience that uses cached yaml only (no first-load).

    If the yaml has not been pre-loaded by an earlier
    ``cost_per_token_usd`` call, this falls through env → floor. Use
    inside hot streaming loops where you've already warmed the cache
    at endpoint setup."""
    if _yaml_cache is not None:
        price = _resolve_from_yaml(_yaml_cache, route=route, model=model)
        if price is not None:
            return price
    env_price = _env_fallback_price(model)
    if env_price is not None:
        return env_price
    return _floor_price()


__all__ = [
    "ENV_AEGIS_POLICY_YAML_PATH",
    "PRICING_SCHEMA_VERSION",
    "TokenPrice",
    "cost_per_token_usd",
    "cost_per_token_usd_sync",
    "reset_cache_for_tests",
]
