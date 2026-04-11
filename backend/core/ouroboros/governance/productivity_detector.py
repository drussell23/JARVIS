"""Productivity-ratio detector for the GENERATE retry loop (EC9).

Motivation
----------
EC3 (``cost_governor.py``) catches *total* cost overruns. EC8
(``forward_progress.py``) catches *byte-identical* candidate repetition.
Neither catches the silent-burn failure mode where the model:

    1. Produces candidate A (cost $0.04)
    2. Fails validation.
    3. Produces candidate A' — semantically identical to A but with
       cosmetic differences (trailing whitespace, rearranged imports,
       whitespace around operators, formatted docstring, etc.) — for
       another $0.04.
    4. Fails validation.
    5. Repeats.

This passes EC8 (bytes differ) and passes EC3 (each call is cheap) while
burning the entire per-op budget on semantically-zero forward progress.
EC9 closes that gap by correlating **cost delta** with **normalized-output
delta**. A trip means: "we're spending money without producing a
meaningfully different artifact" → escape the retry loop.

Design principles
-----------------
1.  **Semantic-aware normalization.** For supported languages we compute
    a canonical form (AST dump for Python, sorted canonical JSON, etc.)
    so that cosmetic variations fold into the same hash. Unsupported
    languages fall back to a whitespace-normalized hash. All normalizers
    are registered in a module-level dispatch table so adding a new
    language is a one-liner.

2.  **Cost-correlated tripping.** Unlike EC8 which trips on N
    consecutive identical outputs regardless of cost, EC9 trips when
    the cost accumulated *since the last semantic change* exceeds a
    configurable USD threshold **and** at least ``min_observations``
    stable observations have been seen. Either condition alone is
    insufficient — expensive one-shots don't trip, cheap repetitions
    don't trip.

3.  **No hardcoding.** Every threshold, every normalization level,
    every TTL is resolved from environment variables with safe
    defaults. Matches the ``CostGovernor`` / ``ForwardProgressDetector``
    pattern.

4.  **Robust fallbacks.** Parse failure in a language-specific
    normalizer drops to whitespace normalization — we never drop the
    candidate entirely. Empty content → no-op. Unknown extensions →
    whitespace normalization.

5.  **Phase-aware abort.** Like its siblings, EC9 only flags the
    condition; the orchestrator chooses the terminal phase via
    ``_l2_escape_terminal``.

6.  **Composable with EC8.** Both detectors can and should run. EC8 is
    the cheap byte-level guard; EC9 is the expensive semantic guard.
    First trip wins.

Compliance
----------
* Manifesto §5 — Intelligence-driven routing: the detector respects
  normalization-level configuration set per deployment.
* Manifesto §6 — Threshold-triggered neuroplasticity: stalled ops trip
  a threshold and escape instead of hammering the provider.
* Manifesto §7 — Absolute observability: every observation, every
  reset, every trip logs with structured detail.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Env-var helpers (self-contained — zero internal deps)
# -----------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[ProductivityDetector] Env %s=%r is not a float; using default %.4f",
            name, raw, default,
        )
        return default
    return val if val >= 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw.strip().lower() if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


# -----------------------------------------------------------------------------
# Normalization level taxonomy
# -----------------------------------------------------------------------------

class NormalizationLevel:
    """Enum-like string constants for the normalization strategy.

    Deliberately a class of constants rather than an ``enum.Enum`` so the
    env var can be a plain lowercase string without coupling callers to
    the enum module.
    """
    BYTE = "byte"               # raw bytes; equivalent to EC8
    WHITESPACE = "whitespace"   # strip trailing WS, collapse blank lines
    AST = "ast"                 # semantic normalization per extension
    ALL = frozenset({BYTE, WHITESPACE, AST})


# -----------------------------------------------------------------------------
# Language-specific normalizers
# -----------------------------------------------------------------------------

def _normalize_bytes(text: str) -> str:
    """No-op normalization — returns text unchanged (byte-identical)."""
    return text or ""


def _normalize_whitespace(text: str) -> str:
    """Whitespace-level canonical form.

    * Strip BOM.
    * Strip trailing whitespace per line.
    * Collapse runs of blank lines to a single blank line.
    * Ensure single trailing newline.

    Deterministic, cheap, and safe for any text format.
    """
    if not text:
        return ""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines: List[str] = []
    prev_blank = False
    for raw in text.splitlines():
        stripped = raw.rstrip()
        is_blank = stripped == ""
        if is_blank and prev_blank:
            continue
        lines.append(stripped)
        prev_blank = is_blank
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def _normalize_python_ast(text: str) -> str:
    """Semantic canonical form for Python via ``ast.dump``.

    Falls back to whitespace normalization on parse errors (e.g. partial
    candidate, syntax error the model hasn't fixed yet). Dumping with
    ``annotate_fields=False`` and ``include_attributes=False`` produces
    a form that's invariant to whitespace, comments, and literal
    formatting but sensitive to semantic changes.
    """
    if not text:
        return ""
    try:
        tree = ast.parse(text)
        return ast.dump(tree, annotate_fields=False, include_attributes=False)
    except (SyntaxError, ValueError, TypeError):
        return _normalize_whitespace(text)


def _normalize_json(text: str) -> str:
    """Canonical JSON form via ``json.dumps(sort_keys=True)``.

    Falls back to whitespace normalization on parse errors.
    """
    if not text:
        return ""
    try:
        obj = json.loads(text)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, ValueError, TypeError):
        return _normalize_whitespace(text)


# -----------------------------------------------------------------------------
# Normalizer registry — add a language here to support it.
# -----------------------------------------------------------------------------

#: Extension → normalizer callable. Keys are lowercase with leading dot.
_AST_REGISTRY: Dict[str, Callable[[str], str]] = {
    ".py": _normalize_python_ast,
    ".pyi": _normalize_python_ast,
    ".json": _normalize_json,
    ".jsonl": _normalize_json,
}


def register_normalizer(extension: str, fn: Callable[[str], str]) -> None:
    """Register a language-specific normalizer.

    Public hook so downstream code (plugins, integration tests) can add
    languages without modifying this file. Extension must include the
    leading dot and is lowercased automatically.
    """
    if not extension.startswith("."):
        extension = "." + extension
    _AST_REGISTRY[extension.lower()] = fn


def _normalize_for_level(
    text: str,
    file_path: str,
    level: str,
) -> str:
    """Apply the requested normalization level to a single content blob."""
    if level == NormalizationLevel.BYTE:
        return _normalize_bytes(text)
    if level == NormalizationLevel.WHITESPACE:
        return _normalize_whitespace(text)
    # AST level: try language-specific, fall back to whitespace.
    if file_path:
        suffix = PurePosixPath(file_path).suffix.lower()
        fn = _AST_REGISTRY.get(suffix)
        if fn is not None:
            try:
                return fn(text)
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "[ProductivityDetector] normalizer for %s raised; falling back",
                    suffix,
                )
    return _normalize_whitespace(text)


# -----------------------------------------------------------------------------
# Public hash function — mirrors forward_progress.candidate_content_hash
# -----------------------------------------------------------------------------

def productivity_content_hash(
    candidate: Any,
    level: str = NormalizationLevel.AST,
) -> str:
    """Return a normalized SHA-256 hash of a candidate's semantic content.

    Handles the same candidate shapes as ``forward_progress``:
      * Single-file ``full_content`` / ``raw_content``.
      * Multi-file ``files: [{file_path, full_content}, ...]``.
      * Duck-typed objects with ``full_content`` / ``raw_content`` attrs.

    Does **not** trust an upstream-stamped ``candidate_hash`` — that hash
    is byte-level and misses the semantic-equivalence case EC9 exists to
    catch.

    Returns an empty string when no content can be extracted. Callers
    must treat an empty hash as a no-op.
    """
    if candidate is None:
        return ""
    level = level if level in NormalizationLevel.ALL else NormalizationLevel.AST

    if isinstance(candidate, Mapping):
        files = candidate.get("files")
        if isinstance(files, list) and files:
            hasher = hashlib.sha256()
            for entry in files:
                if not isinstance(entry, Mapping):
                    continue
                path = str(entry.get("file_path", "") or "")
                content = str(entry.get("full_content", "") or "")
                norm = _normalize_for_level(content, path, level)
                hasher.update(path.encode("utf-8", errors="ignore"))
                hasher.update(b"\x00")
                hasher.update(norm.encode("utf-8", errors="ignore"))
                hasher.update(b"\x00")
            return hasher.hexdigest()

        content = (
            candidate.get("full_content", "")
            or candidate.get("raw_content", "")
            or ""
        )
        if isinstance(content, str) and content:
            path = str(candidate.get("file_path", "") or "")
            norm = _normalize_for_level(content, path, level)
            return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()

    content = (
        getattr(candidate, "full_content", "")
        or getattr(candidate, "raw_content", "")
        or ""
    )
    if isinstance(content, str) and content:
        path = getattr(candidate, "file_path", "") or ""
        norm = _normalize_for_level(content, path, level)
        return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()

    return ""


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductivityDetectorConfig:
    """Immutable config for the detector.

    Trip condition:

        consecutive_stable >= min_observations
        AND cost_since_last_change >= cost_burn_threshold_usd

    Both conditions must be met. A single expensive attempt that happens
    to produce the same hash as the previous one does *not* trip —
    ``min_observations=2`` (default) requires at least one confirmation.
    """

    enabled: bool = field(
        default_factory=lambda: _env_bool("JARVIS_EC9_ENABLED", True)
    )
    cost_burn_threshold_usd: float = field(
        default_factory=lambda: _env_float(
            "JARVIS_EC9_COST_BURN_THRESHOLD_USD", 0.05
        )
    )
    min_observations: int = field(
        default_factory=lambda: _env_int("JARVIS_EC9_MIN_OBSERVATIONS", 2)
    )
    normalization_level: str = field(
        default_factory=lambda: _env_str(
            "JARVIS_EC9_NORMALIZE_LEVEL", NormalizationLevel.AST
        )
    )
    ttl_s: float = field(
        default_factory=lambda: _env_float("JARVIS_EC9_TTL_S", 3600.0)
    )

    def __post_init__(self) -> None:
        # Clamp normalization_level to known taxonomy; unknown values
        # fall back to AST (the safest default).
        if self.normalization_level not in NormalizationLevel.ALL:
            object.__setattr__(
                self, "normalization_level", NormalizationLevel.AST,
            )


# -----------------------------------------------------------------------------
# Per-op ledger entry
# -----------------------------------------------------------------------------

@dataclass
class _OpProductivityEntry:
    op_id: str
    last_hash: str = ""
    consecutive_stable: int = 0
    cost_since_last_change: float = 0.0
    total_cost: float = 0.0
    total_observations: int = 0
    created_at: float = 0.0
    tripped: bool = False
    # History of (timestamp, hash[:12], cost_delta, consecutive_stable)
    # for postmortem analysis. Capped at 32 entries.
    history: List[Tuple[float, str, float, int]] = field(default_factory=list)


_HISTORY_CAP = 32


# -----------------------------------------------------------------------------
# Detector
# -----------------------------------------------------------------------------

class ProductivityDetector:
    """Tracks cost-vs-output productivity ratio per op.

    Usage
    -----
        detector = ProductivityDetector()
        h = productivity_content_hash(candidate, level=detector.level)
        if detector.observe(op_id, cost_delta=0.04, normalized_hash=h):
            # Stalled — abort with phase-aware terminal
            ...
        detector.finish(op_id)  # optional; TTL-pruned otherwise
    """

    def __init__(self, config: Optional[ProductivityDetectorConfig] = None) -> None:
        self._config = config or ProductivityDetectorConfig()
        self._entries: Dict[str, _OpProductivityEntry] = {}

    # --------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------

    @property
    def level(self) -> str:
        """Normalization level — pass to ``productivity_content_hash``."""
        return self._config.normalization_level

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def observe(
        self,
        op_id: str,
        cost_delta: float,
        normalized_hash: str,
    ) -> bool:
        """Record a (cost, hash) observation. Returns True if stalled.

        Parameters
        ----------
        op_id:
            The op being tracked.
        cost_delta:
            USD cost charged for *this* attempt (not cumulative). Zero
            or negative deltas are accepted (cache hits, no-op fallbacks)
            and do not reset stability — they simply don't contribute to
            the burn accumulator.
        normalized_hash:
            The output of ``productivity_content_hash(candidate, level)``.
            An empty string is treated as "no signal" and is a no-op.

        Semantics
        ---------
        * New hash differs from previous → reset ``consecutive_stable``
          and ``cost_since_last_change`` (but keep accumulating
          ``total_cost``).
        * New hash matches previous → increment ``consecutive_stable``
          and add ``cost_delta`` to ``cost_since_last_change``.
        * Trip when both thresholds met. Once tripped, subsequent
          ``observe`` calls return True until ``finish(op_id)``.
        """
        if not self._config.enabled:
            return False
        if not normalized_hash:
            return False

        self._prune_stale()

        entry = self._entries.get(op_id)
        if entry is None:
            entry = _OpProductivityEntry(
                op_id=op_id,
                created_at=time.monotonic(),
            )
            self._entries[op_id] = entry

        delta = max(0.0, float(cost_delta) if cost_delta is not None else 0.0)
        entry.total_observations += 1
        entry.total_cost += delta

        if entry.tripped:
            return True

        if entry.last_hash == normalized_hash:
            entry.consecutive_stable += 1
            entry.cost_since_last_change += delta
            logger.debug(
                "[ProductivityDetector] op=%s hash=%s stable=%d cost_since=$%.4f",
                op_id[:12], normalized_hash[:12],
                entry.consecutive_stable, entry.cost_since_last_change,
            )
        else:
            entry.last_hash = normalized_hash
            entry.consecutive_stable = 1
            entry.cost_since_last_change = delta
            logger.debug(
                "[ProductivityDetector] op=%s new hash=%s (reset stable counter)",
                op_id[:12], normalized_hash[:12],
            )

        # Append to history (capped).
        entry.history.append((
            time.monotonic(),
            normalized_hash[:12],
            round(delta, 6),
            entry.consecutive_stable,
        ))
        if len(entry.history) > _HISTORY_CAP:
            entry.history = entry.history[-_HISTORY_CAP:]

        cost_trip = entry.cost_since_last_change >= self._config.cost_burn_threshold_usd
        obs_trip = entry.consecutive_stable >= self._config.min_observations
        if cost_trip and obs_trip:
            entry.tripped = True
            logger.warning(
                "[ProductivityDetector] op=%s STALLED: "
                "hash %s stable for %d obs, $%.4f burned since last change "
                "(threshold $%.4f, min_obs %d, level=%s)",
                op_id, normalized_hash[:12],
                entry.consecutive_stable,
                entry.cost_since_last_change,
                self._config.cost_burn_threshold_usd,
                self._config.min_observations,
                self._config.normalization_level,
            )
            return True
        return False

    def is_tripped(self, op_id: str) -> bool:
        """Return True if ``op_id`` has been marked stalled."""
        if not self._config.enabled:
            return False
        entry = self._entries.get(op_id)
        return bool(entry and entry.tripped)

    def remaining_burn_budget(self, op_id: str) -> float:
        """Return USD headroom before trip for the current stable streak.

        Returns +inf if detector is disabled or op is untracked.
        Returns 0.0 if already tripped or over threshold.
        """
        if not self._config.enabled:
            return float("inf")
        entry = self._entries.get(op_id)
        if entry is None:
            return float("inf")
        if entry.tripped:
            return 0.0
        return max(
            0.0,
            self._config.cost_burn_threshold_usd - entry.cost_since_last_change,
        )

    def finish(self, op_id: str) -> Optional[Mapping[str, Any]]:
        """Finalize and remove the op entry. Returns summary or None."""
        entry = self._entries.pop(op_id, None)
        if entry is None:
            return None
        return self._summary(entry)

    def summary(self, op_id: str) -> Optional[Mapping[str, Any]]:
        """Return current state for ``op_id`` without removal."""
        entry = self._entries.get(op_id)
        if entry is None:
            return None
        return self._summary(entry)

    def active_op_count(self) -> int:
        return len(self._entries)

    # --------------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------------

    def _summary(self, entry: _OpProductivityEntry) -> Mapping[str, Any]:
        return {
            "op_id": entry.op_id,
            "last_hash": entry.last_hash,
            "consecutive_stable": entry.consecutive_stable,
            "cost_since_last_change_usd": round(entry.cost_since_last_change, 6),
            "total_cost_usd": round(entry.total_cost, 6),
            "total_observations": entry.total_observations,
            "tripped": entry.tripped,
            "config": {
                "cost_burn_threshold_usd": self._config.cost_burn_threshold_usd,
                "min_observations": self._config.min_observations,
                "normalization_level": self._config.normalization_level,
            },
            "history_tail": [
                {
                    "t_mono": round(t, 3),
                    "hash12": h,
                    "cost_delta": c,
                    "stable": s,
                }
                for (t, h, c, s) in entry.history[-8:]
            ],
        }

    def _prune_stale(self) -> int:
        if not self._entries:
            return 0
        now = time.monotonic()
        ttl = self._config.ttl_s
        stale = [
            op_id for op_id, entry in self._entries.items()
            if now - entry.created_at > ttl
        ]
        for op_id in stale:
            self._entries.pop(op_id, None)
        if stale:
            logger.debug(
                "[ProductivityDetector] Pruned %d stale entries (ttl=%.0fs)",
                len(stale), ttl,
            )
        return len(stale)


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class StalledProductivityError(RuntimeError):
    """Raised by orchestrator callers when EC9 trips.

    Carries the op_id and the detector's structured summary so that the
    caller can route through the phase-aware terminal picker and emit
    full telemetry on abort.
    """

    def __init__(self, op_id: str, summary: Mapping[str, Any]) -> None:
        self.op_id = op_id
        self.summary = dict(summary)
        burned = self.summary.get("cost_since_last_change_usd", 0.0)
        stable = self.summary.get("consecutive_stable", 0)
        super().__init__(
            f"stalled_productivity: op={op_id[:12]} "
            f"burned=${burned} stable={stable}"
        )
