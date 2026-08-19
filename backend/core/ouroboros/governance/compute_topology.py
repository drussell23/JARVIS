"""compute_topology — which memory pool a model actually lands in.

THE DEFECT THIS CLOSES
----------------------
``local_model_admission`` guards the act of loading model weights, and it
asks ``memory_pressure_gate`` how much room there is. That gate's probe
cascade is ``psutil → /proc/meminfo → vm_stat → fallback`` — **every stage
of which measures SYSTEM RAM.** Its own module docstring states the premise
out loud:

    "Tier 2 now runs LOCALLY — Ollama on the same unified memory as the HUD.
     ... There is no separate VRAM to spill into, and no pressure valve
     except swap."

On the 16 GB M1 that is not merely true, it is the whole story: unified
memory means one pool, so a system-RAM reading genuinely predicts whether a
weight-load will page the machine and take the microphone down with it.

Move the same code to a discrete-GPU host and the premise inverts. System
RAM stops predicting anything about a load that lands in VRAM: the gate
reads 64 GB of DDR5, finds abundant headroom, and admits a model whose real
ceiling is a 32 GB card it has no probe for. The failure is not a crash the
gate anticipated — it is a CUDA OOM the gate had no opinion about, or a
silent demotion to a smaller brain with no reason attached.

So this is not a missing threshold or a stale constant. It is a **hardware
topology assumed by shared reasoning**, correct on the machine it was
written for and unstated everywhere else. The fix is to make topology a
measured, first-class property, and to route the admission question to the
pool that actually bounds it.

WHY THIS ADDS NO SECOND MEMORY PROBE
------------------------------------
``memory_pressure_gate`` already owns system RAM: four cascade stages, a
process-tree dimension, a reservation dimension, strictest-wins composition
and a graduated flag. **This module never probes system RAM.** It probes the
ACCELERATOR, classifies the topology, and — under ``unified`` — defers to
that gate outright, because under unified memory the gate's reading is the
correct one and a second opinion would be a second thing to keep true. The
day two probes disagreed, the machine would hold two beliefs about whether
it was safe to allocate.

This is the same division of labour that module already documents for
itself: it measures, and the policy layer decides what to do about the
measurement.

MEASURED, NOT ENUMERATED
------------------------
``governed_loop_service._COMPUTE_RANK`` ranks GPUs by NAME —
``{cpu, gpu_t4, gpu_l4, gpu_v100, gpu_a100}``. A name-ranked table cannot
express a card nobody typed into it, which is why a 32 GB consumer card has
no representable class, and why every future card is a code change.

The root cause there is that admission compares ORDINALS when the question
is about BYTES. So this module resolves a class from measurement and exposes
``bytes_for_class()``, letting admission compare capacity to requirement.
Legacy policy strings keep working — they become a lookup into a byte
requirement rather than a rung on a ladder — and a brain may instead declare
``min_vram_gb`` directly and skip the vocabulary altogether. Nothing needs a
new rung, ever.

STATIC FACTS VS DYNAMIC ONES
----------------------------
Total VRAM, device name and topology do not change while the process runs;
free VRAM changes constantly. Caching them on one clock would either re-run
device enumeration forever or serve a stale free-byte reading as fact. They
are therefore cached separately: identity is resolved once and pinned, and
only the free-byte dimension carries a TTL.

AUTHORITY POSTURE
-----------------
* §1 Boundary — **advisory measurement only.** This module returns readings
  and a resolved class. It admits nothing, loads nothing, and reaches into
  no scheduler. Callers pull.
* §5 Tier 0 — stdlib + optional accelerated probes; no LLM, no network.
* §8 Observability — every reading is ``snapshot()``-able and carries the
  cascade stage that produced it.
* Authority invariant: zero imports from ``orchestrator``, ``policy``,
  ``iron_gate``, ``risk_tier``, ``change_engine``, ``candidate_generator``,
  ``gate``. The single governance import is ``memory_pressure_gate``, and
  it is consumed read-only for the unified-memory case.

FAILURE POSTURE
---------------
Never raises. An unresolvable host yields ``UNKNOWN`` topology, which
callers must treat as "do not claim to know" — never as "plenty of room".
An unknown reading may not authorize a load; that is the same epistemic
discipline ``advisor_locality`` applies to blast radius, for the same
reason: a fabricated measurement is worse than an absent one.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ComputeTopology")


COMPUTE_TOPOLOGY_SCHEMA_VERSION = "compute_topology.1"

_BYTES_PER_GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Env helpers — same shape as memory_pressure_gate's, read at CALL time so a
# knob turned mid-session takes effect without a restart.
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Master switch. Default **false** pending graduation.

    OFF is not a degraded mode — it is the pre-module status quo:
    :func:`resolve` returns a reading whose topology is ``UNKNOWN`` and
    whose ``enabled`` flag is False, and every consumer's documented
    unknown-path keeps the legacy behaviour byte for byte.
    """
    return _env_bool("JARVIS_COMPUTE_TOPOLOGY_ENABLED", False)


def probe_timeout_s() -> float:
    """Wall-clock ceiling for any single external probe. Default 4s.

    Bounded because ``nvidia-smi`` on a wedged driver can hang
    indefinitely, and a boot-time capability read must not be able to
    hold the awakening open (§2 Progressive Awakening).
    """
    return _env_float("JARVIS_COMPUTE_TOPOLOGY_PROBE_TIMEOUT_S", 4.0, minimum=0.1)


def free_ttl_s() -> float:
    """TTL for the DYNAMIC dimension (free bytes). Default 5s.

    Identity — device name, total capacity, topology — is pinned after the
    first successful resolution and never re-probed; only free bytes expire.
    """
    return _env_float("JARVIS_COMPUTE_TOPOLOGY_FREE_TTL_S", 5.0, minimum=0.0)


def identity_repin_s() -> float:
    """Age after which pinned identity is re-resolved anyway. Default 0 = never.

    Zero is correct for a physical host: a GPU does not appear mid-session.
    A non-zero value exists for hosts where it can — hot-plugged eGPU, a
    container that gains a device mapping, a VM migrated between nodes.
    """
    return _env_float("JARVIS_COMPUTE_TOPOLOGY_IDENTITY_REPIN_S", 0.0, minimum=0.0)


def unified_budget_fraction() -> float:
    """Share of unified memory a model may treat as accelerator budget.

    Default 0.75, matching the fraction Apple's Metal runtime reports as
    ``recommendedMaxWorkingSetSize`` on Apple Silicon. It is a FRACTION and
    not a constant because the correct number is a property of the host,
    and on a machine also running a real-time audio graph and a vision
    pipeline the honest budget is well under the nameplate.
    """
    return _env_float(
        "JARVIS_COMPUTE_TOPOLOGY_UNIFIED_BUDGET_FRACTION", 0.75,
        minimum=0.05,
    )


