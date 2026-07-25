"""Karen's voice lane — which DW model is allowed to hold a conversation.

The defect this exists to kill
------------------------------
Karen answers through ``rt_gate.gate_completion``, whose DW tier defaults to
``DOUBLEWORD_MODEL`` — the 397B code brain. Measured against a one-sentence
spoken turn on 2026-07-25::

    Qwen/Qwen3.5-397B-A17B-FP8      ttft=22.84s   "I am currently running..."
    google/gemma-4-26B-A4B-it       ttft= 0.84s   "I am currently optimizing..."
    deepseek-ai/DeepSeek-V4-Flash   ttft= 0.94s
    zai-org/GLM-5.2-FP8             ttft= 1.03s
    openai/gpt-oss-20b              ttft=34.44s
    Qwen/Qwen3.5-9B                 (no content — reasoning only)
    Qwen/Qwen3.6-35B-A3B-FP8        (no content — reasoning only)
    moonshotai/Kimi-K2.6            (no content — reasoning only)

A 22-second pause is not a slow conversation; it is a broken one. The model
that writes a multi-file patch is the wrong organ for a spoken sentence, and
these are different budgets, not different sizes: speech is bounded by
TIME-TO-FIRST-TOKEN, because the operator hears nothing until the first token
reaches TTS.

Why this is measured, never listed
----------------------------------
Naming a winner in code would be a constant that rots the day DW rebalances
its cluster or the entitlement set changes — and the table above would have
been WRONG a week earlier. So the lane is *learned*:

  1. an explicit operator override always wins;
  2. otherwise the fastest model in the ledger that DEMONSTRABLY SPOKE inside
     the spoken-turn budget;
  3. otherwise nothing — the caller keeps its existing default, so a cold
     ledger degrades to today's behaviour rather than to silence.

The reasoning-model trap, for free
----------------------------------
Three of the candidates above answered with pure ``reasoning_content`` and no
``content`` at all: they spent the entire voice token budget thinking and
never said a word. That is invisible to a latency probe that counts bytes.
``stream_watchdog._extract_token`` already returns ``""`` for reasoning-only
deltas — documented behaviour, not an accident — so
``dw_deep_probe.deep_probe`` reports ``tokens=0 reason=no_tokens`` for exactly
this class. The discrimination is composition, not new code.

Everything here is bounded, fail-soft, and authority-free: this module chooses
a MODEL STRING. It cannot mutate, apply, or approve anything, and every failure
path returns ``None`` (meaning "caller, keep your default").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")
SCHEMA_VERSION = "karen_voice_lane.1"


# ---------------------------------------------------------------------------
# Knobs — every one env-resolved at call time (tests monkeypatch; operators tune)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except (TypeError, ValueError):
        return int(default)


def voice_lane_enabled() -> bool:
    """Master gate. Default ON, but inert without a ledger — an unprobed
    system resolves to ``None`` and keeps the caller's existing default."""
    return os.environ.get(
        "JARVIS_KAREN_VOICE_LANE_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def spoken_ttft_budget_s() -> float:
    """The conversational floor: how long a human tolerates before a reply
    starts. ~1.5s is a beat of natural silence; past that the operator starts
    wondering whether the mic heard them. Not a performance target — an
    ADMISSION threshold: a model over it is disqualified from speech, however
    good its prose."""
    return max(0.1, _env_float("JARVIS_KAREN_VOICE_TTFT_BUDGET_S", 1.5))


def ledger_ttl_s() -> float:
    """How long a measurement is trusted. Long enough not to re-probe every
    boot, short enough to notice a cluster that has been rebalanced."""
    return max(60.0, _env_float("JARVIS_KAREN_VOICE_LEDGER_TTL_S", 21600.0))


def spoken_ttft_hard_cap_s() -> float:
    """The line past which a voice is unusable, full stop.

    Distinct from the BUDGET, which is a preference: prefer a model that
    starts inside ~1.5s, but when the whole cluster is having a slow hour —
    measured live: every candidate 2.1-2.7s at one moment, 0.9-1.1s an hour
    earlier — electing nobody means a remote-only host answers a heard
    utterance with silence. A voice that starts in 2.5s is degraded; no voice
    is broken. Above THIS cap, silence really is better."""
    return max(1.0, _env_float("JARVIS_KAREN_VOICE_TTFT_HARD_CAP_S", 6.0))


def transport_failure_ttl_s() -> float:
    """How long a TRANSPORT failure is trusted — much shorter, on purpose.

    A probe that ends in a DNS error, a refused connection or a reset says
    nothing about the model; it is evidence about the NETWORK at that moment.
    Caching it under the full TTL poisons the lane: observed live, a
    sandboxed run's ClientConnectorDNSError records left every candidate
    "freshly measured as silent" for six hours, so the lane resolved None,
    the remote-only host had no engine, and Karen answered a heard utterance
    with nothing at all."""
    return max(5.0, _env_float("JARVIS_KAREN_VOICE_FAILURE_TTL_S", 120.0))


def max_probe_candidates() -> int:
    """Ceiling on how many models ONE refresh may probe. Each probe is a real
    (tiny) generation, so this bounds spend explicitly rather than trusting the
    catalog to stay small.

    It bounds the refresh, not the search: a probed model is recorded whatever
    the outcome, so the next refresh skips it and walks further down the
    ranking. A cold 26-model catalog therefore converges over a handful of
    refreshes instead of demanding one expensive sweep."""
    return max(1, _env_int("JARVIS_KAREN_VOICE_MAX_CANDIDATES", 8))


def probe_max_tokens() -> int:
    """Deliberately voice-sized. A generous budget would let a reasoning model
    finish thinking and then speak, hiding precisely the failure mode that
    makes it unusable for conversation."""
    return max(8, _env_int("JARVIS_KAREN_VOICE_PROBE_MAX_TOKENS", 48))


def ledger_path() -> Path:
    raw = os.environ.get("JARVIS_KAREN_VOICE_LEDGER_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.cwd() / ".jarvis" / "karen_voice_lane.json"


def model_override() -> str:
    """Operator's explicit choice. Always wins — measurement informs, it does
    not overrule."""
    return os.environ.get("JARVIS_KAREN_VOICE_MODEL", "").strip()


# ---------------------------------------------------------------------------
# The probe payload — a SPOKEN turn, not a code prompt
# ---------------------------------------------------------------------------


VOICE_PROBE_SYSTEM = (
    "You are Karen, the voice of an autonomous engineering organism. "
    "Reply in ONE short spoken sentence. No markdown, no lists."
)
VOICE_PROBE_USER = "Hey Karen, are you there?"


def build_voice_probe_payload(model: str, *, max_tokens: int = 0) -> dict:
    """Probe body shaped like the workload it is grading.

    ``dw_deep_probe``'s own builders ask for a bare ``ok`` or a 150-token code
    review; neither exercises the thing that disqualifies a voice model. This
    body carries a real system persona and a real spoken question, so a model
    that burns its budget on reasoning does so HERE, where the probe can see
    it, rather than in front of the operator."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": VOICE_PROBE_SYSTEM},
            {"role": "user", "content": VOICE_PROBE_USER},
        ],
        "max_tokens": int(max_tokens or probe_max_tokens()),
        "temperature": 0.6,
        "stream": True,
    }


# ---------------------------------------------------------------------------
# Ledger — measurements survive the process
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceModelRecord:
    """One model's measured fitness for speech."""

    model: str
    ttft_s: float          # seconds to first SPOKEN token; <0 == never spoke
    tokens: int            # content tokens; 0 == reasoned but stayed silent
    spoke: bool            # emitted real content at all
    measured_at: float
    reason: str = ""

    @property
    def conversational(self) -> bool:
        """Fit to hold a conversation: it spoke, and it started in time."""
        return bool(self.spoke) and 0.0 <= self.ttft_s <= spoken_ttft_budget_s()

    @property
    def usable(self) -> bool:
        """Spoke, inside the HARD cap. The degraded tier: eligible only when
        nothing conversational exists, because a slow voice beats no voice."""
        return bool(self.spoke) and 0.0 <= self.ttft_s <= spoken_ttft_hard_cap_s()

    @property
    def transport_failure(self) -> bool:
        """Did the probe fail before it could learn anything about the model?
        ``probe_error:``/``dispatch_error:`` name exception CLASSES raised in
        dispatch — network knowledge, not model knowledge."""
        r = self.reason or ""
        return r.startswith("probe_error:") or r.startswith("dispatch_error:")

    def fresh(self, *, now: Optional[float] = None, ttl_s: Optional[float] = None) -> bool:
        t = time.time() if now is None else float(now)
        if ttl_s is None:
            # Transport faults expire fast: they describe the network at one
            # instant, and treating them as model verdicts poisons the lane
            # for the full TTL (the 2026-07-25 silent-Karen class).
            ttl = (
                transport_failure_ttl_s() if self.transport_failure
                else ledger_ttl_s()
            )
        else:
            ttl = float(ttl_s)
        return (t - float(self.measured_at)) <= ttl


class VoiceLatencyLedger:
    """Durable, bounded record of which models can actually talk.

    Kept deliberately dumb — a dict keyed by model id, atomically rewritten.
    There is no merge policy because there is nothing to merge: a newer
    measurement of a model always supersedes an older one, and cross-process
    interleaving costs at most one re-probe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else ledger_path()
        self._records: Dict[str, VoiceModelRecord] = {}
        self._loaded = False

    # -- io -------------------------------------------------------------

    def load(self) -> "VoiceLatencyLedger":
        """Read the ledger. A corrupt/missing/foreign-schema file yields an
        EMPTY ledger rather than an exception — a bad cache must cost a
        re-probe, never a boot. NEVER raises."""
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return self
            if str(raw.get("schema_version", "")) != SCHEMA_VERSION:
                return self
            for item in raw.get("records") or ():
                try:
                    rec = VoiceModelRecord(
                        model=str(item["model"]),
                        ttft_s=float(item.get("ttft_s", -1.0)),
                        tokens=int(item.get("tokens", 0)),
                        spoke=bool(item.get("spoke", False)),
                        measured_at=float(item.get("measured_at", 0.0)),
                        reason=str(item.get("reason", "")),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._records[rec.model] = rec
        except (OSError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceLane] ledger load degraded", exc_info=True)
        return self

    def save(self) -> bool:
        """Atomic rewrite. False on any failure — a ledger that cannot be
        persisted still works for this process."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "records": [asdict(r) for r in self._records.values()],
                },
                indent=2,
            )
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(str(tmp), str(self._path))
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceLane] ledger save degraded", exc_info=True)
            return False

    # -- query ----------------------------------------------------------

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def record(self, rec: VoiceModelRecord) -> None:
        self._ensure()
        self._records[rec.model] = rec

    def get(self, model: str) -> Optional[VoiceModelRecord]:
        self._ensure()
        return self._records.get(model)

    def all(self) -> Tuple[VoiceModelRecord, ...]:
        self._ensure()
        return tuple(self._records.values())

    def best(self, *, now: Optional[float] = None) -> Optional[str]:
        """Fastest FRESH model that demonstrably spoke inside the budget.

        Ties break on model id so two cockpits booted together converge on the
        same voice instead of drifting apart on dict ordering."""
        self._ensure()
        fresh = [r for r in self._records.values() if r.fresh(now=now)]
        # Tier 1: within the conversational budget — the preference.
        fit = [r for r in fresh if r.conversational]
        # Tier 2: spoke, within the hard cap — degraded, but a voice. Only
        # consulted when tier 1 is empty, so a fast model always wins outright
        # and the degraded tier cannot drag the election down.
        if not fit:
            fit = [r for r in fresh if r.usable]
            if fit:
                logger.info(
                    "[VoiceLane] no candidate inside the %.1fs budget — "
                    "electing the fastest usable voice instead (degraded "
                    "beats silent)", spoken_ttft_budget_s(),
                )
        if not fit:
            return None
        fit.sort(key=lambda r: (round(r.ttft_s, 3), r.model))
        return fit[0].model

    def stale_or_unknown(self, models: List[str], *, now: Optional[float] = None) -> List[str]:
        """Which of *models* still need measuring. Preserves caller order, so
        the classifier's ranking survives into probe order."""
        self._ensure()
        out = []
        for m in models:
            rec = self._records.get(m)
            if rec is None or not rec.fresh(now=now):
                out.append(m)
        return out


