"""Sovereign Fleet Evaluator — calibration persistence + pure math core.

Task 2 of the FleetEvaluator subsystem (spec §4.2). Measures DoubleWord
(DW) model OUTPUT QUALITY (AST-valid code rate, classification adherence)
and exposes the PURE re-rank + graduation-decision functions that drive
quality-weighted model routing.

This is the persistence + math core only:

  * ``QualityScore`` — frozen per-model quality snapshot (EWMA-smoothed)
  * ``ewma_update`` — exponentially-weighted moving average primitive
  * ``valid_tok_per_s`` / ``triage_fitness`` — composite fitness scalars
  * ``FleetCalibrationStore`` — atomic-write JSON store of per-model scores
  * ``fleet_rerank`` — PURE quality-weighted re-rank (preserves unscored slots)
  * ``graduation_ready`` — PURE graduation-decision gate
  * ``fleet_apply_rerank`` — fail-soft store-backed re-rank convenience

Persistence mirrors ``dw_promotion_ledger.py`` style: atomic temp+rename,
lazy load, never raises out of telemetry/query paths. Leaf module —
stdlib only, no sibling/organism imports (split-brain guard).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quality score record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityScore:
    """Frozen per-model quality snapshot (EWMA-smoothed fields).

    Fields are persisted as a flat JSON object via ``to_json_dict`` /
    ``from_json_dict``.
    """

    model_id: str
    ast_pass_rate: float
    label_adherence: float
    ttft_ms: float
    tok_per_s: float
    sample_count: int
    updated_at: float

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "ast_pass_rate": self.ast_pass_rate,
            "label_adherence": self.label_adherence,
            "ttft_ms": self.ttft_ms,
            "tok_per_s": self.tok_per_s,
            "sample_count": self.sample_count,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json_dict(cls, raw: Mapping[str, Any]) -> Optional["QualityScore"]:
        """Parse a persisted record. Returns None on malformed input."""
        try:
            mid = str(raw.get("model_id", "")).strip()
            if not mid:
                return None
            return cls(
                model_id=mid,
                ast_pass_rate=float(raw.get("ast_pass_rate", 0.0) or 0.0),
                label_adherence=float(raw.get("label_adherence", 0.0) or 0.0),
                ttft_ms=float(raw.get("ttft_ms", 0.0) or 0.0),
                tok_per_s=float(raw.get("tok_per_s", 0.0) or 0.0),
                sample_count=max(0, int(raw.get("sample_count", 0) or 0)),
                updated_at=float(raw.get("updated_at", 0.0) or 0.0),
            )
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Pure math primitives
# ---------------------------------------------------------------------------


def ewma_update(prev: Optional[float], new: float, alpha: float) -> float:
    """Exponentially-weighted moving average.

    First sample (``prev is None`` or an unseeded ``NaN`` carry) returns the
    new value directly so the series starts unbiased. Otherwise
    ``alpha*new + (1-alpha)*prev``.
    """
    if prev is None or prev != prev:  # None or NaN -> seed directly
        return float(new)
    return alpha * new + (1.0 - alpha) * prev


def valid_tok_per_s(sc: QualityScore) -> float:
    """Throughput of VALID code: tokens/s weighted by AST-pass rate.

    A blazing-fast model that emits unparseable code scores ~0 here.
    """
    return sc.tok_per_s * sc.ast_pass_rate


def triage_fitness(sc: QualityScore) -> float:
    """Triage fitness: classification adherence per millisecond of latency.

    Rewards correct labels delivered fast. ttft clamped to >=1ms to avoid
    division blowups.
    """
    return sc.label_adherence / max(sc.ttft_ms, 1.0) * 1000.0


# ---------------------------------------------------------------------------
# Env getters (read at call time so tests + operators can flip)
# ---------------------------------------------------------------------------


def _alpha() -> float:
    """``JARVIS_FLEET_EWMA_ALPHA`` (default 0.4). Bad values -> 0.4."""
    try:
        return float(os.environ.get("JARVIS_FLEET_EWMA_ALPHA", "0.4").strip())
    except (ValueError, TypeError):
        return 0.4


def _path() -> Path:
    """``JARVIS_FLEET_CALIBRATION_PATH`` (default
    ``.jarvis/fleet_calibration.json``)."""
    raw = os.environ.get(
        "JARVIS_FLEET_CALIBRATION_PATH",
        ".jarvis/fleet_calibration.json",
    ).strip()
    return Path(raw)


def _min_ast() -> float:
    """``JARVIS_FLEET_GRAD_MIN_AST`` (default 0.8). Bad values -> 0.8."""
    try:
        return float(os.environ.get("JARVIS_FLEET_GRAD_MIN_AST", "0.8").strip())
    except (ValueError, TypeError):
        return 0.8


def route_kind_for_route(route: str) -> str:
    """Map a route string to a fitness kind: ``triage`` or ``code``."""
    norm = (route or "").strip().lower()
    if norm in {"triage", "semantic_triage", "classify"}:
        return "triage"
    return "code"


# ---------------------------------------------------------------------------
# Atomic disk I/O (mirrored from dw_promotion_ledger.py)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Calibration store
# ---------------------------------------------------------------------------


class FleetCalibrationStore:
    """Per-model quality-score store with atomic JSON persistence.

    Lazy load on first access; ``save()`` writes atomically. Telemetry +
    query paths NEVER raise.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path  # resolved lazily so env can be patched
        self._scores: Dict[str, QualityScore] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _path()

    def load(self) -> None:
        """Load from disk. Missing/corrupt -> empty store. NEVER raises."""
        self._loaded = True
        p = self._resolved_path()
        if not p.exists():
            return
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "[FleetCalibrationStore] corrupt or unreadable store at %s — "
                "starting empty (%s)", p, exc,
            )
            return
        if not isinstance(payload, Mapping):
            return
        scores_raw = payload.get("scores", {})
        if not isinstance(scores_raw, Mapping):
            return
        for raw in scores_raw.values():
            if not isinstance(raw, Mapping):
                continue
            sc = QualityScore.from_json_dict(raw)
            if sc is not None:
                self._scores[sc.model_id] = sc

    def save(self) -> None:
        """Write current state to disk atomically. NEVER raises; logs."""
        payload = {
            "scores": {
                mid: self._clamp_unseeded(sc).to_json_dict()
                for mid, sc in self._scores.items()
            },
        }
        try:
            _atomic_write(
                self._resolved_path(),
                json.dumps(payload, sort_keys=True, indent=2),
            )
        except OSError as exc:
            logger.warning(
                "[FleetCalibrationStore] save failed: %s — "
                "store remains in memory", exc,
            )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Telemetry input
    # ------------------------------------------------------------------

    def record_probe(
        self,
        model_id: str,
        *,
        kind: str,
        code_pass: Optional[bool] = None,
        label_score: Optional[float] = None,
        ttft_ms: float,
        tok_per_s: float,
        now: float,
    ) -> None:
        """Fold one probe result into the model's EWMA-smoothed score.

        ``kind="code"`` updates ``ast_pass_rate`` from ``code_pass``;
        ``kind="triage"`` updates ``label_adherence`` from ``label_score``.
        ttft + tok_per_s always folded. NEVER raises.
        """
        try:
            mid = str(model_id or "").strip()
            if not mid:
                return
            self._ensure_loaded()
            alpha = _alpha()
            prev = self._scores.get(mid)

            # An untouched field on a brand-new model is left UNSEEDED (NaN)
            # so the first probe of the *other* kind seeds it directly instead
            # of EWMA-blending against a poisoning 0.0. NaN is clamped to 0.0
            # on every read/persist surface, so it never escapes the store.
            _unseeded = float("nan")
            prev_ast = prev.ast_pass_rate if prev else _unseeded
            prev_label = prev.label_adherence if prev else _unseeded
            prev_ttft = prev.ttft_ms if prev else None
            prev_tps = prev.tok_per_s if prev else None

            if kind == "code":
                new_ast = ewma_update(
                    prev.ast_pass_rate if prev else None,
                    1.0 if code_pass else 0.0,
                    alpha,
                )
                new_label = prev_label
            elif kind == "triage":
                new_label = ewma_update(
                    prev.label_adherence if prev else None,
                    float(label_score or 0.0),
                    alpha,
                )
                new_ast = prev_ast
            else:
                # Unknown kind — only fold latency/throughput.
                new_ast = prev_ast
                new_label = prev_label

            new_ttft = ewma_update(prev_ttft, float(ttft_ms), alpha)
            new_tps = ewma_update(prev_tps, float(tok_per_s), alpha)

            self._scores[mid] = QualityScore(
                model_id=mid,
                ast_pass_rate=new_ast,
                label_adherence=new_label,
                ttft_ms=new_ttft,
                tok_per_s=new_tps,
                sample_count=(prev.sample_count if prev else 0) + 1,
                updated_at=float(now),
            )
        except Exception:  # noqa: BLE001 — defensive: never take down caller
            logger.debug(
                "[FleetCalibrationStore] record_probe failed for %s",
                model_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Queries (read-only)
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp_unseeded(sc: QualityScore) -> QualityScore:
        """Replace any UNSEEDED (NaN) field with 0.0 so NaN never escapes the
        store to consumers (graduation gate, re-rank, persistence)."""
        ast = sc.ast_pass_rate
        lab = sc.label_adherence
        if ast == ast and lab == lab:  # both already real (no NaN)
            return sc
        return QualityScore(
            model_id=sc.model_id,
            ast_pass_rate=ast if ast == ast else 0.0,
            label_adherence=lab if lab == lab else 0.0,
            ttft_ms=sc.ttft_ms,
            tok_per_s=sc.tok_per_s,
            sample_count=sc.sample_count,
            updated_at=sc.updated_at,
        )

    def score(self, model_id: str) -> Optional[QualityScore]:
        self._ensure_loaded()
        sc = self._scores.get(model_id)
        return self._clamp_unseeded(sc) if sc is not None else None

    def all_scores(self) -> Dict[str, QualityScore]:
        self._ensure_loaded()
        return {mid: self._clamp_unseeded(sc) for mid, sc in self._scores.items()}


# ---------------------------------------------------------------------------
# Pure re-rank + graduation decision
# ---------------------------------------------------------------------------


def fleet_rerank(
    _route: str,
    ranked_models: Tuple[str, ...],
    scores: Mapping[str, QualityScore],
    *,
    route_kind: str,
) -> Tuple[str, ...]:
    """PURE quality-weighted re-rank. NEVER raises.

    ``_route`` is accepted positionally for caller symmetry (the route
    string callers already hold) but behavior is driven entirely by the
    explicit keyword-only ``route_kind`` so the pure function stays
    trivially testable; the raw route is intentionally unused here.

    Reorders ONLY the scored models among themselves (descending fitness,
    stable), keeping every UNSCORED model at its original index. Returns
    input unchanged when fewer than 2 scored models are present, or on
    unknown ``route_kind``.
    """
    try:
        if route_kind == "code":
            key = valid_tok_per_s
        elif route_kind == "triage":
            key = triage_fitness
        else:
            return tuple(ranked_models)

        ranked = tuple(ranked_models)
        scored = [
            m for m in ranked
            if m in scores and scores[m].sample_count >= 1
        ]
        if len(scored) < 2:
            return ranked

        ranked_scored = sorted(
            scored, key=lambda m: key(scores[m]), reverse=True,
        )
        scored_set = set(scored)
        out = []
        idx = 0
        for m in ranked:
            if m in scored_set:
                out.append(ranked_scored[idx])
                idx += 1
            else:
                out.append(m)
        return tuple(out)
    except Exception:  # noqa: BLE001 — pure + defensive
        return tuple(ranked_models)


def graduation_ready(
    scores: Mapping[str, QualityScore],
    *,
    default_model: str,
    min_samples: int,
    min_margin: float,
) -> Optional[str]:
    """PURE graduation-decision gate. Returns the winning model or None.

    A candidate qualifies when it is not the default, has enough samples,
    and meets the AST-pass floor. Among qualifiers the best ``valid_tok_per_s``
    wins, but only graduates if the default is broken (missing or
    ast_pass_rate < 0.2) OR the winner beats the default by ``min_margin``×.
    """
    try:
        candidates = [
            m for m, sc in scores.items()
            if m != default_model
            and sc.sample_count >= min_samples
            and sc.ast_pass_rate >= _min_ast()
        ]
        if not candidates:
            return None
        winner = max(candidates, key=lambda m: valid_tok_per_s(scores[m]))
        d = scores.get(default_model)
        if d is None or d.ast_pass_rate < 0.2:
            return winner
        if valid_tok_per_s(scores[winner]) >= min_margin * valid_tok_per_s(d):
            return winner
        return None
    except Exception:  # noqa: BLE001 — pure + defensive
        return None


def fleet_apply_rerank(
    route: str,
    ranked_models: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Fail-soft store-backed re-rank. Returns input unchanged on error."""
    try:
        st = FleetCalibrationStore()
        return fleet_rerank(
            route,
            tuple(ranked_models),
            st.all_scores(),
            route_kind=route_kind_for_route(route),
        )
    except Exception:  # noqa: BLE001 — fail-soft
        return tuple(ranked_models)


__all__ = [
    "QualityScore",
    "ewma_update",
    "valid_tok_per_s",
    "triage_fitness",
    "route_kind_for_route",
    "FleetCalibrationStore",
    "fleet_rerank",
    "graduation_ready",
    "fleet_apply_rerank",
]