def headroom_fraction() -> float:
    """Fraction of accelerator capacity held back from any single load.

    Default 0.10. Weights are not the only resident allocation: KV cache,
    activation scratch, CUDA context and fragmentation all draw on the same
    pool, and a load sized to the last free byte OOMs on its first long
    prompt rather than at load time — which is far harder to attribute.
    """
    return _env_float(
        "JARVIS_COMPUTE_TOPOLOGY_HEADROOM_FRACTION", 0.10, minimum=0.0,
    )


def reference_class_bytes() -> Dict[str, int]:
    """Legacy ``compute_class`` name → the capacity that name IMPLIES.

    This is a COMPATIBILITY MAPPING, not a ladder. It exists so a policy
    written as ``min_compute_class: "gpu_l4"`` keeps meaning what it meant —
    "at least an L4's worth of memory" — while admission compares bytes.

    Fully overridable as JSON via
    ``JARVIS_COMPUTE_TOPOLOGY_CLASS_BYTES`` (values in GiB), because the
    same class name denotes different capacities across vendor SKUs (V100
    ships 16 and 32; A100 ships 40 and 80) and a fleet is entitled to say
    which one it means. Malformed entries are skipped individually — one
    bad key never costs the whole table.
    """
    table_gib: Dict[str, float] = {
        "cpu": 0.0,
        "gpu_t4": 16.0,
        "gpu_l4": 24.0,
        "gpu_v100": 32.0,
        "gpu_a100": 40.0,
    }
    raw = os.environ.get("JARVIS_COMPUTE_TOPOLOGY_CLASS_BYTES")
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                for key, val in override.items():
                    try:
                        table_gib[str(key).strip().lower()] = max(0.0, float(val))
                    except (TypeError, ValueError):
                        continue
        except (ValueError, TypeError):
            logger.debug("[ComputeTopology] class-bytes override unparseable")
    return {k: int(v * _BYTES_PER_GIB) for k, v in table_gib.items()}


def _truthy_env_names() -> Tuple[str, ...]:
    """Env vars whose presence proves a WSL2 guest. Read as data, not code."""
    raw = os.environ.get(
        "JARVIS_COMPUTE_TOPOLOGY_WSL_MARKERS", "WSL_DISTRO_NAME,WSL_INTEROP",
    )
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class MemoryTopology(str, enum.Enum):
    """Where a model's weights land, relative to system RAM.

    ``UNIFIED``   one physical pool shared by CPU and GPU (Apple Silicon).
                  System-RAM pressure IS accelerator pressure; the canonical
                  ``memory_pressure_gate`` reading governs.
    ``DISCRETE``  an accelerator with its own memory. Two independent
                  ceilings; system RAM says nothing about VRAM.
    ``NONE``      no accelerator. CPU inference out of system RAM — which is
                  unified in effect, but named separately because the
                  BUDGET differs: there is no reserved working set.
    ``UNKNOWN``   the host could not be resolved. Not a synonym for NONE:
                  NONE is a measurement, UNKNOWN is its absence.
    """

    UNIFIED = "unified"
    DISCRETE = "discrete"
    NONE = "none"
    UNKNOWN = "unknown"


#: Topologies from which a capacity claim may be trusted. UNKNOWN is
#: excluded by construction — see the module docstring's failure posture.
_MEASURED = (MemoryTopology.UNIFIED, MemoryTopology.DISCRETE, MemoryTopology.NONE)


@dataclass(frozen=True)
class DeviceReading:
    """One accelerator's own numbers.

    Exists because a host with two GPUs has TWO capacities and the collapsed
    view can only carry one. Which one is correct depends on a fact this
    module cannot observe -- whether the serving stack shards a model across
    devices -- so both are reported and the consumer that knows composes them.
    """

    index: int
    name: str
    total_bytes: int
    free_bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "total_gib": round(self.total_bytes / _BYTES_PER_GIB, 2),
            "free_gib": round(self.free_bytes / _BYTES_PER_GIB, 2),
        }


@dataclass(frozen=True)
class AcceleratorProbe:
    """One probe attempt's result.

    ``source`` names the cascade stage so a surprising reading can be
    attributed without re-deriving it. ``ok`` False carries ``error`` and
    contributes nothing to resolution — a failed stage is skipped, never
    interpreted as zero capacity.
    """

    topology: MemoryTopology
    total_bytes: int
    free_bytes: int
    device_name: str
    device_count: int
    source: str
    ok: bool = True
    error: Optional[str] = None
    #: True when ``free_bytes`` was measured; False when it was DERIVED
    #: from a system-RAM reading (unified) and therefore moves with it.
    free_is_measured: bool = True
    #: Per-device readings when the probe could resolve them. Empty is not
    #: "one device" -- it is "this stage could not enumerate", and consumers
    #: must fall back to the collapsed view rather than infer a count.
    devices: Tuple["DeviceReading", ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topology": self.topology.value,
            "devices": [d.to_dict() for d in self.devices],
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "device_name": self.device_name,
            "device_count": self.device_count,
            "source": self.source,
            "ok": self.ok,
            "error": self.error,
            "free_is_measured": self.free_is_measured,
        }


