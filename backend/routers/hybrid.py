#!/usr/bin/env python3
"""
Advanced Hybrid Cloud Intelligence API Router

Provides comprehensive endpoints for hybrid cloud cost tracking, metrics, and management.
Features: REST API, WebSocket support, cleanup triggers, alerts, forecasting.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from core.cost_tracker import get_cost_tracker, initialize_cost_tracking
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field, validator

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/hybrid", tags=["Hybrid Cloud"])

# WebSocket connections — use a set for O(1) add/discard and snapshot-iterate
_ws_connections: Set[WebSocket] = set()


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================


class CostSummaryResponse(BaseModel):
    """Enhanced cost summary response model"""

    period: str
    period_start: str
    period_end: str
    total_vms_created: int
    total_runtime_hours: float
    total_estimated_cost: float
    total_actual_cost: Optional[float] = 0.0
    orphaned_vms_count: int
    orphaned_vms_cost: float
    average_vm_lifetime_hours: float
    max_vm_lifetime_hours: Optional[float] = 0.0
    min_vm_lifetime_hours: Optional[float] = 0.0
    cost_savings_vs_regular: float
    savings_percentage: float
    cost_efficiency_score: Optional[float] = 0.0
    vm_type: Optional[str] = None
    spot_rate: Optional[float] = None
    regular_rate: Optional[float] = None


class RoutingMetricsResponse(BaseModel):
    """Enhanced routing metrics response model"""

    period: str
    total_requests: int
    local_requests: int
    gcp_requests: int
    successful_decisions: Optional[int] = 0
    success_rate: Optional[float] = 0.0
    gcp_routing_ratio: float
    average_local_ram_percent: float
    max_local_ram_percent: Optional[float] = 0.0
    average_decision_latency_ms: Optional[float] = 0.0


class OrphanedVMReport(BaseModel):
    """Orphaned VM report model"""

    total_orphaned_vms: int
    total_orphaned_cost: float
    orphaned_vms: List[Dict[str, Any]]
    max_age_hours: Optional[int] = None


class CleanupRequest(BaseModel):
    """Request to trigger manual cleanup"""

    force: bool = Field(False, description="Force cleanup even for recent VMs")
    max_age_hours: Optional[int] = Field(None, description="Override max age threshold")


class CleanupResponse(BaseModel):
    """Cleanup operation response"""

    checked_at: str
    orphaned_vms_found: int
    orphaned_vms_deleted: int
    errors: List[str]
    vms: List[Dict[str, Any]]


class ConfigUpdateRequest(BaseModel):
    """Request to update configuration"""

    spot_vm_hourly_cost: Optional[float] = None
    alert_threshold_daily: Optional[float] = None
    alert_threshold_weekly: Optional[float] = None
    alert_threshold_monthly: Optional[float] = None
    orphaned_vm_max_age_hours: Optional[int] = None
    enable_auto_cleanup: Optional[bool] = None

    @validator("spot_vm_hourly_cost")
    def validate_cost(cls, v):
        if v is not None and v < 0:
            raise ValueError("Cost must be non-negative")
        return v


class AlertSubscription(BaseModel):
    """Alert subscription configuration"""

    subscribe_cost_alerts: bool = True
    subscribe_orphaned_vm_alerts: bool = True
    subscribe_performance_alerts: bool = True
    min_alert_level: str = Field("warning", pattern="^(info|warning|critical|emergency)$")


# ============================================================================
# DEPENDENCIES
# ============================================================================


async def get_tracker():
    """Dependency to get cost tracker instance"""
    return get_cost_tracker()


# ============================================================================
# CORE ENDPOINTS
# ============================================================================


@router.get("/health")
async def hybrid_health():
    """Health check for hybrid cloud router"""
    tracker = get_cost_tracker()
    return {
        "status": "healthy",
        "service": "hybrid-cloud-intelligence",
        "timestamp": datetime.utcnow().isoformat(),
        "config": {
            "vm_type": tracker.config.vm_instance_type,
            "region": tracker.config.gcp_region,
            "auto_cleanup_enabled": tracker.config.enable_auto_cleanup,
            "cleanup_interval_hours": tracker.config.cleanup_check_interval_hours,
        },
        "websocket_connections": len(_ws_connections),
    }


@router.post("/initialize")
async def initialize_tracking():
    """Initialize cost tracking database (idempotent)"""
    try:
        await initialize_cost_tracking()
        return {
            "status": "initialized",
            "message": "Cost tracking database initialized successfully",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Failed to initialize cost tracking: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# COST ENDPOINTS
# ============================================================================


@router.get("/cost", response_model=CostSummaryResponse)
async def get_cost_summary(
    period: str = Query(
        "all",
        description="Time period: day, week, month, or all",
        regex="^(day|week|month|all)$",
    ),
    tracker=Depends(get_tracker),
):
    """
    Get comprehensive cost summary for specified period.

    Enhanced with:
    - Actual costs (from GCP billing API if available)
    - Cost efficiency score
    - Min/max VM lifetimes
    - VM type and pricing info
    """
    try:
        summary = await tracker.get_cost_summary(period)

        if not summary:
            # Return empty summary instead of 404
            return CostSummaryResponse(
                period=period,
                period_start=datetime.utcnow().isoformat(),
                period_end=datetime.utcnow().isoformat(),
                total_vms_created=0,
                total_runtime_hours=0.0,
                total_estimated_cost=0.0,
                orphaned_vms_count=0,
                orphaned_vms_cost=0.0,
                average_vm_lifetime_hours=0.0,
                cost_savings_vs_regular=0.0,
                savings_percentage=0.0,
            )

        return CostSummaryResponse(**summary)

    except Exception as e:
        logger.error(f"Failed to get cost summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cost/daily")
async def get_daily_cost(tracker=Depends(get_tracker)):
    """Get cost summary for the last 24 hours"""
    try:
        return await tracker.get_cost_summary("day")
    except Exception as e:
        logger.error(f"Failed to get daily cost: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cost/weekly")
async def get_weekly_cost(tracker=Depends(get_tracker)):
    """Get cost summary for the last 7 days"""
    try:
        return await tracker.get_cost_summary("week")
    except Exception as e:
        logger.error(f"Failed to get weekly cost: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cost/monthly")
async def get_monthly_cost(tracker=Depends(get_tracker)):
    """Get cost summary for the last 30 days"""
    try:
        return await tracker.get_cost_summary("month")
    except Exception as e:
        logger.error(f"Failed to get monthly cost: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ROUTING METRICS ENDPOINTS
# ============================================================================


@router.get("/metrics/routing", response_model=RoutingMetricsResponse)
async def get_routing_metrics(
    period: str = Query(
        "day",
        description="Time period: day, week, month, or all",
        regex="^(day|week|month|all)$",
    ),
    tracker=Depends(get_tracker),
):
    """
    Get routing performance metrics.

    Enhanced with:
    - Success rate tracking
    - Decision latency metrics
    - Max RAM usage
    """
    try:
        metrics = await tracker.get_routing_metrics(period)
        return RoutingMetricsResponse(**metrics)

    except Exception as e:
        logger.error(f"Failed to get routing metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ORPHANED VMS ENDPOINTS
# ============================================================================


@router.get("/orphaned-vms", response_model=OrphanedVMReport)
async def get_orphaned_vms(tracker=Depends(get_tracker)):
    """
    Get comprehensive report of all orphaned VMs.

    Shows VMs that weren't properly cleaned up with full details.
    """
    try:
        report = await tracker.get_orphaned_vms_report()
        return OrphanedVMReport(**report)

    except Exception as e:
        logger.error(f"Failed to get orphaned VMs report: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup", response_model=CleanupResponse)
async def trigger_cleanup(
    request: CleanupRequest = CleanupRequest(),
    background_tasks: BackgroundTasks = None,
    tracker=Depends(get_tracker),
):
    """
    Manually trigger orphaned VM cleanup.

    Can override age threshold and force cleanup.
    Runs in background to avoid timeout.
    """
    try:
        # Override config if requested
        if request.max_age_hours:
            original_max_age = tracker.config.orphaned_vm_max_age_hours
            tracker.config.orphaned_vm_max_age_hours = request.max_age_hours
            logger.info(f"Overriding max age: {original_max_age}h -> {request.max_age_hours}h")

        # Run cleanup
        results = await tracker.cleanup_orphaned_vms()

        # Notify WebSocket clients
        if results["orphaned_vms_deleted"] > 0:
            await _broadcast_ws_event(
                {
                    "event": "cleanup_completed",
                    "deleted": results["orphaned_vms_deleted"],
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )

        return CleanupResponse(**results)

    except Exception as e:
        logger.error(f"Failed to trigger cleanup: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# STATUS & CONFIGURATION ENDPOINTS
# ============================================================================


@router.get("/status")
async def get_hybrid_status(tracker=Depends(get_tracker)):
    """
    Get comprehensive hybrid cloud status.

    Returns:
    - Cost summaries (all-time, today, this week, this month)
    - Routing metrics
    - Orphaned VMs report
    - Configuration
    - Active VM sessions
    """
    try:
        # Get all data concurrently
        cost_all, cost_day, cost_week, cost_month, routing, orphaned = await asyncio.gather(
            tracker.get_cost_summary("all"),
            tracker.get_cost_summary("day"),
            tracker.get_cost_summary("week"),
            tracker.get_cost_summary("month"),
            tracker.get_routing_metrics("day"),
            tracker.get_orphaned_vms_report(),
        )

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "cost": {
                "all_time": cost_all,
                "today": cost_day,
                "this_week": cost_week,
                "this_month": cost_month,
            },
            "routing": routing,
            "orphaned_vms": orphaned,
            "active_sessions": {
                "count": len(tracker.active_sessions),
                "instances": list(tracker.active_sessions.keys()),
            },
            "config": {
                "vm_type": tracker.config.vm_instance_type,
                "region": tracker.config.gcp_region,
                "zone": tracker.config.gcp_zone,
                "spot_rate": tracker.config.spot_vm_hourly_cost,
                "auto_cleanup": tracker.config.enable_auto_cleanup,
                "cleanup_interval_hours": tracker.config.cleanup_check_interval_hours,
                "max_vm_age_hours": tracker.config.orphaned_vm_max_age_hours,
            },
            "health": "healthy",
        }

    except Exception as e:
        logger.error(f"Failed to get hybrid status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_configuration(tracker=Depends(get_tracker)):
    """Get current cost tracker configuration"""
    return {
        "database": str(tracker.config.db_path),
        "gcp": {
            "project_id": tracker.config.gcp_project_id,
            "region": tracker.config.gcp_region,
            "zone": tracker.config.gcp_zone,
            "vm_type": tracker.config.vm_instance_type,
        },
        "pricing": {
            "spot_vm_hourly_cost": tracker.config.spot_vm_hourly_cost,
            "regular_vm_hourly_cost": tracker.config.regular_vm_hourly_cost,
        },
        "alerts": {
            "daily_threshold": tracker.config.alert_threshold_daily,
            "weekly_threshold": tracker.config.alert_threshold_weekly,
            "monthly_threshold": tracker.config.alert_threshold_monthly,
        },
        "cleanup": {
            "auto_cleanup_enabled": tracker.config.enable_auto_cleanup,
            "check_interval_hours": tracker.config.cleanup_check_interval_hours,
            "max_age_hours": tracker.config.orphaned_vm_max_age_hours,
        },
        "notifications": {
            "desktop_notifications": tracker.config.enable_desktop_notifications,
            "email_alerts": tracker.config.enable_email_alerts,
            "alert_email": tracker.config.alert_email,
        },
    }


@router.patch("/config")
async def update_configuration(request: ConfigUpdateRequest, tracker=Depends(get_tracker)):
    """
    Update cost tracker configuration.

    Only updates provided fields, leaves others unchanged.
    """
    try:
        updated_fields = []

        if request.spot_vm_hourly_cost is not None:
            tracker.config.spot_vm_hourly_cost = request.spot_vm_hourly_cost
            updated_fields.append("spot_vm_hourly_cost")

        if request.alert_threshold_daily is not None:
            tracker.config.alert_threshold_daily = request.alert_threshold_daily
            updated_fields.append("alert_threshold_daily")

        if request.alert_threshold_weekly is not None:
            tracker.config.alert_threshold_weekly = request.alert_threshold_weekly
            updated_fields.append("alert_threshold_weekly")

        if request.alert_threshold_monthly is not None:
            tracker.config.alert_threshold_monthly = request.alert_threshold_monthly
            updated_fields.append("alert_threshold_monthly")

        if request.orphaned_vm_max_age_hours is not None:
            tracker.config.orphaned_vm_max_age_hours = request.orphaned_vm_max_age_hours
            updated_fields.append("orphaned_vm_max_age_hours")

        if request.enable_auto_cleanup is not None:
            tracker.config.enable_auto_cleanup = request.enable_auto_cleanup
            updated_fields.append("enable_auto_cleanup")

        return {
            "status": "updated",
            "updated_fields": updated_fields,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to update configuration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ALERT ENDPOINTS
# ============================================================================


@router.get("/alerts")
async def get_alerts(
    limit: int = Query(50, ge=1, le=500),
    level: Optional[str] = Query(None, regex="^(info|warning|critical|emergency)$"),
    tracker=Depends(get_tracker),
):
    """
    Get recent alerts from alert history.

    Can filter by alert level.
    """
    try:
        import aiosqlite

        async with aiosqlite.connect(tracker.config.db_path) as db:
            query = "SELECT * FROM alert_history"
            params = []

            if level:
                query += " WHERE alert_level = ?"
                params.append(level)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()

                alerts = []
                for row in rows:
                    alerts.append(
                        {
                            "id": row[0],
                            "timestamp": row[1],
                            "level": row[2],
                            "type": row[3],
                            "message": row[4],
                            "details": row[5],
                            "acknowledged": bool(row[6]),
                        }
                    )

                return {
                    "alerts": alerts,
                    "count": len(alerts),
                    "total_unacknowledged": sum(1 for a in alerts if not a["acknowledged"]),
                }

    except Exception as e:
        logger.error(f"Failed to get alerts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int, tracker=Depends(get_tracker)):
    """Mark an alert as acknowledged"""
    try:
        import aiosqlite

        async with aiosqlite.connect(tracker.config.db_path) as db:
            await db.execute("UPDATE alert_history SET acknowledged = 1 WHERE id = ?", (alert_id,))
            await db.commit()

        return {"status": "acknowledged", "alert_id": alert_id}

    except Exception as e:
        logger.error(f"Failed to acknowledge alert: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# WEBSOCKET ENDPOINTS
# ============================================================================


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time cost tracking updates.

    Clients receive events for:
    - VM creation/deletion
    - Cost alerts
    - Cleanup operations
    - Configuration changes
    """
    await websocket.accept()
    _ws_connections.add(websocket)

    logger.info(f"WebSocket client connected (total: {len(_ws_connections)})")

    try:
        # Send initial status
        tracker = get_cost_tracker()
        status_data = {
            "event": "connected",
            "timestamp": datetime.utcnow().isoformat(),
            "active_sessions": len(tracker.active_sessions),
        }
        await websocket.send_json(status_data)

        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_text()
            # Echo or process client messages if needed
            logger.debug(f"Received from WebSocket: {data}")

    except WebSocketDisconnect:
        _ws_connections.discard(websocket)
        logger.info(f"WebSocket client disconnected (total: {len(_ws_connections)})")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        _ws_connections.discard(websocket)


