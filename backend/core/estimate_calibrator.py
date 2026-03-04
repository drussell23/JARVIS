"""EstimateCalibrator -- self-improving memory estimate accuracy tracker.

Tracks estimate vs actual memory usage per component and computes p95
overrun factors so that future ``get_calibrated_estimate()`` calls
automatically inflate raw estimates by an empirically-derived factor.

Lifecycle::

    record(component_id, estimated, actual)   # after each load
        |
    get_calibrated_estimate(component_id, raw)  # before next grant request
        |
    get_stats()                                 # dashboard / diagnostics

History is persisted as JSON under ``~/.jarvis/memory/estimate_history.json``
using atomic write (tmp + fsync + rename) to survive crashes.

Public API
----------
Classes:
    EstimateCalibrator

Singletons:
    get_estimate_calibrator(), init_estimate_calibrator()
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_calibrator_instance: Optional[EstimateCalibrator] = None  # type: ignore[name-defined]  # forward ref
_calibrator_lock = threading.Lock()


class EstimateCalibrator:
    """Tracks estimate vs actual memory usage and computes p95 overrun factors.

    Each ``record()`` call stores the ratio ``actual / estimated`` for a
    given *component_id*.  ``get_calibrated_estimate()`` then inflates a
    raw estimate by the p95 of those historical ratios (floored at 1.0 so
    estimates are never *shrunk*).

    Thread-safety: all public methods are guarded by a reentrant lock so
    the calibrator can be used from any thread or coroutine executor.
    """

    MAX_HISTORY_PER_COMPONENT: int = 50
    DEFAULT_OVERRUN_FACTOR: float = 1.2  # 20 % padding when < 3 samples

    def __init__(
        self,
        history_file: Path = Path("~/.jarvis/memory/estimate_history.json").expanduser(),
    ) -> None:
        self._history_file = Path(history_file)
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load history from *self._history_file*.

        Handles missing files and corrupt JSON gracefully by starting with
        an empty history dict.
        """
        try:
            if self._history_file.exists():
                raw = self._history_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    # Validate structure: each value must be a list of dicts
                    validated: Dict[str, List[Dict[str, Any]]] = {}
                    for key, entries in data.items():
                        if isinstance(entries, list):
                            validated[str(key)] = [
                                e for e in entries
                                if isinstance(e, dict)
                                   and "ratio" in e
                                   and "estimated" in e
                                   and "actual" in e
                            ]
                    self._history = validated
                    logger.debug(
                        "Loaded estimate history for %d components from %s",
                        len(self._history),
                        self._history_file,
                    )
                else:
                    logger.warning(
                        "Estimate history file has unexpected root type (%s), "
                        "starting fresh",
                        type(data).__name__,
                    )
                    self._history = {}
            else:
                logger.debug(
                    "No estimate history file at %s, starting fresh",
                    self._history_file,
                )
                self._history = {}
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "Failed to load estimate history from %s (%s), starting fresh",
                self._history_file,
                exc,
            )
            self._history = {}

    def _persist(self) -> None:
        """Atomically write history to disk: tmpfile + fsync + rename.

        Creates parent directories if they do not exist.
        """
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)

            # Write to a temporary file in the same directory (same filesystem)
            # to guarantee atomic rename.
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._history_file.parent),
                prefix=".estimate_hist_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(self._history, fp, indent=2)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp_path, str(self._history_file))
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning(
                "Failed to persist estimate history to %s: %s",
                self._history_file,
                exc,
            )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, component_id: str, estimated: int, actual: int) -> None:
        """Record an estimate-vs-actual observation for *component_id*.

        Parameters
        ----------
        component_id:
            A stable identifier such as ``"llm:mistral-7b@Q4_K_M"``.
        estimated:
            The memory estimate (bytes) that was used when requesting a grant.
        actual:
            The observed peak memory consumption (bytes) after loading.
        """
        ratio = actual / max(estimated, 1)

        entry: Dict[str, Any] = {
            "estimated": estimated,
            "actual": actual,
            "ratio": ratio,
        }

        with self._lock:
            entries = self._history.setdefault(component_id, [])
            entries.append(entry)

            # Trim to the most recent MAX_HISTORY_PER_COMPONENT entries
            if len(entries) > self.MAX_HISTORY_PER_COMPONENT:
                self._history[component_id] = entries[-self.MAX_HISTORY_PER_COMPONENT:]

            self._persist()

        logger.debug(
            "Recorded estimate calibration for %s: estimated=%d actual=%d ratio=%.3f",
            component_id,
            estimated,
            actual,
            ratio,
        )

    # ------------------------------------------------------------------
    # Calibrated estimates
    # ------------------------------------------------------------------

    def _p95_factor(self, component_id: str) -> float:
        """Compute the p95 overrun factor for *component_id*.

        Returns ``DEFAULT_OVERRUN_FACTOR`` when fewer than 3 samples exist.
        The factor is floored at 1.0 so estimates are never deflated.
        """
        entries = self._history.get(component_id, [])
        if len(entries) < 3:
            return self.DEFAULT_OVERRUN_FACTOR

        ratios = sorted(e["ratio"] for e in entries)
        # p95 index: ceiling of 0.95 * n, clamped to valid range
        p95_idx = min(math.ceil(0.95 * len(ratios)) - 1, len(ratios) - 1)
        p95_value = ratios[p95_idx]

        # Never shrink below 1.0
        return max(p95_value, 1.0)

    def get_calibrated_estimate(self, component_id: str, raw_estimate: int) -> int:
        """Return *raw_estimate* inflated by the p95 overrun factor.

        If the component has fewer than 3 historical samples the
        ``DEFAULT_OVERRUN_FACTOR`` (1.2x) is applied instead.

        The result is always >= *raw_estimate* (estimates are never shrunk).
        """
        with self._lock:
            factor = self._p95_factor(component_id)

        calibrated = int(raw_estimate * factor)
        # Belt-and-suspenders: never return less than the raw estimate
        return max(calibrated, raw_estimate)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return per-component calibration statistics for dashboards.

        Returns a dict keyed by *component_id* with values::

            {
                "p95_factor": float,
                "samples": int,
                "mean_ratio": float,
            }
        """
        with self._lock:
            result: Dict[str, Dict[str, Any]] = {}
            for cid, entries in self._history.items():
                ratios = [e["ratio"] for e in entries]
                result[cid] = {
                    "p95_factor": self._p95_factor(cid),
                    "samples": len(entries),
                    "mean_ratio": sum(ratios) / len(ratios) if ratios else 0.0,
                }
            return result

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<EstimateCalibrator components={len(self._history)} "
            f"file={self._history_file}>"
        )


# -----------------------------------------------------------------------
# Singleton access
# -----------------------------------------------------------------------


def init_estimate_calibrator(
    history_file: Optional[Path] = None,
) -> EstimateCalibrator:
    """Create (or replace) the module-level EstimateCalibrator singleton.

    Parameters
    ----------
    history_file:
        Override the default ``~/.jarvis/memory/estimate_history.json``.
    """
    global _calibrator_instance
    kwargs: Dict[str, Any] = {}
    if history_file is not None:
        kwargs["history_file"] = history_file
    with _calibrator_lock:
        _calibrator_instance = EstimateCalibrator(**kwargs)
    return _calibrator_instance


def get_estimate_calibrator() -> EstimateCalibrator:
    """Return the module-level EstimateCalibrator singleton.

    If no instance has been initialised via ``init_estimate_calibrator()``
    one is lazily created with default parameters.
    """
    global _calibrator_instance
    if _calibrator_instance is not None:
        return _calibrator_instance
    with _calibrator_lock:
        # Double-check under lock
        if _calibrator_instance is None:
            _calibrator_instance = EstimateCalibrator()
        return _calibrator_instance