@dataclass(frozen=True)
class ComputeReading:
    """A resolved view of this host's accelerator situation."""

    topology: MemoryTopology
    total_bytes: int
    free_bytes: int
    device_name: str
    device_count: int
    source: str
    resolved_class: str
    enabled: bool
    probed_at: float
    free_probed_at: float
    schema_version: str = COMPUTE_TOPOLOGY_SCHEMA_VERSION
    free_is_measured: bool = True
    degraded: bool = False
    error: Optional[str] = None
    notes: Tuple[str, ...] = field(default_factory=tuple)
    #: Per-device readings when the cascade stage could enumerate them.
    #: Empty means "not enumerated", NEVER "one device".
    devices: Tuple["DeviceReading", ...] = field(default_factory=tuple)

    @property
    def measured(self) -> bool:
        """True when the topology is a reading rather than an absence."""
        return self.topology in _MEASURED

    @property
    def usable_bytes(self) -> int:
        """Free capacity minus the reserved headroom. Never negative.

        Single-device: the conservative bound that holds no matter what the
        serving stack does. This is the default every consumer gets.
        """
        return max(0, int(self.free_bytes * (1.0 - headroom_fraction())))

    @property
    def aggregate_free_bytes(self) -> int:
        """Free bytes summed across every enumerated device.

        NOT interchangeable with :attr:`free_bytes`. This number is only
        reachable by a serving stack that SHARDS a model across devices
        (llama.cpp/Ollama layer split, vLLM tensor parallel). On a stack that
        does not, it authorizes a load that cannot land -- which is exactly
        why the collapsed view stays the default.

        Returns 0 when devices were not enumerated, so a caller can never
        mistake "could not enumerate" for "nothing free".
        """
        return sum(d.free_bytes for d in self.devices)

    @property
    def aggregate_total_bytes(self) -> int:
        """Nameplate capacity summed across every enumerated device."""
        return sum(d.total_bytes for d in self.devices)

    @property
    def is_multi_device(self) -> bool:
        """True only when MORE THAN ONE device was actually enumerated.

        Deliberately not `device_count > 1`: a stage can report a count while
        failing to enumerate, and a pooled capacity claimed over devices we
        never read would be a fabrication.
        """
        return len(self.devices) > 1

    def shardable_usable_bytes(self, *, sharding: bool) -> int:
        """Usable bytes under a DECLARED sharding capability.

        *sharding* is not observable from here -- it is a property of the
        serving stack, which this module deliberately knows nothing about. So
        the caller that knows must say, and the honest default is the
        conservative single-device bound.

        Even when sharding is declared, a single-device host returns the
        single-device answer: there is nothing to pool.
        """
        if not sharding or not self.is_multi_device:
            return self.usable_bytes
        return max(0, int(self.aggregate_free_bytes
                          * (1.0 - headroom_fraction())))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "topology": self.topology.value,
            "measured": self.measured,
            "total_bytes": self.total_bytes,
            "total_gib": round(self.total_bytes / _BYTES_PER_GIB, 2),
            "free_bytes": self.free_bytes,
            "free_gib": round(self.free_bytes / _BYTES_PER_GIB, 2),
            "usable_bytes": self.usable_bytes,
            "usable_gib": round(self.usable_bytes / _BYTES_PER_GIB, 2),
            "device_name": self.device_name,
            "device_count": self.device_count,
            "devices": [d.to_dict() for d in self.devices],
            "is_multi_device": self.is_multi_device,
            "aggregate_free_bytes": self.aggregate_free_bytes,
            "aggregate_free_gib": round(
                self.aggregate_free_bytes / _BYTES_PER_GIB, 2),
            "aggregate_total_gib": round(
                self.aggregate_total_bytes / _BYTES_PER_GIB, 2),
            "source": self.source,
            "resolved_class": self.resolved_class,
            "enabled": self.enabled,
            "degraded": self.degraded,
            "free_is_measured": self.free_is_measured,
            "probed_at": self.probed_at,
            "free_age_s": round(max(0.0, time.time() - self.free_probed_at), 3),
            "error": self.error,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FitDecision:
    """Whether a stated weight footprint fits this host.

    ``fits`` False with ``reason_code == "unknown_topology"`` means *we do
    not know*, and callers must not read it as *no*. The distinction is the
    difference between a refusal and a fabrication.
    """

    fits: bool
    required_bytes: int
    usable_bytes: int
    topology: MemoryTopology
    reason_code: str
    resolved_class: str
    schema_version: str = COMPUTE_TOPOLOGY_SCHEMA_VERSION
    #: Bytes that would have to spill to host RAM under this fit. Zero when
    #: resident. Non-zero is not fatal for a MoE with few active params, so
    #: it is REPORTED rather than judged here.
    spill_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fits": self.fits,
            "required_bytes": self.required_bytes,
            "required_gib": round(self.required_bytes / _BYTES_PER_GIB, 2),
            "usable_bytes": self.usable_bytes,
            "usable_gib": round(self.usable_bytes / _BYTES_PER_GIB, 2),
            "spill_bytes": self.spill_bytes,
            "topology": self.topology.value,
            "reason_code": self.reason_code,
            "resolved_class": self.resolved_class,
        }


# ---------------------------------------------------------------------------
# Class resolution — measured, not enumerated
# ---------------------------------------------------------------------------


def bytes_for_class(name: Optional[str]) -> int:
    """Capacity a legacy ``compute_class`` string implies, in bytes.

    An unrecognised name resolves to 0 rather than raising: an unknown
    requirement must not be able to deny a route by being unparseable. The
    caller sees "no stated requirement", which is the honest reading of a
    name this side has never heard of — the same posture
    ``memory_pressure_gate`` takes toward an unrecognised Rust arena level.
    """
    if not name:
        return 0
    return reference_class_bytes().get(str(name).strip().lower(), 0)


def bytes_for_requirement(
    min_compute_class: Optional[str] = None,
    min_vram_gb: Optional[float] = None,
) -> int:
    """The byte requirement a brain declares, by either vocabulary.

    ``min_vram_gb`` is direct and always wins when present — a policy that
    states a number has said precisely what it means, and no name lookup can
    improve on that. ``min_compute_class`` remains supported so existing
    policy keeps working unchanged.
    """
    if min_vram_gb is not None:
        try:
            return max(0, int(float(min_vram_gb) * _BYTES_PER_GIB))
        except (TypeError, ValueError):
            pass
    return bytes_for_class(min_compute_class)


def _local_host_aliases() -> Tuple[str, ...]:
    """Names that denote THIS machine. Overridable, never assumed complete.

    ``JARVIS_COMPUTE_TOPOLOGY_LOCAL_ALIASES`` (comma-separated) extends the
    set for hosts whose advertised name matches neither their hostname nor a
    loopback literal — a container reporting its service name, a VM behind a
    stable DNS alias.
    """
    aliases = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "loopback"}
    for probe in ("gethostname", "getfqdn"):
        try:
            import socket
            value = getattr(socket, probe)()
            if value:
                aliases.add(str(value).strip().lower())
        except Exception:  # noqa: BLE001
            continue
    try:
        import platform
        node = platform.node()
        if node:
            aliases.add(str(node).strip().lower())
    except Exception:  # noqa: BLE001
        pass
    raw = os.environ.get("JARVIS_COMPUTE_TOPOLOGY_LOCAL_ALIASES", "")
    for part in raw.split(","):
        part = part.strip().lower()
        if part:
            aliases.add(part)
    return tuple(sorted(aliases))


