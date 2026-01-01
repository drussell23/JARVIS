"""
JARVIS Monitoring API
=====================

System monitoring, metrics collection, and performance tracking endpoints.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import logging
import asyncio
import psutil
import os
import sys

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

# Metrics storage (in-memory for fast access)
_metrics_history: List[Dict[str, Any]] = []
_api_performance: Dict[str, Dict[str, Any]] = {}
_start_time = datetime.now()


def _get_system_metrics() -> Dict[str, Any]:
    """Get current system metrics"""
    try:
        # CPU metrics
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_count = psutil.cpu_count()

        # Memory metrics
        memory = psutil.virtual_memory()

        # Disk metrics
        disk = psutil.disk_usage('/')

        # Process metrics
        process = psutil.Process(os.getpid())
        process_memory = process.memory_info()

        return {
            "cpu": {
                "percent": cpu_percent,
                "count": cpu_count,
                "per_cpu": psutil.cpu_percent(percpu=True)
            },
            "memory": {
                "total_gb": round(memory.total / (1024**3), 2),
                "available_gb": round(memory.available / (1024**3), 2),
                "used_gb": round(memory.used / (1024**3), 2),
                "percent": memory.percent
            },
            "disk": {
                "total_gb": round(disk.total / (1024**3), 2),
                "used_gb": round(disk.used / (1024**3), 2),
                "free_gb": round(disk.free / (1024**3), 2),
                "percent": round(disk.percent, 1)
            },
            "process": {
                "memory_mb": round(process_memory.rss / (1024**2), 2),
                "cpu_percent": process.cpu_percent(),
                "threads": process.num_threads(),
                "open_files": len(process.open_files()) if hasattr(process, 'open_files') else 0
            }
        }
    except Exception as e:
        logger.error(f"Error getting system metrics: {e}")
        return {"error": str(e)}


@router.get("/metrics")
async def get_system_metrics() -> Dict[str, Any]:
    """
    Get comprehensive system metrics.

    Returns CPU, memory, disk, and process-level metrics.
    """
    try:
        metrics = _get_system_metrics()
        metrics["timestamp"] = datetime.now().isoformat()
        metrics["uptime_seconds"] = (datetime.now() - _start_time).total_seconds()

        # Store in history (keep last 100)
        _metrics_history.append(metrics)
        if len(_metrics_history) > 100:
            _metrics_history.pop(0)

        return metrics
    except Exception as e:
        logger.error(f"Metrics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api-performance")
async def get_api_performance() -> Dict[str, Any]:
    """
    Get API endpoint performance statistics.

    Returns average response times, request counts, and error rates.
    """
    try:
        # Get performance from global tracker if available
        performance_data = {}

        for endpoint, stats in _api_performance.items():
            if stats.get("request_count", 0) > 0:
                performance_data[endpoint] = {
                    "request_count": stats.get("request_count", 0),
                    "avg_response_ms": round(stats.get("total_time_ms", 0) / max(stats.get("request_count", 1), 1), 2),
                    "min_response_ms": stats.get("min_time_ms", 0),
                    "max_response_ms": stats.get("max_time_ms", 0),
                    "error_count": stats.get("error_count", 0),
                    "error_rate": round(stats.get("error_count", 0) / max(stats.get("request_count", 1), 1) * 100, 2)
                }

        return {
            "endpoints": performance_data,
            "total_requests": sum(s.get("request_count", 0) for s in _api_performance.values()),
            "total_errors": sum(s.get("error_count", 0) for s in _api_performance.values()),
            "measurement_period": {
                "start": _start_time.isoformat(),
                "duration_seconds": (datetime.now() - _start_time).total_seconds()
            }
        }
    except Exception as e:
        logger.error(f"API performance error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/resources")
async def get_resource_usage() -> Dict[str, Any]:
    """
    Get detailed resource usage information.

    Returns memory breakdown, file handles, network connections, etc.
    """
    try:
        process = psutil.Process(os.getpid())

        # Memory details
        memory_info = process.memory_info()
        memory_maps = []
        try:
            for mmap in process.memory_maps()[:10]:  # Limit to top 10
                memory_maps.append({
                    "path": mmap.path[:50] if mmap.path else "anonymous",
                    "rss_mb": round(mmap.rss / (1024**2), 2)
                })
        except (psutil.AccessDenied, AttributeError):
            pass

        # Open files
        open_files = []
        try:
            for f in process.open_files()[:20]:  # Limit to 20
                open_files.append(f.path)
        except (psutil.AccessDenied, AttributeError):
            pass

        # Network connections
        connections = []
        try:
            for conn in process.connections()[:10]:  # Limit to 10
                connections.append({
                    "local": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
                    "remote": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
                    "status": conn.status
                })
        except (psutil.AccessDenied, AttributeError):
            pass

        return {
            "memory": {
                "rss_mb": round(memory_info.rss / (1024**2), 2),
                "vms_mb": round(memory_info.vms / (1024**2), 2),
                "shared_mb": round(getattr(memory_info, 'shared', 0) / (1024**2), 2),
                "top_memory_maps": memory_maps
            },
            "files": {
                "open_count": len(open_files),
                "open_files": open_files
            },
            "network": {
                "connection_count": len(connections),
                "connections": connections
            },
            "threads": {
                "count": process.num_threads()
            },
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Resource usage error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health-history")
async def get_health_history(
    minutes: int = 60
) -> Dict[str, Any]:
    """
    Get historical health metrics.

    Args:
        minutes: Number of minutes of history to return (default: 60)
    """
    try:
        cutoff = datetime.now() - timedelta(minutes=minutes)

        # Filter history by time
        recent_history = [
            m for m in _metrics_history
            if datetime.fromisoformat(m.get("timestamp", "2000-01-01")) > cutoff
        ]

        if not recent_history:
            return {
                "history": [],
                "summary": {
                    "data_points": 0,
                    "period_minutes": minutes
                }
            }

        # Calculate averages
        avg_cpu = sum(m.get("cpu", {}).get("percent", 0) for m in recent_history) / len(recent_history)
        avg_memory = sum(m.get("memory", {}).get("percent", 0) for m in recent_history) / len(recent_history)

        return {
            "history": recent_history[-50:],  # Last 50 data points
            "summary": {
                "data_points": len(recent_history),
                "period_minutes": minutes,
                "avg_cpu_percent": round(avg_cpu, 2),
                "avg_memory_percent": round(avg_memory, 2),
                "peak_cpu_percent": max(m.get("cpu", {}).get("percent", 0) for m in recent_history),
                "peak_memory_percent": max(m.get("memory", {}).get("percent", 0) for m in recent_history)
            }
        }
    except Exception as e:
        logger.error(f"Health history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/components")
async def get_component_health() -> Dict[str, Any]:
    """
    Get health status of all JARVIS components.
    """
    try:
        components = {}

        # Check voice system
        try:
            from api.jarvis_voice_api import router as voice_router
            components["voice"] = {"status": "healthy", "available": True}
        except ImportError:
            components["voice"] = {"status": "unavailable", "available": False}

        # Check vision system
        try:
            from api.display_routes import router as display_router
            components["vision"] = {"status": "healthy", "available": True}
        except ImportError:
            components["vision"] = {"status": "unavailable", "available": False}

        # Check memory management
        try:
            from memory import memory_api
            components["memory"] = {"status": "healthy", "available": True}
        except ImportError:
            components["memory"] = {"status": "unavailable", "available": False}

        # Check wake word
        try:
            from api.wake_word_api import router as wake_word_router
            components["wake_word"] = {"status": "healthy", "available": True}
        except ImportError:
            components["wake_word"] = {"status": "unavailable", "available": False}

        # Check voice unlock
        try:
            from api.voice_unlock_api import router as voice_unlock_router
            components["voice_unlock"] = {"status": "healthy", "available": True}
        except ImportError:
            components["voice_unlock"] = {"status": "unavailable", "available": False}

        # Check ML audio
        try:
            from api.ml_audio_api import router as ml_audio_router
            components["ml_audio"] = {"status": "healthy", "available": True}
        except ImportError:
            components["ml_audio"] = {"status": "unavailable", "available": False}

        healthy_count = sum(1 for c in components.values() if c.get("available", False))
        total_count = len(components)

        return {
            "components": components,
            "summary": {
                "healthy": healthy_count,
                "total": total_count,
                "health_percentage": round(healthy_count / total_count * 100, 1) if total_count > 0 else 0
            },
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Component health error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/record-api-call")
async def record_api_call(
    endpoint: str,
    response_time_ms: float,
    success: bool = True
) -> Dict[str, str]:
    """
    Record an API call for performance tracking.

    Internal endpoint for middleware to report API performance.
    """
    try:
        if endpoint not in _api_performance:
            _api_performance[endpoint] = {
                "request_count": 0,
                "total_time_ms": 0,
                "min_time_ms": float('inf'),
                "max_time_ms": 0,
                "error_count": 0
            }

        stats = _api_performance[endpoint]
        stats["request_count"] += 1
        stats["total_time_ms"] += response_time_ms
        stats["min_time_ms"] = min(stats["min_time_ms"], response_time_ms)
        stats["max_time_ms"] = max(stats["max_time_ms"], response_time_ms)

        if not success:
            stats["error_count"] += 1

        return {"status": "recorded"}
    except Exception as e:
        logger.error(f"Record API call error: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/uptime")
async def get_uptime() -> Dict[str, Any]:
    """Get system uptime information."""
    try:
        uptime_delta = datetime.now() - _start_time

        return {
            "start_time": _start_time.isoformat(),
            "current_time": datetime.now().isoformat(),
            "uptime": {
                "total_seconds": uptime_delta.total_seconds(),
                "days": uptime_delta.days,
                "hours": uptime_delta.seconds // 3600,
                "minutes": (uptime_delta.seconds % 3600) // 60,
                "seconds": uptime_delta.seconds % 60
            },
            "formatted": str(uptime_delta).split('.')[0]
        }
    except Exception as e:
        logger.error(f"Uptime error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Helper function to track API calls (can be used by middleware)
def track_api_call(endpoint: str, response_time_ms: float, success: bool = True):
    """Track an API call - non-async version for middleware"""
    if endpoint not in _api_performance:
        _api_performance[endpoint] = {
            "request_count": 0,
            "total_time_ms": 0,
            "min_time_ms": float('inf'),
            "max_time_ms": 0,
            "error_count": 0
        }

    stats = _api_performance[endpoint]
    stats["request_count"] += 1
    stats["total_time_ms"] += response_time_ms
    stats["min_time_ms"] = min(stats["min_time_ms"], response_time_ms)
    stats["max_time_ms"] = max(stats["max_time_ms"], response_time_ms)

    if not success:
        stats["error_count"] += 1


# =============================================================================
# v10.0: AI LOADER OPTIMIZATION STATS
# =============================================================================

@router.get("/optimization/stats")
async def get_optimization_stats(request) -> Dict[str, Any]:
    """
    Get comprehensive AI Loader optimization statistics.

    Returns:
    - AI Loader status (available, initialized)
    - Router statistics (available engines, their capabilities)
    - Per-model stats (status, engine, load time, memory)
    - Summary (total models, ready, loading, failed)

    This endpoint provides full visibility into:
    - Which optimization engine each model is using (Rust, JIT, ONNX, Safetensors)
    - Model loading progress and status
    - Memory usage per model
    - Request queuing statistics
    """
    try:
        from core.proxy_helpers import get_optimization_stats as _get_opt_stats

        stats = _get_opt_stats(request.app.state)
        stats["timestamp"] = datetime.now().isoformat()
        stats["uptime_seconds"] = (datetime.now() - _start_time).total_seconds()

        return stats
    except ImportError as e:
        return {
            "status": "unavailable",
            "message": "Proxy helpers not available",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Optimization stats error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/optimization/engines")
async def get_engine_breakdown(request) -> Dict[str, Any]:
    """
    Get breakdown of models by optimization engine.

    Returns which models are using which engines:
    - RUST_INT8: Rust-accelerated INT8 quantization
    - JIT_TRACE: TorchScript traced (70x faster startup)
    - ONNX: ONNX Runtime optimized
    - SAFETENSORS: Zero-copy mmap loading
    - STANDARD: Standard PyTorch loading

    Example response:
    {
        "SAFETENSORS": {
            "count": 8,
            "models": ["ecapa_speaker", "vision_analyzer", ...],
            "total_memory_mb": 245.5
        },
        "JIT_TRACE": {
            "count": 2,
            "models": ["whisper_encoder", "vad_silero"],
            "total_memory_mb": 120.0
        }
    }
    """
    try:
        from core.proxy_helpers import get_engine_breakdown as _get_breakdown

        breakdown = _get_breakdown(request.app.state)
        breakdown["timestamp"] = datetime.now().isoformat()

        return breakdown
    except ImportError as e:
        return {
            "status": "unavailable",
            "message": "Proxy helpers not available",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Engine breakdown error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/optimization/proxies")
async def get_proxy_status(request) -> Dict[str, Any]:
    """
    Get detailed status of all Ghost Proxies.

    Returns per-proxy information:
    - status: GHOST, QUEUED, LOADING, READY, FAILED
    - ready: boolean
    - category: voice, vision, intelligence
    - loading: boolean

    This is useful for debugging which models are still warming up.
    """
    try:
        from core.proxy_helpers import get_all_proxy_stats

        stats = get_all_proxy_stats(request.app.state)

        # Calculate summary
        total = len(stats)
        ready = sum(1 for s in stats.values() if s.get("ready"))
        loading = sum(1 for s in stats.values() if s.get("loading"))

        return {
            "proxies": stats,
            "summary": {
                "total": total,
                "ready": ready,
                "loading": loading,
                "not_ready": total - ready,
            },
            "timestamp": datetime.now().isoformat()
        }
    except ImportError as e:
        return {
            "status": "unavailable",
            "message": "Proxy helpers not available",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Proxy status error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/health/ai-loader")
async def get_ai_loader_health(request) -> Dict[str, Any]:
    """
    Quick health check for the AI Loader system.

    Returns a simple health status suitable for load balancer checks:
    - healthy: All critical models are ready
    - warming: Some models still loading
    - degraded: Some models failed to load
    """
    try:
        ai_manager = getattr(request.app.state, 'ai_manager', None)

        if not ai_manager:
            return {
                "status": "disabled",
                "message": "AI Loader not initialized",
                "timestamp": datetime.now().isoformat()
            }

        stats = ai_manager.get_stats()
        summary = stats["summary"]

        # Determine health status
        if summary["failed"] > 0:
            status = "degraded"
        elif summary["loading"] > 0:
            status = "warming"
        else:
            status = "healthy"

        return {
            "status": status,
            "models": {
                "total": summary["total"],
                "ready": summary["ready"],
                "loading": summary["loading"],
                "failed": summary["failed"],
            },
            "router": {
                "engines_available": stats["router"]["available_count"],
                "engines_total": stats["router"]["total_count"],
            },
            "memory_mb": summary["total_memory_mb"],
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"AI Loader health check error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


# =============================================================================
# v9.0: GCP RATE LIMIT & QUOTA MONITORING
# =============================================================================

@router.get("/gcp/rate-limits")
async def get_gcp_rate_limit_status() -> Dict[str, Any]:
    """
    Get current GCP rate limit and quota status.
    
    Returns comprehensive rate limiting information for all GCP services:
    - Token bucket state (available tokens, capacity)
    - Sliding window state (current requests, limit)
    - Quota utilization
    - Cooldown status
    - Statistics (requests made, rate limited, etc.)
    """
    try:
        from core.gcp_rate_limit_manager import get_rate_limit_manager
        
        manager = await get_rate_limit_manager()
        status = manager.get_status()
        
        return {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "rate_limits": status
        }
    except ImportError:
        return {
            "status": "unavailable",
            "message": "GCP Rate Limit Manager not available",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting rate limit status: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/gcp/quotas")
async def get_gcp_quota_status() -> Dict[str, Any]:
    """
    Get current GCP quota status.
    
    Returns:
    - Cached quota information
    - Quota utilization percentages
    - Exceeded quotas (if any)
    """
    try:
        from core.gcp_rate_limit_manager import get_rate_limit_manager
        
        manager = await get_rate_limit_manager()
        
        # Check quotas for VM creation
        can_create, blocking_quotas, message = await manager.check_quota_for_vm()
        
        return {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "can_create_vm": can_create,
            "message": message,
            "blocking_quotas": [
                {
                    "metric": q.metric_name,
                    "limit": q.limit,
                    "usage": q.usage,
                    "available": q.available,
                    "utilization_percent": q.utilization_percent,
                    "region": q.region,
                }
                for q in blocking_quotas
            ],
            "quota_manager_status": manager._quota_manager.get_status() if hasattr(manager, '_quota_manager') else {}
        }
    except ImportError:
        return {
            "status": "unavailable",
            "message": "GCP Rate Limit Manager not available",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting quota status: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


# =============================================================================
# v9.0: GCP RATE LIMIT & QUOTA MONITORING
# =============================================================================

@router.get("/gcp/rate-limits")
async def get_gcp_rate_limit_status() -> Dict[str, Any]:
    """
    Get current GCP rate limit and quota status.
    
    Returns comprehensive rate limiting information for all GCP services:
    - Token bucket state (available tokens, capacity)
    - Sliding window state (current requests, limit)
    - Quota utilization
    - Cooldown status
    - Statistics (requests made, rate limited, etc.)
    """
    try:
        from core.gcp_rate_limit_manager import get_rate_limit_manager
        
        manager = await get_rate_limit_manager()
        status = manager.get_status()
        
        return {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "rate_limits": status
        }
    except ImportError:
        return {
            "status": "unavailable",
            "message": "GCP Rate Limit Manager not available",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting rate limit status: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/gcp/quotas")
async def get_gcp_quota_status() -> Dict[str, Any]:
    """
    Get current GCP quota status.
    
    Returns:
    - Cached quota information
    - Quota utilization percentages
    - Exceeded quotas (if any)
    """
    try:
        from core.gcp_rate_limit_manager import get_rate_limit_manager
        
        manager = await get_rate_limit_manager()
        
        # Check quotas for VM creation
        can_create, blocking_quotas, message = await manager.check_quota_for_vm()
        
        return {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "can_create_vm": can_create,
            "message": message,
            "blocking_quotas": [
                {
                    "metric": q.metric_name,
                    "limit": q.limit,
                    "usage": q.usage,
                    "available": q.available,
                    "utilization_percent": q.utilization_percent,
                    "region": q.region,
                }
                for q in blocking_quotas
            ],
            "quota_manager_status": manager._quota_manager.get_status() if hasattr(manager, '_quota_manager') else {}
        }
    except ImportError:
        return {
            "status": "unavailable",
            "message": "GCP Rate Limit Manager not available",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting quota status: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }
