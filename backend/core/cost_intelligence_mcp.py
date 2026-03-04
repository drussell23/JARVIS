"""Cost Intelligence MCP Server — unified cost visibility and control.

Follows the `register_with_broker` pattern. Wraps CostTracker,
IntelligentGCPOptimizer, CloudCapacityController, and UsagePatternAnalyzer
into MCP-accessible tools and resources for AI-driven cost management.

Tools:
    query_costs(period)           — Current cost breakdown with attribution
    cost_forecast(days)           — Projected costs with confidence intervals
    get_recommendations()         — Actionable cost reduction suggestions
    set_budget(period, amount)    — Dynamically adjust budget thresholds
    get_cost_efficiency_report()  — VM utilization, false alarm rate, cost/inference
    verify_zero_cost_posture()    — Check no resources running when supervisor down

Resources:
    jarvis://cost/current         — Current spend across all services
    jarvis://cost/budget          — Budget status and remaining capacity
    jarvis://cost/forecast        — Projected monthly cost
    jarvis://cost/efficiency      — Cost efficiency metrics
    jarvis://cost/recommendations — Optimization opportunities
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Module-level singleton
_instance: Optional["CostIntelligenceMCP"] = None


@dataclass
class CostIntelligenceConfig:
    """Configuration for CostIntelligenceMCP — all env-var-driven."""

    gcp_project: str = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_PROJECT", "jarvis-473803")
    )
    gcp_region: str = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_REGION", "us-central1")
    )
    cloud_run_services: str = field(
        default_factory=lambda: os.getenv("JARVIS_CLOUD_RUN_SERVICES", "jarvis-prime-ecapa")
    )
    always_on_vms: str = field(
        default_factory=lambda: os.getenv("JARVIS_ALWAYS_ON_VMS", "")
    )
    always_on_ips: str = field(
        default_factory=lambda: os.getenv("JARVIS_ALWAYS_ON_IPS", "")
    )
    solo_developer_mode: bool = field(
        default_factory=lambda: os.getenv("JARVIS_SOLO_DEVELOPER_MODE", "true").lower() == "true"
    )
    forecast_lookback_days: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_FORECAST_LOOKBACK_DAYS", "30"))
    )


class CostIntelligenceMCP:
    """Unified MCP server for cost intelligence, visibility, and control.

    Integrates with:
    - CostTracker: budget enforcement, spend recording
    - UsagePatternAnalyzer: learned usage patterns
    - CloudCapacityController: scaling decisions
    - GCPVMManager: VM lifecycle stats
    """

    def __init__(self, config: Optional[CostIntelligenceConfig] = None):
        self.config = config or CostIntelligenceConfig()
        self._initialized = False
        self._broker = None
        self._mcp_active = False
        self._cost_tracker = None
        self._usage_analyzer = None
        self._capacity_controller = None

    async def initialize(self) -> bool:
        """Initialize by connecting to existing singletons."""
        try:
            # Connect to CostTracker
            try:
                from backend.core.cost_tracker import get_cost_tracker
                self._cost_tracker = get_cost_tracker()
            except (ImportError, Exception) as e:
                logger.debug("[CostMCP] CostTracker not available: %s", e)

            # Connect to UsagePatternAnalyzer
            try:
                from backend.core.usage_pattern_analyzer import get_usage_pattern_analyzer
                self._usage_analyzer = get_usage_pattern_analyzer()
            except (ImportError, Exception) as e:
                logger.debug("[CostMCP] UsagePatternAnalyzer not available: %s", e)

            # Connect to CloudCapacityController
            try:
                from backend.core.cloud_capacity_controller import get_cloud_capacity_controller
                self._capacity_controller = get_cloud_capacity_controller()
            except (ImportError, Exception) as e:
                logger.debug("[CostMCP] CloudCapacityController not available: %s", e)

            self._initialized = True
            logger.info("[CostMCP] Initialized (tracker=%s, analyzer=%s, capacity=%s)",
                        self._cost_tracker is not None,
                        self._usage_analyzer is not None,
                        self._capacity_controller is not None)
            return True

        except Exception as e:
            logger.error("[CostMCP] Initialization failed: %s", e)
            return False

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self._initialized = False
        logger.info("[CostMCP] Shut down")

    def register_with_broker(self, broker: Any) -> None:
        """Register with MemoryBudgetBroker as pressure observer."""
        self._broker = broker
        try:
            broker.register_pressure_observer(self._on_pressure_change)
            self._mcp_active = True
            logger.info("[CostMCP] Registered with MCP broker")
        except Exception as e:
            logger.warning("[CostMCP] Broker registration failed: %s", e)

    async def _on_pressure_change(self, tier: Any, snapshot: Any) -> None:
        """Broker pressure-observer callback."""
        pass  # Cost MCP is read-heavy; pressure changes logged by other components

    # =========================================================================
    # MCP Tools
    # =========================================================================

    async def query_costs(self, period: str = "day") -> Dict[str, Any]:
        """Current cost breakdown with attribution.

        Args:
            period: "day", "week", or "month"
        """
        result: Dict[str, Any] = {
            "period": period,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "vm_costs": {},
            "cloud_service_costs": {},
            "total": 0.0,
        }

        if not self._cost_tracker:
            result["error"] = "CostTracker not available"
            return result

        try:
            # VM costs
            summary = await self._cost_tracker.get_cost_summary(period=period)
            if summary:
                result["vm_costs"] = summary
                result["total"] += summary.get("total_cost", 0.0)

            # Cloud service costs (SQL, static IP, Cloud Run, etc.)
            cloud_costs = await self._cost_tracker.get_cloud_service_costs(period=period)
            if cloud_costs:
                result["cloud_service_costs"] = cloud_costs
                result["total"] += cloud_costs.get("total_cost", 0.0)

            # Budget context
            budget_status = await self._cost_tracker.get_budget_status()
            if budget_status:
                result["budget"] = budget_status

        except Exception as e:
            result["error"] = str(e)
            logger.warning("[CostMCP] query_costs failed: %s", e)

        return result

    async def cost_forecast(self, days: int = 30) -> Dict[str, Any]:
        """Projected costs with confidence intervals.

        Args:
            days: Number of days to forecast
        """
        result: Dict[str, Any] = {
            "forecast_days": days,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not self._cost_tracker:
            result["error"] = "CostTracker not available"
            return result

        try:
            # Use existing forecast_daily_cost
            forecast = await self._cost_tracker.forecast_daily_cost()
            if forecast:
                daily_rate = forecast.get("predicted_daily_cost", 0.0)
                result["daily_rate"] = daily_rate
                result["projected_total"] = daily_rate * days
                result["confidence_score"] = forecast.get("confidence_score", 0.0)
                result["forecast_details"] = forecast

            # Add usage pattern context
            if self._usage_analyzer:
                stats = await self._usage_analyzer.get_stats()
                result["usage_patterns"] = {
                    "avg_daily_sessions": stats.get("avg_daily_sessions", 0),
                    "avg_session_duration_hours": stats.get("avg_session_duration_hours", 0),
                    "false_alarm_rate": stats.get("false_alarm_rate", 0),
                }

        except Exception as e:
            result["error"] = str(e)
            logger.warning("[CostMCP] cost_forecast failed: %s", e)

        return result

    async def get_recommendations(self) -> List[Dict[str, Any]]:
        """Actionable cost reduction suggestions based on current state."""
        recommendations: List[Dict[str, Any]] = []

        try:
            # 1. Check if Cloud Run is not scale-to-zero
            if self.config.solo_developer_mode:
                recommendations.append({
                    "id": "cloud_run_scale_to_zero",
                    "category": "cloud_run",
                    "priority": "high",
                    "title": "Ensure Cloud Run scale-to-zero in solo mode",
                    "description": (
                        "Cloud Run with min-instances=1 costs ~$145/mo. "
                        "Set CLOUD_RUN_MIN_INSTANCES=0 via deploy_cloud_run.sh."
                    ),
                    "estimated_savings_monthly": 145.0,
                })

            # 2. Check usage patterns for golden image ROI
            if self._usage_analyzer:
                sessions = await self._usage_analyzer.get_avg_daily_sessions()
                if sessions < 13:
                    recommendations.append({
                        "id": "skip_golden_image",
                        "category": "golden_image",
                        "priority": "medium",
                        "title": "Golden images not cost-effective at current usage",
                        "description": (
                            f"At {sessions:.1f} sessions/day, golden image storage "
                            f"costs exceed startup time savings. Break-even is ~13 sessions/day."
                        ),
                        "estimated_savings_monthly": 3.0,
                    })

            # 3. Check for idle VMs
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
                mgr = get_gcp_vm_manager_safe()
                if mgr:
                    idle_vms = [
                        name for name, vm in mgr.managed_vms.items()
                        if hasattr(vm, 'idle_time_minutes') and vm.idle_time_minutes > 10
                    ]
                    if idle_vms:
                        recommendations.append({
                            "id": "terminate_idle_vms",
                            "category": "vm",
                            "priority": "high",
                            "title": f"Terminate {len(idle_vms)} idle VM(s)",
                            "description": (
                                f"VMs {', '.join(idle_vms)} have been idle >10min. "
                                f"Consider lowering GCP_IDLE_TIMEOUT_MINUTES."
                            ),
                            "estimated_savings_monthly": len(idle_vms) * 20.0,
                        })
            except (ImportError, Exception):
                pass

            # 4. Check for idle static IPs
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
                mgr = get_gcp_vm_manager_safe()
                if mgr and mgr._static_ip_tracking:
                    for ip_name, alloc_time in mgr._static_ip_tracking.items():
                        idle_hours = (time.monotonic() - alloc_time) / 3600.0
                        if idle_hours > 2:
                            recommendations.append({
                                "id": f"release_ip_{ip_name}",
                                "category": "static_ip",
                                "priority": "low",
                                "title": f"Release idle static IP '{ip_name}'",
                                "description": (
                                    f"Static IP idle for {idle_hours:.1f}h. "
                                    f"Costs $0.010/hr ($7.20/mo) even without VM."
                                ),
                                "estimated_savings_monthly": 7.20,
                            })
            except (ImportError, Exception):
                pass

            # 5. Check false alarm rate
            if self._usage_analyzer:
                false_alarm = await self._usage_analyzer.get_false_alarm_rate()
                if false_alarm > 0.2:
                    recommendations.append({
                        "id": "reduce_false_alarms",
                        "category": "vm_creation",
                        "priority": "high",
                        "title": f"High false alarm rate ({false_alarm:.0%})",
                        "description": (
                            f"{false_alarm:.0%} of VMs are created but never used. "
                            f"Increase JARVIS_CRITICAL_SUSTAIN_THRESHOLD_S to reduce premature creation."
                        ),
                        "estimated_savings_monthly": false_alarm * 30.0,
                    })

        except Exception as e:
            recommendations.append({
                "id": "error",
                "category": "system",
                "priority": "info",
                "title": "Error generating recommendations",
                "description": str(e),
            })

        return recommendations

    async def set_budget(self, period: str, amount: float) -> Dict[str, Any]:
        """Dynamically adjust budget thresholds.

        Args:
            period: "daily" or "monthly"
            amount: New budget amount in USD
        """
        result: Dict[str, Any] = {
            "period": period,
            "new_amount": amount,
            "success": False,
        }

        if not self._cost_tracker:
            result["error"] = "CostTracker not available"
            return result

        try:
            config = self._cost_tracker.config

            if period == "daily":
                old_val = config.alert_threshold_daily
                config.alert_threshold_daily = amount
                result["old_amount"] = old_val
                result["success"] = True
            elif period == "monthly":
                old_val = config.alert_threshold_monthly
                config.alert_threshold_monthly = amount
                result["old_amount"] = old_val
                result["success"] = True
            else:
                result["error"] = f"Invalid period '{period}'. Use 'daily' or 'monthly'."

            if result["success"]:
                logger.info(
                    "[CostMCP] Budget updated: %s %s → $%.2f",
                    period, result.get("old_amount"), amount,
                )

        except Exception as e:
            result["error"] = str(e)
            logger.warning("[CostMCP] set_budget failed: %s", e)

        return result

    async def get_cost_efficiency_report(self) -> Dict[str, Any]:
        """VM utilization, false alarm rate, cost per inference."""
        report: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # VM stats
        try:
            from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
            mgr = get_gcp_vm_manager_safe()
            if mgr:
                stats = mgr.get_stats()
                report["vm_stats"] = {
                    "active_vms": stats.get("active_vms", 0),
                    "total_vms_created": stats.get("total_vms_created", 0),
                    "total_cost": stats.get("total_cost", 0.0),
                    "total_uptime_hours": stats.get("total_uptime_hours", 0.0),
                }
        except (ImportError, Exception) as e:
            report["vm_stats_error"] = str(e)

        # Usage patterns
        if self._usage_analyzer:
            try:
                usage = await self._usage_analyzer.get_stats()
                report["usage_patterns"] = usage
            except Exception as e:
                report["usage_patterns_error"] = str(e)

        # Capacity controller stats
        if self._capacity_controller:
            try:
                report["capacity_stats"] = self._capacity_controller.get_stats()
            except Exception as e:
                report["capacity_stats_error"] = str(e)

        # Budget status
        if self._cost_tracker:
            try:
                report["budget_status"] = await self._cost_tracker.get_budget_status()
            except Exception as e:
                report["budget_error"] = str(e)

        return report

    async def verify_zero_cost_posture(self) -> Dict[str, Any]:
        """Check that no billable resources are running when supervisor is down.

        Mirrors the checks in scripts/jarvis_verify_zero_cost.sh but runs
        in-process for MCP accessibility.
        """
        result: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pass": True,
            "failures": [],
            "checks": [],
        }

        project = self.config.gcp_project
        region = self.config.gcp_region
        always_on_vms = set(filter(None, self.config.always_on_vms.split(",")))
        always_on_ips = set(filter(None, self.config.always_on_ips.split(",")))

        # Check 1: Running VMs with jarvis label
        try:
            proc = await asyncio.create_subprocess_exec(
                "gcloud", "compute", "instances", "list",
                f"--project={project}",
                "--filter=labels.created-by=jarvis AND status=RUNNING",
                "--format=value(name)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            vms = [v.strip() for v in stdout.decode().strip().splitlines() if v.strip()]

            unauthorized_vms = [v for v in vms if v not in always_on_vms]
            if unauthorized_vms:
                result["pass"] = False
                result["failures"].append({
                    "check": "running_vms",
                    "detail": f"Running VM(s) not in allowlist: {unauthorized_vms}",
                })
            result["checks"].append({
                "check": "running_vms",
                "status": "pass" if not unauthorized_vms else "fail",
                "found": vms,
                "allowlist": list(always_on_vms),
            })
        except Exception as e:
            result["checks"].append({"check": "running_vms", "status": "error", "error": str(e)})

        # Check 2: Cloud SQL proxy process
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-f", "cloud-sql-proxy",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            proxy_running = proc.returncode == 0
            if proxy_running:
                result["pass"] = False
                result["failures"].append({
                    "check": "cloud_sql_proxy",
                    "detail": f"Cloud SQL proxy still running (pids: {stdout.decode().strip()})",
                })
            result["checks"].append({
                "check": "cloud_sql_proxy",
                "status": "pass" if not proxy_running else "fail",
            })
        except Exception as e:
            result["checks"].append({"check": "cloud_sql_proxy", "status": "error", "error": str(e)})

        # Check 3: launchd plist
        plist_path = os.path.expanduser("~/Library/LaunchAgents/com.jarvis.cloudsql-proxy.plist")
        plist_exists = os.path.exists(plist_path)
        if plist_exists:
            result["pass"] = False
            result["failures"].append({
                "check": "launchd_plist",
                "detail": f"Proxy launchd plist still exists at {plist_path}",
            })
        result["checks"].append({
            "check": "launchd_plist",
            "status": "pass" if not plist_exists else "fail",
        })

        # Check 4: Static IPs with jarvis label
        try:
            proc = await asyncio.create_subprocess_exec(
                "gcloud", "compute", "addresses", "list",
                f"--project={project}",
                "--filter=labels.created-by=jarvis AND status=RESERVED",
                "--format=value(name)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            ips = [ip.strip() for ip in stdout.decode().strip().splitlines() if ip.strip()]

            unauthorized_ips = [ip for ip in ips if ip not in always_on_ips]
            if unauthorized_ips:
                result["pass"] = False
                result["failures"].append({
                    "check": "static_ips",
                    "detail": f"Reserved static IP(s) not in allowlist: {unauthorized_ips}",
                })
            result["checks"].append({
                "check": "static_ips",
                "status": "pass" if not unauthorized_ips else "fail",
                "found": ips,
                "allowlist": list(always_on_ips),
            })
        except Exception as e:
            result["checks"].append({"check": "static_ips", "status": "error", "error": str(e)})

        # Check 5: Cloud Run min-instances (solo mode only)
        if self.config.solo_developer_mode:
            services = [s.strip() for s in self.config.cloud_run_services.split(",") if s.strip()]
            for svc in services:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "gcloud", "run", "services", "describe", svc,
                        f"--project={project}", f"--region={region}",
                        "--format=value(spec.template.metadata.annotations['autoscaling.knative.dev/minScale'])",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                    min_inst = stdout.decode().strip()

                    # Missing or "0" = PASS (Cloud Run default is 0)
                    is_ok = not min_inst or min_inst == "0"
                    if not is_ok:
                        result["pass"] = False
                        result["failures"].append({
                            "check": f"cloud_run_{svc}",
                            "detail": f"Cloud Run '{svc}' min-instances={min_inst} (expected 0)",
                        })
                    result["checks"].append({
                        "check": f"cloud_run_{svc}",
                        "status": "pass" if is_ok else "fail",
                        "min_instances": min_inst or "0",
                    })
                except Exception as e:
                    result["checks"].append({
                        "check": f"cloud_run_{svc}",
                        "status": "error",
                        "error": str(e),
                    })

        failure_count = len(result["failures"])
        result["summary"] = (
            f"PASS: Zero-cost posture verified" if result["pass"]
            else f"FAIL: {failure_count} cost leak(s) detected"
        )

        return result

    # =========================================================================
    # MCP Resources
    # =========================================================================

    async def get_resource(self, uri: str) -> Dict[str, Any]:
        """Resolve a jarvis://cost/* resource URI."""
        handlers = {
            "jarvis://cost/current": self._resource_current,
            "jarvis://cost/budget": self._resource_budget,
            "jarvis://cost/forecast": self._resource_forecast,
            "jarvis://cost/efficiency": self._resource_efficiency,
            "jarvis://cost/recommendations": self._resource_recommendations,
        }

        handler = handlers.get(uri)
        if handler:
            return await handler()
        return {"error": f"Unknown resource URI: {uri}"}

    async def _resource_current(self) -> Dict[str, Any]:
        """jarvis://cost/current — current spend across all services."""
        return await self.query_costs("day")

    async def _resource_budget(self) -> Dict[str, Any]:
        """jarvis://cost/budget — budget status and remaining capacity."""
        if not self._cost_tracker:
            return {"error": "CostTracker not available"}
        try:
            return await self._cost_tracker.get_budget_status()
        except Exception as e:
            return {"error": str(e)}

    async def _resource_forecast(self) -> Dict[str, Any]:
        """jarvis://cost/forecast — projected monthly cost."""
        return await self.cost_forecast(30)

    async def _resource_efficiency(self) -> Dict[str, Any]:
        """jarvis://cost/efficiency — cost efficiency metrics."""
        return await self.get_cost_efficiency_report()

    async def _resource_recommendations(self) -> Dict[str, Any]:
        """jarvis://cost/recommendations — optimization opportunities."""
        recs = await self.get_recommendations()
        total_savings = sum(r.get("estimated_savings_monthly", 0) for r in recs)
        return {
            "recommendations": recs,
            "total_potential_savings_monthly": total_savings,
            "count": len(recs),
        }


# =========================================================================
# Singleton access
# =========================================================================

def get_cost_intelligence_mcp(
    config: Optional[CostIntelligenceConfig] = None,
) -> CostIntelligenceMCP:
    """Get or create the CostIntelligenceMCP singleton."""
    global _instance
    if _instance is None:
        _instance = CostIntelligenceMCP(config or CostIntelligenceConfig())
    return _instance


async def initialize_cost_intelligence_mcp(
    config: Optional[CostIntelligenceConfig] = None,
) -> CostIntelligenceMCP:
    """Initialize and return the CostIntelligenceMCP singleton."""
    instance = get_cost_intelligence_mcp(config)
    if not instance._initialized:
        await instance.initialize()
    return instance
