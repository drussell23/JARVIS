"""
Remote Status API — HTTP endpoint for pipeline visibility.

Gap 5: TUI dashboard is local only. This module exposes pipeline status,
sensor health, cost report, entropy scores, and team progress via HTTP
so you can monitor the organism from mobile/web.

Runs alongside the EventChannelServer on a configurable port.
All endpoints are read-only. No authentication by default (bind to
localhost only for security).

Boundary Principle:
  Deterministic: JSON serialization of internal state. No inference.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATUS_PORT = int(os.environ.get("JARVIS_STATUS_PORT", "8098"))
_STATUS_HOST = os.environ.get("JARVIS_STATUS_HOST", "127.0.0.1")
_ENABLED = os.environ.get(
    "JARVIS_REMOTE_STATUS_ENABLED", "true"
).lower() in ("true", "1", "yes")


class RemoteStatusAPI:
    """HTTP API for remote monitoring of the Ouroboros pipeline.

    Endpoints:
      GET /status          — pipeline state, uptime, active operations
      GET /sensors          — health of all 13 sensors
      GET /costs            — unified cost report across providers
      GET /entropy          — recent entropy measurements
      GET /team             — agent team progress (if active)
      GET /checkpoints      — workspace checkpoint list
      GET /health           — simple health check
    """

    def __init__(
        self,
        gls: Optional[Any] = None,         # GovernedLoopService
        cost_aggregator: Optional[Any] = None,
        checkpoint_mgr: Optional[Any] = None,
        channel_server: Optional[Any] = None,
    ) -> None:
        self._gls = gls
        self._cost_aggregator = cost_aggregator
        self._checkpoint_mgr = checkpoint_mgr
        self._channel_server = channel_server
        self._boot_time = time.time()
        self._site: Optional[Any] = None

    async def start(self) -> None:
        if not _ENABLED:
            return

        try:
            from aiohttp import web

            app = web.Application()
            app.router.add_get("/status", self._handle_status)
            app.router.add_get("/sensors", self._handle_sensors)
            app.router.add_get("/costs", self._handle_costs)
            app.router.add_get("/entropy", self._handle_entropy)
            app.router.add_get("/team", self._handle_team)
            app.router.add_get("/checkpoints", self._handle_checkpoints)
            app.router.add_get("/health", self._handle_health)

            runner = web.AppRunner(app)
            await runner.setup()
            self._site = web.TCPSite(runner, _STATUS_HOST, _STATUS_PORT)
            await self._site.start()

            logger.info(
                "[RemoteStatus] API started on %s:%d",
                _STATUS_HOST, _STATUS_PORT,
            )
        except Exception as exc:
            logger.warning("[RemoteStatus] Failed to start: %s", exc)

    async def stop(self) -> None:
        if self._site:
            await self._site.stop()

    async def _handle_health(self, request: Any) -> Any:
        from aiohttp import web
        return web.json_response({
            "status": "healthy",
            "uptime_s": round(time.time() - self._boot_time),
        })

    async def _handle_status(self, request: Any) -> Any:
        from aiohttp import web
        status: Dict[str, Any] = {
            "uptime_s": round(time.time() - self._boot_time),
            "governance_mode": os.environ.get("JARVIS_GOVERNANCE_MODE", "sandbox"),
            "python_version": "",
        }

        import platform
        status["python_version"] = platform.python_version()

        # GLS status
        if self._gls is not None:
            try:
                status["gls_running"] = getattr(self._gls, "_running", False)
                status["active_operations"] = getattr(self._gls, "_active_op_count", 0)
            except Exception:
                pass

        # Channel server stats
        if self._channel_server is not None:
            try:
                status["channels"] = self._channel_server.get_stats()
            except Exception:
                pass

        return web.json_response(status)

    async def _handle_sensors(self, request: Any) -> Any:
        from aiohttp import web
        sensors: List[Dict[str, Any]] = []

        if self._gls is not None:
            try:
                intake = getattr(self._gls, "_intake_layer", None)
                if intake is not None:
                    for sensor in getattr(intake, "_sensors", []):
                        try:
                            health = sensor.health() if hasattr(sensor, "health") else {}
                            sensors.append(health)
                        except Exception:
                            sensors.append({
                                "sensor": type(sensor).__name__,
                                "error": "health check failed",
                            })
            except Exception:
                pass

        return web.json_response({"sensors": sensors, "count": len(sensors)})

    async def _handle_costs(self, request: Any) -> Any:
        from aiohttp import web
        if self._cost_aggregator is not None:
            try:
                report = self._cost_aggregator.generate_report()
                return web.json_response({
                    "total_cost_usd": report.total_cost_usd,
                    "total_tokens": report.total_tokens,
                    "total_requests": report.total_requests,
                    "providers": [
                        {
                            "provider": s.provider,
                            "cost_usd": s.total_cost_usd,
                            "tokens": s.total_input_tokens + s.total_output_tokens,
                            "requests": s.total_requests,
                        }
                        for s in report.providers
                    ],
                })
            except Exception:
                pass
        return web.json_response({"total_cost_usd": 0, "providers": []})

    async def _handle_entropy(self, request: Any) -> Any:
        from aiohttp import web
        # Read recent entropy measurements from ledger
        entries: List[Dict[str, Any]] = []
        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                LearningConsolidator,
            )
            consolidator = LearningConsolidator()
            for domain, rules in consolidator._rules.items():
                for rule in rules[-3:]:
                    entries.append({
                        "domain": domain,
                        "rule_type": rule.rule_type,
                        "confidence": rule.confidence,
                        "sample_size": rule.sample_size,
                        "description": rule.description[:100],
                    })
        except Exception:
            pass
        return web.json_response({"entropy_rules": entries})

    async def _handle_team(self, request: Any) -> Any:
        from aiohttp import web
        try:
            from backend.core.ouroboros.governance.agent_team import (
                AgentTeamCoordinator,
            )
            # Check for active teams
            import glob
            from pathlib import Path
            teams_dir = Path.home() / ".jarvis" / "ouroboros" / "teams"
            if teams_dir.exists():
                team_names = [d.name for d in teams_dir.iterdir() if d.is_dir()]
                teams = []
                for name in team_names[:5]:
                    team = AgentTeamCoordinator(name)
                    teams.append(team.get_progress())
                return web.json_response({"teams": teams})
        except Exception:
            pass
        return web.json_response({"teams": []})

    async def _handle_checkpoints(self, request: Any) -> Any:
        from aiohttp import web
        if self._checkpoint_mgr is not None:
            cps = self._checkpoint_mgr.list_checkpoints()
            return web.json_response({
                "checkpoints": [
                    {
                        "id": cp.checkpoint_id,
                        "op_id": cp.op_id,
                        "description": cp.description,
                        "files": len(cp.files_snapshot),
                        "created_at": cp.created_at,
                    }
                    for cp in cps
                ],
            })
        return web.json_response({"checkpoints": []})