async def _broadcast_ws_event(event_data: Dict[str, Any]):
    """Broadcast event to all connected WebSocket clients"""
    if not _ws_connections:
        return

    disconnected = []
    for ws in list(_ws_connections):  # snapshot to avoid mutation during iteration
        try:
            await ws.send_json(event_data)
        except Exception as e:
            logger.error(f"Failed to send to WebSocket client: {e}")
            disconnected.append(ws)

    # Clean up disconnected clients
    for ws in disconnected:
        _ws_connections.discard(ws)


# Register callback to broadcast events
def _setup_event_broadcasting():
    """Setup event broadcasting to WebSocket clients"""

    async def broadcast_callback(event_type: str, data: Dict[str, Any]):
        await _broadcast_ws_event(
            {"event": event_type, "data": data, "timestamp": datetime.utcnow().isoformat()}
        )

    tracker = get_cost_tracker()
    tracker.register_alert_callback(broadcast_callback)


# Initialize on module load
try:
    _setup_event_broadcasting()
except Exception as e:
    logger.warning(f"Could not setup event broadcasting: {e}")


# ============================================================================
# TESTING & DEBUGGING ENDPOINTS
# ============================================================================


@router.post("/test/vm-event")
async def test_vm_event(
    event_type: str = Query(..., regex="^(created|deleted)$"),
    instance_id: str = Query("test-vm-123"),
    tracker=Depends(get_tracker),
):
    """
    Test endpoint to simulate VM events.

    For testing WebSocket and alert systems.
    """
    try:
        if event_type == "created":
            await tracker.record_vm_created(
                instance_id=instance_id, components=["test"], trigger_reason="MANUAL"
            )
        else:
            await tracker.record_vm_deleted(instance_id=instance_id)

        return {
            "status": "success",
            "event": event_type,
            "instance_id": instance_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"Test event failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
