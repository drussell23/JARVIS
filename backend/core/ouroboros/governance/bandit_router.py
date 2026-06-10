"""Slice 201 — Contextual Bandit Routing Advisor (Thompson Sampling).

The provider/model-selection decision is a textbook contextual-bandit problem:
pick an arm (model), observe a reward (success × cost × latency), learn. This
module is the lightweight, classical online-learner for it — Thompson Sampling
over per-arm Beta posteriors. No deep RL, no GPU, no training loop; ~one
``random.betavariate`` per arm per decision.

ADVISORY-ONLY, STRUCTURALLY FAIL-CLOSED. The advisor reorders WITHIN the
caller's ``ranked_models`` list — which is already the brain_selection_policy
active set for the route (``topology.dw_models_for_route``). Because the
policy-bounded list is the advisor's entire input domain, it can NEVER select
an out-of-policy arm; the most it can do is change the *order* in which the
deterministic sentinel walker tries policy-permitted models. Whenever the
advisor is disabled, errors, or has no opinion it returns ``None`` and the
caller keeps the deterministic order. The hand-rolled router stays
authoritative — the bandit only advises.

Reward = (Success·W_s − Cost·W_c − Latency·W_l), mapped to [0,1] and folded
into the arm's Beta(α, β) posterior (the standard Bernoulli-Thompson
relaxation for continuous rewards: ``α += r``, ``β += (1−r)``). Cost and
latency are normalized against env-tunable scales; an unknown cost/latency is
a zero penalty, so a pure success/fail signal is still fully usable.

Gated ``JARVIS_BANDIT_ROUTER_ENABLED`` default-FALSE (it influences routing
order — authority-adjacent, opt-in). Durable state in
``.jarvis/bandit_router_state.json``. NEVER raises into the dispatch path.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_BANDIT_ROUTER_ENABLED"
_ENV_STATE_PATH = "JARVIS_BANDIT_STATE_PATH"
_DEFAULT_STATE_PATH = ".jarvis/bandit_router_state.json"


def bandit_router_enabled() -> bool:
    """Master gate (default FALSE — influences routing order). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v >= 0 else default
    except Exception:  # noqa: BLE001
        return default


def compute_reward(
    success: bool,
    cost_usd: Optional[float] = None,
    latency_s: Optional[float] = None,
) -> float:
    """Reward = (Success·W_s − Cost·W_c − Latency·W_l) mapped to [0,1].

    An unknown cost/latency contributes zero penalty (a pure success signal is
    still fully usable). NEVER raises."""
    try:
        ws = _envf("JARVIS_BANDIT_W_SUCCESS", 1.0)
        wc = _envf("JARVIS_BANDIT_W_COST", 0.3)
        wl = _envf("JARVIS_BANDIT_W_LATENCY", 0.2)
        cost_scale = _envf("JARVIS_BANDIT_COST_SCALE_USD", 0.05)
        lat_scale = _envf("JARVIS_BANDIT_LATENCY_SCALE_S", 60.0)

        succ_term = 1.0 if success else 0.0
        cost_norm = 0.0
        if cost_usd is not None and cost_scale > 0:
            cost_norm = min(1.0, max(0.0, float(cost_usd)) / cost_scale)
        lat_norm = 0.0
        if latency_s is not None and lat_scale > 0:
            lat_norm = min(1.0, max(0.0, float(latency_s)) / lat_scale)

        raw = ws * succ_term - wc * cost_norm - wl * lat_norm
        span = ws + wc + wl
        if span <= 0:
            return 1.0 if success else 0.0
        # map [-(wc+wl) .. ws] → [0 .. 1]
        reward = (raw + (wc + wl)) / span
        return min(1.0, max(0.0, reward))
    except Exception:  # noqa: BLE001
        return 1.0 if success else 0.0