def describes_this_host(capability: Any) -> bool:
    """Does a fetched capability describe the machine this process runs on?

    **Why this gate exists.** A capability payload arrives over HTTP from a
    J-Prime endpoint that may be a GCP VM on the other side of the planet.
    A local accelerator reading says nothing whatsoever about that machine,
    and using one to authorize a route to the other would be the very defect
    this module was written to close — measuring something real and
    presenting it as the answer to a different question.

    **Positive proof only.** Returns True only when the payload's ``host``
    matches a known alias for this machine. Absence, mismatch, or a
    malformed payload all return False, and the caller falls back to the
    class-name comparison that never claimed to be a measurement. An
    unproven locality is not a local host.
    """
    try:
        host = None
        if isinstance(capability, dict):
            host = capability.get("host") or capability.get("execution_host")
        else:
            host = getattr(capability, "host", None)
        if not host:
            return False
        return str(host).strip().lower() in _local_host_aliases()
    except Exception:  # noqa: BLE001
        return False


def class_for_bytes(total_bytes: int, topology: MemoryTopology) -> str:
    """Name this host's capacity honestly.

    Returns a MEASURED descriptor — ``gpu_32gib``, ``unified_16gib``, ``cpu``
    — rather than the nearest legacy rung. Naming a 32 GiB consumer card
    ``gpu_v100`` because that rung happens to sit at 32 GiB would be a
    fabrication in a field an operator reads, and the reference table exists
    to interpret REQUIREMENTS, not to relabel hardware.

    Admission never consumes this string; it compares bytes. This is for
    humans and for telemetry.
    """
    if topology is MemoryTopology.UNKNOWN:
        return "unknown"
    if topology is MemoryTopology.NONE or total_bytes <= 0:
        return "cpu"
    gib = int(total_bytes // _BYTES_PER_GIB)
    prefix = "unified" if topology is MemoryTopology.UNIFIED else "gpu"
    return f"{prefix}_{gib}gib"


# ---------------------------------------------------------------------------
# Probe cascade — accelerator only. System RAM belongs to memory_pressure_gate.
# ---------------------------------------------------------------------------


def _is_wsl() -> bool:
    """WSL2 guest detection.

    Load-bearing for attribution rather than for the reading itself: under
    WSL2 the CUDA probes report the HOST's physical card correctly, while
    ``/proc/meminfo`` reports only the guest VM's slice of host RAM. A
    surprising system-RAM number on this host has a known cause, and the
    note says so rather than leaving it to be rediscovered.
    """
    try:
        if any(os.environ.get(n) for n in _truthy_env_names()):
            return True
        rel = "/proc/sys/kernel/osrelease"
        if os.path.exists(rel):
            with open(rel, "r", encoding="utf-8", errors="replace") as fh:
                return "microsoft" in fh.read().lower()
    except OSError:
        pass
    return False


def _probe_torch_cuda() -> Optional[AcceleratorProbe]:
    """Most authoritative CUDA reading: driver-level free AND total.

    ``mem_get_info`` reports what the driver will actually hand out, which
    already accounts for the CUDA context and other processes' allocations —
    a number no arithmetic over nameplate capacity can reconstruct.

    Multi-GPU resolves to the LARGEST SINGLE device, not the sum. A model
    must fit on one device unless it was sharded, and sharding is a property
    of the serving stack rather than of the host; summing would authorize a
    load that cannot physically land.
    """
    try:
        # Optional accelerated probe: torch is not a dependency of this
        # module and must never become one. Absent, CPU-only, or a broken
        # CUDA build all decline identically.
        import torch  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # noqa: BLE001 — absent, or broken build
        return None
    try:
        import torch  # type: ignore[import-not-found]
        if not torch.cuda.is_available():
            return None
        count = int(torch.cuda.device_count())
        if count <= 0:
            return None
        readings: List[DeviceReading] = []
        for idx in range(count):
            try:
                free_b, total_b = torch.cuda.mem_get_info(idx)
                name = str(torch.cuda.get_device_name(idx))
            except Exception:  # noqa: BLE001 — one bad device never voids the rest
                continue
            readings.append(DeviceReading(
                index=idx, name=name,
                total_bytes=int(total_b), free_bytes=int(free_b)))
        devices = tuple(readings)
        collapsed = collapse_to_largest(devices)
        if collapsed is None:
            return None
        free_b, total_b, name, _n = collapsed
        return AcceleratorProbe(
            topology=MemoryTopology.DISCRETE,
            total_bytes=total_b, free_bytes=free_b,
            device_name=name, device_count=count, source="torch_cuda",
            devices=devices,
        )
    except Exception as exc:  # noqa: BLE001
        return AcceleratorProbe(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="torch_cuda",
            ok=False, error=str(exc),
        )


def nvidia_smi_binary_names() -> Tuple[str, ...]:
    """Names the driver CLI may carry. Overridable, ordered by likelihood."""
    raw = os.environ.get(
        "JARVIS_COMPUTE_TOPOLOGY_SMI_NAMES", "nvidia-smi,nvidia-smi.exe",
    )
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def nvidia_smi_search_dirs() -> Tuple[str, ...]:
    """Directories to search when ``PATH`` does not carry the driver CLI.

    **The root cause this closes.** Binary discovery bound to ``PATH`` alone
    is bound to a variable the LAUNCH CONTEXT owns, not the machine. WSL2
    injects its GPU stub directory from the interactive login profile, so a
    shell finds ``nvidia-smi`` and a systemd unit, cron job or launchd daemon
    started with a sanitized environment does not. The host is identical; the
    answer differs. A probe whose verdict depends on how its process was
    started is not measuring hardware.

    So discovery composes ``PATH`` (operator intent, always first) with the
    locations a driver actually installs to. Data on the module, overridable
    wholesale via ``JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS`` (``os.pathsep``
    separated) — the same posture as ``reference_class_bytes`` and
    ``_local_host_aliases``: a fleet is entitled to say where its own
    binaries live, and none of this belongs inline in execution logic.

    Order is authority-descending and the last entry is deliberate: the
    Windows-interop binary answers correctly from inside WSL2 but crosses the
    9p filesystem boundary to do it, which is slow enough that it must never
    outrank a native one.
    """
    override = os.environ.get("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS")
    if override is not None:
        return tuple(p.strip() for p in override.split(os.pathsep) if p.strip())
    return (
        # WSL2: the driver stub the Windows host projects into the guest.
        "/usr/lib/wsl/lib",
        # Ordinary Linux driver installs.
        "/usr/bin", "/usr/local/bin", "/usr/local/nvidia/bin", "/opt/bin",
        # Native Windows, when this ever runs there directly.
        "C:\\Windows\\System32",
        "C:\\Program Files\\NVIDIA Corporation\\NVSMI",
        # Last resort: reach the Windows binary through WSL interop. Correct,
        # and slow — 9p, so only when nothing native answered.
        "/mnt/c/Windows/System32",
    )


def _executable_at(path_str: str, name: str) -> Optional[str]:
    """A runnable file at ``path_str/name``, or None. NEVER raises.

    Rejects the three shapes that look like a hit and are not: a directory
    carrying the binary's name, a dangling symlink, and a present-but-not-
    executable file. Each would otherwise reach ``create_subprocess_exec``
    and surface as an opaque OSError attributed to the driver.
    """
    try:
        import pathlib
        candidate = pathlib.Path(path_str) / name
        if not candidate.is_file():          # False for dirs AND dead links
            return None
        if not os.access(str(candidate), os.X_OK):
            return None
        return str(candidate.resolve())
    except (OSError, ValueError):
        return None


def _resolve_nvidia_smi_uncached() -> Optional[str]:
    """Locate the driver CLI across launch contexts. NEVER raises."""
    for name in nvidia_smi_binary_names():
        found = shutil.which(name)          # PATH first: operator intent wins
        if found:
            return found
    for directory in nvidia_smi_search_dirs():
        # An override may name the binary itself rather than its directory —
        # an operator who wrote a full path has been unambiguous, and
        # demanding they split it would be pedantry.
        direct = _executable_at(os.path.dirname(directory) or "/",
                                os.path.basename(directory))
        if direct and os.path.basename(directory) in nvidia_smi_binary_names():
            return direct
        for name in nvidia_smi_binary_names():
            found = _executable_at(directory, name)
            if found:
                return found
    return None


_smi_path_cache: Dict[str, Optional[str]] = {}
_smi_path_lock = threading.Lock()


def resolve_nvidia_smi() -> Optional[str]:
    """Cached driver-CLI path, revalidated on every read. NEVER raises.

    A driver binary does not move, so the search is cached — but the cache is
    *checked*, not trusted: a package upgrade or a container layer swap can
    delete the resolved path underneath a long-lived daemon, and a stale hit
    would fail every probe thereafter with an error that names the wrong
    cause. Revalidation is one ``access`` call; a miss re-runs discovery.
    """
    with _smi_path_lock:
        cached = _smi_path_cache.get("path", "__unset__")
    if cached != "__unset__" and cached is not None:
        try:
            if os.access(cached, os.X_OK):
                return cached
        except (OSError, ValueError):
            pass
        with _smi_path_lock:
            _smi_path_cache.pop("path", None)
    elif cached is None:
        return None
    resolved = _resolve_nvidia_smi_uncached()
    with _smi_path_lock:
        _smi_path_cache["path"] = resolved
    if resolved:
        logger.debug("[ComputeTopology] driver CLI at %s", resolved)
    return resolved


def reset_nvidia_smi_cache() -> None:
    """Drop the resolved path — for tests and after a driver reinstall."""
    with _smi_path_lock:
        _smi_path_cache.clear()


def _parse_nvidia_smi_devices(text: str) -> Tuple["DeviceReading", ...]:
    """Every device ``nvidia-smi`` reported, in driver order.

    Values arrive in MiB. Rows that do not parse are skipped rather than
    zeroed, for the same reason a failed cascade stage is skipped: a
    malformed row is missing data, not a device with no memory.
    """
    out: List[DeviceReading] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total_mib = int(float(re.sub(r"[^\d.]", "", parts[0]) or 0))
            free_mib = int(float(re.sub(r"[^\d.]", "", parts[1]) or 0))
        except (TypeError, ValueError):
            continue
        if total_mib <= 0:
            continue
        out.append(DeviceReading(
            index=len(out), name=parts[2],
            total_bytes=total_mib * 1024 * 1024,
            free_bytes=free_mib * 1024 * 1024,
        ))
    return tuple(out)


def collapse_to_largest(
    devices: "Tuple[DeviceReading, ...]",
) -> Optional[Tuple[int, int, str, int]]:
    """``(free, total, name, count)`` for the LARGEST SINGLE device.

    The collapsed view, and the conservative one: a model must fit on one
    device unless the serving stack shards it, and sharding is a property of
    that stack rather than of the host. Summing here would authorize a load
    that cannot physically land. Callers that KNOW their stack shards read
    the aggregate instead -- see :meth:`ComputeReading.shardable_free_bytes`.
    """
    if not devices:
        return None
    best = max(devices, key=lambda d: d.total_bytes)
    return best.free_bytes, best.total_bytes, best.name, len(devices)


def _parse_nvidia_smi(text: str) -> Optional[Tuple[int, int, str, int]]:
    """(free, total, name, count) from ``nvidia-smi`` CSV. None if unparseable.

    A derived view of :func:`_parse_nvidia_smi_devices` so there is exactly
    one place that knows the CSV shape.
    """
    return collapse_to_largest(_parse_nvidia_smi_devices(text))


async def _probe_nvidia_smi_async() -> Optional[AcceleratorProbe]:
    """Driverless-Python CUDA reading via ``nvidia-smi``.

    Reached when torch is absent or CPU-only — the common shape on a serving
    host where inference runs in Ollama/vLLM and the Python process merely
    governs. Uses ``create_subprocess_exec`` with an explicit wait bound so a
    wedged driver cannot hold the event loop: on timeout the child is killed
    and reaped rather than left to accumulate.

    Discovery runs off-loop: it stats candidate directories, and one of them
    may be a 9p or network mount where a stat can block for seconds. The
    binary is never shell-invoked, so an operator-supplied search path cannot
    become an injection surface.
    """
    binary = await asyncio.to_thread(resolve_nvidia_smi)
    if not binary:
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--query-gpu=memory.total,memory.free,name",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=probe_timeout_s(),
        )
    except asyncio.TimeoutError:
        await _terminate(proc)
        return AcceleratorProbe(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="nvidia_smi",
            ok=False, error="timeout",
        )
    except (OSError, ValueError) as exc:
        await _terminate(proc)
        return AcceleratorProbe(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="nvidia_smi",
            ok=False, error=str(exc),
        )
    if proc.returncode != 0:
        return AcceleratorProbe(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="nvidia_smi",
            ok=False,
            error=f"rc={proc.returncode} {(err or b'').decode(errors='replace')[:120]}",
        )
    _devices = _parse_nvidia_smi_devices((out or b"").decode(errors="replace"))
    parsed = collapse_to_largest(_devices)
    if parsed is None:
        return AcceleratorProbe(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="nvidia_smi",
            ok=False, error="unparseable",
        )
    free_b, total_b, name, count = parsed
    return AcceleratorProbe(
        topology=MemoryTopology.DISCRETE, total_bytes=total_b,
        free_bytes=free_b, device_name=name, device_count=count,
        source="nvidia_smi", devices=_devices,
    )