_DEFAULT_LEDGER: Optional[VoiceLatencyLedger] = None


def get_default_ledger() -> VoiceLatencyLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = VoiceLatencyLedger().load()
    return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    """Test seam — drops the process-wide ledger handle."""
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


# ---------------------------------------------------------------------------
# Candidates — from the live catalog, ranked by the existing classifier
# ---------------------------------------------------------------------------


def candidate_models(
    *,
    snapshot: Any = None,
    promotion_ledger: Any = None,
    limit: Optional[int] = None,
) -> List[str]:
    """Models worth probing for speech, best guess first.

    Sourced from the EXISTING catalog + classifier stack (``dw_catalog_client``
    discovers, ``dw_catalog_classifier`` ranks) via a ``voice`` route, so this
    module owns no discovery, no HTTP and no ranking of its own. When discovery
    is off or the catalog is cold the list is empty — and an empty list means
    "keep the current default", never "go silent".

    Returns the FULL ranking by default. DW's ``/models`` response carries
    nothing but an id — no params, no pricing, no modality — so the ranking is
    a weak prior over heuristics, and an OCR or embedding model can perfectly
    well sort above a chat model. Truncating here would strand the search at
    whatever the prior happened to put first; truncation belongs at the PROBE,
    which is the only thing that actually knows.

    (The temptation is to filter ids that look non-conversational. That is
    forbidden: ``dw_modality_ledger`` carries a standing operator mandate that
    modality verdicts come from ground truth only — "regex pattern-matching on
    model_id is strictly forbidden". Measurement decides; names never do.)
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.dw_catalog_client import (
            load_cached_snapshot,
        )
        from backend.core.ouroboros.governance.dw_catalog_classifier import (
            DwCatalogClassifier,
        )
        snap = snapshot if snapshot is not None else load_cached_snapshot()
        if snap is None:
            return []
        # The classifier consults the promotion ledger as a hard input (Zero-
        # Trust quarantine), so it is required, not optional — passing None
        # raises inside classify(). Constructed read-only: this module elects a
        # voice, it must never promote or demote an op-lane model.
        led = promotion_ledger
        if led is None:
            from backend.core.ouroboros.governance.dw_promotion_ledger import (
                PromotionLedger,
            )
            led = PromotionLedger(autosave=False)
        # Honour existing modality ground truth: a model already PROVEN
        # non-chat is excluded from every route by the classifier's hard gate,
        # so those probes are never spent twice across the whole organism.
        mod = None
        try:
            from backend.core.ouroboros.governance.dw_modality_ledger import (
                ModalityLedger,
            )
            mod = ModalityLedger()
            mod.load()
        except Exception:  # noqa: BLE001 — no ledger is survivable
            mod = None
        outcome = DwCatalogClassifier().classify(snap, led, modality_ledger=mod)
        ranked = list(outcome.for_route("voice"))
        return ranked if limit is None else ranked[:max(1, int(limit))]
    except Exception:  # noqa: BLE001
        logger.debug("[VoiceLane] candidate discovery degraded", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Probe — grade one model against a spoken turn
# ---------------------------------------------------------------------------


async def probe_voice_model(
    model: str,
    *,
    dispatch_fn: Optional[Callable[[dict], Any]] = None,
    budget_s: Optional[float] = None,
) -> VoiceModelRecord:
    """Measure one model's fitness to speak. NEVER raises.

    Delegates the whole streaming/watchdog/ITL apparatus to the existing
    ``deep_probe`` and supplies only what is voice-specific: the spoken payload
    and the interpretation. ``tokens == 0`` is the reasoning-model verdict —
    the model answered, but said nothing aloud."""
    budget = spoken_ttft_budget_s() if budget_s is None else float(budget_s)
    try:
        from backend.core.ouroboros.governance.dw_deep_probe import (
            _default_dw_stream_dispatch, deep_probe,
        )
        dispatch = dispatch_fn or _default_dw_stream_dispatch
        # The probe's OWN ttft bound stays generous (cold VRAM load is not the
        # same fault as a chatty model); admission against the conversational
        # budget is judged here, on the measurement, so a slow-but-alive model
        # is recorded honestly instead of being lost as a stream rupture.
        result = await deep_probe(
            dispatch_fn=dispatch,
            model=model,
            max_tokens=probe_max_tokens(),
            payload_builder=build_voice_probe_payload,
        )
        spoke = int(getattr(result, "tokens", 0) or 0) > 0
        ttft = float(getattr(result, "ttft_s", -1.0))
        return VoiceModelRecord(
            model=model,
            ttft_s=ttft if spoke else -1.0,
            tokens=int(getattr(result, "tokens", 0) or 0),
            spoke=spoke,
            measured_at=time.time(),
            reason=(
                str(getattr(result, "reason", "") or "")
                if not spoke
                else (
                    "ok" if ttft <= budget
                    else "slow" if ttft <= spoken_ttft_hard_cap_s()
                    else "too_slow_for_speech"
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return VoiceModelRecord(
            model=model, ttft_s=-1.0, tokens=0, spoke=False,
            measured_at=time.time(), reason=f"probe_error:{type(exc).__name__}",
        )


async def refresh_voice_lane(
    *,
    models: Optional[List[str]] = None,
    dispatch_fn: Optional[Callable[[dict], Any]] = None,
    ledger: Optional[VoiceLatencyLedger] = None,
    force: bool = False,
) -> Optional[str]:
    """Probe the stale candidates concurrently and return the elected model.

    Concurrent because these are independent measurements and a serial sweep
    would take as long as the sum of the slowest — including the 20s+ models
    it exists to disqualify. NEVER raises; returns ``None`` when nothing
    qualifies, which leaves the caller's default in place."""
    if not voice_lane_enabled():
        return None
    led = ledger if ledger is not None else get_default_ledger()
    try:
        cands = list(models) if models is not None else candidate_models()
        if not cands:
            return led.best()
        todo = (cands if force else led.stale_or_unknown(cands))[
            : max_probe_candidates()
        ]
        if todo:
            results = await asyncio.gather(
                *(probe_voice_model(m, dispatch_fn=dispatch_fn) for m in todo),
                return_exceptions=True,
            )
            for rec in results:
                if isinstance(rec, VoiceModelRecord):
                    led.record(rec)
                    logger.info(
                        "[VoiceLane] %s ttft=%.2fs tokens=%d %s",
                        rec.model, rec.ttft_s, rec.tokens, rec.reason,
                    )
            led.save()
        return led.best()
    except Exception:  # noqa: BLE001
        logger.debug("[VoiceLane] refresh degraded", exc_info=True)
        try:
            return led.best()
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Resolution — the one call the answer path makes
# ---------------------------------------------------------------------------