class BanditRouter:
    """Thompson-sampling advisor over model arms. All methods NEVER raise."""

    def __init__(
        self,
        state_path: Optional[Path] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._path = Path(state_path) if state_path is not None else _state_path()
        self._rng = rng if rng is not None else random.Random()
        # arm -> {alpha, beta, n, cost_ema, latency_ema}
        self._arms: Dict[str, Dict[str, float]] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._arms = {
                        str(k): dict(v) for k, v in data.items()
                        if isinstance(v, dict)
                    }
        except Exception:  # noqa: BLE001
            self._arms = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._arms), encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def _arm(self, model_id: str) -> Dict[str, float]:
        a = self._arms.get(model_id)
        if a is None:
            a = {"alpha": 1.0, "beta": 1.0, "n": 0.0,
                 "cost_ema": 0.0, "latency_ema": 0.0}
            self._arms[model_id] = a
        return a

    # -- public API --------------------------------------------------------

    def record_outcome(
        self,
        model_id: str,
        success: bool,
        cost_usd: Optional[float] = None,
        latency_s: Optional[float] = None,
    ) -> None:
        """Fold one dispatch outcome into the arm's posterior. NEVER raises."""
        try:
            if not model_id or not isinstance(model_id, str):
                return
            reward = compute_reward(success, cost_usd, latency_s)
            with self._lock:
                a = self._arm(model_id)
                a["alpha"] = float(a.get("alpha", 1.0)) + reward
                a["beta"] = float(a.get("beta", 1.0)) + (1.0 - reward)
                a["n"] = float(a.get("n", 0.0)) + 1.0
                if cost_usd is not None:
                    a["cost_ema"] = 0.8 * float(a.get("cost_ema", 0.0)) \
                        + 0.2 * max(0.0, float(cost_usd))
                if latency_s is not None:
                    a["latency_ema"] = 0.8 * float(a.get("latency_ema", 0.0)) \
                        + 0.2 * max(0.0, float(latency_s))
                self._save()
        except Exception:  # noqa: BLE001
            pass

    def advise(self, arms: Optional[List[str]]) -> Optional[List[str]]:
        """Return ``arms`` reordered best-first by a Thompson sample of each
        arm's posterior. The result is always a permutation of the input
        (coerced to str) — never an invented or out-of-set arm. Returns None
        when the input is empty/None or on any error (caller keeps its order).
        NEVER raises."""
        try:
            if not arms:
                return None
            clean = [str(a) for a in arms if a is not None]
            if not clean:
                return None
            with self._lock:
                scored = []  # (input_index, sample, arm)
                for idx, arm in enumerate(clean):
                    a = self._arm(arm)
                    sample = self._rng.betavariate(
                        max(1e-6, float(a.get("alpha", 1.0))),
                        max(1e-6, float(a.get("beta", 1.0))),
                    )
                    scored.append((idx, sample, arm))
            # sort desc by sample; ties keep input order (stable via idx)
            scored.sort(key=lambda t: (-t[1], t[0]))
            return [arm for _, _, arm in scored]
        except Exception:  # noqa: BLE001
            return None

    def best_arm(self, arms: Optional[List[str]]) -> Optional[str]:
        order = self.advise(arms)
        return order[0] if order else None

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        try:
            with self._lock:
                return {k: dict(v) for k, v in self._arms.items()}
        except Exception:  # noqa: BLE001
            return {}


def _state_path() -> Path:
    raw = os.environ.get(_ENV_STATE_PATH, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_STATE_PATH)


_singleton: Optional[BanditRouter] = None
_singleton_lock = threading.Lock()


def get_bandit_router() -> BanditRouter:
    """Process-wide singleton. The advisory consult/record sites use this.
    When the master is OFF, advise() short-circuits to None so the singleton
    is inert (no routing influence). NEVER raises."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = _GatedBanditRouter()
    return _singleton


def _reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None


class _GatedBanditRouter(BanditRouter):
    """The singleton variant: advise() is a no-op (None) unless the master
    flag is on, so an un-opted-in deployment has zero routing influence even
    if a consult site calls advise()."""

    def advise(self, arms: Optional[List[str]]) -> Optional[List[str]]:
        if not bandit_router_enabled():
            return None
        return super().advise(arms)