async def _terminate(proc: Any) -> None:
    """Kill and REAP a child. Never raises.

    Reaping matters: an unawaited transport leaks a zombie and a
    ``ResourceWarning`` per probe, and this runs on a TTL.
    """
    if proc is None:
        return
    try:
        if proc.returncode is None:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=1.0)
    except Exception:  # noqa: BLE001
        pass


def _probe_unified() -> Optional[AcceleratorProbe]:
    """Apple Silicon: one pool, budgeted.

    The accelerator "capacity" here is a FRACTION of system RAM, not its
    total — the GPU's working set is bounded well below nameplate, and on
    this host the same bytes also carry a real-time audio graph and a vision
    pipeline. Free bytes are DERIVED from the canonical system-RAM gate
    rather than probed, and flagged ``free_is_measured=False`` so no
    consumer mistakes a derivation for a driver reading.
    """
    if not sys.platform.startswith("darwin"):
        return None
    try:
        import platform
        if platform.machine().lower() not in ("arm64", "aarch64"):
            return None  # Intel Mac: any dGPU is discrete, and not CUDA
    except Exception:  # noqa: BLE001
        return None
    total_ram, avail_ram = _system_ram_from_canonical_gate()
    if total_ram <= 0:
        return AcceleratorProbe(
            topology=MemoryTopology.UNIFIED, total_bytes=0, free_bytes=0,
            device_name="Apple Silicon", device_count=1, source="unified",
            ok=False, error="system ram unresolved", free_is_measured=False,
        )
    frac = unified_budget_fraction()
    return AcceleratorProbe(
        topology=MemoryTopology.UNIFIED,
        total_bytes=int(total_ram * frac),
        free_bytes=int(min(avail_ram, total_ram * frac)),
        device_name="Apple Silicon (unified)", device_count=1,
        source="unified", free_is_measured=False,
    )


