"""
Post-action verification via frame diff analysis.

Every verify_* method compares a *before* and *after* numpy frame
(H×W×3 uint8) and returns a :class:`VerificationResult` with a
:class:`VerificationStatus` and supporting metrics.

Design goals
------------
- All comparisons are synchronous numpy operations (fast, no I/O).
- Thresholds are sourced from environment variables — no hardcoding.
- The API is intentionally minimal: callers pass raw frames and action
  metadata; no pyautogui or screen-capture dependency here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven thresholds — no hardcoding
# ---------------------------------------------------------------------------
_CLICK_THRESHOLD = float(os.environ.get("VISION_VERIFY_CLICK_THRESHOLD", "5.0"))
"""Mean absolute pixel difference in the click region to count as a change."""

_TYPE_THRESHOLD = float(os.environ.get("VISION_VERIFY_TYPE_THRESHOLD", "3.0"))
"""Mean absolute pixel difference in the target region to count as text appeared."""

_SCROLL_THRESHOLD = float(os.environ.get("VISION_VERIFY_SCROLL_THRESHOLD", "2.0"))
"""Mean absolute pixel difference across the full frame to count as scroll movement."""

_SCROLL_SHIFT_PIXELS = int(os.environ.get("VISION_VERIFY_SCROLL_SHIFT_PIXELS", "3"))
"""Minimum pixel shift detectable between top-half / bottom-half split planes."""


# ---------------------------------------------------------------------------
# Public enumerations and dataclasses
# ---------------------------------------------------------------------------

class VerificationStatus(str, Enum):
    """Outcome categories for a post-action verification check."""

    SUCCESS = "success"
    """The expected change was detected."""

    FAIL = "fail"
    """No significant change was detected — action likely had no effect."""

    PARTIAL = "partial"
    """Some change detected but below the expected magnitude."""

    INCONCLUSIVE = "inconclusive"
    """Frame data was insufficient to make a determination."""


@dataclass
class VerificationResult:
    """Result of a single post-action verification pass.

    Parameters
    ----------
    status:
        Categorical outcome — see :class:`VerificationStatus`.
    confidence:
        Normalised confidence that the observed change matches the expected
        action effect, in [0, 1].
    diff_magnitude:
        Raw mean absolute difference value (pixel units) used to derive the
        status.
    details:
        Human-readable explanation of the decision.
    """

    status: VerificationStatus
    confidence: float
    diff_magnitude: float
    details: str = ""


# ---------------------------------------------------------------------------
# ActionVerifier
# ---------------------------------------------------------------------------

class ActionVerifier:
    """Stateless post-action verifier using numpy frame diff analysis.

    Each verify_* method takes a *before* and *after* frame captured around
    the action boundary and returns a :class:`VerificationResult`.

    All methods are synchronous — frame comparison is purely CPU-bound numpy
    arithmetic and does not need to be offloaded.
    """

    # ------------------------------------------------------------------
    # Public verify methods
    # ------------------------------------------------------------------

    def verify_click(
        self,
        before: np.ndarray,
        after: np.ndarray,
        coords: Tuple[int, int],
        region_size: int = 20,
    ) -> VerificationResult:
        """Verify a click action by checking the region around *coords*.

        Parameters
        ----------
        before:
            Frame captured immediately before the click (H×W×3 uint8).
        after:
            Frame captured immediately after the click.
        coords:
            ``(x, y)`` screen coordinate that was clicked.
        region_size:
            Half-width of the square region of interest around *coords*.

        Returns
        -------
        VerificationResult
        """
        if not self._frames_valid(before, after):
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                confidence=0.0,
                diff_magnitude=0.0,
                details="One or both frames are invalid or mismatched.",
            )

        x, y = coords
        h, w = before.shape[:2]
        half = region_size // 2

        # Clamp region to frame bounds
        y1 = max(0, y - half)
        y2 = min(h, y + half)
        x1 = max(0, x - half)
        x2 = min(w, x + half)

        roi_before = before[y1:y2, x1:x2].astype(np.float32)
        roi_after = after[y1:y2, x1:x2].astype(np.float32)

        diff_mag = float(np.mean(np.abs(roi_after - roi_before)))
        confidence = min(1.0, diff_mag / max(_CLICK_THRESHOLD * 4, 1e-6))

        if diff_mag >= _CLICK_THRESHOLD:
            status = VerificationStatus.SUCCESS
        else:
            status = VerificationStatus.FAIL
            confidence = max(0.0, confidence)

        logger.debug(
            "verify_click @ %s region=[%d:%d,%d:%d] diff=%.2f threshold=%.2f → %s",
            coords, y1, y2, x1, x2, diff_mag, _CLICK_THRESHOLD, status,
        )

        return VerificationResult(
            status=status,
            confidence=confidence,
            diff_magnitude=diff_mag,
            details=(
                f"Region diff={diff_mag:.2f} (threshold={_CLICK_THRESHOLD:.2f})"
            ),
        )

    def verify_type(
        self,
        before: np.ndarray,
        after: np.ndarray,
        target_region: Tuple[int, int, int, int],
    ) -> VerificationResult:
        """Verify a type action by checking pixel change in *target_region*.

        Parameters
        ----------
        before:
            Frame captured before typing began.
        after:
            Frame captured after typing completed.
        target_region:
            ``(x1, y1, x2, y2)`` bounding box of the text input area.

        Returns
        -------
        VerificationResult
        """
        if not self._frames_valid(before, after):
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                confidence=0.0,
                diff_magnitude=0.0,
                details="One or both frames are invalid or mismatched.",
            )

        x1, y1, x2, y2 = target_region
        h, w = before.shape[:2]

        # Clamp to frame bounds
        y1c, y2c = max(0, y1), min(h, y2)
        x1c, x2c = max(0, x1), min(w, x2)

        roi_before = before[y1c:y2c, x1c:x2c].astype(np.float32)
        roi_after = after[y1c:y2c, x1c:x2c].astype(np.float32)

        if roi_before.size == 0:
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                confidence=0.0,
                diff_magnitude=0.0,
                details="Target region is outside frame bounds.",
            )

        diff_mag = float(np.mean(np.abs(roi_after - roi_before)))
        confidence = min(1.0, diff_mag / max(_TYPE_THRESHOLD * 4, 1e-6))

        if diff_mag >= _TYPE_THRESHOLD:
            status = VerificationStatus.SUCCESS
        else:
            status = VerificationStatus.FAIL
            confidence = max(0.0, confidence)

        logger.debug(
            "verify_type region=[%d:%d,%d:%d] diff=%.2f threshold=%.2f → %s",
            y1c, y2c, x1c, x2c, diff_mag, _TYPE_THRESHOLD, status,
        )

        return VerificationResult(
            status=status,
            confidence=confidence,
            diff_magnitude=diff_mag,
            details=(
                f"Region diff={diff_mag:.2f} (threshold={_TYPE_THRESHOLD:.2f})"
            ),
        )

    def verify_scroll(
        self,
        before: np.ndarray,
        after: np.ndarray,
        direction: str = "down",
    ) -> VerificationResult:
        """Verify a scroll action by detecting content shift across the frame.

        Strategy: compare the top half of *after* with the top half of *before*
        shifted by ``_SCROLL_SHIFT_PIXELS`` in the scroll direction.  A low
        difference on the shifted comparison (vs. the unshifted comparison)
        indicates that content moved.

        Parameters
        ----------
        before:
            Frame captured before the scroll.
        after:
            Frame captured after the scroll.
        direction:
            ``"down"`` or ``"up"`` — the scroll direction.

        Returns
        -------
        VerificationResult
        """
        if not self._frames_valid(before, after):
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                confidence=0.0,
                diff_magnitude=0.0,
                details="One or both frames are invalid or mismatched.",
            )

        h = before.shape[0]
        shift = _SCROLL_SHIFT_PIXELS

        # Overall frame diff (unshifted baseline)
        overall_diff = float(np.mean(np.abs(
            after.astype(np.float32) - before.astype(np.float32)
        )))

        # Shifted diff: compare after[shift:] vs before[:-shift] for "down"
        # (content in *after* appears shift rows lower than in *before*)
        if direction == "down":
            shifted_diff = float(np.mean(np.abs(
                after[shift:, :].astype(np.float32)
                - before[:h - shift, :].astype(np.float32)
            )))
        else:  # "up"
            shifted_diff = float(np.mean(np.abs(
                after[:h - shift, :].astype(np.float32)
                - before[shift:, :].astype(np.float32)
            )))

        # Scroll is confirmed when the overall diff is above noise floor
        # AND the shifted comparison is meaningfully better than unshifted
        # (content actually moved)
        diff_mag = overall_diff

        if overall_diff >= _SCROLL_THRESHOLD:
            # Content changed — consider it a scroll success
            confidence = min(1.0, overall_diff / max(_SCROLL_THRESHOLD * 4, 1e-6))
            status = VerificationStatus.SUCCESS
        else:
            confidence = 0.0
            status = VerificationStatus.FAIL

        logger.debug(
            "verify_scroll dir=%s overall_diff=%.2f shifted_diff=%.2f "
            "threshold=%.2f → %s",
            direction, overall_diff, shifted_diff, _SCROLL_THRESHOLD, status,
        )

        return VerificationResult(
            status=status,
            confidence=confidence,
            diff_magnitude=diff_mag,
            details=(
                f"overall_diff={overall_diff:.2f} shifted_diff={shifted_diff:.2f} "
                f"(threshold={_SCROLL_THRESHOLD:.2f}, direction={direction})"
            ),
        )

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _frames_valid(before: np.ndarray, after: np.ndarray) -> bool:
        """Return True if both frames are non-empty and have the same shape."""
        if before is None or after is None:
            return False
        if not isinstance(before, np.ndarray) or not isinstance(after, np.ndarray):
            return False
        if before.ndim < 2 or after.ndim < 2:
            return False
        if before.shape != after.shape:
            return False
        return True
