"""Pre-Flight Critic — Phases 2-3 of the Self-Correction & DPO Alignment Engine.

A cheap local quality gate on a generated patch candidate BEFORE the expensive paths run — the heavy
sandbox test suite AND (critically) the frontier-model cascade. Targets the DoubleWord stability
problem directly: DW is the cheap primary provider but produces structurally-unreliable output
(malformed JSON, non-diff, schema drift); a local critic that predicts "this DW candidate will fail"
lets O+V catch it early and regenerate, instead of cascading to Claude (which costs money — and just
hit a zero-credit wall in the live soak).

Phase 2 — Predictive Failure Probability: ``predict_failure(candidate, context)`` calls the
fine-tuned local model served by Reactor-Core (Llama-3.2-3B) → a 0-1 failure probability.

Phase 3 — Stochastic gating + policy feedback: if the failure probability crosses the threshold,
``evaluate()`` short-circuits — bypass the sandbox, convert the critic's inference into a prompt
constraint clause, and signal "route back to generation" for an instant targeted correction.

**Model-collapse / confirmation-bias armor:** the critic is ADVISORY pressure, not ground truth. A
sampling floor (``JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE``) always lets some candidates through to the
real sandbox regardless of the critic's verdict, so the empirical test signal keeps flowing and the
loop can never collapse into trusting the critic blindly.

**Honest status:** this is the CONSUMER of the training loop. It is **inert until a critic model is
actually trained (by the RepairTrajectoryEmitter → Reactor pipeline) and served** — with no model,
``predict_failure`` returns ``None`` and ``evaluate`` never short-circuits (the system behaves exactly
as today). Gated ``JARVIS_PREFLIGHT_CRITIC_ENABLED`` (default OFF). Fail-soft throughout.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["PreflightCritic", "CriticVerdict", "critic_enabled"]


def critic_enabled() -> bool:
    """``JARVIS_PREFLIGHT_CRITIC_ENABLED`` (default OFF) — master for the local pre-flight critic."""
    return os.environ.get("JARVIS_PREFLIGHT_CRITIC_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _fail_threshold() -> float:
    """``JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD`` (default 0.85) — predicted-failure prob above which
    the candidate is short-circuited back to generation."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD", "0.85"))))
    except ValueError:
        return 0.85


def _sample_rate() -> float:
    """``JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE`` (default 0.1) — fraction of candidates that ALWAYS go to
    the real sandbox regardless of the critic, keeping the empirical signal alive (anti-collapse)."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE", "0.1"))))
    except ValueError:
        return 0.1


@dataclass
class CriticVerdict:
    """The critic's pre-flight judgment on a candidate."""
    failure_probability: Optional[float]   # None → critic unavailable / inert
    short_circuit: bool                    # True → skip sandbox, route back to generation
    constraint_clause: str = ""            # prompt constraint injected on short-circuit
    reason: str = ""


