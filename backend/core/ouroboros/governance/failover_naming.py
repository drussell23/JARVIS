"""failover_naming.py -- Cryptographic Asset Namespacing for the elastic fleet.

Concurrent CPU + GPU node lifecycles require GCP asset names that are guaranteed
not to collide. Every ephemeral VM and firewall rule is named:

    {base}-{class}-{hash8}          e.g. jarvis-prime-failover-gpu-a1b2c3d4
    {base}-{class}-{hash8}-fw       the matching firewall rule

The 8-char suffix is ``sha256(salt : class : kind)[:8]`` -- DETERMINISTIC (so
teardown reconstructs the name without persisting it) yet class-DISTINCT (CPU and
GPU hash different inputs -> mathematically cannot collide). The class label is
kept in the name for human/operator legibility. All outputs are valid GCE
resource names (``[a-z]([-a-z0-9]*[a-z0-9])?``, <=63 chars, lowercase).

Env
---
JARVIS_FAILOVER_NODE_NAME        base name (default "jarvis-prime-failover")
JARVIS_FAILOVER_NAMESPACE_SALT   per-deployment salt (default = the base name)
"""
from __future__ import annotations

import hashlib
import os
import re

_DEFAULT_BASE = "jarvis-prime-failover"
_NON_DNS = re.compile(r"[^a-z0-9-]")


def _base() -> str:
    raw = (os.environ.get("JARVIS_FAILOVER_NODE_NAME", _DEFAULT_BASE) or _DEFAULT_BASE)
    return _sanitize(raw) or _DEFAULT_BASE


def _salt() -> str:
    # A distinct salt per deployment -> distinct namespace. Defaults to the base
    # name so a single deployment is stable across restarts.
    return (os.environ.get("JARVIS_FAILOVER_NAMESPACE_SALT", "") or _base()).strip()


def _sanitize(s: str) -> str:
    return _NON_DNS.sub("-", str(s or "").strip().lower()).strip("-")


def _suffix(node_class: str, kind: str) -> str:
    raw = "{}:{}:{}".format(_salt(), _sanitize(node_class), kind).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


def _clamp_gce(name: str) -> str:
    """Guarantee a valid GCE resource name: lowercase DNS chars, leading letter,
    <=63 chars (truncate the BASE, never the discriminating class+hash tail)."""
    name = _sanitize(name)
    if not name or not name[0].isalpha():
        name = "j" + name
    if len(name) > 63:
        # Preserve the class+hash tail (the collision-discriminating part).
        tail = name[-40:]
        name = (name[:63 - len(tail)] + tail)[:63]
    return name.strip("-")


def node_name(node_class: str) -> str:
    """Crypto-namespaced ephemeral VM name for a node class (e.g. cpu / gpu)."""
    cls = _sanitize(node_class) or "node"
    return _clamp_gce("{}-{}-{}".format(_base(), cls, _suffix(node_class, "vm")))


def firewall_name(node_class: str) -> str:
    """Crypto-namespaced ephemeral /32 firewall-rule name for a node class. A
    distinct rule per class -> reaping one node never deletes another's rule."""
    cls = _sanitize(node_class) or "node"
    return _clamp_gce("{}-{}-{}-fw".format(_base(), cls, _suffix(node_class, "fw")))


__all__ = ["node_name", "firewall_name"]
