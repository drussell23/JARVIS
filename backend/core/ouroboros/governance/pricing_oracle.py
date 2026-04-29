"""Pricing Oracle — pattern-matched fallback pricing for DW catalog models.

Closes the Static Pricing Blindspot diagnosed in soak #6: when DW's
``/models`` response omits per-token pricing, the catalog classifier
flags the model as "ambiguous metadata" and SPECULATIVE-quarantines
it (per Zero-Trust §3.6). The result: every BG op gets blocked
because no model qualifies for the BG route — even though we know
the prices for the model families.

This module ships a registry of family-pattern → pricing entries.
When ``ModelCard.from_api_dict`` finds no pricing in the API
response, it consults this oracle. If a registered pattern matches
the model_id (case-insensitive fnmatch glob), the oracle returns
the fallback ``(price_in, price_out)`` tuple. Pricing data flows
into the ModelCard, ``has_ambiguous_metadata()`` returns False,
and the BG route admits the model.

Architecture
------------

  * ``PricingPattern`` — frozen registry value type
  * Pattern matching via ``fnmatch.fnmatch`` (glob-style; e.g.,
    ``"*qwen*3.5*397b*"`` matches any Qwen 3.5 397B variant)
  * Patterns are ordered: more-specific first (specific size hints),
    generic family fallbacks last. First-match-wins.
  * Per-model-id resolution cache so repeated lookups are O(1)
  * Operator-extensible at runtime (mirrors all prior priority
    registry patterns: A2, B1, C, E, F)

Master flag
-----------

``JARVIS_PRICING_ORACLE_ENABLED`` (default ``true``). When off,
``resolve_pricing`` returns ``None`` for every input — caller falls
back to the legacy "no pricing → ambiguous" path.

Hot-revert
----------

Single env knob — ``export JARVIS_PRICING_ORACLE_ENABLED=false``
returns the system to the pre-oracle blindspot.

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runner / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian.
  * Pure stdlib (fnmatch, threading, os, logging) only.
  * NEVER raises out of any public method.
  * Read-only — never modifies the underlying ModelCard or catalog.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


PRICING_ORACLE_SCHEMA_VERSION: str = "pricing_oracle.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def pricing_oracle_enabled() -> bool:
    """``JARVIS_PRICING_ORACLE_ENABLED`` (default ``true``).

    Asymmetric env semantics — empty/whitespace = unset marker =
    graduated default; explicit false-class hot-reverts."""
    raw = os.environ.get(
        "JARVIS_PRICING_ORACLE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# PricingPattern — registry value type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PricingPattern:
    """One family-pattern → pricing entry.

    Frozen + hashable for safe registry storage and replay-stability.

    Fields
    ------
    pattern_kind:
        Stable identifier for this pattern (e.g., ``"qwen_3_5_397b"``).
        Used for dedup + observability filtering.
    glob_pattern:
        fnmatch-style glob (case-insensitive). Matched against the
        model_id from DW's ``/models`` response. Examples:
          * ``"*qwen*397b*"`` — any Qwen 397B variant
          * ``"*deepseek*v3*"`` — any DeepSeek V3 variant
          * ``"*llama*3*70b*"`` — Llama 3 70B
        Specific patterns should register BEFORE generic family
        fallbacks (first-match-wins).
    pricing_in_per_m_usd:
        Input token cost in USD per million tokens. MUST be >= 0.
    pricing_out_per_m_usd:
        Output token cost in USD per million tokens. MUST be >= 0.
        Output cost is what the catalog classifier's BG-route gate
        compares against (default threshold $0.5/M).
    description:
        Human-readable explanation. Surfaced via ``/help pricing``
        (future).
    """

    pattern_kind: str
    glob_pattern: str
    pricing_in_per_m_usd: float
    pricing_out_per_m_usd: float
    description: str = ""
    schema_version: str = PRICING_ORACLE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Registry — ordered list (insertion order = priority)
# ---------------------------------------------------------------------------


_REGISTRY: List[PricingPattern] = []
_REGISTRY_LOCK = threading.RLock()

# Per-model-id resolution cache — first lookup matches a pattern,
# subsequent calls return cached (price_in, price_out) tuple in O(1).
# A None value means "no pattern matched"; we cache the negative
# result too so we don't re-walk the registry on every miss.
_RESOLUTION_CACHE: Dict[str, Optional[Tuple[float, float]]] = {}
_CACHE_LOCK = threading.RLock()


def register_pricing_pattern(
    pattern: PricingPattern, *, overwrite: bool = False,
) -> None:
    """Install a pricing pattern in the registry. NEVER raises.

    Registration order matters: more-specific patterns should be
    registered BEFORE generic family fallbacks. First-match-wins
    semantics mean later registrations are reached only when no
    earlier pattern matches the model_id.

    Idempotent on identical re-register. Rejects different-content
    re-register without ``overwrite=True``."""
    if not isinstance(pattern, PricingPattern):  # pyright: ignore[reportUnnecessaryIsInstance]
        return
    safe_kind = (
        str(pattern.pattern_kind).strip()
        if pattern.pattern_kind else ""
    )
    if not safe_kind:
        return
    if not pattern.glob_pattern:
        return
    try:
        in_p = float(pattern.pricing_in_per_m_usd)
        out_p = float(pattern.pricing_out_per_m_usd)
        if in_p < 0 or out_p < 0:
            return
    except (TypeError, ValueError):
        return
    with _REGISTRY_LOCK:
        # Find existing entry by pattern_kind
        for idx, existing in enumerate(_REGISTRY):
            if existing.pattern_kind == safe_kind:
                if existing == pattern:
                    return  # silent no-op on identical re-register
                if not overwrite:
                    logger.info(
                        "[PricingOracle] pattern %r already registered",
                        safe_kind,
                    )
                    return
                # Overwrite — replace in place to preserve order
                _REGISTRY[idx] = pattern
                _invalidate_cache()
                return
        # Not present — append
        _REGISTRY.append(pattern)
        _invalidate_cache()


def unregister_pricing_pattern(pattern_kind: str) -> bool:
    """Remove a pattern. Returns True if removed, False if not
    present. NEVER raises. Invalidates the resolution cache so
    future lookups don't return stale answers."""
    safe_kind = str(pattern_kind).strip() if pattern_kind else ""
    if not safe_kind:
        return False
    with _REGISTRY_LOCK:
        before = len(_REGISTRY)
        _REGISTRY[:] = [
            p for p in _REGISTRY
            if p.pattern_kind != safe_kind
        ]
        removed = len(_REGISTRY) < before
    if removed:
        _invalidate_cache()
    return removed