class PreflightCritic:
    """Local pre-flight critic gate. Injectable inference fn (production = Reactor model_server)."""

    def __init__(self, infer: Any = None, *, sampler: Any = None) -> None:
        # infer: async callable(candidate_source, context) -> Optional[float] (failure prob)
        self._infer = infer
        # sampler: callable() -> float in [0,1) for the anti-collapse always-sandbox draw
        self._sampler = sampler

    async def predict_failure(self, candidate_source: str, context: str = "") -> Optional[float]:
        """Phase 2: predicted failure probability for *candidate_source*. None when no critic model is
        available (inert). Fail-soft."""
        if not candidate_source:
            return None
        infer = self._infer
        if infer is None:
            infer = await self._resolve_reactor_infer()
        if infer is None:
            return None  # no served critic model yet → inert
        try:
            import asyncio
            res = infer(candidate_source, context)
            prob = await res if asyncio.iscoroutine(res) else res
            if prob is None:
                return None
            return min(1.0, max(0.0, float(prob)))
        except Exception as exc:  # noqa: BLE001 — critic is advisory; never break the loop
            logger.debug("[PreflightCritic] inference failed (non-fatal): %s", exc)
            return None

    async def _resolve_reactor_infer(self) -> Any:
        """Resolve the inference callable. Default = the M1-native OnlineTopologicalCritic (on-device,
        no CUDA/cloud). It is meaningful only once it has learned from real trajectories; an untrained
        critic returns ``None`` (inert) so the gate never acts on noise. Fail-soft."""
        try:
            critic = get_default_critic()
            if not critic.is_warm():
                return None  # not enough learned signal yet → inert (never gate on a cold model)
            def _infer(src: str, ctx: str = "") -> Optional[float]:
                return critic.predict_failure(src, file_path="", graph=None)
            self._infer = _infer
            return _infer
        except Exception:  # noqa: BLE001
            return None

    async def evaluate(self, candidate_source: str, context: str = "") -> CriticVerdict:
        """Phase 3: predict + gate. Short-circuits (skip sandbox → regenerate) only when the critic is
        confident the candidate will fail AND this candidate wasn't drawn into the anti-collapse
        always-sandbox sample. Inert (never short-circuits) when disabled / no model."""
        if not critic_enabled():
            return CriticVerdict(failure_probability=None, short_circuit=False, reason="disabled")
        prob = await self.predict_failure(candidate_source, context)
        if prob is None:
            return CriticVerdict(failure_probability=None, short_circuit=False, reason="no_critic_model")
        if prob < _fail_threshold():
            return CriticVerdict(failure_probability=prob, short_circuit=False,
                                 reason=f"below_threshold({prob:.2f})")
        # Anti-collapse: always let a sampled fraction reach the real sandbox even if the critic
        # predicts failure — preserves the empirical signal that would retrain/calibrate the critic.
        draw = self._sampler() if self._sampler is not None else _deterministic_draw(candidate_source)
        if draw < _sample_rate():
            return CriticVerdict(failure_probability=prob, short_circuit=False,
                                 reason=f"anti_collapse_sampled({prob:.2f})")
        clause = (
            "## PRE-FLIGHT CRITIC CONSTRAINT\n"
            f"A local critic predicts this candidate is very likely to FAIL validation "
            f"(p_fail={prob:.0%}). Do NOT resubmit it as-is. Reconsider the approach — common "
            "structural failure modes to avoid: malformed/incomplete output, wrong response schema "
            "(emit a valid 2b.1-diff), and breaking the failing test's contract. Produce a "
            "materially different candidate."
        )
        return CriticVerdict(failure_probability=prob, short_circuit=True, constraint_clause=clause,
                             reason=f"short_circuit(p_fail={prob:.2f})")


def _deterministic_draw(s: str) -> float:
    """Deterministic [0,1) draw from the candidate text (no RNG — auditable, reproducible)."""
    import hashlib
    h = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()
    return (int(h[:8], 16) % 10000) / 10000.0


# ===========================================================================
# M1-Native Online Topological Critic (on-device, ONNX/Metal + numpy SGD)
# ===========================================================================
# Replaces the CUDA 3B-LoRA dependency with a lightweight, hardware-native engine that trains AND
# infers on the M1 in milliseconds: a multi-modal Structural-Semantic Risk Tensor (local fastembed
# code vector ⊕ graph-topology metrics) fed to an online logistic head updated per trajectory via
# prequential (test-then-train) SGD. No heavy daemons, no unified-memory blowup.

_HASH_DIM = 256  # zero-dependency hashing featurizer width (used when fastembed is unavailable)


def _hashing_features(text: str, dim: int = _HASH_DIM) -> "Any":
    """Deterministic hashing-trick token vector — instant, zero-dep, always-works M1 baseline."""
    import numpy as np
    import re
    vec = np.zeros(dim, dtype=np.float32)
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[^\sA-Za-z0-9]", text)[:4000]:
        vec[hash(tok) % dim] += 1.0
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0 else vec


