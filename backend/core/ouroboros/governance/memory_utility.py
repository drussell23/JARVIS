"""Did injecting this memory actually help — and does the router know?

Selection was open-loop. `ModuleContextRouter` ranked topics by structural
overlap and embedding similarity, injected three, and never learned anything
from what happened next. A topic present in forty ops that all verified and
a topic present in twelve that all failed VALIDATE ranked identically,
forever, because nothing joined the two facts.

Closing that loop is the move CC structurally cannot make: it has no
operation outcomes to learn from. O+V terminates every op with a measured
verdict, and since this arc the admission ledger records exactly which
topics were in the prompt when it did.

The join key is the payload
---------------------------
`content_hash`, never the path. A topic that moves directory keeps its
history; a topic whose body is EDITED gets a new hash and starts neutral —
which is correct, not a bug. The evidence was about the old text. Carrying a
rewritten topic's reputation forward would be attributing outcomes to words
that were not in the prompt.

Reuses the outcome vocabulary that already exists
--------------------------------------------------
Polarity (`_outcome_polarity_weight`) and the exponential half-life
(`action_outcome_recency_halflife_days`) come from
:mod:`action_outcome_memory`. A second definition of "how good is
APPLIED_REVERTED" or "how fast does evidence age" would be a second policy
that silently disagrees with the first. Near-duplicate detection reuses
:mod:`module_routing`'s `content_hash`-keyed embedding cache — the vectors
are already computed and on disk.

Neutral is the corpus, not a constant
--------------------------------------
A topic is promoted iff it outperforms the DECAYED CORPUS MEAN, and demoted
iff it underperforms it. That matters for more than elegance: a hardcoded
midpoint would make the whole corpus drift up or down together during a good
or bad week, so every topic would be re-ranked by the weather rather than by
its own contribution. Measuring against the corpus cancels that exactly, and
it needs no magic constant to define "average".

Cold start is neutral, never negative
--------------------------------------
No observations → utility 1.0, the score the ranker produced before this
module existed. Absence of evidence must not demote — the same invariant
`Drift.UNKNOWN` keeps in :mod:`memory_corpus`, for the same reason: a
penalty applied to the unmeasured is a corpus rewrite disguised as learning.

Confidence saturates
--------------------
One observation is not a verdict. The influence of a topic's history is
scaled by ``1 - exp(-mass / scale)``, so a single outcome nudges and a
sustained pattern moves it. This is the guard against FALSE ATTRIBUTION,
which is real and unavoidable here: an op fails for reasons that usually
have nothing to do with the three topics that happened to be in its prompt.
The design answer is not to pretend otherwise but to make one coincidence
cost almost nothing and a repeated correlation cost something.

Advisory, never authority
-------------------------
This produces a rank MULTIPLIER. It cannot admit a topic the ranker did not
select, cannot exclude one it did, and is never consulted by any gate.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.MemoryUtility")

MEMORY_UTILITY_SCHEMA_VERSION: str = "memory_utility.1"

__all__ = [
    "MEMORY_UTILITY_SCHEMA_VERSION",
    "Observation",
    "UtilityReading",
    "arm_outcome_listener",
    "corpus_mean_polarity",
    "observe_op_outcome",
    "reading_for",
    "reset_for_tests",
    "utility_enabled",
    "utility_for",
]


# ---------------------------------------------------------------------------
# Knobs — every one env-tunable and clamped; none is a threshold in disguise
# ---------------------------------------------------------------------------


def _flag(name: str, default: str = "1") -> bool:
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "").strip() or default)))
    except Exception:  # noqa: BLE001
        return default


def utility_enabled() -> bool:
    """``JARVIS_MEMORY_UTILITY_ENABLED`` (default true).

    OFF makes :func:`utility_for` return exactly 1.0 for everything, which is
    a byte-identical ranking rollback rather than a different code path.
    """
    return _flag("JARVIS_MEMORY_UTILITY_ENABLED", "1")


def _gain() -> float:
    """How far a fully-confident, maximally-divergent topic may move.

    ``JARVIS_MEMORY_UTILITY_GAIN``. Deliberately modest: the ranker's
    structural and semantic signals MEASURE relevance to this op, while this
    one infers a correlation across ops. The weaker evidence gets the smaller
    lever.
    """
    return _num("JARVIS_MEMORY_UTILITY_GAIN", 0.5, 0.0, 2.0)


def _evidence_scale() -> float:
    """Decayed observation mass at which confidence reaches ~63%.

    ``JARVIS_MEMORY_UTILITY_EVIDENCE_SCALE``. A SCALE, not a threshold —
    nothing switches when it is crossed; the curve is smooth everywhere, so
    there is no cliff for a corpus to sit on the wrong side of.
    """
    return _num("JARVIS_MEMORY_UTILITY_EVIDENCE_SCALE", 3.0, 0.5, 100.0)


def _clamp_bounds() -> Tuple[float, float]:
    """Hard floor/ceiling on the multiplier. Bounded so no amount of history
    can silence a topic the structural signal says is directly on-target."""
    lo = _num("JARVIS_MEMORY_UTILITY_MIN", 0.25, 0.01, 1.0)
    hi = _num("JARVIS_MEMORY_UTILITY_MAX", 1.75, 1.0, 10.0)
    return lo, hi


def _near_dup_threshold() -> float:
    """Cosine above which two topics are treated as saying the same thing.

    ``JARVIS_MEMORY_NEAR_DUP_COSINE``. High by construction: at 0.97 the
    embeddings are near-identical, so propagating a failure signal is closer
    to bookkeeping than to inference.
    """
    return _num("JARVIS_MEMORY_NEAR_DUP_COSINE", 0.97, 0.5, 0.9999)


def _near_dup_enabled() -> bool:
    return _flag("JARVIS_MEMORY_NEAR_DUP_PROPAGATION", "1")


def _max_observations() -> int:
    """Ring bound on the persisted log. ``JARVIS_MEMORY_UTILITY_MAX_OBS``."""
    return int(_num("JARVIS_MEMORY_UTILITY_MAX_OBS", 20000, 100, 1000000))


def _halflife_days() -> float:
    """Exponential half-life, borrowed from :mod:`action_outcome_memory`.

    Borrowed rather than redeclared: two half-lives for "how fast does an
    outcome stop mattering" would be two policies that disagree without
    anyone noticing.
    """
    try:
        from backend.core.ouroboros.governance.action_outcome_memory import (
            action_outcome_recency_halflife_days,
        )
        return float(action_outcome_recency_halflife_days())
    except Exception:  # noqa: BLE001
        return 14.0


# ---------------------------------------------------------------------------
# Observation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One (topic, outcome) datum.

    ``weight`` is the polarity in [0, 1] from ``action_outcome_memory``'s
    active preset. ``credit`` scales a propagated signal by its similarity to
    the topic that actually earned it — a direct observation has credit 1.0.
    """

    content_hash: str
    weight: float
    at: float
    op_id: str = ""
    credit: float = 1.0
    source: str = "direct"

    def decay(self, now: float, halflife_days: float) -> float:
        """Decayed evidence mass at *now*. NEVER raises, never negative."""
        try:
            age_days = max(0.0, (now - self.at) / 86400.0)
            hl = max(1e-6, float(halflife_days))
            return max(0.0, self.credit) * math.exp(
                -math.log(2.0) * age_days / hl)
        except Exception:  # noqa: BLE001
            return 0.0

    def as_json(self) -> str:
        return json.dumps({
            "h": self.content_hash, "w": round(self.weight, 6),
            "t": round(self.at, 3), "op": self.op_id,
            "c": round(self.credit, 6), "s": self.source,
        }, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Optional["Observation"]:
        try:
            blob = json.loads(line)
            return cls(
                content_hash=str(blob["h"]), weight=float(blob["w"]),
                at=float(blob["t"]), op_id=str(blob.get("op", "")),
                credit=float(blob.get("c", 1.0)),
                source=str(blob.get("s", "direct")),
            )
        except Exception:  # noqa: BLE001
            return None


@dataclass(frozen=True)
class UtilityReading:
    """A topic's standing, with the evidence that produced it."""

    content_hash: str
    multiplier: float
    polarity: Optional[float]
    corpus_polarity: Optional[float]
    mass: float
    confidence: float
    observations: int

    @property
    def cold(self) -> bool:
        """True when nothing was ever observed — reported, not inferred."""
        return self.observations == 0

    def describe(self) -> str:
        if self.cold:
            return "neutral (no outcomes observed)"
        direction = "promoted" if self.multiplier > 1.0 else (
            "demoted" if self.multiplier < 1.0 else "neutral")
        return (f"{direction} ×{self.multiplier:.2f} "
                f"(p={self.polarity:.2f} vs corpus "
                f"{self.corpus_polarity:.2f}, conf {self.confidence:.2f}, "
                f"n={self.observations})")


_NEUTRAL = UtilityReading("", 1.0, None, None, 0.0, 0.0, 0)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class UtilityStore:
    """In-memory index over an append-only observation log. Thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._by_hash: Dict[str, List[Observation]] = {}
        self._count = 0
        self._loaded = False

    # --- persistence -----------------------------------------------------

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if self._path is None or not self._path.is_file():
                return
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        obs = Observation.from_json(line)
                        if obs is not None:
                            self._by_hash.setdefault(
                                obs.content_hash, []).append(obs)
                            self._count += 1
            except Exception:  # noqa: BLE001
                logger.debug("[MemoryUtility] log read degraded", exc_info=True)

    def _append_disk(self, observations: Iterable[Observation]) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                for obs in observations:
                    handle.write(obs.as_json() + "\n")
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryUtility] log append degraded", exc_info=True)

    def _compact_if_needed(self) -> None:
        """Rewrite the log keeping the newest observations. NEVER raises.

        Append-only in normal operation (§8); compaction is the bound that
        makes append-only survivable, and it drops the OLDEST — the ones the
        half-life has already made nearly weightless.
        """
        cap = _max_observations()
        if self._count <= cap or self._path is None:
            return
        try:
            flat = sorted(
                (o for obs in self._by_hash.values() for o in obs),
                key=lambda o: o.at)[-cap:]
            self._by_hash = {}
            for obs in flat:
                self._by_hash.setdefault(obs.content_hash, []).append(obs)
            self._count = len(flat)
            tmp = self._path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                for obs in flat:
                    handle.write(obs.as_json() + "\n")
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryUtility] compaction degraded", exc_info=True)

    # --- write -----------------------------------------------------------

    def add(self, observations: List[Observation]) -> int:
        """Record *observations*. Returns how many landed. NEVER raises."""
        if not observations:
            return 0
        try:
            self._ensure_loaded()
            with self._lock:
                for obs in observations:
                    self._by_hash.setdefault(obs.content_hash, []).append(obs)
                    self._count += 1
                self._append_disk(observations)
                self._compact_if_needed()
            return len(observations)
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryUtility] add degraded", exc_info=True)
            return 0

    # --- read ------------------------------------------------------------

    def _decayed(self, content_hash: str, now: float,
                 hl: float) -> Tuple[float, float, int]:
        """``(mass, weighted_polarity_sum, n)`` for one hash."""
        obs = self._by_hash.get(content_hash, ())
        mass = 0.0
        acc = 0.0
        for entry in obs:
            d = entry.decay(now, hl)
            mass += d
            acc += d * entry.weight
        return mass, acc, len(obs)

    def corpus_polarity(self, now: float, hl: float) -> Optional[float]:
        """Decayed mean polarity across the whole store, or None if empty."""
        self._ensure_loaded()
        with self._lock:
            mass = 0.0
            acc = 0.0
            for obs in self._by_hash.values():
                for entry in obs:
                    d = entry.decay(now, hl)
                    mass += d
                    acc += d * entry.weight
        if mass <= 0.0:
            return None
        return acc / mass

    def reading(self, content_hash: str) -> UtilityReading:
        """One topic's :class:`UtilityReading`. NEVER raises."""
        try:
            if not utility_enabled() or not content_hash:
                return _NEUTRAL
            self._ensure_loaded()
            now = time.time()
            hl = _halflife_days()
            with self._lock:
                mass, acc, n = self._decayed(content_hash, now, hl)
            if n == 0 or mass <= 0.0:
                # Cold, or every observation has decayed to nothing. Both are
                # "no usable evidence", and both must be neutral.
                return UtilityReading(content_hash, 1.0, None, None,
                                      0.0, 0.0, n)

            corpus = self.corpus_polarity(now, hl)
            polarity = acc / mass
            if corpus is None:
                return UtilityReading(content_hash, 1.0, polarity, None,
                                      mass, 0.0, n)

            confidence = 1.0 - math.exp(-mass / max(1e-6, _evidence_scale()))
            raw = 1.0 + _gain() * confidence * (polarity - corpus)
            lo, hi = _clamp_bounds()
            return UtilityReading(
                content_hash, min(hi, max(lo, raw)), polarity, corpus,
                mass, confidence, n,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryUtility] reading degraded", exc_info=True)
            return _NEUTRAL

    def hashes(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(self._by_hash)


_store: Optional[UtilityStore] = None
_store_lock = threading.Lock()
_store_root: Optional[Path] = None


def _default_path() -> Optional[Path]:
    root = _store_root
    if root is None:
        try:
            root = Path(__file__).resolve().parents[4]
        except Exception:  # noqa: BLE001
            return None
    return root / ".jarvis" / "memory_utility.jsonl"


def get_store() -> UtilityStore:
    """The process-wide store. NEVER raises."""
    global _store  # noqa: PLW0603
    with _store_lock:
        if _store is None:
            _store = UtilityStore(_default_path())
        return _store


def reset_for_tests(root: Optional[Path] = None) -> None:
    """Drop the store, optionally rebinding its root. Test-only."""
    global _store, _store_root  # noqa: PLW0603
    with _store_lock:
        _store_root = root
        _store = None


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def utility_for(content_hash: str) -> float:
    """A topic's rank multiplier. 1.0 when cold or disabled. NEVER raises."""
    return get_store().reading(content_hash).multiplier


def reading_for(content_hash: str) -> UtilityReading:
    """A topic's full :class:`UtilityReading`. NEVER raises."""
    return get_store().reading(content_hash)


def corpus_mean_polarity() -> Optional[float]:
    """The decayed corpus mean, or None when nothing is recorded."""
    try:
        return get_store().corpus_polarity(time.time(), _halflife_days())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Write path — the join
# ---------------------------------------------------------------------------


def _polarity_for(passed: int, total: int) -> Optional[float]:
    """Map a VERIFY result to the shared polarity scale. None when unusable.

    A zero-total VERIFY is not a pass — it is a run that proved nothing, and
    crediting it would reward topics for ops that never tested anything.
    Returning None (not 0.0) keeps that distinct from a genuine failure.
    """
    try:
        if total <= 0:
            return None
        from backend.core.ouroboros.governance.action_outcome_memory import (
            OutcomeKind, _outcome_polarity_weight,
        )
        kind = (OutcomeKind.APPLIED_VERIFIED if passed >= total
                else OutcomeKind.APPLIED_REVERTED if passed > 0
                else OutcomeKind.REJECTED)
        return float(_outcome_polarity_weight(kind))
    except Exception:  # noqa: BLE001
        return None


def _near_duplicates(content_hash: str, universe: Iterable[str]) -> List[
        Tuple[str, float]]:
    """``(hash, cosine)`` for topics whose text is near-identical.

    Reads the embedding cache :mod:`module_routing` already persists, keyed
    by the same content hash. No embedding is computed here — a topic absent
    from the cache is simply skipped, so this can never become a hidden
    inference cost on the write path.
    """
    if not _near_dup_enabled():
        return []
    try:
        from backend.core.ouroboros.governance.module_routing import (
            _cosine_score, _emb_cache,
        )
        anchor = _emb_cache.get(content_hash)
        if not anchor:
            return []
        floor = _near_dup_threshold()
        out: List[Tuple[str, float]] = []
        for other in universe:
            if other == content_hash:
                continue
            vec = _emb_cache.get(other)
            if not vec:
                continue
            sim = _cosine_score(anchor, vec)
            if sim >= floor:
                out.append((other, float(sim)))
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryUtility] near-dup scan degraded", exc_info=True)
        return []


def observe_op_outcome(
    op_id: str,
    *,
    passed: int,
    total: int,
    admitted_hashes: Optional[Iterable[str]] = None,
) -> int:
    """Credit the topics that were in *op_id*'s prompt. NEVER raises.

    Returns the number of observations recorded (0 when the op routed no
    memory, the ledger has no record, or the verdict is unusable).

    *admitted_hashes* is injectable so this is testable without a live
    ledger; production leaves it None and the admission record answers.
    """
    if not utility_enabled():
        return 0
    try:
        polarity = _polarity_for(int(passed), int(total))
        if polarity is None:
            return 0

        hashes: List[str] = list(admitted_hashes or ())
        if not hashes:
            from backend.core.ouroboros.governance.memory_admission import (
                ledger_for,
            )
            record = ledger_for(op_id).latest()
            if record is None:
                # The op routed no memory, or the ledger was off. Nothing was
                # injected, so nothing earned credit — silence here is
                # correct, and distinct from a neutral observation.
                return 0
            hashes = [r.content_hash for r in record.rows if r.admitted]
        if not hashes:
            return 0

        now = time.time()
        store = get_store()
        observations = [
            Observation(content_hash=h, weight=polarity, at=now,
                        op_id=op_id, credit=1.0, source="direct")
            for h in hashes
        ]

        # Near-duplicate propagation, on NEGATIVE evidence only.
        #
        # Asymmetric on purpose. Demoting a topic without demoting its
        # near-identical twin leaves the router a free fallback into the same
        # failure — it swaps one copy of the advice for another and repeats
        # the loop. Promotion has no such trap: a successful topic's twin
        # gains nothing by inheriting credit it did not earn, and spreading
        # praise across duplicates would rank a redundant corpus above a
        # concise one.
        corpus_mean = store.corpus_polarity(now, _halflife_days())
        if corpus_mean is not None and polarity < corpus_mean:
            universe = set(store.hashes()) | set(hashes)
            for h in hashes:
                for twin, sim in _near_duplicates(h, universe):
                    if twin in hashes:
                        continue  # already credited directly
                    observations.append(Observation(
                        content_hash=twin, weight=polarity, at=now,
                        op_id=op_id,
                        # Scaled by similarity, so an inherited signal is
                        # always strictly weaker than a witnessed one.
                        credit=max(0.0, float(sim)), source="near_dup",
                    ))

        landed = store.add(observations)
        if landed:
            logger.info(
                "[MemoryUtility] op=%s polarity=%.2f credited=%d "
                "(%d direct, %d near-dup)",
                op_id, polarity, landed, len(hashes), landed - len(hashes),
            )
        return landed
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryUtility] observe degraded", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Listener — arms itself onto the ops-digest fan-out
# ---------------------------------------------------------------------------


