"""Which machine measured this? -- a stable, distinct identity for physics.

THE DEFECT THIS CLOSES
----------------------
``local_inference_director.physics_key`` was ``model_name@num_ctx``, and its
docstring gives the reason it excluded the endpoint:

    "the DURABLE physics identity: (model, ctx-bucket) -- NOT the endpoint
     (node IPs change every run; the physics belongs to the brain+window)."

That reasoning is right about IPs and wrong about machines. It holds across a
mesh of like nodes, where the only thing distinguishing two endpoints is an
address that will be different tomorrow. It does NOT hold across a 16GB M1 and
an RTX 5090: the same model name on both writes ONE ledger key, so whichever
measured last governs both. In the bridged configuration this project is
building -- a Mac driving a Windows inference host -- that means **the M1's
~95ms/token would size the 5090's lane count**, and the ThroughputGovernor
would faithfully compute the wrong answer from honest data.

The fix keeps the original insight and adds the missing axis: physics belongs
to the brain, the window, AND THE HARDWARE IT RAN ON. Not to the address.

WHAT MAKES A SIGNATURE
----------------------
Ordered by how much identity each component actually carries:

  * **GPU UUID** -- driver-assigned, survives reboot, PCIe slot swap and
    driver reinstall. The only identifier that can tell two identical cards
    apart. name+capacity cannot; index is assignment order, not identity.
  * **machine id** -- the platform's own durable host identifier
    (``/etc/machine-id``, ``IOPlatformUUID``, ``MachineGuid``). Not the
    hostname, which an operator renames.
  * **topology class** -- discrete vs unified, so a Mac and a PC never
    collide even if both probes degrade.

Hashed into a short digest so it can sit in a ledger key without carrying a
machine's identifiers around in cleartext.

WHOSE HARDWARE, THOUGH
----------------------
The signature must describe the host that RAN THE INFERENCE, not the host that
timed it. A Mac probing its own GPUs while dispatching to a Windows box would
produce the Mac's signature and re-create the exact conflation this module
exists to remove. So resolution is per-endpoint:

  * a LOOPBACK endpoint is served by this machine -> probe local hardware,
    provenance ``probed``;
  * a REMOTE endpoint is served by a machine we cannot probe -> derive from
    its HOST (never the port: one box serving two models on two ports is one
    piece of hardware), provenance ``endpoint``.

The remote case is deliberately weaker and says so. It is still strictly
better than the status quo -- two hosts get two keys -- and it is the seam
where a future ``/hardware`` endpoint on the serving host upgrades
``endpoint`` to ``probed`` without any caller changing.

Python 3.9+. Stdlib only at import time; the compute probe is lazy and
fail-soft, and every result is cached for the process lifetime because
hardware does not change under a running process.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import sys
import threading
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("Ouroboros.HardwareSignature")

ENABLED_ENV = "JARVIS_HARDWARE_SIGNATURE_ENABLED"
DIGEST_CHARS_ENV = "JARVIS_HARDWARE_SIGNATURE_CHARS"

#: What a degraded resolution reports. A CONSTANT, not a random value: two
#: unknowable hosts sharing one bucket is a known limitation, whereas a random
#: per-process id would silently prevent the ledger from ever warm-starting.
UNKNOWN_DIGEST = "unknown"

_TRUTHY = ("1", "true", "yes", "on")
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1", "localhost", "::1", "0.0.0.0", "", "host.docker.internal",
})


def signature_enabled() -> bool:
    """Master gate. Default ON. OFF restores the pre-2026-08-18 key shape
    exactly, conflation included. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def digest_chars() -> int:
    """Hex characters kept from the digest. Default 16 (64 bits).

    Long enough that a collision across the handful of machines one operator
    owns is not a practical concern; short enough to keep a ledger key
    readable in a log line. Clamped to [8, 64]."""
    try:
        return max(8, min(64, int(os.environ.get(DIGEST_CHARS_ENV, "16"))))
    except (TypeError, ValueError):
        return 16


@dataclass(frozen=True)
class HardwareSignature:
    """A machine's identity, and how confidently we know it."""

    digest: str
    #: ``probed`` -- from this host's own machine id + GPU UUIDs.
    #: ``endpoint`` -- from a remote endpoint's host; distinct per machine but
    #:   not verified to BE that machine's hardware.
    #: ``unknown`` -- nothing identifying was resolvable.
    provenance: str
    #: The unhashed inputs, for audit. Never contains the port, and never a
    #: full URL -- a signature is an identity, not a connection string.
    components: Tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        return self.digest

    @property
    def is_strong(self) -> bool:
        """True only for a directly probed identity."""
        return self.provenance == "probed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest": self.digest,
            "provenance": self.provenance,
            "components": list(self.components),
            "is_strong": self.is_strong,
        }


