"""
Autonomous Service API
======================
FastAPI endpoints for the autonomous orch with zero configuration.
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import asyncio
import logging
import time
from typing import Dict, List, Any, Optional
from datetime import datetime
import json

import os
import sys

# Add parent directory to path for imports
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# Check if we should use memory-optimized version
use_memory_optimized = os.getenv('MEMORY_OPTIMIZED_MODE', 'true').lower() == 'true'

logger = logging.getLogger(__name__)

# Lazy loading of orchestrator
orchestrator = None

def get_lazy_orchestrator():
    """Lazy load orchestrator to reduce startup time and memory"""
    global orchestrator
    if orchestrator is None:
        if use_memory_optimized:
            from core.memory_optimized_orchestrator import get_memory_optimized_orchestrator
            orchestrator = get_memory_optimized_orchestrator(memory_limit_mb=400)
            logger.info("Using memory-optimized orchestrator (400MB limit)")
        else:
            from core.autonomous_orchestrator import get_orchestrator
            orchestrator = get_orchestrator()
            logger.info("Using standard orchestrator")
    return orchestrator

# Import ServiceInfo separately
try:
    from core.autonomous_orchestrator import ServiceInfo
except ImportError:
    # Fallback definition if import fails
    from typing import NamedTuple
    ServiceInfo = NamedTuple('ServiceInfo', [
        ('name', str),
        ('host', str), 
        ('port', int),
        ('protocol', str),
        ('health_score', float)
    ])

router = APIRouter(prefix="/services", tags=["Autonomous Services"])

# Task for orch
_orch_task = None

async def ensure_orch_running():
    """Ensure the orch is running"""
    global _orch_task
    orch = get_lazy_orchestrator()
    if _orch_task is None or _orch_task.done():
        logger.info("Starting autonomous orch...")
        _orch_task = asyncio.create_task(orch.start())
        await asyncio.sleep(2)  # Give it time to discover services

@router.on_event("startup")
async def startup_event():
    """Start the orch on API startup"""
    await ensure_orch_running()
    logger.info("✅ Autonomous service orch started")

@router.get("/discovery")
async def get_discovered_services():
    """Get all discovered services"""
    await ensure_orch_running()
    
    services = {}
    
    # Handle memory-optimized orch with limited features
    orch = get_lazy_orchestrator()
    if use_memory_optimized:
        for name, service in orch.services.items():
            services[name] = {
                "name": service.name,
                "host": "localhost",
                "port": service.port,
                "protocol": service.protocol,
                "endpoints": {},  # Memory-optimized version doesn't track endpoints
                "health_score": service.health_score,
                "last_seen": datetime.fromtimestamp(service.last_seen).isoformat(),
                "error_count": 0,  # Not tracked in memory-optimized version
                "auto_discovered": True,
                "average_response_time": 0  # Not tracked in memory-optimized version
            }
    else:
        # Full orch with all features
        for name, service in orch.services.items():
            services[name] = {
                "name": service.name,
                "host": service.host,
                "port": service.port,
                "protocol": service.protocol,
                "endpoints": service.endpoints,
                "health_score": service.health_score,
                "last_seen": service.last_seen.isoformat() if service.last_seen else None,
                "error_count": service.error_count,
                "auto_discovered": service.auto_discovered,
                "average_response_time": sum(service.response_times) / len(service.response_times) if service.response_times else 0
            }
    
    return JSONResponse({
        "services": services,
        "total": len(services),
        "healthy": sum(1 for s in orch.services.values() if s.health_score > 0.7),
        "unhealthy": sum(1 for s in orch.services.values() if s.health_score <= 0.7),
        "mode": "memory_optimized" if use_memory_optimized else "full"
    })

@router.get("/config")
async def get_dynamic_config():
    """Get dynamic configuration for frontend"""
    await ensure_orch_running()
    
    if use_memory_optimized:
        # Simplified config for memory-optimized version
        config = {
            "API_BASE_URL": None,
            "WS_BASE_URL": None,
            "ENDPOINTS": {},
            "SERVICES": {}
        }
        
        # Find backend service
        backend = orch.services.get("backend_api")
        if backend:
            config["API_BASE_URL"] = f"http://localhost:{backend.port}"
            config["WS_BASE_URL"] = f"ws://localhost:{backend.port}"
        
        # Add all discovered services
        for name, service in orch.services.items():
            config["SERVICES"][name] = {
                "url": f"{service.protocol}://localhost:{service.port}",
                "health_score": service.health_score,
                "endpoints": {}
            }
    else:
        # Full config from standard orch
        config = orch.get_frontend_config()
    
    # Add additional metadata
    config["metadata"] = {
        "generated_at": datetime.now().isoformat(),
        "orch_version": "2.0.0" if use_memory_optimized else "1.0.0",
        "mode": "memory_optimized" if use_memory_optimized else "full",
        "discovery_enabled": True,
        "self_healing_enabled": not use_memory_optimized  # Memory-optimized doesn't have self-healing
    }
    
    return JSONResponse(config)

@router.post("/register/{service_name}")
async def register_service(service_name: str, service_data: Dict[str, Any]):
    """Manually register a service"""
    await ensure_orch_running()
    
    try:
        service = ServiceInfo(
            name=service_name,
            host=service_data.get("host", "localhost"),
            port=service_data.get("port"),
            protocol=service_data.get("protocol", "http"),
            endpoints=service_data.get("endpoints", {}),
            auto_discovered=False
        )
        
        await orch._register_service(service)
        
        return JSONResponse({
            "status": "registered",
            "service": service_name,
            "url": f"{service.protocol}://{service.host}:{service.port}"
        })
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/health/{service_name}")
async def check_service_health(service_name: str):
    """Check health of a specific service"""
    await ensure_orch_running()
    
    service = orch.services.get(service_name)
    if not service:
        # Try to discover it
        await orch._discover_and_register_service(service_name)
        service = orch.services.get(service_name)
        
    if not service:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")
    
    # Perform health check
    is_healthy = await orch._check_service_health(service)
    
    return JSONResponse({
        "service": service_name,
        "healthy": is_healthy,
        "health_score": service.health_score,
        "last_seen": service.last_seen.isoformat() if service.last_seen else None,
        "error_count": service.error_count,
        "average_response_time": sum(service.response_times) / len(service.response_times) if service.response_times else 0
    })

@router.post("/heal/{service_name}")
async def heal_service(service_name: str):
    """Manually trigger healing for a service"""
    await ensure_orch_running()
    
    service = orch.services.get(service_name)
    if not service:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")
    
    # Trigger healing
    await orch._heal_services([service])
    
    # Check new health
    is_healthy = await orch._check_service_health(service)
    
    return JSONResponse({
        "service": service_name,
        "healing_applied": True,
        "healthy": is_healthy,
        "health_score": service.health_score
    })

@router.websocket("/monitor")
async def service_monitor_websocket(websocket: WebSocket):
    """WebSocket for real-time service monitoring"""
    await websocket.accept()
    await ensure_orch_running()
    
    try:
        # Send initial state
        services_data = {
            "type": "initial_state",
            "services": {
                name: {
                    "name": service.name,
                    "port": service.port,
                    "health_score": service.health_score,
                    "healthy": service.health_score > 0.7
                }
                for name, service in orch.services.items()
            }
        }
        await websocket.send_json(services_data)
        
        # Monitor for changes with connection timeout protection
        last_state = services_data["services"].copy()
        connection_start = time.monotonic()
        max_connection_time = float(os.getenv("TIMEOUT_SSE_CONNECTION", "3600.0"))  # 1 hour default

        while True:
            # Check connection timeout
            if time.monotonic() - connection_start > max_connection_time:
                logger.info("Autonomous service WebSocket connection timeout, closing")
                break

            await asyncio.sleep(2)  # Check every 2 seconds
            
            current_state = {
                name: {
                    "name": service.name,
                    "port": service.port,
                    "health_score": service.health_score,
                    "healthy": service.health_score > 0.7
                }
                for name, service in orch.services.items()
            }
            
            # Check for changes
            changes = []
            
            # New services
            for name in current_state:
                if name not in last_state:
                    changes.append({
                        "type": "service_discovered",
                        "service": name,
                        "data": current_state[name]
                    })
            
            # Removed services
            for name in last_state:
                if name not in current_state:
                    changes.append({
                        "type": "service_lost",
                        "service": name
                    })
            
            # Health changes
            for name in current_state:
                if name in last_state:
                    if current_state[name]["health_score"] != last_state[name]["health_score"]:
                        changes.append({
                            "type": "health_changed",
                            "service": name,
                            "data": current_state[name],
                            "previous_score": last_state[name]["health_score"]
                        })
                    
                    if current_state[name]["port"] != last_state[name]["port"]:
                        changes.append({
                            "type": "service_relocated",
                            "service": name,
                            "old_port": last_state[name]["port"],
                            "new_port": current_state[name]["port"]
                        })
            
            # Send changes
            if changes:
                await websocket.send_json({
                    "type": "updates",
                    "changes": changes,
                    "timestamp": datetime.now().isoformat()
                })
            
            last_state = current_state.copy()
            
    except WebSocketDisconnect:
        logger.info("Service monitor WebSocket disconnected")
    except Exception as e:
        logger.error(f"Service monitor error: {e}")
        await websocket.close()

@router.get("/proxy/{service_name}/{path:path}")
async def proxy_service_request(service_name: str, path: str = ""):
    """Proxy requests to discovered services"""
    await ensure_orch_running()
    
    # Use orch to call service
    result = await orch.call_service(
        service_name,
        path,
        method="GET"
    )
    
    if result is None:
        raise HTTPException(
            status_code=503, 
            detail=f"Service {service_name} unavailable or endpoint /{path} not found"
        )
    
    return JSONResponse(result)

@router.get("/diagnostics")
async def get_system_diagnostics():
    """Get comprehensive system diagnostics"""
    await ensure_orch_running()
    
    # Collect diagnostics
    diagnostics = {
        "orch": {
            "running": orch._running,
            "services_discovered": len(orch.services),
            "port_registry": dict(orch.port_registry),
            "cache_size": len(orch.endpoint_cache)
        },
        "services": {},
        "healing_history": {},
        "patterns": {}
    }
    
    # Service details
    for name, service in orch.services.items():
        diagnostics["services"][name] = {
            "port": service.port,
            "health_score": service.health_score,
            "error_count": service.error_count,
            "response_times": {
                "average": sum(service.response_times) / len(service.response_times) if service.response_times else 0,
                "min": min(service.response_times) if service.response_times else 0,
                "max": max(service.response_times) if service.response_times else 0
            },
            "endpoints": list(service.endpoints.keys())
        }
    
    # Healing strategies
    for service_name, strategies in orch.healing_strategies.items():
        diagnostics["healing_history"][service_name] = [
            {
                "name": s.name,
                "priority": s.priority,
                "success_rate": s.success_rate,
                "attempts": s.attempts,
                "last_success": s.last_success.isoformat() if s.last_success else None
            }
            for s in strategies
        ]
    
    # Pattern learner insights
    if orch.pattern_learner:
        patterns = orch.pattern_learner.get_patterns()
        for service_name, service_patterns in patterns.items():
            diagnostics["patterns"][service_name] = {
                "total_patterns": len(service_patterns),
                "recent_events": [p["event"] for p in service_patterns[-10:]]
            }
    
    return JSONResponse(diagnostics)

# Export router for inclusion in main app
__all__ = ["router"]