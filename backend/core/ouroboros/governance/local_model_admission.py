"""Do not load a local model onto a machine that is about to swap.

NAMING, BECAUSE THIS CODEBASE HAS TWO "MEMORY"S
-------------------------------------------------
`memory_admission.py` already exists and is about REMEMBERED CONTEXT — which
memory topics reached a prompt and why the rest did not. This module is about
RAM. Neither name is wrong and both are load-bearing, so this one says
`local_model` out loud: it guards the act of loading model weights, and
nothing else.

WHAT IT PROTECTS
------------------
Tier 2 now runs LOCALLY — Ollama on the same unified memory as the HUD. On a
16 GB M1 that memory is shared by the GPU, the CoreAudio graph, the vision
pipeline and the model. There is no separate VRAM to spill into, and no
pressure valve except swap.

So a background op deciding to think can take the microphone down. Not
metaphorically: the audio tap runs on a real-time thread,
`HALC_ProxyIOContext :: skipping cycle due to overload` is already in the boot
log at idle, and a swap storm during model load is exactly the condition that
turns a dropped buffer into a severed sentence. The voice path was taken from
15,000 ms to 37 ms; one Tier-2 dispatch that pages the machine gives that back.

A static `num_ctx` cap does not fix this. It is a guess made once, at config
time, about a condition that changes second to second — the same shape of
mistake as a per-soak budget measured against an all-time ledger. What is
needed is a reading taken at the moment of dispatch.

WHY THIS ADDS NO PROBE
------------------------
`memory_pressure_gate` already exists, already graduated (2026-04-21), and
already does the hard part: a stdlib probe cascade (psutil → /proc/meminfo →
`vm_stat` → fallback) behind a four-level enum with env-tunable thresholds.
A second probe here would be a second thing to keep correct, and the day the
two disagreed the machine would hold two opinions about whether it was safe
to allocate.

This is the POLICY layer only: what a generation request should DO about what
the gate already measures.

THE THREE OUTCOMES
--------------------
    OK / WARN     admit unchanged
    HIGH          admit, but PRUNE — shrink the KV footprint before it is
                  allocated, by dropping the oldest droppable context
    CRITICAL      DEFER — do not load, and say so as a normal result

Deferral is a RESULT, not an exception, for the same reason
`OPERATOR_DENIED_EXECUTION` is one: the caller must reason about it, retry it,
or route elsewhere. An exception would be caught by a generic handler and
rendered as a provider fault, tripping failover for a condition that is not a
failure — the machine is fine, it is merely busy.

WHY PRUNING TARGETS THE KV CACHE
----------------------------------
`mmap` keeps model WEIGHTS off the heap: pages load on demand and the OS
evicts them under pressure. The KV cache has no such escape. It is a live
allocation proportional to context length, it cannot be paged out without
destroying the generation, and at 262 K native context it is measured in
gigabytes. The weights look after themselves; the context is the part a
caller can actually give back.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.LocalModelAdmission")

LOCAL_MODEL_ADMISSION_SCHEMA_VERSION: str = "local_model_admission.v2"

_GIB = 1024 ** 3


def unknown_topology_ceiling_bytes() -> int:
    """Largest weight footprint admissible on an UNMEASURED accelerator.

    The degradation state for a malformed, hung, or absent topology probe.
    Zero disables the ceiling entirely (pure v1 behaviour — the host bound
    alone). The default is deliberately modest: enough for the small brains
    that keep an organism alive, never enough to blind-load a frontier model
    onto a capacity nobody could read.
    """
    try:
        raw = (os.environ.get(
            "JARVIS_LOCAL_MODEL_UNKNOWN_CEILING_GB", "") or "").strip()
        return int(max(0.0, float(raw)) * _GIB) if raw else 8 * _GIB
    except (TypeError, ValueError):
        return 8 * _GIB

#: Fed back verbatim when a request is deferred. A stable, greppable token
#: rather than prose — the same discipline as `capability_router
#: .DENIED_PAYLOAD`, because a reworded sentence reads as a new condition to
#: anything matching on it.
DEFERRED_PAYLOAD: str = "[SYSTEM: DEFERRED_DUE_TO_MEMORY_PRESSURE]"


def admission_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off admits everything unchanged — the behaviour from before Tier 2 was
    local, and safe only while Tier 2 is remote.
    """
    return (os.environ.get("JARVIS_LOCAL_MODEL_ADMISSION_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def accelerator_sharding_declared() -> bool:
    """Does the serving stack split ONE model across MULTIPLE devices?

    Default FALSE, and deliberately a declaration rather than an inference.

    Whether a model can span two GPUs is a property of the SERVING STACK
    (llama.cpp/Ollama layer split, vLLM tensor parallel), not of the host.
    `compute_topology` can enumerate the devices but cannot see the stack, so
    it reports the conservative collapsed view -- the largest single device --
    and something that knows the stack has to say otherwise.

    Getting this wrong in the optimistic direction is the expensive
    direction: declaring sharding on a stack that does not shard admits a
    model that then fails to load, having already passed the gate whose whole
    job was to prevent that. Declaring it off on a stack that does merely
    under-uses the second card.

    Turn it ON for a host whose runtime is configured to split -- e.g. a
    dual-GPU box running Ollama/llama.cpp with the layer splitter enabled,
    where the two cards genuinely pool.

    A future upgrade can PROVE this instead of trusting it: a serving stack
    reporting a resident model larger than the largest single device has
    demonstrated sharding. That is an observation, and would outrank this
    flag. NEVER raises.
    """
    return (os.environ.get("JARVIS_LOCAL_ACCEL_SHARDING", "false")
            or "").strip().lower() in ("1", "true", "yes", "on")


def prune_floor_messages() -> int:
    """Messages never pruned, counted from the END. NEVER raises.

    The most recent turns are what the model is actually answering. Pruning
    into them does not save memory, it changes the question — and a smaller
    wrong answer is worse than a deferred right one.
    """
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_PRUNE_FLOOR", "") or "").strip()
        return max(2, min(32, int(raw))) if raw else 6
    except (TypeError, ValueError):
        return 6


def pruned_ctx_fraction() -> float:
    """How much of the requested context survives a HIGH reading."""
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_PRUNE_FRACTION", "") or "").strip()
        return max(0.1, min(1.0, float(raw))) if raw else 0.35
    except (TypeError, ValueError):
        return 0.35


class Admission(str, enum.Enum):
    """What to do with a generation request, given the machine's state."""

    ADMIT = "admit"
    PRUNE = "prune"
    DEFER = "defer"

    @property
    def proceeds(self) -> bool:
        return self is not Admission.DEFER


@dataclass
class AdmissionDecision:
    """One ruling, carrying the reading that produced it."""

    action: str
    level: str = "unknown"
    free_pct: float = -1.0
    source: str = ""
    #: What the caller should request instead. None = unchanged.
    num_ctx: Optional[int] = None
    pruned: int = 0
    reason: str = ""
    #: For a person, when this reaches one.
    spoken_reason: str = ""
    schema_version: str = LOCAL_MODEL_ADMISSION_SCHEMA_VERSION

    # -- accelerator dimension (v2) -------------------------------------
    #: Which pool the weights land in, per ``compute_topology``.
    topology: str = "unknown"
    #: Free accelerator bytes at the moment of the ruling — re-read at
    #: dispatch, never inherited from the boot prewarm.
    accel_free_bytes: int = 0
    #: What the caller said it intends to allocate. 0 = it did not say.
    weight_bytes: int = 0
    #: Bytes held back beyond the caller's request: static headroom plus
    #: whatever this model's OOM history has taught us to add.
    margin_bytes: int = 0
    #: The largest weight footprint that would be admitted right now. The
    #: caller may use it to pick a smaller artifact instead of deferring —
    #: a downshift is a better outcome than a refusal.
    max_weight_bytes: int = 0
    #: Which dimension produced the ruling: ``accelerator`` | ``host`` |
    #: ``none``. Without this a DEFER on a 64 GB machine reads as a bug.
    bound: str = "none"
    #: Soft claim recorded for this admission, or None. The caller releases
    #: it via :func:`release_reservation` when the load finishes or fails.
    #: Forgetting is survivable — the claim settles out and is reconciled —
    #: but releasing promptly is what keeps concurrent workers unthrottled.
    reservation_id: Optional[str] = None

    @property
    def proceeds(self) -> bool:
        try:
            return Admission(self.action).proceeds
        except Exception:  # noqa: BLE001 — an unreadable ruling never proceeds
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "action": self.action,
            "level": self.level, "free_pct": round(self.free_pct, 2),
            "source": self.source, "num_ctx": self.num_ctx,
            "pruned": self.pruned, "reason": self.reason,
            "topology": self.topology, "bound": self.bound,
            "accel_free_gib": round(self.accel_free_bytes / _GIB, 2),
            "weight_gib": round(self.weight_bytes / _GIB, 2),
            "margin_gib": round(self.margin_bytes / _GIB, 2),
            "max_weight_gib": round(self.max_weight_bytes / _GIB, 2),
        }


# ---------------------------------------------------------------------------
# Adaptive margin — the only defensible proxy for what cannot be measured
# ---------------------------------------------------------------------------


def margin_base_fraction() -> float:
    """Starting margin, as a fraction of the requested weight footprint."""
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_MARGIN_BASE", "") or "").strip()
        return max(0.0, min(1.0, float(raw))) if raw else 0.08
    except (TypeError, ValueError):
        return 0.08


def margin_step_fraction() -> float:
    """How much one observed OOM raises this model's margin."""
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_MARGIN_STEP", "") or "").strip()
        return max(0.0, min(1.0, float(raw))) if raw else 0.06
    except (TypeError, ValueError):
        return 0.06


def margin_max_fraction() -> float:
    """Ceiling, so a pathological host cannot learn its way to refusing all."""
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_MARGIN_MAX", "") or "").strip()
        return max(0.0, min(0.9, float(raw))) if raw else 0.40
    except (TypeError, ValueError):
        return 0.40


def margin_decay_s() -> float:
    """Age after which one recorded OOM stops counting. Default 1h."""
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_MARGIN_DECAY_S", "") or "").strip()
        return max(1.0, float(raw)) if raw else 3600.0
    except (TypeError, ValueError):
        return 3600.0


class _MarginLedger:
    """What this host's OOM history has taught us, per model.

    **Why a ledger and not a constant.** Contiguous VRAM is not observable
    (no CUDA or NVML call returns the largest allocatable block), so no probe
    can predict a fragmentation-induced OOM. What CAN be observed is that a
    load of size S failed on this host while the driver reported F free. That
    observation is the only real evidence available, and it is worth exactly
    what it says: on THIS machine, for THIS model, F free was not enough.

    So the margin is learned rather than declared: each OOM raises it a step,
    each recorded success and the passage of time decay it back down. Bounded
    at both ends — a host that OOMs constantly cannot learn its way to
    refusing everything, and a lucky streak cannot erase a real constraint
    faster than the decay window.

    Bounded in size, thread-safe, and process-local by design: this is a
    property of a machine at a moment, not a fact worth persisting across a
    reboot that may have changed the hardware.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[str, list] = {}

    def _key(self, model_id: Optional[str]) -> str:
        return (model_id or "*").strip().lower() or "*"

    def record_oom(self, model_id: Optional[str]) -> None:
        now = time.time()
        cap = 32
        with self._lock:
            bucket = self._events.setdefault(self._key(model_id), [])
            bucket.append(now)
            if len(bucket) > cap:
                del bucket[: len(bucket) - cap]
            if len(self._events) > 64:
                # Bounded: drop the least recently touched model.
                oldest = min(self._events.items(),
                             key=lambda kv: max(kv[1], default=0.0))
                self._events.pop(oldest[0], None)

    def record_success(self, model_id: Optional[str]) -> None:
        """A clean load retires one prior OOM — evidence cuts both ways."""
        with self._lock:
            bucket = self._events.get(self._key(model_id))
            if bucket:
                bucket.pop(0)

    def fraction_for(self, model_id: Optional[str]) -> float:
        cutoff = time.time() - margin_decay_s()
        with self._lock:
            bucket = [t for t in self._events.get(self._key(model_id), [])
                      if t >= cutoff]
            self._events[self._key(model_id)] = bucket
        learned = margin_base_fraction() + len(bucket) * margin_step_fraction()
        return min(margin_max_fraction(), learned)

    def snapshot(self) -> Dict[str, Any]:
        cutoff = time.time() - margin_decay_s()
        with self._lock:
            return {
                k: len([t for t in v if t >= cutoff])
                for k, v in self._events.items()
            }


_margin_ledger = _MarginLedger()


def record_load_outcome(
    model_id: Optional[str] = None, *, ok: bool, error: Any = None,
) -> None:
    """Feed the ledger the one thing that is ground truth: what happened.

    The load itself is the authority no probe can be. Call this after every
    local weight-load attempt. An OOM raises this model's margin; a clean
    load retires one prior OOM.

    Only memory-shaped failures count. A 404 on a missing artifact says
    nothing about capacity, and letting it raise the margin would teach the
    host a superstition. NEVER raises.
    """
    try:
        if ok:
            _margin_ledger.record_success(model_id)
            return
        text = f"{type(error).__name__}: {error}".lower() if error else ""
        markers = ("out of memory", "oom", "cuda error", "alloc",
                   "insufficient", "resource exhausted")
        if not text or any(m in text for m in markers):
            _margin_ledger.record_oom(model_id)
            logger.info(
                "[LocalModelAdmission] recorded OOM for model=%s; margin now %.0f%%",
                model_id or "*", _margin_ledger.fraction_for(model_id) * 100.0,
            )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Anticipatory ledger — what has been PROMISED but is not yet resident
# ---------------------------------------------------------------------------


def reservation_settle_s() -> float:
    """Age after which a grant is assumed RESIDENT and stops being counted.

    Mirrors ``memory_pressure_gate.reservation_settle_s`` deliberately: past
    the settle window the OS probe carries the allocation's weight, so
    continuing to subtract it would double-count one set of bytes. Same
    contract, same default, different pool.
    """
    try:
        raw = (os.environ.get("JARVIS_VRAM_RESERVATION_SETTLE_S", "") or "").strip()
        return max(1.0, float(raw)) if raw else 120.0
    except (TypeError, ValueError):
        return 120.0


def reconcile_after_s() -> float:
    """Age at which a grant becomes eligible for EVIDENCE-based reconciliation.

    Shorter than the settle window on purpose: a crashed worker's phantom
    reservation is detectable long before it would time out, and waiting the
    full window would throttle every other worker on a promise nobody is
    keeping.
    """
    try:
        raw = (os.environ.get("JARVIS_VRAM_RECONCILE_AFTER_S", "") or "").strip()
        return max(1.0, float(raw)) if raw else 20.0
    except (TypeError, ValueError):
        return 20.0


def reconcile_epsilon_bytes() -> int:
    """Slack when judging whether reserved bytes were ever really consumed.

    Free VRAM drifts by tens of MiB from compositor and driver activity
    alone, so an exact comparison would call every reservation phantom.
    """
    try:
        raw = (os.environ.get("JARVIS_VRAM_RECONCILE_EPSILON_MB", "") or "").strip()
        return int(max(0.0, float(raw)) * 1024 * 1024) if raw else 256 * 1024 * 1024
    except (TypeError, ValueError):
        return 256 * 1024 * 1024


@dataclass
class _Reservation:
    """One worker's soft claim on accelerator memory it is about to allocate."""

    rid: str
    bytes_: int
    owner: str
    granted_at: float
    #: Free bytes the OS reported at grant time. This is the evidence the
    #: reconciler compares against — without it a phantom is indistinguishable
    #: from a slow loader.
    free_at_grant: int


class _VramLedger:
    """Soft reservations across concurrent workers, with self-healing.

    **The race this closes.** ``BackgroundAgentPool`` runs three workers and
    L3 fans out further. Each asks "does my model fit?", each is told yes
    against the same free-byte reading, and all three then allocate. The
    admission gate was correct three times and the machine still OOMs,
    because nothing represented *intent* — only current state.

    So a grant is recorded the moment admission says yes, and subsequent
    callers see free bytes MINUS what has been promised. This is the same
    contract ``ProactiveResourceGuard`` implements for system RAM, and
    deliberately NOT the same storage: that guard's unsettled total is
    subtracted from system-RAM free-%, so posting VRAM bytes into it would
    corrupt the RAM dimension for every unrelated consumer. Same discipline,
    separate pool.

    **Three ways a reservation ends**, and the third is the interesting one:

    1. ``release()`` — the worker finished or failed. The normal path.
    2. **Settle** — past ``reservation_settle_s`` the allocation is assumed
       resident and the OS probe now carries it. Stopping the subtraction
       here is what prevents a permanent double-count.
    3. **Reconciliation** — the worker died without releasing. See
       :meth:`_reconcile`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: Dict[str, _Reservation] = {}
        self._seq = 0
        self._phantoms_dropped = 0

    def reserve(self, nbytes: int, *, owner: str, free_now: int) -> Optional[str]:
        """Record a soft claim. Returns its id, or None when unusable."""
        if nbytes <= 0:
            return None
        with self._lock:
            self._seq += 1
            rid = f"r{self._seq}"
            self._live[rid] = _Reservation(
                rid=rid, bytes_=int(nbytes), owner=str(owner or "?"),
                granted_at=time.time(), free_at_grant=int(max(0, free_now)),
            )
            return rid

    def release(self, rid: Optional[str]) -> None:
        if not rid:
            return
        with self._lock:
            self._live.pop(rid, None)

    def unsettled_bytes(self, *, free_now: int) -> int:
        """Bytes promised but not yet visible to the OS probe.

        Runs reconciliation first, so a caller never budgets against a
        promise that has already been proven phantom.
        """
        self._reconcile(free_now=free_now)
        cutoff = time.time() - reservation_settle_s()
        with self._lock:
            return sum(r.bytes_ for r in self._live.values()
                       if r.granted_at >= cutoff)

    def _reconcile(self, *, free_now: int) -> None:
        """Resolve ledger-vs-OS desyncs on EVIDENCE, not on a timer.

        **The hallucination.** A worker crashes between admission and load —
        SIGKILL, OOM-killer, a hard driver fault. Its reservation is never
        released, so the ledger insists memory is spoken for while the OS
        reports it free. Every other worker is then throttled by a promise
        nobody is keeping, and the longer the process lives the more of these
        accumulate. A pure timeout eventually clears them, but it clears a
        *live* slow loader on exactly the same schedule as a dead one.

        **The evidence.** Each grant recorded the free bytes at the moment it
        was made. If the reservation is old enough to have been acted on and
        free memory has NOT fallen — it is at or above where it started, less
        an epsilon for compositor drift — then the promised allocation never
        happened. That is a measurement, not a guess, and it distinguishes a
        dead worker from a slow one: a slow worker that has begun allocating
        shows a falling free count and is left alone.

        **Direction of authority is fixed.** When ledger and OS disagree the
        OS wins, always: the ledger is a claim about the future, the probe is
        a fact about the present. So reconciliation only ever DROPS phantom
        claims — it never invents one to explain memory the probe cannot see.
        Failing in that direction is what keeps a desync from becoming a
        self-inflicted outage.
        """
        if free_now <= 0:
            return  # No usable evidence — leave every claim standing.
        now = time.time()
        eligible_before = now - reconcile_after_s()
        epsilon = reconcile_epsilon_bytes()
        dropped = []
        with self._lock:
            for rid, r in list(self._live.items()):
                if r.granted_at > eligible_before:
                    continue
                if free_now + epsilon >= r.free_at_grant:
                    self._live.pop(rid, None)
                    self._phantoms_dropped += 1
                    dropped.append(r)
        for r in dropped:
            logger.warning(
                "[LocalModelAdmission] reconciled phantom reservation "
                "%s owner=%s %.1f GiB — granted %.0fs ago with %.1f GiB free, "
                "now %.1f GiB free: the allocation never happened",
                r.rid, r.owner, r.bytes_ / _GIB, now - r.granted_at,
                r.free_at_grant / _GIB, free_now / _GIB,
            )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "live": len(self._live),
                "promised_gib": round(
                    sum(r.bytes_ for r in self._live.values()) / _GIB, 2),
                "phantoms_dropped": self._phantoms_dropped,
                "owners": sorted({r.owner for r in self._live.values()}),
            }


_vram_ledger = _VramLedger()


def release_reservation(rid: Optional[str]) -> None:
    """Hand back a soft claim. Safe to call twice, or with None."""
    try:
        _vram_ledger.release(rid)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The two bounds. Neither is derived here — both are CONSUMED.
# ---------------------------------------------------------------------------


def _read_host_pressure() -> Tuple[str, float, str]:
    """(level, free_pct, source) from the canonical system-RAM gate.

    Fails toward OK, deliberately and unchanged from v1: a probe that cannot
    read memory must not itself become a reason to refuse work — that would
    let one broken cascade stage silently disable Tier 2 for a whole session,
    a larger outage than the swap storm it was guarding against.
    """
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )
        gate = get_default_gate()
        probe = gate.probe()
        level = gate.level_for_free_pct(probe.free_pct)
        return (str(getattr(level, "value", level)), float(probe.free_pct),
                str(probe.source or "unknown"))
    except Exception:  # noqa: BLE001
        logger.debug("[LocalModelAdmission] host pressure probe unavailable",
                     exc_info=True)
        return ("unknown", -1.0, "unavailable")


# ---------------------------------------------------------------------------
# Contention dimension — the machine is not "fine below critical"
# ---------------------------------------------------------------------------
#
# `free_pct` is a LEVEL. It answers "how much is left", and on unified memory
# that is a lagging, compressed, and frankly optimistic number: measured on
# this host at the moment of writing, the canonical gate said `warn` (22.8%
# free) while the machine was swapping in at 3.6 MB/s and `coreaudiod` had
# logged a real overload — `safety_violation: 1`, and page faults taken INSIDE
# the IO cycle — four minutes earlier.
#
# So the level says "room to spare" at the exact moment the audio graph is
# being severed by paging. What is missing is a RATE and a VICTIM:
#
#   * paging rate  — swap-in bytes per second. A level cannot distinguish a
#     machine sitting still at 11 GB of swap (survivable) from one actively
#     paging (not), and it is the paging that costs the real-time thread its
#     deadline.
#   * audio contention — `audio_contention_probe`, which reads the overload
#     records coreaudiod already publishes. Its `paging_implicated` flag is
#     the causal join: audio missed its deadline AND took page faults doing
#     it. That is this module's docstring, finally measured instead of feared.
#
# Each independent signal escalates the host level ONE rung through the
# EXISTING ladder (admit → prune → defer). No new action vocabulary, no
# second decision path: strictest-wins composition, the same discipline this
# module already applies across its accelerator and host bounds.


def paging_rate_threshold_bps() -> float:
    """Swap-in bytes/sec above which the machine counts as actively paging.

    Default 1 MiB/s. Not a capacity figure — a *sustained transfer* figure:
    below it, paging is incidental; above it, the machine is moving working
    set through swap continuously, which is the condition that costs a
    real-time audio thread its deadline.
    """
    return _envf("JARVIS_LOCAL_PAGING_RATE_BPS", 1_048_576.0, 0.0, 1e12)


def contention_enabled() -> bool:
    """``JARVIS_LOCAL_CONTENTION_GATE_ENABLED`` (default true).

    Off restores the v2 ladder byte-for-byte: level-only, no rate, no audio.
    """
    raw = (os.environ.get("JARVIS_LOCAL_CONTENTION_GATE_ENABLED", "1") or "").strip()
    return raw.lower() not in ("0", "false", "no", "off")


def _envf(name: str, default: float, lo: float, hi: float) -> float:
    """Env float, clamped. NEVER raises."""
    try:
        raw = (os.environ.get(name) or "").strip()
        return max(lo, min(hi, float(raw))) if raw else default
    except Exception:  # noqa: BLE001
        return default


class _PagingSampler:
    """Swap-in RATE, from two samples. Thread-safe. NEVER raises.

    Cascades psutil → `vm_stat`, mirroring `memory_pressure_gate`'s own probe
    cascade rather than inventing a second one. The cascade is not
    defensive decoration: `psutil.swap_memory()` raises OSError outright
    under a restricted sandbox on this very machine (verified), while
    `vm_stat` answers there — so a single-source probe would report
    "unknown" for every caller that happens to be sandboxed.

    A FIRST call can only return unknown: a rate needs two points in time,
    and inventing one from a cumulative counter would be a fabricated
    measurement driving a real refusal.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: Optional[Tuple[float, int]] = None      # (monotonic, bytes)
        self._rate: Optional[float] = None                  # last GOOD rate
        self._rate_at: float = 0.0

    @staticmethod
    def _read_swapin_bytes() -> Optional[int]:
        """Cumulative swap-in bytes since boot, or None. NEVER raises."""
        try:
            import psutil
            return int(psutil.swap_memory().sin)
        except Exception:  # noqa: BLE001 — OSError under sandbox, or absent
            pass
        try:
            from backend.core.bounded_subprocess import run_bounded
            completed = run_bounded(["vm_stat"], timeout=3.0, text=True)
            if completed is None or completed.returncode != 0:
                return None
            page_size = 4096
            m = re.search(r"page size of (\d+) bytes", completed.stdout or "")
            if m:
                page_size = int(m.group(1))
            # `Pageins` is the closest cumulative analogue vm_stat exposes;
            # it counts pages faulted in from backing store, which is the
            # same event psutil reports as `sin`.
            m = re.search(r"Pageins:\s+(\d+)", completed.stdout or "")
            if not m:
                return None
            return int(m.group(1)) * page_size
        except Exception:  # noqa: BLE001
            return None

    def rate_bps(self) -> Optional[float]:
        """Bytes/sec, or None when unknowable.

        HOLDS THE LAST GOOD RATE between samples, and that is load-bearing
        rather than an optimisation. The first version recomputed on every
        call, and a caller that sampled twice a millisecond apart measured a
        near-zero delta over a near-zero interval and got ~0 B/s — so the
        guard silently DISARMED itself under exactly the rapid-polling
        pattern an admission check produces. Observed: 27.4 MiB/s on one
        read, `admit` on the next call in the same breath.

        A rate needs time to exist. Below `_MIN_INTERVAL_S` no new rate is
        computed and the previous one stands, ageing out after
        `_RATE_TTL_S` — after which the honest answer is again "unknown",
        not "zero".
        """
        now = time.monotonic()
        with self._lock:
            prev = self._last
            cached, cached_at = self._rate, self._rate_at

        if prev is not None and (now - prev[0]) < self._min_interval_s():
            # Too soon to measure again — serve the last good rate if it is
            # still fresh, else admit we do not know.
            if cached is not None and (now - cached_at) < self._rate_ttl_s():
                return cached
            return None

        total = self._read_swapin_bytes()
        if total is None:
            return None
        with self._lock:
            self._last = (now, total)
        if prev is None:
            return None                      # first sample: no rate exists yet
        dt = now - prev[0]
        if dt <= 0:
            return None
        delta = total - prev[1]
        if delta < 0:
            return None                      # counter reset (reboot / wrap)
        rate = delta / dt
        with self._lock:
            self._rate, self._rate_at = rate, now
        return rate

    @staticmethod
    def _min_interval_s() -> float:
        """Shortest span over which a swap-in rate is meaningful."""
        return _envf("JARVIS_LOCAL_PAGING_MIN_INTERVAL_S", 2.0, 0.2, 60.0)

    @staticmethod
    def _rate_ttl_s() -> float:
        """How long a computed rate stays quotable before it is stale."""
        return _envf("JARVIS_LOCAL_PAGING_RATE_TTL_S", 20.0, 1.0, 600.0)


_paging_sampler = _PagingSampler()

_LEVEL_ORDER: Tuple[str, ...] = ("ok", "warn", "high", "critical")


def _escalate(level: str, rungs: int) -> str:
    """Move `level` up the EXISTING ladder, saturating at critical.

    An unrecognised level (notably ``unknown``, which `_read_host_pressure`
    returns when the probe fails) is left ALONE. Escalating from a level we
    could not read would let a broken memory probe manufacture a refusal out
    of nothing — the exact fail-toward-OK posture this module already keeps.
    """
    if rungs <= 0 or level not in _LEVEL_ORDER:
        return level
    idx = min(len(_LEVEL_ORDER) - 1, _LEVEL_ORDER.index(level) + rungs)
    return _LEVEL_ORDER[idx]


def _read_contention() -> Tuple[int, List[str], Dict[str, Any]]:
    """(rungs_to_escalate, reasons, evidence). NEVER raises.

    Synchronous and cheap by construction: the paging sampler is two counter
    reads, and the audio probe is TTL-cached — its ~2s `log show` runs at
    most once per TTL and never on this call's critical path when warm. A
    cold audio probe here would block, so an unwarmed cache reports unknown
    and the memory dimension rules alone; `warm_contention_async` exists for
    callers that can await and want the audio dimension live.
    """
    rungs = 0
    reasons: List[str] = []
    evidence: Dict[str, Any] = {}

    if not contention_enabled():
        return (0, [], {"enabled": False})

    # -- paging rate ----------------------------------------------------
    try:
        rate = _paging_sampler.rate_bps()
    except Exception:  # noqa: BLE001
        rate = None
    evidence["paging_bps"] = rate
    if rate is not None:
        threshold = paging_rate_threshold_bps()
        evidence["paging_threshold_bps"] = threshold
        if rate > threshold:
            rungs += 1
            reasons.append(
                f"swapping in at {rate / (1024 * 1024):.1f} MiB/s")

    # -- audio contention ------------------------------------------------
    try:
        from backend.core.ouroboros.governance.audio_contention_probe import (
            probe as _audio_probe,
        )
        audio = _audio_probe()
        evidence["audio"] = audio.to_dict()
        # Only `paging_implicated` moves an ADMISSION decision. A bare
        # overload can be caused by things a local model cannot influence
        # (a USB interface, a hostile plugin), and refusing local inference
        # for those would be a guard punishing the innocent. Page faults
        # inside the IO cycle are memory pressure, which loading weights
        # provably makes worse.
        if audio.paging_implicated:
            rungs += 1
            reasons.append(
                f"audio graph missed {audio.overloads} deadline(s) while "
                f"page-faulting")
    except Exception:  # noqa: BLE001 — probe absent ⇒ memory dimension rules
        evidence["audio"] = {"ok": False, "error": "unavailable"}

    return (rungs, reasons, evidence)


async def warm_contention_async() -> None:
    """Refresh the audio probe OFF the loop so `_read_contention` finds it
    warm. NEVER raises, never blocks the caller beyond the offload hop.

    Exists because the audio probe costs ~1.4-3.7s (measured) and must never
    run inside a synchronous admission decision. A caller about to dispatch
    local work awaits this first; everything else reads the cache.
    """
    try:
        from backend.core.ouroboros.governance.audio_contention_probe import (
            probe_async,
        )
        await probe_async()
    except Exception:  # noqa: BLE001
        logger.debug("[LocalModelAdmission] audio warm failed", exc_info=True)


async def assess_async(
    requested_ctx: Optional[int] = None,
    *,
    weight_bytes: int = 0,
    model_id: Optional[str] = None,
) -> AdmissionDecision:
    """:func:`assess`, with a JIT probe instead of a cached reading.

    **Why a separate entry point rather than making ``assess`` async.** The
    sync form is called from hot paths that are already inside a running
    loop, where blocking to re-read a driver would be the very starvation
    this codebase has spent slices removing. So the sync form serves the
    short-TTL cache — correct, and up to ``free_ttl_s`` stale — and callers
    that are *about to allocate*, and can await, use this one.

    The freshness gap this closes is real: an external process (a compositor,
    another model server, a zombie holding a context) can consume gigabytes
    between one cached reading and the load it authorized. Re-reading here
    narrows that window to the dispatch itself. It cannot close it — nothing
    can, short of the allocator — which is why the pessimistic margin and the
    OOM ledger sit behind it.

    Identity is not re-enumerated; only the free-byte dimension is re-read.
    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance import compute_topology as ct
        if ct.is_enabled():
            await ct.resolve_jit()
    except Exception as exc:  # noqa: BLE001 — a stale reading beats no ruling
        logger.debug("[LocalModelAdmission] JIT probe degraded: %s", exc)
    # This is the caller that can await, so it is the one that pays for a
    # live audio reading — `assess` itself must stay synchronous and cheap.
    await warm_contention_async()
    return assess(requested_ctx, weight_bytes=weight_bytes, model_id=model_id)


def _read_accelerator() -> Any:
    """This host's accelerator reading, from ``compute_topology``.

    Consumed, never re-derived: that module owns device enumeration, the
    unified/discrete distinction, the free-byte TTL and the malformed-host
    degradation state. Duplicating any of it here would create a second
    authority for a number the operator reads in one place.

    **Freshness is the whole point.** ``resolve_sync`` serves a cache whose
    DYNAMIC dimension carries a short TTL, so the free-byte figure is re-read
    at dispatch rather than inherited from the boot prewarm — which is the
    window an external process would otherwise race through.

    Returns None when topology is unavailable or unresolved; callers must
    treat that as "do not know", never as "plenty".
    """
    try:
        from backend.core.ouroboros.governance import compute_topology as ct
        if not ct.is_enabled():
            return None
        reading = ct.resolve_sync()
        return reading if getattr(reading, "measured", False) else None
    except Exception:  # noqa: BLE001
        logger.debug("[LocalModelAdmission] topology unavailable", exc_info=True)
        return None


def assess(
    requested_ctx: Optional[int] = None,
    *,
    weight_bytes: int = 0,
    model_id: Optional[str] = None,
) -> AdmissionDecision:
    """Should this generation be admitted right now?

    **NEVER raises — structurally, not by inspection.** The contract is
    enforced by this wrapper rather than by auditing every path inside, so a
    fault introduced later in the decision logic degrades to an ADMIT instead
    of escaping into the provider as a lane failure. That direction is
    deliberate and matches the v1 posture: a broken gate must not silently
    disable Tier 2 for a whole session, which is a larger outage than the
    condition it guards against.
    """
    try:
        return _assess_impl(requested_ctx, weight_bytes=weight_bytes,
                            model_id=model_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[LocalModelAdmission] assess degraded: %s", exc,
                     exc_info=True)
        return AdmissionDecision(
            action=Admission.ADMIT.value, level="unknown", free_pct=-1.0,
            source="degraded", bound="none",
            reason=f"admission check degraded ({type(exc).__name__}) — admitting")


def _assess_impl(
    requested_ctx: Optional[int] = None,
    *,
    weight_bytes: int = 0,
    model_id: Optional[str] = None,
) -> AdmissionDecision:
    """The decision itself. See :func:`assess` for the raise contract.

    Two INDEPENDENT bounds, composed strictest-wins — the same composition
    discipline ``memory_pressure_gate`` applies across its own dimensions:

    * **accelerator** — will the weights plus their margin fit in the pool
      they actually land in? Meaningful only on ``discrete``, where VRAM and
      system RAM are genuinely separate ceilings.
    * **host** — is the machine about to swap? This is the v1 question, and
      it remains the whole story under ``unified``.

    Under UNIFIED the two are the same physical pool, and
    ``compute_topology`` already derives its free figure FROM the canonical
    RAM gate. Applying both would double-count one constraint, so the
    accelerator bound is skipped and the host bound rules — which is not a
    weakening: under unified memory the host bound is the correct and
    complete answer.

    ``weight_bytes`` is optional. A caller that does not state a footprint
    gets the v1 behaviour exactly: the host bound alone. Nothing is inferred
    on its behalf, because a guessed footprint would be a fabricated
    measurement driving a real refusal.
    """
    if not admission_enabled():
        return AdmissionDecision(action=Admission.ADMIT.value,
                                 reason="admission control disabled")

    level, free_pct, source = _read_host_pressure()

    # Contention escalation, applied BEFORE the ladder so every rung below
    # reads ONE effective level rather than each branch learning about
    # paging separately. See the block comment above `_read_contention`:
    # measured on this host, the gate said `warn` (22.8% free) while the
    # machine swapped in at 3.6 MiB/s and coreaudiod logged a real overload
    # with page faults inside the IO cycle.
    _rungs, _reasons, _ = _read_contention()
    if _rungs:
        _raw = level
        level = _escalate(level, _rungs)
        if level != _raw:
            logger.info(
                "[LocalModelAdmission] host level %s -> %s — %.1f%% free says "
                "otherwise, but: %s",
                _raw, level, free_pct, "; ".join(_reasons))
            source = f"{source}+contention"

    reading = _read_accelerator()
    topology = getattr(getattr(reading, "topology", None), "value", "unknown")

    # -- accelerator bound (discrete only; see docstring) ----------------
    accel_free = 0
    margin = 0
    max_weight = 0
    if reading is not None and topology == "discrete" and weight_bytes > 0:
        # Single-device by default; pooled ONLY under a declared sharding
        # capability. `shardable_usable_bytes` returns the single-device
        # answer for a single-device host even when sharding is declared,
        # so the flag cannot invent capacity that is not there.
        _sharding = accelerator_sharding_declared()
        _shardable = getattr(reading, "shardable_usable_bytes", None)
        if callable(_shardable):
            measured_free = int(_shardable(sharding=_sharding) or 0)
        else:
            measured_free = int(getattr(reading, "usable_bytes", 0) or 0)
        _pooled = bool(
            _sharding and getattr(reading, "is_multi_device", False))
        # Subtract what OTHER workers have already been promised but have not
        # yet allocated. Without this, N concurrent workers each pass against
        # the same reading and collectively overcommit — the gate is correct
        # N times and the machine still OOMs. Reconciliation runs inside, so
        # a crashed worker's phantom claim is dropped before it throttles
        # anyone.
        promised = _vram_ledger.unsettled_bytes(free_now=measured_free)
        accel_free = max(0, measured_free - promised)
        margin = int(weight_bytes * _margin_ledger.fraction_for(model_id))
        max_weight = max(0, accel_free - margin)
        if weight_bytes + margin > accel_free:
            return AdmissionDecision(
                action=Admission.DEFER.value, level=level, free_pct=free_pct,
                source=source, topology=topology,
                bound="accelerator_pooled" if _pooled else "accelerator",
                accel_free_bytes=accel_free, weight_bytes=weight_bytes,
                margin_bytes=margin, max_weight_bytes=max_weight,
                reason=(
                    f"{weight_bytes / _GIB:.1f} GiB of weights plus a "
                    f"{margin / _GIB:.1f} GiB learned margin exceeds the "
                    f"{accel_free / _GIB:.1f} GiB free "
                    + (
                        f"pooled across {len(getattr(reading, 'devices', ()))} "
                        f"accelerators (sharding declared)"
                        if _pooled else "on this accelerator"
                    )
                    + f" — largest admissible now is {max_weight / _GIB:.1f} GiB"
                ),
                spoken_reason=("That model is larger than the free space on "
                               "my graphics card right now."))

    # -- unknown topology with a stated footprint ------------------------
    # Fail-open on WHETHER to run, fail-closed on HOW BIG. An unresolved host
    # may still serve a small model — refusing everything would be the
    # session-wide outage v1's fail-toward-OK posture exists to prevent — but
    # it may not authorize a large one on a capacity nobody measured.
    if reading is None and weight_bytes > 0:
        ceiling = unknown_topology_ceiling_bytes()
        if ceiling > 0 and weight_bytes > ceiling:
            return AdmissionDecision(
                action=Admission.DEFER.value, level=level, free_pct=free_pct,
                source=source, topology="unknown", bound="accelerator",
                weight_bytes=weight_bytes, max_weight_bytes=ceiling,
                reason=(
                    f"accelerator capacity is unmeasured and "
                    f"{weight_bytes / _GIB:.1f} GiB exceeds the "
                    f"{ceiling / _GIB:.1f} GiB unverified-host ceiling"
                ),
                spoken_reason=("I can't see my graphics card's memory, so I "
                               "won't load something that large blind."))

    # -- host bound (v1, unchanged) --------------------------------------
    if level == "critical":
        return AdmissionDecision(
            action=Admission.DEFER.value, level=level, free_pct=free_pct,
            source=source, topology=topology, bound="host",
            accel_free_bytes=accel_free, weight_bytes=weight_bytes,
            margin_bytes=margin, max_weight_bytes=max_weight,
            reason=(f"{free_pct:.1f}% memory free — loading a local model now "
                    f"would swap, and the audio graph shares this memory"),
            spoken_reason=("My machine is low on memory, so I've held that "
                           "thought rather than risk the microphone."))

    if level == "high":
        pruned_ctx = (max(1024, int(requested_ctx * pruned_ctx_fraction()))
                      if requested_ctx else None)
        return AdmissionDecision(
            action=Admission.PRUNE.value, level=level, free_pct=free_pct,
            source=source, topology=topology, bound="host",
            accel_free_bytes=accel_free, weight_bytes=weight_bytes,
            margin_bytes=margin, max_weight_bytes=max_weight,
            num_ctx=pruned_ctx,
            reason=(f"{free_pct:.1f}% memory free — shrinking the KV cache "
                    f"rather than refusing the work"))

    # An ADMIT is a promise, so record it. Only on the discrete path with a
    # stated footprint: elsewhere there is no distinct pool to overcommit,
    # and a reservation nobody can act on is bookkeeping that only ever
    # throttles. The caller MUST release it — `release_reservation` on
    # completion — but a caller that forgets, or dies, is handled: the claim
    # settles out on its own window and is reconciled away earlier than that
    # if the allocation provably never happened.
    rid = None
    if accel_free and weight_bytes > 0:
        rid = _vram_ledger.reserve(
            weight_bytes + margin, owner=str(model_id or "?"),
            free_now=accel_free)
    return AdmissionDecision(
        action=Admission.ADMIT.value, level=level, free_pct=free_pct,
        source=source, topology=topology,
        bound="accelerator" if accel_free else "none",
        accel_free_bytes=accel_free, weight_bytes=weight_bytes,
        margin_bytes=margin, max_weight_bytes=max_weight,
        reservation_id=rid,
        reason=f"{free_pct:.1f}% memory free")


def prune_messages(messages: Any, decision: AdmissionDecision) -> Tuple[Any, int]:
    """Drop the oldest droppable messages. Returns (messages, dropped).

    NEVER raises. Acts only on a PRUNE ruling.

    KEEPS THE FIRST AND THE LAST. The first message is almost always the
    system prompt — the instructions that make the output parseable at all,
    and the one whose removal turns a slow answer into a malformed one. The
    last `prune_floor_messages()` are the live question. What sits between is
    history, which is what a shrinking machine can afford to forget.
    """
    try:
        if decision.action != Admission.PRUNE.value:
            return messages, 0
        floor = prune_floor_messages()
        if not isinstance(messages, list) or len(messages) <= floor + 1:
            return messages, 0
        head, tail = messages[:1], messages[-floor:]
        middle = messages[1:-floor]
        keep = int(len(middle) * pruned_ctx_fraction())
        kept = middle[-keep:] if keep else []
        dropped = len(middle) - len(kept)
        if dropped <= 0:
            return messages, 0
        logger.warning(
            "[LocalModelAdmission] pruned %d message(s) of history under %s "
            "pressure (%.1f%% free) — system prompt and last %d turns kept",
            dropped, decision.level, decision.free_pct, floor)
        decision.pruned = dropped
        return head + kept + tail, dropped
    except Exception:  # noqa: BLE001 — never mangle a payload on a fault
        logger.debug("[LocalModelAdmission] prune degraded", exc_info=True)
        return messages, 0


def report(decision: AdmissionDecision, *, what: str = "local Tier 2") -> None:
    """Tell O+V the machine hit its own limits. NEVER raises.

    Routed through `RuntimeHealthSensor.report` — the same push entry point
    `TaskHarvester` and `LoopSentinel` use, through the same `make_envelope`
    and the same intake router. No second metrics path: an organism that
    learns about its hardware through a different pipe than its software will
    eventually reason about them separately.

    Only DEFER and PRUNE are reported. An admitted request is the machine
    working, and a signal for each would drown the intake queue in good news.
    """
    try:
        if decision.action == Admission.ADMIT.value:
            return
        from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
            HealthFinding, get_runtime_health_sensor,
        )
        finding = HealthFinding(
            category="memory_pressure_admission",
            severity=("high" if decision.action == Admission.DEFER.value
                      else "normal"),
            summary=(f"{what} {decision.action} at {decision.free_pct:.1f}% "
                     f"free memory — {decision.reason}"),
            details=decision.to_dict(),
            target_files=("backend/core/ouroboros/governance/providers.py",),
        )
        sensor = get_runtime_health_sensor()
        if sensor is None:
            # Boot ordering: intake may not exist yet. `TaskHarvester` owns the
            # buffer that flushes on sensor registration, so this rides it
            # rather than keeping a second queue.
            from backend.core.ouroboros.telemetry.task_harvester import (
                TaskFailure, get_task_harvester,
            )
            held = TaskFailure(what=what, summary=finding.summary,
                               severity=finding.severity,
                               target_files=finding.target_files)
            held.as_finding = lambda f=finding: f
            get_task_harvester()._pending.append(held)
            return
        result = sensor.report(finding)
        if result is not None and hasattr(result, "__await__"):
            import asyncio
            try:
                asyncio.get_event_loop().create_task(result)
            except RuntimeError:
                result.close()
    except Exception:  # noqa: BLE001 — telemetry never blocks admission
        logger.debug("[LocalModelAdmission] report degraded", exc_info=True)


def snapshot() -> Dict[str, Any]:
    """Current admission posture, for `/observability` and `ov doctor`.

    Carries BOTH bounds and their provenance. A DEFER on a machine with 60 GB
    of free RAM is incomprehensible without the accelerator half, and an
    operator staring at a refusal they cannot explain will disable the gate.
    NEVER raises.
    """
    level, free_pct, source = _read_host_pressure()
    reading = _read_accelerator()
    out: Dict[str, Any] = {
        "schema_version": LOCAL_MODEL_ADMISSION_SCHEMA_VERSION,
        "enabled": admission_enabled(),
        "level": level,
        "free_pct": round(free_pct, 2),
        "source": source,
        "prune_floor_messages": prune_floor_messages(),
        "pruned_ctx_fraction": pruned_ctx_fraction(),
        "unknown_ceiling_gib": round(
            unknown_topology_ceiling_bytes() / _GIB, 2),
        "margin_base_fraction": margin_base_fraction(),
        "margin_max_fraction": margin_max_fraction(),
        "observed_ooms": _margin_ledger.snapshot(),
        "reservations": _vram_ledger.snapshot(),
        "reservation_settle_s": reservation_settle_s(),
        "reconcile_after_s": reconcile_after_s(),
    }
    if reading is not None:
        out["accelerator"] = {
            "topology": getattr(getattr(reading, "topology", None), "value",
                                "unknown"),
            "resolved_class": getattr(reading, "resolved_class", "unknown"),
            "usable_gib": round(
                int(getattr(reading, "usable_bytes", 0) or 0) / _GIB, 2),
            "source": getattr(reading, "source", ""),
            "free_is_measured": getattr(reading, "free_is_measured", False),
        }
    else:
        out["accelerator"] = {
            "topology": "unknown",
            "note": ("unmeasured — admission falls back to the host bound "
                     "with the unverified-host ceiling"),
        }
    return out