class _OutcomeListener:
    """Bridges VERIFY telemetry into the utility store.

    Only ``on_verify_completed`` carries a measured verdict; APPLY and commit
    say a thing happened, not whether it was right. Crediting on those would
    reward a topic for an op that applied cleanly and then failed every test.
    """

    def on_apply_succeeded(self, *, op_id: str, mode: str, files: int) -> None:
        return

    def on_verify_completed(
        self, *, op_id: str, passed: int, total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        if not scoped_to_applied_op:
            # Repo-wide health is not this op's result. Attributing it to the
            # topics in this op's prompt is the false-attribution failure in
            # its purest form — every concurrent op would be graded on the
            # same unrelated number.
            return
        observe_op_outcome(op_id, passed=passed, total=total)

    def on_commit_succeeded(self, *, op_id: str, commit_hash: str) -> None:
        return


_listener = _OutcomeListener()
_armed = False


def arm_outcome_listener() -> bool:
    """Subscribe to ops-digest telemetry. Idempotent. NEVER raises.

    Returns True when the listener is registered. Additive: the harness's
    ``SessionRecorder`` keeps the primary slot, which is why
    ``ops_digest_observer`` grew a fan-out rather than this module stealing
    the registration.
    """
    global _armed  # noqa: PLW0603
    try:
        from backend.core.ouroboros.governance.ops_digest_observer import (
            add_ops_digest_listener,
        )
        add_ops_digest_listener(_listener)
        _armed = True
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryUtility] arming failed", exc_info=True)
        return False