def list_pricing_patterns() -> Tuple[PricingPattern, ...]:
    """Return all patterns in registration order. NEVER raises."""
    with _REGISTRY_LOCK:
        return tuple(_REGISTRY)


def reset_for_tests() -> None:
    """Test isolation — clear the registry + cache, then re-seed.
    NEVER raises."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _invalidate_cache()
    _register_seed_patterns()


def _invalidate_cache() -> None:
    """Drop the per-model-id resolution cache. Called on any
    registry mutation so subsequent lookups respect the new state."""
    with _CACHE_LOCK:
        _RESOLUTION_CACHE.clear()


# ---------------------------------------------------------------------------
# Resolver — the public entry point ModelCard consults
# ---------------------------------------------------------------------------


def resolve_pricing(
    model_id: str,
) -> Optional[Tuple[float, float]]:
    """Return ``(pricing_in_per_m_usd, pricing_out_per_m_usd)`` for
    ``model_id`` if any registered pattern matches; else ``None``.

    Match semantics:
      * fnmatch glob (case-insensitive)
      * Walks registered patterns in registration order
      * First match wins
      * Result cached per model_id (positive AND negative results)

    NEVER raises. Master-flag-gated: when off, returns ``None``
    immediately — caller's legacy fallback path runs."""
    if not pricing_oracle_enabled():
        return None
    if not model_id or not isinstance(model_id, str):
        return None
    safe_id = model_id.strip()
    if not safe_id:
        return None

    # Cache hit fast-path
    with _CACHE_LOCK:
        if safe_id in _RESOLUTION_CACHE:
            return _RESOLUTION_CACHE[safe_id]

    # Cache miss — walk the registry
    safe_id_lower = safe_id.lower()
    matched: Optional[Tuple[float, float]] = None
    matched_kind: str = ""
    try:
        with _REGISTRY_LOCK:
            patterns_snapshot = list(_REGISTRY)
        for pattern in patterns_snapshot:
            try:
                if fnmatch.fnmatch(
                    safe_id_lower,
                    pattern.glob_pattern.lower(),
                ):
                    matched = (
                        float(pattern.pricing_in_per_m_usd),
                        float(pattern.pricing_out_per_m_usd),
                    )
                    matched_kind = pattern.pattern_kind
                    break
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001 — defensive
        matched = None

    # Cache the result (positive OR negative)
    with _CACHE_LOCK:
        _RESOLUTION_CACHE[safe_id] = matched

    if matched is not None:
        logger.debug(
            "[PricingOracle] resolved model_id=%s pattern=%s "
            "in=$%.4f/M out=$%.4f/M",
            safe_id, matched_kind, matched[0], matched[1],
        )
    return matched


