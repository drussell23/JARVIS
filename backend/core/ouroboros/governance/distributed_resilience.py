"""
Distributed Resilience Manager — JARVIS-Level Tier 4.

"I'm still here, sir."

Heartbeat + state replication + failover. Primary sends heartbeats
to GCP every 60s. State synced every 5 min. If primary goes offline,
GCP takes over. All subprocess calls argv-based (SSH, rsync).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HEARTBEAT_S = float(os.environ.get("JARVIS_RESILIENCE_HEARTBEAT_S", "60"))
_FAILOVER_S = float(os.environ.get("JARVIS_RESILIENCE_FAILOVER_TIMEOUT_S", "300"))
_SYNC_S = float(os.environ.get("JARVIS_RESILIENCE_SYNC_INTERVAL_S", "300"))
_GCP_HOST = os.environ.get("JARVIS_PRIME_HOST", "136.113.252.164")
_GCP_USER = os.environ.get("JARVIS_GCP_USER", "djrussell23")
_ENABLED = os.environ.get("JARVIS_RESILIENCE_ENABLED", "false").lower() in ("true", "1", "yes")

_STATE_FILES = [
    "consolidated_rules.json", "success_patterns.json",
    "negative_constraints.json", "evolution_epochs.json",
    "dialogues.json", "threshold_observations.json", "prompt_adaptations.json",
]
_LOCAL_DIR = Path(os.environ.get("JARVIS_SELF_EVOLUTION_DIR", str(Path.home() / ".jarvis/ouroboros/evolution")))
_REMOTE_DIR = "/opt/jarvis-prime/ouroboros-state"


class DistributedResilienceManager:
    """Heartbeat + state sync + failover. Argv-based subprocess only."""

    def __init__(self) -> None:
        self._running = False
        self._hb_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._last_hb: float = 0.0
        self._last_sync: float = 0.0
        self._synced: int = 0

    async def start(self) -> None:
        if not _ENABLED:
            logger.info("[Resilience] Disabled (JARVIS_RESILIENCE_ENABLED=false)")
            return
        self._running = True
        self._hb_task = asyncio.create_task(self._hb_loop(), name="resilience_hb")
        self._sync_task = asyncio.create_task(self._sync_loop(), name="resilience_sync")
        logger.info("[Resilience] Started — hb=%ds, sync=%ds, host=%s", _HEARTBEAT_S, _SYNC_S, _GCP_HOST)

    def stop(self) -> None:
        self._running = False
        for t in (self._hb_task, self._sync_task):
            if t and not t.done(): t.cancel()

    async def _hb_loop(self) -> None:
        while self._running:
            try: await self._send_heartbeat()
            except asyncio.CancelledError: break
            except Exception: logger.debug("[Resilience] HB failed", exc_info=True)
            try: await asyncio.sleep(_HEARTBEAT_S)
            except asyncio.CancelledError: break

    async def _send_heartbeat(self) -> None:
        """SSH heartbeat to GCP. Argv-based, no shell."""
        payload = json.dumps({
            "ts": time.time(), "host": platform.node(),
            "sensors": 13, "emergency": "GREEN",
        })
        # Write payload to remote via SSH (argv, no shell interpolation)
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"{_GCP_USER}@{_GCP_HOST}",
            "tee", "/tmp/jarvis_heartbeat.json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(
                proc.communicate(input=payload.encode()), timeout=10.0,
            )
            if proc.returncode == 0:
                self._last_hb = time.time()
        except asyncio.TimeoutError:
            logger.warning("[Resilience] HB SSH timeout")

    async def _sync_loop(self) -> None:
        await asyncio.sleep(30)
        while self._running:
            try: await self._sync_state()
            except asyncio.CancelledError: break
            except Exception: logger.debug("[Resilience] Sync failed", exc_info=True)
            try: await asyncio.sleep(_SYNC_S)
            except asyncio.CancelledError: break

    async def _sync_state(self) -> None:
        """Rsync state files to GCP. Argv-based."""
        if not _LOCAL_DIR.exists(): return
        synced = 0
        for sf in _STATE_FILES:
            lp = _LOCAL_DIR / sf
            if not lp.exists(): continue
            rp = f"{_GCP_USER}@{_GCP_HOST}:{_REMOTE_DIR}/{sf}"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "rsync", "-az", "--timeout=10", str(lp), rp,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await asyncio.wait_for(proc.communicate(), timeout=30.0)
                if proc.returncode == 0: synced += 1
            except Exception: pass
        self._synced = synced
        self._last_sync = time.time()
        if synced: logger.info("[Resilience] Synced %d/%d files", synced, len(_STATE_FILES))

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "enabled": _ENABLED, "mode": "primary" if self._running else "offline",
            "last_heartbeat": self._last_hb,
            "seconds_since_hb": round(now - self._last_hb) if self._last_hb else -1,
            "last_sync": self._last_sync, "files_synced": self._synced,
            "gcp_host": _GCP_HOST, "failover_timeout_s": _FAILOVER_S,
        }