def render_utility_lines(top: int = 8) -> List[str]:
    """Markup lines for ``/memory utility``. NEVER raises."""
    if not utility_enabled():
        return ["  [dim]utility feedback disabled "
                "(JARVIS_MEMORY_UTILITY_ENABLED=0)[/dim]"]
    try:
        store = get_store()
        hashes = store.hashes()
        if not hashes:
            return [
                "  [bold]memory · utility[/bold]",
                "    [dim]no outcomes observed yet — every topic is "
                "neutral[/dim]",
                f"    [dim]listener {'armed' if _armed else 'NOT armed'}"
                "[/dim]",
            ]
        readings = sorted((store.reading(h) for h in hashes),
                          key=lambda r: -abs(r.multiplier - 1.0))
        mean = corpus_mean_polarity()
        out = [
            "  [bold]memory · utility[/bold]  "
            f"[dim]{len(hashes)} topic(s) with history · corpus mean "
            f"{mean:.2f}[/dim]" if mean is not None else
            "  [bold]memory · utility[/bold]",
        ]
        for reading in readings[: max(1, int(top))]:
            out.append(f"    {reading.content_hash}  {reading.describe()}")
        if len(readings) > top:
            out.append(f"    [dim]… {len(readings) - top} more[/dim]")
        return out
    except Exception as exc:  # noqa: BLE001
        return [f"  [dim]utility surface degraded: "
                f"{type(exc).__name__}: {exc}[/dim]"]
