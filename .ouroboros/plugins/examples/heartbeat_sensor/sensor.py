"""Heartbeat sensor plugin — example of the SensorPlugin on_tick model.

Emits a low-urgency ``heartbeat`` signal every tick so operators can
verify the plugin lifecycle is working without editing real governance
code. Real plugins would do something useful here (poll an external
service, watch a file, etc.).
"""
from __future__ import annotations

from typing import List

from backend.core.ouroboros.plugins.plugin_base import (
    SensorPlugin,
    SensorPluginSignal,
)


class HeartbeatSensor(SensorPlugin):
    """Emits one heartbeat signal per tick. Real plugins replace
    ``on_tick`` with their actual polling logic."""

    async def start(self) -> None:
        self.context.emit_info("started — heartbeat every tick")

    async def on_tick(self) -> List[SensorPluginSignal]:
        # Low urgency, no target files — this is purely a liveness
        # indicator. In production you'd propose signals only when
        # you detect an actionable condition.
        return [
            SensorPluginSignal(
                description="plugin heartbeat — example only, no action expected",
                urgency="low",
                evidence={"heartbeat": True},
            ),
        ]