def _system_ram_from_canonical_gate() -> Tuple[int, int]:
    """(total, available) system RAM — from ``memory_pressure_gate``, always.

    DRY, and more than DRY: that module's cascade already handles psutil
    absence, container limits, ``/proc/meminfo`` and ``vm_stat``, and it is
    the reading every other consumer in the system acts on. Re-deriving the
    number here would create a second authority for a value the operator
    sees in one place, which is this codebase's most-repeated defect.

    Returns ``(0, 0)`` when the gate cannot resolve — never a guess.
    """
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )
        probe = get_default_gate().probe()
        if not getattr(probe, "ok", False):
            return 0, 0
        return int(probe.total_bytes), int(probe.available_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ComputeTopology] canonical RAM gate unavailable: %s", exc)
        return 0, 0


def _probe_cpu_only() -> AcceleratorProbe:
    """Terminal stage: no accelerator found, but the host IS resolved.

    NONE is a measurement and must be distinguishable from UNKNOWN. Reaching
    here means every accelerator probe declined — not that they errored — so
    a CPU-inference budget out of system RAM is a legitimate answer.
    """
    total_ram, avail_ram = _system_ram_from_canonical_gate()
    if total_ram <= 0:
        return AcceleratorProbe(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="cpu_only",
            ok=False, error="system ram unresolved", free_is_measured=False,
        )
    frac = unified_budget_fraction()
    return AcceleratorProbe(
        topology=MemoryTopology.NONE,
        total_bytes=int(total_ram * frac),
        free_bytes=int(min(avail_ram, total_ram * frac)),
        device_name="cpu", device_count=0, source="cpu_only",
        free_is_measured=False,
    )


# ---------------------------------------------------------------------------
# Resolver — single-flight, split-clock cache
# ---------------------------------------------------------------------------


