"""Dynamic Fault-Recovery Loader — the Strangler-Fig safety net.

Supervisor Refactor Campaign Step 1 (operator authorization
2026-07-19). During the aggressive pruning phase, capability zones
identified as inert are MIGRATED (never deleted) into the
``backend/core/quarantine/`` namespace, recorded in a manifest. This
module installs a ``sys.meta_path`` finder that guarantees ZERO
runtime destruction: if any live code path — dynamic import,
reflection, a sensor nobody knew about — asks for a quarantined
module by its ORIGINAL name, the finder resolves it from quarantine,
loads it under the original name (aliases intact for isinstance/
pickle), and emits the high-priority breach beacon::

    [QUARANTINE_BREACH] Module revived at runtime: <original> ← <quarantine>

A breach is a LIVENESS FINDING, not an error: it proves the sweep's
static+topology cross-reference missed a dynamic consumer — the
module graduates straight back out of quarantine in the next slice.
The loader is deliberately LOUD; a silent revival path would mask the
exact truths the sweep exists to surface.

Manifest (``backend/core/quarantine/manifest.json``)::

    {"schema_version": "quarantine.1",
     "modules": {"backend.core.some_zone": "backend.core.quarantine.some_zone"}}

Master ``JARVIS_QUARANTINE_LOADER_ENABLED`` (default ON while the
campaign runs — the safety net IS the license to prune). NEVER raises
anywhere; a broken manifest disables the finder, it never breaks
imports that were fine.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("Ouroboros.Quarantine")

QUARANTINE_SCHEMA_VERSION = "quarantine.1"

_TRUTHY = ("1", "true", "yes", "on")
_LOCK = threading.Lock()
_INSTALLED: Optional["QuarantineFinder"] = None


def loader_enabled() -> bool:
    """Master gate — default ON (the pruning phase's safety net).
    NEVER raises."""
    return os.environ.get(
        "JARVIS_QUARANTINE_LOADER_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def manifest_path() -> Path:
    return Path(__file__).resolve().parent / "quarantine" / "manifest.json"


def load_manifest() -> Dict[str, str]:
    """original-module-name → quarantine-module-name. NEVER raises;
    a broken/absent manifest reads empty (finder inert)."""
    try:
        path = manifest_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        modules = data.get("modules", {})
        if not isinstance(modules, dict):
            return {}
        return {str(k): str(v) for k, v in modules.items()}
    except Exception:  # noqa: BLE001
        logger.warning("[Quarantine] manifest unreadable — finder inert")
        return {}


class QuarantineFinder(importlib.abc.MetaPathFinder):
    """The ``sys.meta_path`` interceptor. Consulted ONLY after the
    normal finders fail (installed at the tail of meta_path), so a
    healthy module never pays a lookup; only a genuinely-missing name
    reaches us — exactly the migrated-zone case."""

    def __init__(self) -> None:
        self._map = load_manifest()
        self.stats: Dict[str, int] = {"breaches": 0, "misses": 0}

    def refresh(self) -> None:
        """Re-read the manifest (a new slice migrated more zones).
        NEVER raises."""
        try:
            self._map = load_manifest()
        except Exception:  # noqa: BLE001
            pass

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        try:
            quarantined = self._map.get(fullname)
            if quarantined is None:
                return None
            spec = importlib.util.find_spec(quarantined)
            if spec is None:
                self.stats["misses"] += 1
                logger.error(
                    "[Quarantine] manifest maps %s -> %s but the "
                    "quarantine module is MISSING", fullname, quarantined,
                )
                return None
            self.stats["breaches"] += 1
            # High-priority beacon — a breach is a liveness FINDING:
            # a dynamic consumer the sweep missed. Graduate the module
            # back out in the next slice.
            logger.critical(
                "[QUARANTINE_BREACH] Module revived at runtime: %s ← %s "
                "(dynamic consumer missed by the sweep — graduate it back)",
                fullname, quarantined,
            )
            # Load the quarantine source under the ORIGINAL name so
            # isinstance/pickle/module-identity all stay coherent.
            revived = importlib.util.spec_from_file_location(
                fullname, spec.origin,
                submodule_search_locations=spec.submodule_search_locations,
            )
            return revived
        except Exception:  # noqa: BLE001
            logger.debug("[Quarantine] find_spec degraded", exc_info=True)
            return None


def install_quarantine_loader() -> bool:
    """Idempotent install at the TAIL of ``sys.meta_path``. Returns
    True when the finder is active. NEVER raises."""
    global _INSTALLED
    try:
        if not loader_enabled():
            return False
        with _LOCK:
            if _INSTALLED is not None:
                _INSTALLED.refresh()
                return True
            finder = QuarantineFinder()
            sys.meta_path.append(finder)
            _INSTALLED = finder
            logger.info(
                "[Quarantine] fault-recovery loader installed "
                "(%d migrated module(s) mapped)", len(finder._map),
            )
            return True
    except Exception:  # noqa: BLE001
        return False


def uninstall_quarantine_loader() -> None:
    """Remove the finder (tests / campaign end). NEVER raises."""
    global _INSTALLED
    try:
        with _LOCK:
            if _INSTALLED is not None:
                try:
                    sys.meta_path.remove(_INSTALLED)
                except ValueError:
                    pass
                _INSTALLED = None
    except Exception:  # noqa: BLE001
        pass


def get_installed_finder() -> Optional[QuarantineFinder]:
    return _INSTALLED


__all__ = [
    "QUARANTINE_SCHEMA_VERSION",
    "QuarantineFinder",
    "get_installed_finder",
    "install_quarantine_loader",
    "load_manifest",
    "loader_enabled",
    "manifest_path",
    "uninstall_quarantine_loader",
]