def cache_size() -> int:
    """Return the number of cached resolutions (positive + negative
    combined). Useful for test pins."""
    with _CACHE_LOCK:
        return len(_RESOLUTION_CACHE)


# ---------------------------------------------------------------------------
# Seed patterns
# ---------------------------------------------------------------------------
#
# Operator extensibility — register additional patterns at runtime
# from operator modules. The seed set covers the families currently
# routed via DW + the closest open-weight peers we might add.
#
# Pricing sources (verified circa 2026-04-29):
#   * Qwen 3.5 series — DoubleWord catalog + DeepInfra
#   * DeepSeek V3 — DeepSeek API
#   * Llama 3 70B — Together / DeepInfra mid-tier
#   * GPT-OSS — OpenAI open-weight pricing
#
# Order matters — specific size hints (e.g., 397B) MUST register
# BEFORE generic family fallbacks (e.g., qwen-*) so first-match-wins
# semantics route the right price.


def _register_seed_patterns() -> None:
    """Module-load: register the seed pricing patterns."""
    # ---- Qwen 3.5 family (specific → generic) ----
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="qwen_3_5_397b",
            glob_pattern="*qwen*3.5*397b*",
            pricing_in_per_m_usd=0.10,
            pricing_out_per_m_usd=0.40,
            description=(
                "Qwen 3.5 397B-A17B (MoE) — DoubleWord-published "
                "$0.10 in / $0.40 out per million tokens"
            ),
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="qwen_3_5_72b",
            glob_pattern="*qwen*3.5*72b*",
            pricing_in_per_m_usd=0.30,
            pricing_out_per_m_usd=0.90,
            description="Qwen 3.5 72B dense",
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="qwen_3_5_27b",
            glob_pattern="*qwen*3.5*27b*",
            pricing_in_per_m_usd=0.10,
            pricing_out_per_m_usd=0.30,
            description="Qwen 3.5 27B dense",
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="qwen_generic",
            glob_pattern="*qwen*",
            pricing_in_per_m_usd=0.20,
            pricing_out_per_m_usd=0.60,
            description=(
                "Generic Qwen fallback — applied when a more-specific "
                "Qwen pattern doesn't match. Conservative mid-range "
                "estimate to admit to BG/STANDARD without false-pass."
            ),
        ),
    )
    # ---- DeepSeek family (specific → generic) ----
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="deepseek_v4",
            glob_pattern="*deepseek*v4*",
            pricing_in_per_m_usd=0.27,
            pricing_out_per_m_usd=1.10,
            description="DeepSeek V4 estimated mid-2026 pricing",
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="deepseek_v3",
            glob_pattern="*deepseek*v3*",
            pricing_in_per_m_usd=0.27,
            pricing_out_per_m_usd=1.10,
            description="DeepSeek V3 published API pricing",
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="deepseek_generic",
            glob_pattern="*deepseek*",
            pricing_in_per_m_usd=0.50,
            pricing_out_per_m_usd=1.50,
            description="Generic DeepSeek fallback",
        ),
    )
    # ---- Llama 3 family (size-aware) ----
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="llama_3_70b",
            glob_pattern="*llama*3*70b*",
            pricing_in_per_m_usd=0.59,
            pricing_out_per_m_usd=0.79,
            description="Llama 3 70B (Together / DeepInfra mid-tier)",
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="llama_3_8b",
            glob_pattern="*llama*3*8b*",
            pricing_in_per_m_usd=0.07,
            pricing_out_per_m_usd=0.07,
            description="Llama 3 8B (cheap dense)",
        ),
    )
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="llama_generic",
            glob_pattern="*llama*",
            pricing_in_per_m_usd=0.60,
            pricing_out_per_m_usd=0.80,
            description="Generic Llama fallback",
        ),
    )
    # ---- GPT-OSS family ----
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="gpt_oss",
            glob_pattern="*gpt-oss*",
            pricing_in_per_m_usd=0.10,
            pricing_out_per_m_usd=0.40,
            description=(
                "GPT-OSS (OpenAI open-weight) — observed mid-2026 "
                "pricing on multi-tenant providers"
            ),
        ),
    )
    # ---- Mistral family ----
    register_pricing_pattern(
        PricingPattern(
            pattern_kind="mistral_generic",
            glob_pattern="*mistral*",
            pricing_in_per_m_usd=0.50,
            pricing_out_per_m_usd=1.50,
            description="Generic Mistral fallback",
        ),
    )


_register_seed_patterns()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "PRICING_ORACLE_SCHEMA_VERSION",
    "PricingPattern",
    "cache_size",
    "list_pricing_patterns",
    "pricing_oracle_enabled",
    "register_pricing_pattern",
    "reset_for_tests",
    "resolve_pricing",
    "unregister_pricing_pattern",
]