class ComputeTopologyResolver:
    """Resolves and caches this host's accelerator situation.

    Two clocks, because the facts have two lifetimes: identity (device,
    capacity, topology) is pinned on first success; free bytes carry a TTL.

    Single-flight: concurrent callers await ONE in-flight probe rather than
    each spawning ``nvidia-smi``. Under an L3 fan-out that difference is
    dozens of subprocesses against a driver that is already the reason the
    probe is bounded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identity: Optional[AcceleratorProbe] = None
        self._identity_at: float = 0.0
        self._free_bytes: int = 0
        self._free_at: float = 0.0
        self._inflight: Optional["asyncio.Future[AcceleratorProbe]"] = None
        #: The loop that owns ``_inflight``. A future may only be awaited
        #: from its own loop, and this process has more than one.
        self._inflight_loop: Optional[Any] = None
        self._notes: Tuple[str, ...] = ()

    # -- cascade ---------------------------------------------------------

    async def _run_cascade(self) -> AcceleratorProbe:
        """Ordered probe cascade. Never raises.

        Order is authority-descending: a driver-level reading beats a CLI
        one, which beats a derivation from system RAM. A stage returning
        ``None`` DECLINED (not applicable here); a stage returning
        ``ok=False`` FAILED and is recorded but skipped. Conflating the two
        would let a broken driver masquerade as a machine with no GPU.
        """
        errors: List[str] = []

        for stage in (self._stage_torch, self._stage_nvidia_smi,
                      self._stage_unified):
            try:
                probe = await stage()
            except Exception as exc:  # noqa: BLE001 — a stage never voids the cascade
                errors.append(f"{getattr(stage, '__name__', 'stage')}:{exc}")
                continue
            if probe is None:
                continue
            if probe.ok:
                if errors:
                    self._notes = tuple(errors)
                return probe
            errors.append(f"{probe.source}:{probe.error}")

        self._notes = tuple(errors)
        return _probe_cpu_only()

    async def _stage_torch(self) -> Optional[AcceleratorProbe]:
        """torch's CUDA API — off-loop: import and driver init both block."""
        return await asyncio.to_thread(_probe_torch_cuda)

    async def _stage_nvidia_smi(self) -> Optional[AcceleratorProbe]:
        return await _probe_nvidia_smi_async()

    async def _stage_unified(self) -> Optional[AcceleratorProbe]:
        return await asyncio.to_thread(_probe_unified)

    # -- public ----------------------------------------------------------

    async def resolve(
        self, *, force: bool = False, max_free_age_s: Optional[float] = None,
    ) -> ComputeReading:
        """Current reading. Async-native, single-flight, never raises.

        ``max_free_age_s`` overrides the free-byte TTL for THIS call only.
        Zero forces a just-in-time re-read of the dynamic dimension while
        leaving pinned identity untouched — the admission gate's JIT probe,
        which must see the OS state as of microseconds ago rather than as of
        whenever the last caller happened to ask. It is a per-call argument
        rather than an env knob because freshness is a property of the
        QUESTION: a dashboard refresh and a weight-load decision have
        legitimately different tolerances for a stale byte count.

        ``force`` re-runs the whole cascade including device enumeration and
        is reserved for boot and for a deliberate re-probe.
        """
        if not is_enabled():
            return self._disabled_reading()

        now = time.time()
        identity = self._pinned_identity(now, force)
        ttl = free_ttl_s() if max_free_age_s is None else max(0.0, max_free_age_s)

        if identity is not None and not force:
            if (now - self._free_at) <= ttl:
                return self._compose(identity, self._free_bytes, self._free_at)
            fresh = await self._refresh_free(identity)
            return self._compose(identity, fresh, time.time())

        probe = await self._single_flight()
        with self._lock:
            if probe.ok and probe.topology in _MEASURED:
                self._identity = probe
                self._identity_at = time.time()
            self._free_bytes = probe.free_bytes
            self._free_at = time.time()
        return self._compose(probe, probe.free_bytes, self._free_at)

    def resolve_sync(self) -> ComputeReading:
        """Synchronous façade for callers that are not async.

        Serves the cache when one exists — which is the common case, since
        boot resolves early. With no cache and no running loop it drives the
        cascade to completion on a private loop. With no cache INSIDE a
        running loop it refuses to block that loop and returns a degraded
        reading instead; blocking an event loop to answer a capability
        question is precisely the failure class this codebase has spent
        slices removing (§3 Asynchronous Tendrils).
        """
        if not is_enabled():
            return self._disabled_reading()
        now = time.time()
        identity = self._pinned_identity(now, False)
        if identity is not None:
            return self._compose(identity, self._free_bytes, self._free_at)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(self.resolve())
            except Exception as exc:  # noqa: BLE001
                return self._degraded_reading(str(exc))
        return self._degraded_reading("async context; awaiting first resolve")

    async def fits(
        self,
        required_bytes: int,
        *,
        allow_spill: bool = False,
        spill_capacity_bytes: Optional[int] = None,
    ) -> FitDecision:
        """Does a stated weight footprint fit this host?

        ``allow_spill`` models the MoE case honestly: a 120B model with ~5B
        active params can run with experts resident in host RAM at a real
        but tolerable cost. The spill remainder is REPORTED either way; only
        the verdict changes, because whether that trade is acceptable is a
        policy question and this module does not hold policy.

        Spill capacity defaults to what the canonical system-RAM gate says
        is available — never to nameplate, and never to a constant.
        """
        reading = await self.resolve()
        return self._decide_fit(
            reading, required_bytes, allow_spill, spill_capacity_bytes,
        )

    def _decide_fit(
        self,
        reading: ComputeReading,
        required_bytes: int,
        allow_spill: bool,
        spill_capacity_bytes: Optional[int],
    ) -> FitDecision:
        req = max(0, int(required_bytes))
        usable = reading.usable_bytes

        if not reading.measured:
            # Epistemic humility: an unresolved host may not authorize a
            # load, and may not be reported as having refused one.
            return FitDecision(
                fits=False, required_bytes=req, usable_bytes=0,
                topology=reading.topology, reason_code="unknown_topology",
                resolved_class=reading.resolved_class,
            )
        if req == 0:
            return FitDecision(
                fits=True, required_bytes=0, usable_bytes=usable,
                topology=reading.topology, reason_code="no_requirement",
                resolved_class=reading.resolved_class,
            )
        if req <= usable:
            return FitDecision(
                fits=True, required_bytes=req, usable_bytes=usable,
                topology=reading.topology, reason_code="resident",
                resolved_class=reading.resolved_class,
            )

        spill = req - usable
        if not allow_spill:
            return FitDecision(
                fits=False, required_bytes=req, usable_bytes=usable,
                topology=reading.topology, reason_code="exceeds_accelerator",
                resolved_class=reading.resolved_class, spill_bytes=spill,
            )
        # Under UNIFIED/NONE the "spill pool" IS the pool already counted —
        # there is nowhere else for it to go, so spill cannot rescue a fit.
        if reading.topology is not MemoryTopology.DISCRETE:
            return FitDecision(
                fits=False, required_bytes=req, usable_bytes=usable,
                topology=reading.topology, reason_code="no_spill_pool",
                resolved_class=reading.resolved_class, spill_bytes=spill,
            )
        capacity = spill_capacity_bytes
        if capacity is None:
            _, capacity = _system_ram_from_canonical_gate()
        capacity = max(0, int(capacity or 0))
        headroom_adjusted = int(capacity * (1.0 - headroom_fraction()))
        if spill <= headroom_adjusted:
            return FitDecision(
                fits=True, required_bytes=req, usable_bytes=usable,
                topology=reading.topology, reason_code="fits_with_spill",
                resolved_class=reading.resolved_class, spill_bytes=spill,
            )
        return FitDecision(
            fits=False, required_bytes=req, usable_bytes=usable,
            topology=reading.topology, reason_code="exceeds_host_memory",
            resolved_class=reading.resolved_class, spill_bytes=spill,
        )

    async def snapshot(self) -> Dict[str, Any]:
        """Observability projection (§8). Read-only, never raises."""
        reading = await self.resolve()
        out = reading.to_dict()
        out["wsl"] = _is_wsl()
        out["headroom_fraction"] = headroom_fraction()
        out["unified_budget_fraction"] = unified_budget_fraction()
        out["free_ttl_s"] = free_ttl_s()
        return out

    # -- internals -------------------------------------------------------

    def _pinned_identity(self, now: float, force: bool) -> Optional[AcceleratorProbe]:
        if force:
            return None
        with self._lock:
            identity = self._identity
            pinned_at = self._identity_at
        if identity is None:
            return None
        repin = identity_repin_s()
        if repin > 0.0 and (now - pinned_at) > repin:
            return None
        return identity

    async def _single_flight(self) -> AcceleratorProbe:
        """One cascade at a time; concurrent callers share the result.

        The in-flight future is bound to the loop that created it. A caller
        arriving from a DIFFERENT loop — this codebase runs
        ``asyncio.to_thread`` and the harness owns more than one loop over a
        session — cannot legally await it, so it runs its own cascade rather
        than raising a cross-loop error. Correctness first: sharing is an
        optimisation, and an optimisation may never be the reason a
        capability read fails.
        """
        loop = asyncio.get_running_loop()
        share: Optional["asyncio.Future[AcceleratorProbe]"] = None
        fut: Optional["asyncio.Future[AcceleratorProbe]"] = None

        with self._lock:
            existing = self._inflight
            existing_loop = self._inflight_loop
            if (existing is not None and not existing.done()
                    and existing_loop is loop):
                share = existing
            elif existing is None or existing.done():
                fut = loop.create_future()
                self._inflight = fut
                self._inflight_loop = loop

        if share is not None:
            try:
                return await asyncio.shield(share)
            except Exception:  # noqa: BLE001 — owner failed; answer honestly
                return _probe_cpu_only()

        try:
            probe = await self._run_cascade()
        except Exception as exc:  # noqa: BLE001
            probe = AcceleratorProbe(
                topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
                device_name="", device_count=0, source="cascade",
                ok=False, error=str(exc),
            )

        if fut is not None:
            with self._lock:
                if self._inflight is fut:
                    self._inflight = None
                    self._inflight_loop = None
            if not fut.done():
                fut.set_result(probe)
        return probe

    async def _refresh_free(self, identity: AcceleratorProbe) -> int:
        """Re-read ONLY the dynamic dimension. Falls back to the last value.

        A failed refresh must not demote a host that was already resolved:
        the identity stands and the previous free reading is reused, with
        its age visible in ``snapshot()`` so staleness is legible rather
        than silent — the ``_heartbeat_age`` discipline, applied to bytes.
        """
        try:
            if identity.source == "torch_cuda":
                probe = await asyncio.to_thread(_probe_torch_cuda)
            elif identity.source == "nvidia_smi":
                probe = await _probe_nvidia_smi_async()
            elif identity.source == "unified":
                probe = await asyncio.to_thread(_probe_unified)
            else:
                probe = await asyncio.to_thread(_probe_cpu_only)
        except Exception:  # noqa: BLE001
            probe = None
        if probe is not None and probe.ok:
            with self._lock:
                self._free_bytes = probe.free_bytes
                self._free_at = time.time()
            return probe.free_bytes
        with self._lock:
            return self._free_bytes

    def _compose(
        self, probe: AcceleratorProbe, free_bytes: int, free_at: float,
    ) -> ComputeReading:
        notes = list(self._notes)
        if _is_wsl():
            notes.append(
                "wsl2: accelerator reading is the host GPU; system-RAM "
                "readings are the guest's allocation, not the host's",
            )
        return ComputeReading(
            topology=probe.topology,
            total_bytes=probe.total_bytes,
            free_bytes=max(0, int(free_bytes)),
            device_name=probe.device_name,
            device_count=probe.device_count,
            source=probe.source,
            resolved_class=class_for_bytes(probe.total_bytes, probe.topology),
            enabled=True,
            probed_at=self._identity_at or time.time(),
            free_probed_at=free_at,
            free_is_measured=probe.free_is_measured,
            degraded=not probe.ok,
            error=probe.error,
            notes=tuple(notes),
            devices=probe.devices,
        )

    def _disabled_reading(self) -> ComputeReading:
        return ComputeReading(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="disabled",
            resolved_class="unknown", enabled=False,
            probed_at=0.0, free_probed_at=0.0, free_is_measured=False,
        )

    def _degraded_reading(self, error: str) -> ComputeReading:
        return ComputeReading(
            topology=MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="degraded",
            resolved_class="unknown", enabled=True,
            probed_at=0.0, free_probed_at=0.0, free_is_measured=False,
            degraded=True, error=error,
        )