def _semantic_features(text: str) -> "Any":
    """Local fastembed (ONNX-CoreML bge-small) code vector; None if unavailable (→ hashing fallback)."""
    try:
        from backend.core.ouroboros.governance.semantic_index import _embedder_factory  # type: ignore
        emb = _embedder_factory()
        vecs = emb.embed([text[:8000]]) if emb is not None else None
        if vecs:
            import numpy as np
            return np.asarray(vecs[0], dtype=np.float32)
    except Exception:  # noqa: BLE001
        pass
    return None


def _topology_features(file_path: str, graph: Any) -> "Any":
    """Structural graph-topology metrics for the target file: local degree (in/out — a cheap, honest
    proxy for centrality; full betweenness over ~29k nodes per-op is impractical) + blast-radius depth.
    log-scaled; zeros when unavailable. Fail-soft."""
    import numpy as np
    feats = np.zeros(4, dtype=np.float32)
    if graph is None or not file_path:
        return feats
    try:
        nodes = graph.find_nodes_in_file(file_path) or []
        in_deg = out_deg = blast = 0
        for n in nodes[:8]:
            try:
                out_deg += len(graph.get_dependencies(n) or [])
                in_deg += len(graph.get_dependents(n) or [])
                b = graph.compute_blast_radius(n)
                blast += len(getattr(b, "transitively_affected", set()) or set())
            except Exception:  # noqa: BLE001
                continue
        feats[0] = np.log1p(in_deg)
        feats[1] = np.log1p(out_deg)
        feats[2] = np.log1p(blast)
        feats[3] = np.log1p(len(nodes))
    except Exception:  # noqa: BLE001
        pass
    return feats


def featurize(candidate_source: str, file_path: str = "", graph: Any = None) -> "Any":
    """Compile the multi-modal Structural-Semantic Risk Tensor: code vector ⊕ topology metrics ⊕ bias."""
    import numpy as np
    sem = _semantic_features(candidate_source)
    code_vec = sem if sem is not None else _hashing_features(candidate_source)
    topo = _topology_features(file_path, graph)
    return np.concatenate([code_vec, topo, np.ones(1, dtype=np.float32)])