def resolve_voice_model(*, ledger: Optional[VoiceLatencyLedger] = None) -> Optional[str]:
    """The DW model Karen should SPEAK through, or ``None``.

    Synchronous and allocation-cheap by contract: this sits on the turn path,
    where a network call would add the very latency it exists to remove. All
    measurement happens out of band in :func:`refresh_voice_lane`.

    ``None`` is a first-class answer meaning "no evidence — keep the caller's
    default". Returning a guess instead would trade a known-slow voice for an
    unknown one. NEVER raises."""
    try:
        override = model_override()
        if override:
            return override
        if not voice_lane_enabled():
            return None
        return (ledger if ledger is not None else get_default_ledger()).best()
    except Exception:  # noqa: BLE001
        return None


_WARM_LOCK = threading.Lock()
_WARMED = False


def ensure_voice_lane_warm(*, force: bool = False) -> bool:
    """Kick ONE background refresh if the lane has no elected model yet.

    True when a refresh was started. Returns IMMEDIATELY — the caller is on a
    conversational turn, and a lane that made the first reply slower in order
    to make later replies faster would be self-defeating. The current turn
    therefore uses whatever default is already in place; the election lands for
    the next one. Self-healing rather than blocking.

    A plain daemon thread, not a task: this is called from the chat
    multiplexer's worker thread, which has no running loop, and from the async
    supervisor, which has one. A thread is the only seam correct in both — and
    ``asyncio.run`` inside it gives the probes their own loop either way.

    Once per process (the guard is the point: several turns arriving together
    must not each start a sweep). NEVER raises."""
    global _WARMED
    try:
        if not voice_lane_enabled() or model_override():
            return False
        with _WARM_LOCK:
            if _WARMED and not force:
                return False
            if not force and get_default_ledger().best() is not None:
                _WARMED = True          # already elected — nothing to learn
                return False
            _WARMED = True

        def _run() -> None:
            try:
                asyncio.run(refresh_voice_lane(force=force))
            except Exception:  # noqa: BLE001
                logger.debug("[VoiceLane] warm refresh degraded", exc_info=True)

        threading.Thread(
            target=_run, name="karen-voice-lane-warm", daemon=True,
        ).start()
        return True
    except Exception:  # noqa: BLE001
        return False