# ---------------------------------------------------------------------------
# Module-level singleton — mirrors memory_pressure_gate's accessor shape
# ---------------------------------------------------------------------------

_singleton: Optional[ComputeTopologyResolver] = None
_singleton_lock = threading.Lock()


def get_default_resolver() -> ComputeTopologyResolver:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ComputeTopologyResolver()
        return _singleton


def reset_default_resolver() -> None:
    """Drop the singleton — for tests and for a deliberate re-probe."""
    global _singleton
    with _singleton_lock:
        _singleton = None


async def resolve(max_free_age_s: Optional[float] = None) -> ComputeReading:
    """Module-level convenience over the default resolver."""
    return await get_default_resolver().resolve(max_free_age_s=max_free_age_s)


async def resolve_jit() -> ComputeReading:
    """A reading whose free-byte dimension is re-read NOW.

    The admission gate's probe. Identity stays pinned — device enumeration
    is not repeated — so the cost is one bounded driver query, not a full
    cascade. Use when the answer will authorize an allocation; use
    :func:`resolve` when it will render a dashboard.
    """
    return await get_default_resolver().resolve(max_free_age_s=0.0)


def resolve_sync() -> ComputeReading:
    return get_default_resolver().resolve_sync()


async def fits(required_bytes: int, **kwargs: Any) -> FitDecision:
    return await get_default_resolver().fits(required_bytes, **kwargs)


async def snapshot() -> Dict[str, Any]:
    return await get_default_resolver().snapshot()


def prewarm_budget_s() -> float:
    """Total wall-clock the boot prewarm may consume. Default 15s.

    Covers the WHOLE cascade, not one stage: importing ``torch`` alone can
    take many seconds on a cold page cache, and that import happens off-loop
    but still inside this budget.
    """
    return _env_float(
        "JARVIS_COMPUTE_TOPOLOGY_PREWARM_BUDGET_S", 15.0, minimum=0.5,
    )


async def prewarm() -> ComputeReading:
    """Resolve identity early so ``resolve_sync`` never has to refuse.

    **Why this is load-bearing rather than an optimisation.**
    ``_check_compute_admission`` runs inside a live event loop (its caller
    awaits ``fetch_capability``). ``resolve_sync`` refuses to block a running
    loop — correctly, per §3 Asynchronous Tendrils — so with no cache it
    returns a degraded reading and the measured path never engages. Without
    this call at boot the entire measured admission would be *wired but
    inert*: present, tested, and never reached. Prewarm is what makes it
    live.

    Bounded and fail-soft (§2 Progressive Awakening): a wedged driver costs
    the budget and nothing more. On timeout or fault the resolver simply
    stays unpinned, every consumer takes its documented unknown-path, and
    admission falls back to the legacy ordinal comparison — which is exactly
    the pre-module behaviour.
    """
    resolver = get_default_resolver()
    try:
        return await asyncio.wait_for(
            resolver.resolve(force=True), timeout=prewarm_budget_s(),
        )
    except asyncio.TimeoutError:
        logger.info(
            "[ComputeTopology] prewarm exceeded %.1fs budget; "
            "admission falls back to ordinal comparison",
            prewarm_budget_s(),
        )
        return resolver._degraded_reading("prewarm timeout")
    except Exception as exc:  # noqa: BLE001 — boot is never blocked by a probe
        logger.debug("[ComputeTopology] prewarm failed: %s", exc)
        return resolver._degraded_reading(str(exc))