class OnlineTopologicalCritic:
    """Online logistic critic (numpy SGD) over the Structural-Semantic Risk Tensor. Prequential
    test-then-train: each sample is first PREDICTED (for an honest running accuracy) then learned —
    in milliseconds, on the M1, no offline daemon. Persists weights to ``.jarvis/preflight_critic.npz``."""

    def __init__(self, *, lr: float = 0.05, l2: float = 1e-4, path: Optional[str] = None) -> None:
        self._lr = lr
        self._l2 = l2
        self._w = None            # lazily sized to the feature dim on first sample
        self._n = 0               # real samples learned
        self._correct = 0         # prequential correct predictions
        self._recent: list = []   # rolling window of recent correctness (for windowed accuracy)
        self._path = path or os.path.join(
            os.environ.get("JARVIS_HOME", os.path.join(os.path.expanduser("~"), ".jarvis")),
            "preflight_critic.npz",
        )
        self._load()

    # ---- model ----
    @staticmethod
    def _sigmoid(z: float) -> float:
        import math
        if z < -60:
            return 0.0
        if z > 60:
            return 1.0
        return 1.0 / (1.0 + math.exp(-z))

    def _ensure(self, dim: int) -> None:
        import numpy as np
        if self._w is None:
            self._w = np.zeros(dim, dtype=np.float32)

    def predict_failure(self, candidate_source: str, file_path: str = "", graph: Any = None) -> Optional[float]:
        """p(fail) for a candidate. None if the model is cold (no learned signal)."""
        if self._w is None or self._n == 0:
            return None
        import numpy as np
        x = featurize(candidate_source, file_path, graph)
        if x.shape[0] != self._w.shape[0]:
            return None  # feature dim changed (embedder swapped) → treat as cold
        return float(self._sigmoid(float(np.dot(self._w, x))))

    def _update(self, x: "Any", y: int) -> None:
        import numpy as np
        self._ensure(x.shape[0])
        w = self._w
        if w is None or x.shape[0] != w.shape[0]:
            return
        p = self._sigmoid(float(np.dot(w, x)))
        # prequential: score BEFORE the update
        pred = 1 if p >= 0.5 else 0
        ok = int(pred == y)
        self._correct += ok
        self._recent.append(ok)
        if len(self._recent) > 200:
            self._recent.pop(0)
        self._n += 1
        self._w = w - self._lr * ((p - y) * x + self._l2 * w)

    def learn_pair(self, rejected_source: str, chosen_source: str,
                   file_path: str = "", graph: Any = None) -> None:
        """Learn one DPO pair: rejected → fail(1), chosen → pass(0). The core online step."""
        if rejected_source:
            self._update(featurize(rejected_source, file_path, graph), 1)
        if chosen_source:
            self._update(featurize(chosen_source, file_path, graph), 0)
        self._save()

    # ---- status / graduation ----
    def samples(self) -> int:
        return self._n

    def windowed_accuracy(self) -> float:
        return (sum(self._recent) / len(self._recent)) if self._recent else 0.0

    def is_warm(self) -> bool:
        """Enough learned signal to be worth consulting at all (not graduation — just non-cold)."""
        try:
            floor = int(os.environ.get("JARVIS_PREFLIGHT_CRITIC_WARM_SAMPLES", "40"))
        except ValueError:
            floor = 40
        return self._n >= floor

    def is_graduation_ready(self) -> bool:
        """Honest auto-graduation bar: enough REAL samples AND a prequential accuracy above threshold.
        A synthetic suite can prove the MECHANISM, but gating production requires real accumulated
        accuracy — so a cold/undertrained critic never auto-enables."""
        try:
            min_n = int(os.environ.get("JARVIS_PREFLIGHT_CRITIC_GRAD_SAMPLES", "200"))
            min_acc = float(os.environ.get("JARVIS_PREFLIGHT_CRITIC_GRAD_ACCURACY", "0.8"))
        except ValueError:
            min_n, min_acc = 200, 0.8
        return self._n >= min_n and self.windowed_accuracy() >= min_acc

    # ---- persistence ----
    def _save(self) -> None:
        try:
            import numpy as np
            if self._w is None:
                return
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            np.savez(self._path, w=self._w, n=np.array([self._n]),
                     correct=np.array([self._correct]), recent=np.array(self._recent, dtype=np.int8))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[OnlineCritic] save failed (non-fatal): %s", exc)

    def _load(self) -> None:
        try:
            import numpy as np
            if not os.path.isfile(self._path):
                return
            d = np.load(self._path, allow_pickle=False)
            self._w = d["w"].astype(np.float32)
            self._n = int(d["n"][0])
            self._correct = int(d["correct"][0])
            self._recent = list(int(x) for x in d.get("recent", np.array([], dtype=np.int8)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[OnlineCritic] load failed (non-fatal): %s", exc)

    # ---- async offload (Metal/CPU executor ring — zero latency on the control bus) ----
    async def alearn_pair(self, rejected_source: str, chosen_source: str,
                          file_path: str = "", graph: Any = None) -> None:
        import asyncio
        await asyncio.to_thread(self.learn_pair, rejected_source, chosen_source, file_path, graph)

    async def apredict_failure(self, candidate_source: str, file_path: str = "", graph: Any = None):
        import asyncio
        return await asyncio.to_thread(self.predict_failure, candidate_source, file_path, graph)


_DEFAULT_CRITIC: Optional[OnlineTopologicalCritic] = None


def get_default_critic() -> OnlineTopologicalCritic:
    """Process-wide singleton (shared by the gate + the trajectory emitter; persistent on disk)."""
    global _DEFAULT_CRITIC
    if _DEFAULT_CRITIC is None:
        _DEFAULT_CRITIC = OnlineTopologicalCritic()
    return _DEFAULT_CRITIC