def _digest(components: Tuple[str, ...]) -> str:
    """Stable hash of the component set.

    SORTED before hashing so probe ORDER cannot change a machine's identity --
    nvidia-smi and torch may enumerate the same two cards differently, and a
    signature that flips with enumeration order would split one machine's
    ledger in two.
    """
    payload = "\x1f".join(sorted(components)).encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:digest_chars()]


# --------------------------------------------------------------------------
# machine identity -- cached for the process lifetime
# --------------------------------------------------------------------------

_MACHINE_ID: Optional[str] = None
_MACHINE_LOCK = threading.Lock()


def _read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except Exception:  # noqa: BLE001
        return ""


def _probe_machine_id() -> str:
    """This host's durable identifier, or "". NEVER raises.

    Explicitly NOT the hostname: an operator renames a machine, and a
    signature that changes when they do would orphan every measurement taken
    before the rename.
    """
    # Linux / most containers: a file read, no subprocess.
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        got = _read_first_line(path)
        if got:
            return got

    if sys.platform == "darwin":
        # IOPlatformUUID is the Mac's durable id. One bounded subprocess per
        # PROCESS (the result is cached below), routed through the canonical
        # bounded runner so a wedged ioreg cannot hold anything.
        try:
            from backend.core.bounded_subprocess import run_bounded
            done = run_bounded(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                timeout=3.0, text=True,
            )
            if done is not None and done.returncode == 0:
                for line in (done.stdout or "").splitlines():
                    if "IOPlatformUUID" in line and '"' in line:
                        return line.rsplit('"', 2)[-2].strip()
        except Exception:  # noqa: BLE001
            pass

    if os.name == "nt":
        try:
            import winreg  # type: ignore[import-not-found]
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except Exception:  # noqa: BLE001
            pass

    # Last resort: the NIC-derived node id. Weaker (it can change with the
    # active interface) but still machine-scoped, and vastly better than
    # collapsing two hosts into one bucket.
    try:
        node = _uuid_mod.getnode()
        # getnode() sets the multicast bit when it had to invent a random
        # value -- a random id is NOT an identity, so decline it.
        if node and not (node >> 40) & 0x01:
            return "node-%x" % node
    except Exception:  # noqa: BLE001
        pass
    return ""


def machine_id() -> str:
    """Cached :func:`_probe_machine_id`. Hardware does not change under a
    running process, so this is resolved at most once."""
    global _MACHINE_ID
    with _MACHINE_LOCK:
        if _MACHINE_ID is None:
            try:
                _MACHINE_ID = _probe_machine_id()
            except Exception:  # noqa: BLE001
                _MACHINE_ID = ""
        return _MACHINE_ID


