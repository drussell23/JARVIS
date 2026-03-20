"""
Tiered VisionRouter — L1 scene graph → L2 J-Prime GPU → L3 Claude Vision.

Routing strategy
----------------
1. L1 (scene graph): synchronous dict lookup in KnowledgeFabric (<5 ms).
   Match criteria: ``text_content`` contains ``target_text`` (case-insensitive),
   or nearest-element lookup when ``coords_hint`` is provided.
2. L2 (J-Prime GPU): async call to the on-premise LLaVA endpoint via
   ``_call_jprime_vision()``.  Fully mockable for tests and Tasks 7/8.
3. L3 (Claude Vision): async paid-API fallback via ``_call_claude_vision()``.
   Also fully mockable.
4. DEGRADED: both remote tiers unreachable → return empty result with
   ``tier=VisionTier.DEGRADED``.

Scene-graph updates
-------------------
Every L2 or L3 hit writes the resolved element back to the scene partition
so the next identical query is served from L1 at no cost.

Operational level
-----------------
Tracked independently from MindClient:
  0 — healthy (L2 reachable)
  1 — L2 down (falling through to L3)
  2 — all down (DEGRADED path active)

Consecutive-failure thresholds are driven by env vars; no hardcoding.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

from backend.knowledge.fabric import KnowledgeFabric

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven tuning — zero hardcoding
# ---------------------------------------------------------------------------
_L2_FAILURE_THRESHOLD = int(os.environ.get("VISION_L2_FAILURE_THRESHOLD", "3"))
_L1_TTL_SECONDS = float(os.environ.get("VISION_L1_TTL_SECONDS", "5.0"))
_L2_TIMEOUT_S = float(os.environ.get("VISION_L2_TIMEOUT_S", "8.0"))
_L3_TIMEOUT_S = float(os.environ.get("VISION_L3_TIMEOUT_S", "15.0"))
# Minimum confidence below which an L1 hit is treated as a miss
_L1_CONFIDENCE_FLOOR = float(os.environ.get("VISION_L1_CONFIDENCE_FLOOR", "0.5"))


# ---------------------------------------------------------------------------
# Public enumerations and dataclasses
# ---------------------------------------------------------------------------

class VisionTier(Enum):
    """Which tier resolved the vision query."""
    L1_SCENE = auto()   # scene-graph cache hit
    L2_JPRIME = auto()  # J-Prime GPU (LLaVA)
    L3_CLAUDE = auto()  # Claude Vision API (paid fallback)
    DEGRADED = auto()   # all tiers unavailable


@dataclass
class VisionQuery:
    """Describes what the caller wants to find on screen."""

    target_description: str
    """Natural-language description (e.g. "the blue Submit button")."""

    target_element_type: str = ""
    """Optional element type filter (e.g. "button", "textfield")."""

    target_text: str = ""
    """Exact or partial text content to match against ``text_content`` field."""

    vision_task_type: str = "ui_element_detection"
    """Brain-selector hint — controls which vision model L2 prefers.

    Known values:
        ``ui_element_detection`` — default, fast YOLO-style scan
        ``complex_ui_analysis``  — full LLaVA reasoning pass
        ``ocr_extraction``       — text-heavy regions
        ``spatial_reasoning``    — relative-position queries
    """

    frame_artifact_ref: str = ""
    """Optional reference to a pre-captured frame artifact for L2/L3 calls."""

    coords_hint: Optional[Tuple[int, int]] = None
    """Approximate screen location if known — used to seed L1 spatial lookup."""


@dataclass
class VisionRouterResult:
    """Result returned by :meth:`VisionRouter.route`."""

    tier: VisionTier
    """Which tier produced this result."""

    coords: Optional[Tuple[int, int]]
    """Resolved ``(x, y)`` screen coordinate, or ``None`` on DEGRADED."""

    confidence: float
    """Confidence score in [0, 1]."""

    element_data: Optional[Dict]
    """Full element payload from the resolving tier."""

    backend_used: str
    """Human-readable backend tag: ``"scene_graph"``, ``"jprime_llava"``, or
    ``"claude_vision"``."""

    latency_ms: float
    """Wall-clock time from route() entry to result, in milliseconds."""


# ---------------------------------------------------------------------------
# VisionRouter
# ---------------------------------------------------------------------------

class VisionRouter:
    """Three-tier vision router with independent operational-level tracking.

    Parameters
    ----------
    fabric:
        :class:`~backend.knowledge.fabric.KnowledgeFabric` instance used for
        L1 scene-graph lookups and post-resolution write-back.
    """

    def __init__(self, fabric: KnowledgeFabric) -> None:
        self._fabric = fabric
        self._consecutive_l2_failures: int = 0
        self._consecutive_degraded: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def operational_level(self) -> int:
        """Current operational level.

        Returns
        -------
        int
            0 — healthy (L2 reachable)
            1 — L2 down (routing via L3)
            2 — all tiers down (DEGRADED)
        """
        if self._consecutive_degraded > 0:
            return 2
        if self._consecutive_l2_failures >= _L2_FAILURE_THRESHOLD:
            return 1
        return 0

    async def route(self, query: VisionQuery) -> VisionRouterResult:
        """Route *query* through the tier cascade and return the first hit.

        Tier order: L1 scene graph → L2 J-Prime → L3 Claude → DEGRADED.

        Parameters
        ----------
        query:
            The vision query to resolve.

        Returns
        -------
        VisionRouterResult
        """
        t0 = time.monotonic()

        # --- L1: scene-graph cache lookup ---
        l1_result = self._query_l1(query)
        if l1_result is not None:
            return VisionRouterResult(
                tier=VisionTier.L1_SCENE,
                coords=l1_result.get("position"),
                confidence=float(l1_result.get("confidence", 1.0)),
                element_data=l1_result,
                backend_used="scene_graph",
                latency_ms=(time.monotonic() - t0) * 1000,
            )

        # --- L2: J-Prime GPU ---
        try:
            l2_response = await asyncio.wait_for(
                self._call_jprime_vision(query),
                timeout=_L2_TIMEOUT_S,
            )
            element = self._pick_best_element(l2_response)
            if element is not None:
                coords = self._normalise_coords(element.get("coords"))
                self._write_to_scene(element, coords)
                self._consecutive_l2_failures = 0  # reset on success
                self._consecutive_degraded = 0
                return VisionRouterResult(
                    tier=VisionTier.L2_JPRIME,
                    coords=coords,
                    confidence=float(element.get("confidence", 0.0)),
                    element_data=element,
                    backend_used="jprime_llava",
                    latency_ms=(time.monotonic() - t0) * 1000,
                )
        except (Exception, asyncio.TimeoutError) as exc:
            self._consecutive_l2_failures += 1
            logger.warning(
                "L2 J-Prime vision failed (consecutive=%d): %s",
                self._consecutive_l2_failures,
                exc,
            )

        # --- L3: Claude Vision paid fallback ---
        try:
            l3_response = await asyncio.wait_for(
                self._call_claude_vision(query),
                timeout=_L3_TIMEOUT_S,
            )
            element = self._pick_best_element(l3_response)
            if element is not None:
                coords = self._normalise_coords(element.get("coords"))
                self._write_to_scene(element, coords)
                self._consecutive_degraded = 0
                return VisionRouterResult(
                    tier=VisionTier.L3_CLAUDE,
                    coords=coords,
                    confidence=float(element.get("confidence", 0.0)),
                    element_data=element,
                    backend_used="claude_vision",
                    latency_ms=(time.monotonic() - t0) * 1000,
                )
        except (Exception, asyncio.TimeoutError) as exc:
            self._consecutive_degraded += 1
            logger.error(
                "L3 Claude vision also failed (consecutive=%d): %s",
                self._consecutive_degraded,
                exc,
            )

        # --- DEGRADED ---
        return VisionRouterResult(
            tier=VisionTier.DEGRADED,
            coords=None,
            confidence=0.0,
            element_data=None,
            backend_used="none",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # Mockable remote-tier stubs
    # ------------------------------------------------------------------

    async def _call_jprime_vision(self, query: VisionQuery) -> Dict:
        """Call J-Prime GPU vision endpoint via MindClient.

        Sends the frame reference and target description to POST /v1/vision/analyze.
        Returns dict with status + elements (coords, confidence).
        """
        try:
            from backend.core.mind_client import get_mind_client
            client = get_mind_client()
            result = await client.send_vision_frame(
                frame_ref=query.frame_artifact_ref or "live_capture",
                target_description=query.target_description,
                action_intent="click",
                vision_task_type=query.vision_task_type,
            )
            if result is None:
                raise RuntimeError("MindClient.send_vision_frame returned None")
            return result
        except Exception as exc:
            logger.warning("[VisionRouter] L2 J-Prime vision call failed: %s", exc)
            raise

    async def _call_claude_vision(self, query: VisionQuery) -> Dict:
        """Call Claude Vision API (paid fallback).

        This is a stub placeholder.  A future task will implement the real
        Anthropic API call with base64-encoded screenshot payload.

        Parameters
        ----------
        query:
            The vision query.

        Returns
        -------
        dict
            Same shape as :meth:`_call_jprime_vision`.
        """
        raise NotImplementedError(
            "_call_claude_vision is a stub — mock it in tests or implement "
            "the Anthropic Vision API call."
        )

    # ------------------------------------------------------------------
    # L1 scene-graph helpers
    # ------------------------------------------------------------------

    def _query_l1(self, query: VisionQuery) -> Optional[Dict]:
        """Attempt to resolve *query* from the L1 scene partition.

        Two strategies are tried in order:

        1. **Text match** — iterate live scene entities and check if
           ``text_content`` contains ``query.target_text`` (case-insensitive).
           Only entries whose confidence meets ``_L1_CONFIDENCE_FLOOR`` qualify.
        2. **Spatial hint** — if ``query.coords_hint`` is set and text match
           found nothing, call ``fabric.query_nearest_element()``.

        Returns the matching element dict or ``None``.
        """
        # Strategy 1: text-content scan
        if query.target_text:
            needle = query.target_text.casefold()
            scene = self._fabric.scene
            now = time.monotonic()
            for eid, data in list(scene._store.items()):
                if now >= scene._expiry.get(eid, 0):
                    continue  # expired — skip without pruning (read-only pass)
                text = str(data.get("text_content", "")).casefold()
                if needle in text:
                    conf = float(data.get("confidence", 1.0))
                    if conf >= _L1_CONFIDENCE_FLOOR:
                        logger.debug("L1 text hit: %s (conf=%.2f)", eid, conf)
                        return data

        # Strategy 2: spatial proximity
        if query.coords_hint is not None:
            nearest = self._fabric.query_nearest_element(
                query.coords_hint, max_distance=50.0
            )
            if nearest is not None:
                conf = float(nearest.get("confidence", 1.0))
                if conf >= _L1_CONFIDENCE_FLOOR:
                    logger.debug(
                        "L1 spatial hit near %s (conf=%.2f)",
                        query.coords_hint,
                        conf,
                    )
                    return nearest

        return None

    # ------------------------------------------------------------------
    # Scene-graph write-back
    # ------------------------------------------------------------------

    def _write_to_scene(
        self,
        element: Dict,
        coords: Optional[Tuple[int, int]],
    ) -> None:
        """Write a resolved element back to the L1 scene partition.

        Generates a unique entity ID using the element type and a UUID suffix
        so that multiple elements of the same type can coexist in the cache.
        """
        if coords is None:
            return
        element_type = element.get("element_type", "element")
        uid = uuid.uuid4().hex[:8]
        entity_id = f"kg://scene/{element_type}/{uid}"
        payload = dict(element)
        payload["position"] = coords
        try:
            self._fabric.write(entity_id, payload, ttl_seconds=_L1_TTL_SECONDS)
            logger.debug("Scene write-back: %s @ %s", entity_id, coords)
        except Exception as exc:  # pragma: no cover
            logger.warning("Scene write-back failed for %s: %s", entity_id, exc)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_best_element(response: Dict) -> Optional[Dict]:
        """Extract the highest-confidence element from an L2/L3 response.

        Parameters
        ----------
        response:
            Dict with ``"status"`` and ``"elements"`` list.

        Returns
        -------
        dict or None
        """
        if not isinstance(response, dict):
            return None
        if response.get("status") != "found":
            return None
        elements: List[Dict] = response.get("elements", [])
        if not elements:
            return None
        return max(elements, key=lambda e: float(e.get("confidence", 0.0)))

    @staticmethod
    def _normalise_coords(raw) -> Optional[Tuple[int, int]]:
        """Convert various coord representations to ``(int, int)`` or ``None``.

        Accepts:
        - ``[x, y]`` list / tuple
        - ``{"x": ..., "y": ...}`` dict
        - ``None``
        """
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return (int(raw[0]), int(raw[1]))
        if isinstance(raw, dict) and "x" in raw and "y" in raw:
            return (int(raw["x"]), int(raw["y"]))
        return None
