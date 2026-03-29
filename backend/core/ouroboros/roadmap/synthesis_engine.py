"""
FeatureSynthesisEngine — Clock 2
==================================

Runs Tier 0 deterministic gap hints (and, in future, Doubleword 397B model
synthesis) against the current :class:`RoadmapSnapshot`.

Key properties
--------------
- **Single-flight guard**: if a synthesis run is already in progress the
  second caller immediately returns the last cached result rather than
  scheduling a parallel run.
- **Min-interval gating**: after a successful run, further runs are skipped
  until ``config.min_interval_s`` seconds have elapsed (unless
  ``force=True``).
- **Input fingerprint**: synthesis is keyed on a SHA-256 fingerprint of
  ``(snapshot_hash, prompt_version, model_id)`` so identical inputs hit
  the cache even across process restarts.
- **Deduplication**: hypotheses from different sources are merged by
  ``hypothesis_fingerprint``; deterministic provenance wins over model
  provenance when fingerprints collide.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot
from backend.core.ouroboros.roadmap.tier0_hints import generate_tier0_hints

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def compute_input_fingerprint(
    snapshot_hash: str,
    prompt_version: int,
    model_id: str,
) -> str:
    """Return the SHA-256 hex digest of ``snapshot_hash\\tprompt_version\\tmodel_id``.

    The full 64-character hex string is returned so that callers can truncate
    or use it as-is depending on their storage requirements.
    """
    payload = f"{snapshot_hash}\t{prompt_version}\t{model_id}"
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SynthesisConfig:
    """Tunable parameters for :class:`FeatureSynthesisEngine`.

    Attributes
    ----------
    min_interval_s:
        Minimum number of seconds between full synthesis runs.
        Defaults to 21600 (6 hours).
    ttl_s:
        Cache TTL in seconds.  Hypotheses older than this are treated as
        stale by :meth:`HypothesisCache.is_stale`.
        Defaults to 86400 (24 hours).
    prompt_version:
        Integer version of the synthesis prompt.  Incrementing this
        invalidates all cached results keyed against older versions.
    model_id:
        Logical model identifier used in the input fingerprint.
        Defaults to ``"doubleword-397b"``.
    """

    min_interval_s: float = 21600.0
    ttl_s: float = 86400.0
    prompt_version: int = 1
    model_id: str = "doubleword-397b"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class FeatureSynthesisEngine:
    """Clock 2: synthesise :class:`FeatureHypothesis` objects from a snapshot.

    Parameters
    ----------
    oracle:
        Object exposing ``find_nodes_by_name(name, fuzzy) -> List`` used
        by Tier 0 hint generation.  Pass ``None`` to disable Tier 0 (empty
        output).
    doubleword:
        Doubleword 397B client (reserved for future v2 integration).
        Currently unused; pass ``None``.
    cache:
        :class:`HypothesisCache` instance for persistent storage.
    config:
        :class:`SynthesisConfig` tuning knobs.
    """

    def __init__(
        self,
        oracle: Optional[Any],
        doubleword: Optional[Any],
        cache: HypothesisCache,
        config: SynthesisConfig,
        narrator: Optional[Any] = None,
    ) -> None:
        self._oracle = oracle
        self._doubleword = doubleword
        self._cache = cache
        self._config = config
        self._narrator = narrator

        self._synthesis_lock: asyncio.Lock = asyncio.Lock()
        self._last_synthesis_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        snapshot: RoadmapSnapshot,
        *,
        force: bool = False,
    ) -> List[FeatureHypothesis]:
        """Return the current hypothesis list for *snapshot*.

        Resolution order
        ----------------
        1. Cache hit on ``input_fingerprint`` → return immediately.
        2. Lock held (synthesis in flight) → return ``cache.load()``.
        3. Within ``min_interval_s`` and not forced → return ``cache.load()``.
        4. Acquire lock, run :meth:`_run_synthesis`, return result.

        Parameters
        ----------
        snapshot:
            The roadmap snapshot to synthesise against.
        force:
            When ``True``, bypass the min-interval gate and always run
            fresh synthesis (cache-hit check is still applied).
        """
        fingerprint = compute_input_fingerprint(
            snapshot.content_hash,
            self._config.prompt_version,
            self._config.model_id,
        )

        # 1. Exact cache hit — no work needed.
        cached = self._cache.get_if_valid(fingerprint)
        if cached is not None:
            logger.debug(
                "FeatureSynthesisEngine: cache hit (fingerprint=%s…)", fingerprint[:12]
            )
            return cached

        # 2. Single-flight guard — if a run is already in progress, bail out.
        if self._synthesis_lock.locked():
            logger.debug(
                "FeatureSynthesisEngine: synthesis in flight — returning stale cache"
            )
            return self._cache.load()

        # 3. Min-interval cooldown.
        if not force:
            elapsed = time.monotonic() - self._last_synthesis_at
            if self._last_synthesis_at > 0.0 and elapsed < self._config.min_interval_s:
                logger.debug(
                    "FeatureSynthesisEngine: min_interval not elapsed "
                    "(%.1fs remaining) — returning cached",
                    self._config.min_interval_s - elapsed,
                )
                return self._cache.load()

        # 4. Acquire lock and run full synthesis.
        async with self._synthesis_lock:
            return await self._run_synthesis(snapshot, fingerprint)

    async def trigger(self, snapshot: RoadmapSnapshot) -> None:
        """Fire-and-forget synthesis triggered by a snapshot change.

        Errors are logged but not re-raised so that the calling sensor
        remains stable even if synthesis fails.
        """
        try:
            await self.synthesize(snapshot)
        except Exception:
            logger.exception("FeatureSynthesisEngine.trigger: synthesis failed")

    def health(self) -> dict:
        """Return a snapshot of engine health for diagnostics."""
        return {
            "last_synthesis_at": self._last_synthesis_at,
            "lock_held": self._synthesis_lock.locked(),
            "config": dataclasses.asdict(self._config),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_synthesis(
        self,
        snapshot: RoadmapSnapshot,
        fingerprint: str,
    ) -> List[FeatureHypothesis]:
        """Execute Tier 0 hint generation, dedup, persist, and return results.

        This method is always called while ``_synthesis_lock`` is held.

        v1 uses Tier 0 only.  Doubleword 397B integration is deferred to v2.
        """
        logger.info(
            "FeatureSynthesisEngine: running synthesis "
            "(snapshot_hash=%s…, fingerprint=%s…)",
            snapshot.content_hash[:12],
            fingerprint[:12],
        )

        # --- Tier 0: deterministic hints (zero model tokens) ---
        tier0: List[FeatureHypothesis] = generate_tier0_hints(snapshot, self._oracle)

        # --- Doubleword 397B synthesis ---
        model_hints: List[FeatureHypothesis] = []
        if self._doubleword is not None and hasattr(self._doubleword, "prompt_only"):
            try:
                model_hints = await self._run_doubleword(snapshot, tier0)
            except Exception as exc:
                logger.warning("FeatureSynthesisEngine: Doubleword failed: %s", exc)

        # --- Merge & dedup by hypothesis_fingerprint ---
        # Deterministic provenance wins over model provenance on collision.
        merged: List[FeatureHypothesis] = _dedup_hypotheses(tier0, model_hints)

        logger.info(
            "FeatureSynthesisEngine: produced %d hypotheses "
            "(%d from tier0, %d from model)",
            len(merged),
            len(tier0),
            len(model_hints),
        )

        # --- Persist ---
        self._cache.save(
            merged,
            input_fingerprint=fingerprint,
            snapshot_hash=snapshot.content_hash,
        )
        self._last_synthesis_at = time.monotonic()

        if self._narrator is not None:
            await self._narrator.on_event("synthesis.complete", {"hypothesis_count": len(merged)})

        # Emit to TelemetryBus for TUI panel (best-effort)
        try:
            from backend.core.telemetry_contract import get_telemetry_bus, TelemetryEnvelope
            bus = get_telemetry_bus()
            bus.emit(TelemetryEnvelope.create(
                event_schema="ouroboros.synthesis.complete@1.0.0",
                source="synthesis_engine",
                trace_id=f"synthesis-{fingerprint[:12]}",
                span_id=f"synthesis-complete-{fingerprint[:12]}",
                partition_key="ouroboros",
                payload={"hypothesis_count": len(merged)},
            ))
        except Exception:
            pass  # TUI telemetry is best-effort

        return merged

    async def _run_doubleword(
        self,
        snapshot: RoadmapSnapshot,
        tier0_hints: List[FeatureHypothesis],
    ) -> List[FeatureHypothesis]:
        """Call Doubleword 397B for model-driven gap synthesis.

        Builds the synthesis prompt, trims it to a 6000-token budget via
        :func:`shed_context`, submits it to Doubleword via ``prompt_only()``,
        and parses the JSON response into :class:`FeatureHypothesis` objects.

        Returns an empty list when the context budget is exceeded or the
        response cannot be parsed — the caller logs a warning and falls back
        to Tier 0 only.
        """
        from backend.core.ouroboros.roadmap.synthesis_prompt import (
            build_synthesis_prompt,
            shed_context,
            SYNTHESIS_JSON_SCHEMA,
            ContextBudgetExceededError,
        )

        prompt = build_synthesis_prompt(snapshot, tier0_hints, oracle_summary="")
        try:
            prompt = shed_context(prompt, max_tokens=6000)
        except ContextBudgetExceededError:
            logger.warning(
                "FeatureSynthesisEngine: context budget exceeded, using tier0 only"
            )
            return []

        response = await self._doubleword.prompt_only(
            prompt=prompt,
            caller_id="synthesis_engine",
            response_format=SYNTHESIS_JSON_SCHEMA,
        )
        return self._parse_doubleword_response(response, snapshot)

    def _parse_doubleword_response(
        self,
        response: str,
        snapshot: RoadmapSnapshot,
    ) -> List[FeatureHypothesis]:
        """Parse a raw Doubleword JSON response into :class:`FeatureHypothesis` objects.

        Expects the response to conform to :data:`SYNTHESIS_JSON_SCHEMA`:
        ``{"gaps": [{description, evidence_fragments, gap_type, confidence,
        urgency, suggested_scope, suggested_repos}, ...]}``.

        Malformed items are skipped with a ``DEBUG`` log rather than raising,
        so a partially-valid response still contributes hypotheses.

        Parameters
        ----------
        response:
            Raw string returned by :meth:`~DoublewordProvider.prompt_only`.
        snapshot:
            The snapshot the hypotheses were synthesised against.

        Returns
        -------
        List[FeatureHypothesis]
            Parsed hypotheses with ``provenance="model:doubleword-397b"`` and
            ``confidence_rule_id="model_inference"``.
        """
        import json as _json

        if not response:
            logger.debug("FeatureSynthesisEngine: Doubleword returned empty response")
            return []

        try:
            data = _json.loads(response)
        except _json.JSONDecodeError as exc:
            logger.warning(
                "FeatureSynthesisEngine: Doubleword response is not valid JSON: %s", exc
            )
            return []

        gaps = data.get("gaps")
        if not isinstance(gaps, list):
            logger.warning(
                "FeatureSynthesisEngine: Doubleword response missing 'gaps' array"
            )
            return []

        fingerprint = compute_input_fingerprint(
            snapshot.content_hash,
            self._config.prompt_version,
            self._config.model_id,
        )

        hypotheses: List[FeatureHypothesis] = []
        for item in gaps:
            if not isinstance(item, dict):
                continue
            try:
                h = FeatureHypothesis.new(
                    description=str(item["description"]),
                    evidence_fragments=tuple(item.get("evidence_fragments", [])),
                    gap_type=str(item["gap_type"]),
                    confidence=float(item.get("confidence", 0.5)),
                    confidence_rule_id="model_inference",
                    urgency=str(item.get("urgency", "medium")),
                    suggested_scope=str(item.get("suggested_scope", "refactor")),
                    suggested_repos=tuple(item.get("suggested_repos", [])),
                    provenance="model:doubleword-397b",
                    synthesized_for_snapshot_hash=snapshot.content_hash,
                    synthesis_input_fingerprint=fingerprint,
                )
                hypotheses.append(h)
            except Exception as exc:
                logger.debug(
                    "FeatureSynthesisEngine: skipping malformed gap item: %s — %s",
                    item,
                    exc,
                )

        logger.info(
            "FeatureSynthesisEngine: Doubleword produced %d hypothesis(es)",
            len(hypotheses),
        )
        return hypotheses


# ---------------------------------------------------------------------------
# Deduplication helper
# ---------------------------------------------------------------------------

def _dedup_hypotheses(
    deterministic: List[FeatureHypothesis],
    model: List[FeatureHypothesis],
) -> List[FeatureHypothesis]:
    """Merge two hypothesis lists, deduplicating by ``hypothesis_fingerprint``.

    When the same logical hypothesis appears in both lists, the deterministic
    entry wins (its ``provenance="deterministic"`` is treated as ground truth).

    Within each list, the first occurrence of each fingerprint is kept so that
    the ordering returned by Tier 0 / the model is preserved.
    """
    seen: dict = {}

    # Deterministic hypotheses have priority — insert first.
    for h in deterministic:
        if h.hypothesis_fingerprint not in seen:
            seen[h.hypothesis_fingerprint] = h

    # Model hypotheses fill in gaps only.
    for h in model:
        if h.hypothesis_fingerprint not in seen:
            seen[h.hypothesis_fingerprint] = h

    return list(seen.values())