def accelerator_components() -> Tuple[str, ...]:
    """Identity fragments for this host's accelerators. NEVER raises.

    Prefers the driver UUID; falls back to name+capacity, which cannot tell
    two identical cards apart but still separates a 5090 host from a 4090
    host. Reuses the EXISTING compute_topology reading rather than probing --
    a second probe would be a second opinion about one machine.
    """
    try:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            compute_topology as ct,
        )
        # resolve_sync() is the cached synchronous view. When the topology
        # subsystem is DISABLED (its default) this returns a reading with no
        # devices -- which is why the machine id must be able to carry the
        # signature alone. GPU UUIDs enrich a host identity; they are not
        # required to establish one.
        reading = ct.resolve_sync()
    except Exception:  # noqa: BLE001
        return ()
    out = []
    try:
        topo = getattr(getattr(reading, "topology", None), "value", "")
        # ONLY a measured topology contributes. "unknown" is a statement about
        # our PROBE, not about the hardware -- including it would make the
        # signature move when `JARVIS_COMPUTE_TOPOLOGY_ENABLED` is flipped on,
        # orphaning every measurement taken before the flip. A signature must
        # change only when the machine does.
        if topo and topo not in ("unknown", "disabled", "degraded"):
            out.append("topo:%s" % topo)
        for dev in (getattr(reading, "devices", ()) or ()):
            if getattr(dev, "uuid", ""):
                out.append("gpu:%s" % dev.uuid)
            else:
                out.append("gpu:%s:%d" % (
                    getattr(dev, "name", "?"),
                    int(getattr(dev, "total_bytes", 0) or 0)))
        if not getattr(reading, "devices", ()):
            # Unified memory (Apple Silicon) enumerates no discrete devices.
            # Its capacity IS its identity fragment.
            name = str(getattr(reading, "device_name", "") or "")
            total = int(getattr(reading, "total_bytes", 0) or 0)
            if name or total:
                out.append("accel:%s:%d" % (name or "?", total))
    except Exception:  # noqa: BLE001
        return tuple(out)
    return tuple(out)


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def endpoint_host(base_url: str) -> str:
    """Host portion of *base_url*, lowercased, WITHOUT the port.

    The port is excluded deliberately: one machine serving two models on two
    ports is ONE piece of hardware, and splitting its ledger by port would
    re-introduce the amnesia the physics ledger exists to cure.
    NEVER raises.
    """
    try:
        raw = str(base_url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "http://" + raw
        return (urlparse(raw).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def is_local_endpoint(base_url: str) -> bool:
    """True when *base_url* is served by this machine. NEVER raises."""
    return endpoint_host(base_url) in _LOOPBACK_HOSTS


_CACHE: Dict[str, HardwareSignature] = {}
_CACHE_LOCK = threading.Lock()


def signature_for(base_url: str = "") -> HardwareSignature:
    """Identity of the machine SERVING *base_url*. NEVER raises.

    Cached per host for the process lifetime.
    """
    if not signature_enabled():
        return HardwareSignature(digest="", provenance="disabled")
    host = endpoint_host(base_url)
    with _CACHE_LOCK:
        hit = _CACHE.get(host)
    if hit is not None:
        return hit
    try:
        sig = _resolve(base_url, host)
    except Exception:  # noqa: BLE001
        sig = HardwareSignature(digest=UNKNOWN_DIGEST, provenance="unknown")
    with _CACHE_LOCK:
        _CACHE[host] = sig
    if sig.provenance != "probed":
        logger.debug(
            "[HardwareSignature] %s -> %s (%s) — a weaker identity than a "
            "local probe; two hosts still get two ledgers",
            host or "<local>", sig.digest, sig.provenance,
        )
    return sig


def _resolve(base_url: str, host: str) -> HardwareSignature:
    if is_local_endpoint(base_url):
        # ONE SPINE, and enrichment must never move it.
        #
        # The machine id alone already answers the question this module
        # exists for -- is this the M1 or the 5090 -- and it is available
        # whether or not the topology subsystem is enabled. Accelerator
        # components were originally APPENDED to it, which made the digest
        # depend on `JARVIS_COMPUTE_TOPOLOGY_ENABLED`: flipping that flag
        # re-keyed the ledger and orphaned every prior measurement, on a
        # machine that had not changed at all.
        #
        # So GPU identity is a SUBSTITUTE for a missing spine, never an
        # addition to a present one. Distinguishing two GPUs WITHIN one host
        # is a different question, answered at a different granularity by
        # `DeviceReading.uuid` where per-device routing actually needs it.
        mid = machine_id()
        if mid:
            components = ("machine:%s" % mid,)
            return HardwareSignature(
                digest=_digest(components), provenance="probed",
                components=components,
            )
        accel = accelerator_components()
        if accel:
            return HardwareSignature(
                digest=_digest(accel), provenance="probed", components=accel,
            )
        # Nothing identifying resolved. Fall through to the platform class,
        # which at least keeps a Mac and a PC apart.
        fallback = ("platform:%s:%s" % (sys.platform, platform.machine()),)
        return HardwareSignature(
            digest=_digest(fallback), provenance="unknown",
            components=fallback,
        )

    # Remote: we cannot probe that machine's hardware. Its host is a distinct
    # -- if unverified -- identity, which is all the ledger needs to stop
    # conflating two boxes.
    components = ("endpoint:%s" % host,)
    return HardwareSignature(
        digest=_digest(components), provenance="endpoint",
        components=components,
    )


def reset_for_tests() -> None:
    """Drop every cached identity."""
    global _MACHINE_ID
    with _CACHE_LOCK:
        _CACHE.clear()
    with _MACHINE_LOCK:
        _MACHINE_ID = None