def reset_warm_state() -> None:
    """Test seam — re-arms the once-per-process guard."""
    global _WARMED
    with _WARM_LOCK:
        _WARMED = False


def voice_lane_status() -> Dict[str, Any]:
    """Operator-facing snapshot for ``/provider`` and friends. Read-only."""
    try:
        led = get_default_ledger()
        recs = sorted(led.all(), key=lambda r: (not r.conversational, r.ttft_s))
        return {
            "enabled": voice_lane_enabled(),
            "override": model_override() or None,
            "elected": resolve_voice_model(),
            "budget_s": spoken_ttft_budget_s(),
            "measured": [
                {
                    "model": r.model, "ttft_s": round(r.ttft_s, 3),
                    "tokens": r.tokens, "spoke": r.spoke,
                    "fit": r.conversational, "reason": r.reason,
                }
                for r in recs
            ],
        }
    except Exception:  # noqa: BLE001
        return {"enabled": False, "elected": None, "measured": []}


__all__ = [
    "SCHEMA_VERSION",
    "VOICE_PROBE_SYSTEM",
    "VoiceLatencyLedger",
    "VoiceModelRecord",
    "build_voice_probe_payload",
    "candidate_models",
    "ensure_voice_lane_warm",
    "get_default_ledger",
    "probe_voice_model",
    "refresh_voice_lane",
    "reset_default_ledger",
    "reset_warm_state",
    "resolve_voice_model",
    "spoken_ttft_budget_s",
    "voice_lane_enabled",
    "voice_lane_status",
]
