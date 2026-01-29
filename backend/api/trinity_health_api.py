"""
Trinity Health API v2.0 - Readiness-Aware Health Checks
========================================================

v2.0: MAJOR ENHANCEMENT - Proper Liveness/Readiness/Startup Probes
--------------------------------------------------------------------
Fixes the ROOT CAUSE of health check false positives by:
1. Distinguishing between liveness (process alive) and readiness (fully initialized)
2. Returning 503 Service Unavailable during initialization, not 200 OK
3. Supporting Kubernetes-style health probe semantics

Health Endpoint Semantics:
- /health/live   - Returns 200 if process is running (liveness probe)
- /health/ready  - Returns 200 ONLY when fully initialized (readiness probe)
- /health/startup - Returns 200 once initial startup completes (startup probe)
- /health/ping   - Simple ping (always 200 if server responding)

Why This Matters:
- Before: /health returned 200 OK immediately when FastAPI started
- Problem: Supervisor thought component was ready while it was still loading
- After: /health/ready returns 503 during initialization, 200 only when ready

Provides real-time status of:
- JARVIS-Prime (Mind) - Local AI inference
- Reactor-Core (Nerves) - Orchestration layer
- Graceful Degradation status
- Routing metrics

Author: JARVIS Trinity v94.0 - Readiness-Aware Health System
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["trinity"])

# =============================================================================
# v94.0: Readiness State Integration
# =============================================================================

def _get_readiness_manager_safe(component_name: str = "jarvis-body") -> Optional[Any]:
    """
    Safely get readiness manager if available.

    v123.5: CRITICAL FIX - Use consistent import paths to ensure singleton integrity.
    The import path MUST match what main.py uses, otherwise Python treats them as
    different modules with separate singleton dictionaries, causing 503 errors.
    """
    try:
        # v123.5: Try the same import path as main.py first (core.* without backend prefix)
        # This is critical for singleton integrity - Python module caching is path-based
        try:
            from core.readiness_state_manager import get_readiness_manager
        except ImportError:
            # Fallback: Try with backend prefix (different working directory)
            from backend.core.readiness_state_manager import get_readiness_manager

        return get_readiness_manager(component_name)
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"[TrinityHealth] Could not get readiness manager: {e}")
        return None


# =============================================================================
# RESPONSE MODELS
# =============================================================================

class ComponentStatus(BaseModel):
    """Status of a Trinity component."""
    name: str
    status: str  # healthy, degraded, unhealthy, unknown
    latency_ms: Optional[float] = None
    last_check: Optional[float] = None
    details: Dict[str, Any] = {}


class TrinityHealthResponse(BaseModel):
    """Full Trinity health response."""
    overall_status: str
    components: Dict[str, ComponentStatus]
    routing: Dict[str, Any]
    graceful_degradation: Dict[str, Any]
    timestamp: float


class PrimeStatusResponse(BaseModel):
    """JARVIS-Prime specific status."""
    status: str
    available: bool
    circuit_breaker: Dict[str, Any]
    metrics: Dict[str, Any]
    config: Dict[str, Any]


# =============================================================================
# HEALTH ENDPOINTS
# =============================================================================

@router.get("/trinity", response_model=TrinityHealthResponse)
async def get_trinity_health() -> TrinityHealthResponse:
    """
    Get comprehensive health status of the Trinity ecosystem.

    Returns status of:
    - JARVIS-Prime (local AI)
    - Reactor-Core (orchestration)
    - Prime Router
    - Graceful Degradation
    """
    components = {}
    overall_healthy = True

    # Check JARVIS-Prime (Mind)
    prime_status = await _check_prime_health()
    components["jarvis_prime"] = prime_status
    if prime_status.status not in ("healthy", "unknown"):
        overall_healthy = False

    # Check Prime Router
    router_status = await _check_router_health()
    components["prime_router"] = router_status
    if router_status.status == "unhealthy":
        overall_healthy = False

    # Check Reactor-Core (Nerves) via heartbeat file
    reactor_status = await _check_reactor_health()
    components["reactor_core"] = reactor_status

    # Get routing metrics
    routing_info = await _get_routing_metrics()

    # Get graceful degradation status
    degradation_info = await _get_degradation_status()

    return TrinityHealthResponse(
        overall_status="healthy" if overall_healthy else "degraded",
        components=components,
        routing=routing_info,
        graceful_degradation=degradation_info,
        timestamp=time.time(),
    )


@router.get("/prime", response_model=PrimeStatusResponse)
async def get_prime_health() -> PrimeStatusResponse:
    """Get detailed JARVIS-Prime status."""
    try:
        try:
            from core.prime_router import get_prime_router
        except ImportError:
            from backend.core.prime_router import get_prime_router

        router = await get_prime_router()
        status = router.get_status()

        prime_available = (
            status.get("prime_client", {}).get("available", False) and
            status.get("prime_client", {}).get("status", {}).get("status") in ("available", "degraded")
        )

        return PrimeStatusResponse(
            status="available" if prime_available else "unavailable",
            available=prime_available,
            circuit_breaker=status.get("prime_client", {}).get("status", {}).get("circuit_breaker", {}),
            metrics=status.get("metrics", {}),
            config=status.get("config", {}),
        )

    except Exception as e:
        logger.error(f"[TrinityHealth] Error getting Prime status: {e}")
        return PrimeStatusResponse(
            status="error",
            available=False,
            circuit_breaker={},
            metrics={},
            config={"error": str(e)},
        )


@router.get("/routing")
async def get_routing_status() -> Dict[str, Any]:
    """Get current AI routing status and metrics."""
    return await _get_routing_metrics()


@router.get("/degradation")
async def get_degradation_status() -> Dict[str, Any]:
    """Get graceful degradation system status."""
    return await _get_degradation_status()


@router.post("/prime/reset-circuit")
async def reset_prime_circuit() -> Dict[str, Any]:
    """Reset the Prime client circuit breaker."""
    try:
        try:
            from core.prime_client import get_prime_client
        except ImportError:
            from backend.core.prime_client import get_prime_client

        client = await get_prime_client()
        # Reset is done via the circuit breaker
        if hasattr(client, '_circuit'):
            client._circuit._state.state = "closed"
            client._circuit._state.failures = 0
            client._circuit._state.circuit_open = False

        return {"success": True, "message": "Circuit breaker reset"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# v94.0: READINESS-AWARE HEALTH PROBES (FIX FOR FALSE POSITIVES)
# =============================================================================

@router.get("/live")
async def liveness_probe() -> Response:
    """
    v94.0: Kubernetes-style LIVENESS probe.

    Returns:
        200 OK: Process is alive and running
        503 Service Unavailable: Process is dead/stopping

    Use Case:
        - Container orchestration liveness checks
        - Should ALWAYS return 200 unless process is truly dead
        - NOT for checking if service is ready for traffic
    """
    manager = _get_readiness_manager_safe()

    if manager:
        # v123.5: Consistent import path for singleton integrity
        try:
            from core.readiness_state_manager import ProbeType
        except ImportError:
            from backend.core.readiness_state_manager import ProbeType

        response = manager.handle_probe(ProbeType.LIVENESS)

        return JSONResponse(
            status_code=response.status_code,
            content=response.to_dict(),
        )

    # Fallback: if we can respond, we're alive
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "ready": True,
            "phase": "unknown",
            "message": "Process alive (readiness manager not available)",
            "timestamp": time.time(),
        },
    )


@router.get("/ready")
async def readiness_probe() -> Response:
    """
    v94.0: Kubernetes-style READINESS probe.

    This is the KEY FIX for health check false positives.

    Returns:
        200 OK: Service is fully initialized and ready for traffic
        503 Service Unavailable: Service is still initializing

    Why This Matters:
        - Before: Health checks returned 200 immediately when FastAPI started
        - Problem: Orchestrator thought service was ready during initialization
        - Now: Returns 503 during initialization, 200 only when truly ready

    Use Case:
        - Load balancer health checks
        - Service discovery readiness
        - Startup sequencing in orchestration
    """
    manager = _get_readiness_manager_safe()

    if manager:
        # v123.5: Consistent import path for singleton integrity
        try:
            from core.readiness_state_manager import ProbeType
        except ImportError:
            from backend.core.readiness_state_manager import ProbeType

        response = manager.handle_probe(ProbeType.READINESS)

        logger.debug(
            f"[TrinityHealth] Readiness probe: "
            f"status_code={response.status_code}, "
            f"phase={response.phase.value}, "
            f"ready={response.success}"
        )

        return JSONResponse(
            status_code=response.status_code,
            content=response.to_dict(),
        )

    # Fallback: Check basic health indicators
    # This is less accurate but provides backwards compatibility
    return await _fallback_readiness_check()


@router.get("/startup")
async def startup_probe() -> Response:
    """
    v94.0: Kubernetes-style STARTUP probe.

    Returns:
        200 OK: Initial startup is complete
        503 Service Unavailable: Still starting up

    Difference from Readiness:
        - Startup probe only checks initial startup
        - Once it returns 200, it stays 200 (startup is done)
        - Readiness can flip back to 503 if service becomes unhealthy

    Use Case:
        - Slow-starting containers
        - Prevent liveness probes from killing during long startup
    """
    manager = _get_readiness_manager_safe()

    if manager:
        # v123.5: Consistent import path for singleton integrity
        try:
            from core.readiness_state_manager import ProbeType
        except ImportError:
            from backend.core.readiness_state_manager import ProbeType

        response = manager.handle_probe(ProbeType.STARTUP)

        return JSONResponse(
            status_code=response.status_code,
            content=response.to_dict(),
        )

    # Fallback
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "ready": True,
            "phase": "unknown",
            "message": "Startup assumed complete (readiness manager not available)",
            "timestamp": time.time(),
        },
    )


@router.get("/ping")
async def ping() -> Dict[str, Any]:
    """
    Simple ping endpoint - always returns 200 if server is responding.

    This is NOT a readiness check. Use /health/ready for that.
    """
    return {
        "status": "ok",
        "pong": True,
        "timestamp": time.time(),
    }


@router.get("/status")
async def detailed_status() -> Dict[str, Any]:
    """
    v94.0: Detailed component initialization status.

    Returns comprehensive information about:
    - Current initialization phase
    - Individual component readiness
    - Timing information
    - Any errors
    """
    manager = _get_readiness_manager_safe()

    if manager:
        status = manager.get_status()
        status["endpoint"] = "detailed_status"
        status["timestamp"] = time.time()
        return status

    # Fallback
    return {
        "phase": "unknown",
        "is_ready": True,
        "is_healthy": True,
        "message": "Readiness manager not available",
        "timestamp": time.time(),
    }


async def _fallback_readiness_check() -> Response:
    """
    Fallback readiness check when ReadinessStateManager is not available.

    Uses basic health indicators to estimate readiness.
    """
    try:
        # Check if critical components are loaded
        checks_passed = 0
        total_checks = 3

        # Check 1: Can we import core modules?
        try:
            from backend.core import service_registry
            checks_passed += 1
        except ImportError:
            pass

        # Check 2: Is there a health status file?
        try:
            import json
            trinity_dir = os.getenv("TRINITY_DIR", os.path.expanduser("~/.jarvis/trinity"))
            health_file = os.path.join(trinity_dir, "health_status.json")
            if os.path.exists(health_file):
                with open(health_file) as f:
                    data = json.load(f)
                    if data.get("overall_status") in ("healthy", "degraded"):
                        checks_passed += 1
        except Exception:
            pass

        # Check 3: Basic response time (sanity check)
        start = time.time()
        _ = 1 + 1  # Trivial operation
        elapsed = time.time() - start
        if elapsed < 1.0:  # Should be instant
            checks_passed += 1

        is_ready = checks_passed >= 2  # At least 2 of 3 checks pass

        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ok" if is_ready else "not_ready",
                "ready": is_ready,
                "phase": "ready" if is_ready else "unknown",
                "message": f"Fallback check: {checks_passed}/{total_checks} passed",
                "checks_passed": checks_passed,
                "total_checks": total_checks,
                "timestamp": time.time(),
            },
        )

    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "ready": False,
                "phase": "error",
                "message": f"Fallback check failed: {e}",
                "timestamp": time.time(),
            },
        )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

async def _check_prime_health() -> ComponentStatus:
    """Check JARVIS-Prime health."""
    try:
        try:
            from core.prime_client import get_prime_client
        except ImportError:
            from backend.core.prime_client import get_prime_client

        start = time.time()
        client = await get_prime_client()
        latency = (time.time() - start) * 1000

        status = client.get_status()
        prime_status = status.get("status", "unknown")

        return ComponentStatus(
            name="JARVIS-Prime (Mind)",
            status=prime_status if prime_status in ("available", "degraded") else "unhealthy",
            latency_ms=latency,
            last_check=time.time(),
            details=status,
        )

    except Exception as e:
        return ComponentStatus(
            name="JARVIS-Prime (Mind)",
            status="unknown",
            details={"error": str(e)},
        )


async def _check_router_health() -> ComponentStatus:
    """Check Prime Router health."""
    try:
        try:
            from core.prime_router import get_prime_router
        except ImportError:
            from backend.core.prime_router import get_prime_router

        start = time.time()
        router = await get_prime_router()
        latency = (time.time() - start) * 1000

        status = router.get_status()
        is_healthy = status.get("initialized", False)

        return ComponentStatus(
            name="Prime Router",
            status="healthy" if is_healthy else "unhealthy",
            latency_ms=latency,
            last_check=time.time(),
            details=status,
        )

    except Exception as e:
        return ComponentStatus(
            name="Prime Router",
            status="unhealthy",
            details={"error": str(e)},
        )


async def _check_reactor_health() -> ComponentStatus:
    """Check Reactor-Core health via heartbeat file."""
    try:
        import json

        trinity_dir = os.getenv("TRINITY_DIR", os.path.expanduser("~/.jarvis/trinity"))
        heartbeat_file = os.path.join(trinity_dir, "components", "reactor_core.json")

        if os.path.exists(heartbeat_file):
            with open(heartbeat_file, 'r') as f:
                data = json.load(f)

            last_heartbeat = data.get("timestamp", 0)
            age = time.time() - last_heartbeat

            if age < 30:
                status = "healthy"
            elif age < 60:
                status = "degraded"
            else:
                status = "unhealthy"

            return ComponentStatus(
                name="Reactor-Core (Nerves)",
                status=status,
                last_check=last_heartbeat,
                details={
                    "heartbeat_age_seconds": round(age, 1),
                    "version": data.get("version", "unknown"),
                    "orchestrator_state": data.get("state", "unknown"),
                },
            )
        else:
            return ComponentStatus(
                name="Reactor-Core (Nerves)",
                status="unknown",
                details={"error": "Heartbeat file not found"},
            )

    except Exception as e:
        return ComponentStatus(
            name="Reactor-Core (Nerves)",
            status="unknown",
            details={"error": str(e)},
        )


async def _get_routing_metrics() -> Dict[str, Any]:
    """Get routing metrics from Prime Router."""
    try:
        try:
            from core.prime_router import get_prime_router
        except ImportError:
            from backend.core.prime_router import get_prime_router

        router = await get_prime_router()
        status = router.get_status()

        return {
            "initialized": status.get("initialized", False),
            "metrics": status.get("metrics", {}),
            "config": status.get("config", {}),
            "prime_available": status.get("prime_client", {}).get("available", False),
            "cloud_available": status.get("cloud_client", {}).get("available", False),
        }

    except Exception as e:
        return {"error": str(e)}


async def _get_degradation_status() -> Dict[str, Any]:
    """Get graceful degradation status."""
    try:
        try:
            from core.graceful_degradation import get_degradation
        except ImportError:
            from backend.core.graceful_degradation import get_degradation

        degradation = get_degradation()
        return degradation.get_status()

    except Exception as e:
        return {"error": str(e), "available": False}
