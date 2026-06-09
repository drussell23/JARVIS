"""Slice 185 Phase 4 — purge DW learned-state corrupted by the NameError phantom.

The surface-health ledger and per-model calibration files were populated, in part, from
internal NameErrors mislabeled as `live_transport` vendor ruptures (Slice 185 research). That
state is poisoned — the cortex calibrated thresholds against a rupture rate ~2× inflated by our
own bug. This wipes those files so the post-fix soak relearns from CLEAN signals only.

Gated `JARVIS_DW_LEDGER_WIPE_ON_BOOT` (default FALSE — set TRUE for the one clean relaunch,
then remove). NEVER raises — a wipe failure must not block boot.
"""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List, Optional


def dw_ledger_wipe_enabled() -> bool:
    """Opt-in master — default FALSE (only wipe on an explicitly-requested clean boot)."""
    return os.environ.get("JARVIS_DW_LEDGER_WIPE_ON_BOOT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _state_dir(explicit: Optional[str]) -> str:
    return explicit or os.environ.get("JARVIS_STATE_DIR", "").strip() or ".jarvis"


def wipe_corrupted_dw_ledgers(*, state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Remove the DW surface-health ledger + per-model calibration files corrupted by the
    NameError phantom. No-op unless ``dw_ledger_wipe_enabled()``. NEVER raises. Returns a
    report ``{wiped: [...], errors: int, enabled: bool}``."""
    report: Dict[str, Any] = {"wiped": [], "errors": 0, "enabled": dw_ledger_wipe_enabled()}
    if not report["enabled"]:
        return report
    base = _state_dir(state_dir)
    targets: List[str] = [os.path.join(base, "dw_surface_health.json")]
    try:
        targets.extend(glob.glob(os.path.join(base, "dw_threshold_calibration_*.json")))
    except Exception:  # noqa: BLE001
        report["errors"] += 1
    for path in targets:
        try:
            if os.path.exists(path):
                os.remove(path)
                report["wiped"].append(path)
        except Exception:  # noqa: BLE001 — a single unremovable file must not block boot
            report["errors"] += 1
    return report
