"""
JARVIS Proxy Helpers v2.0
=========================

Utilities for safely accessing Ghost Model Proxies throughout the application.

These helpers provide:
- Safe proxy retrieval with graceful degradation
- Warming state detection and handling
- HTTP-friendly error responses for FastAPI routes
- Async-aware waiting with configurable timeouts
- FastAPI Dependency Injection support

Usage in FastAPI routes:

    # Option 1: Dependency Injection (RECOMMENDED)
    from core.proxy_helpers import RequiresModel

    @router.post("/analyze")
    async def analyze_image(
        request: Request,
        vision: Any = Depends(RequiresModel("vision_analyzer"))
    ):
        return await vision.analyze(image)

    # Option 2: Decorator
    from core.proxy_helpers import requires_model

    @router.post("/analyze")
    @requires_model("vision_analyzer")
    async def analyze_image(request: Request):
        vision = request.app.state.vision_analyzer
        return await vision.analyze(image)

    # Option 3: Manual (for complex scenarios)
    from core.proxy_helpers import require_model_ready

    @router.post("/analyze")
    async def analyze_image(request: Request):
        vision = await require_model_ready(request.app.state, "vision_analyzer")
        return await vision.analyze(image)

Version: 2.0.0 - FastAPI Dependency Injection + Ghost Proxy Utilities
"""

import asyncio
import logging
from typing import Any, Optional, TypeVar, Union
from enum import Enum

logger = logging.getLogger("jarvis.proxy_helpers")

# Type variable for generic proxy typing
T = TypeVar("T")


class ProxyState(Enum):
    """States a Ghost Proxy can be in."""
    NOT_REGISTERED = "not_registered"
    GHOST = "ghost"
    QUEUED = "queued"
    LOADING = "loading"
    QUANTIZING = "quantizing"
    READY = "ready"
    FAILED = "failed"
    UNLOADED = "unloaded"


class ProxyNotReadyError(Exception):
    """Raised when a proxy is accessed but the model isn't ready."""

    def __init__(
        self,
        model_name: str,
        state: ProxyState,
        message: Optional[str] = None,
        retry_after: Optional[float] = None,
    ):
        self.model_name = model_name
        self.state = state
        self.retry_after = retry_after
        self.message = message or f"Model '{model_name}' is {state.value}"
        super().__init__(self.message)


class ProxyNotFoundError(Exception):
    """Raised when a proxy is not registered."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        super().__init__(f"Model '{model_name}' is not registered")


def get_model_proxy(
    app_state: Any,
    model_name: str,
    raise_if_missing: bool = True,
) -> Optional[Any]:
    """
    Safely retrieve a Ghost Proxy from app state.

    Args:
        app_state: FastAPI app.state object
        model_name: Name of the model/proxy to retrieve
        raise_if_missing: If True, raises ProxyNotFoundError if not found

    Returns:
        The Ghost Proxy if found, None if not found and raise_if_missing=False

    Raises:
        ProxyNotFoundError: If model not registered and raise_if_missing=True

    Example:
        vision = get_model_proxy(request.app.state, "vision_analyzer")
        if vision.is_ready:
            result = vision.analyze(image)
    """
    proxy = getattr(app_state, model_name, None)

    if proxy is None:
        # Check if it's in the voice_model_proxies dict
        voice_proxies = getattr(app_state, 'voice_model_proxies', {})
        proxy = voice_proxies.get(model_name)

    if proxy is None:
        # Check if it's in the vision_model_proxies dict
        vision_proxies = getattr(app_state, 'vision_model_proxies', {})
        proxy = vision_proxies.get(model_name)

    if proxy is None:
        # Check if it's in the intelligence_proxies dict
        intel_proxies = getattr(app_state, 'intelligence_proxies', {})
        proxy = intel_proxies.get(model_name)

    if proxy is None and raise_if_missing:
        raise ProxyNotFoundError(model_name)

    return proxy


def get_proxy_state(proxy: Any) -> ProxyState:
    """
    Get the current state of a Ghost Proxy.

    Args:
        proxy: A GhostModelProxy instance

    Returns:
        ProxyState enum value
    """
    if proxy is None:
        return ProxyState.NOT_REGISTERED

    # Check if it's a GhostModelProxy
    if hasattr(proxy, 'status'):
        status = proxy.status
        # Map ModelStatus to ProxyState
        status_name = status.name if hasattr(status, 'name') else str(status)
        try:
            return ProxyState[status_name.upper()]
        except KeyError:
            return ProxyState.GHOST

    # If it's a real model (not a proxy), it's ready
    return ProxyState.READY


def is_proxy_ready(proxy: Any) -> bool:
    """
    Check if a Ghost Proxy is ready for use.

    Args:
        proxy: A GhostModelProxy instance or real model

    Returns:
        True if ready, False otherwise
    """
    if proxy is None:
        return False

    # Check for GhostModelProxy
    if hasattr(proxy, 'is_ready'):
        return proxy.is_ready

    # If it's a real model (not a proxy), it's ready
    return True


def is_proxy_loading(proxy: Any) -> bool:
    """
    Check if a Ghost Proxy is currently loading.

    Args:
        proxy: A GhostModelProxy instance

    Returns:
        True if loading/queued/quantizing, False otherwise
    """
    if proxy is None:
        return False

    if hasattr(proxy, 'is_loading'):
        return proxy.is_loading

    return False


async def wait_for_proxy(
    proxy: Any,
    timeout: Optional[float] = None,
    poll_interval: float = 0.1,
) -> bool:
    """
    Wait for a Ghost Proxy to become ready.

    Args:
        proxy: A GhostModelProxy instance
        timeout: Max seconds to wait (None = use proxy's default)
        poll_interval: Seconds between ready checks

    Returns:
        True if proxy became ready, False if timeout
    """
    if proxy is None:
        return False

    if hasattr(proxy, 'wait_ready'):
        return await proxy.wait_ready(timeout=timeout)

    # If it's already a real model, it's ready
    return True


async def require_model_ready(
    app_state: Any,
    model_name: str,
    timeout: Optional[float] = 30.0,
    raise_on_timeout: bool = True,
) -> Any:
    """
    Get a model proxy and wait for it to be ready.

    This is the recommended way to access models in FastAPI routes
    when you need the model to be fully loaded.

    Args:
        app_state: FastAPI app.state object
        model_name: Name of the model/proxy to retrieve
        timeout: Max seconds to wait for model to be ready
        raise_on_timeout: If True, raises ProxyNotReadyError on timeout

    Returns:
        The ready model/proxy

    Raises:
        ProxyNotFoundError: If model not registered
        ProxyNotReadyError: If model not ready after timeout

    Example:
        @router.post("/transcribe")
        async def transcribe_audio(request: Request, audio: UploadFile):
            whisper = await require_model_ready(request.app.state, "whisper_model")
            return await whisper.transcribe(audio)
    """
    proxy = get_model_proxy(app_state, model_name)

    if is_proxy_ready(proxy):
        return proxy

    # Wait for the proxy to become ready
    ready = await wait_for_proxy(proxy, timeout=timeout)

    if not ready and raise_on_timeout:
        state = get_proxy_state(proxy)
        raise ProxyNotReadyError(
            model_name=model_name,
            state=state,
            message=f"Model '{model_name}' not ready after {timeout}s (state: {state.value})",
            retry_after=5.0,  # Suggest client retry after 5s
        )

    return proxy


def get_all_proxy_stats(app_state: Any) -> dict:
    """
    Get status of all registered proxies.

    Args:
        app_state: FastAPI app.state object

    Returns:
        Dict with proxy names and their states
    """
    stats = {}

    # Check voice proxies
    voice_proxies = getattr(app_state, 'voice_model_proxies', {})
    for name, proxy in voice_proxies.items():
        stats[name] = {
            "category": "voice",
            "state": get_proxy_state(proxy).value,
            "ready": is_proxy_ready(proxy),
            "loading": is_proxy_loading(proxy),
        }

    # Check vision proxies
    vision_proxies = getattr(app_state, 'vision_model_proxies', {})
    for name, proxy in vision_proxies.items():
        stats[name] = {
            "category": "vision",
            "state": get_proxy_state(proxy).value,
            "ready": is_proxy_ready(proxy),
            "loading": is_proxy_loading(proxy),
        }

    # Check intelligence proxies
    intel_proxies = getattr(app_state, 'intelligence_proxies', {})
    for name, proxy in intel_proxies.items():
        stats[name] = {
            "category": "intelligence",
            "state": get_proxy_state(proxy).value,
            "ready": is_proxy_ready(proxy),
            "loading": is_proxy_loading(proxy),
        }

    # Check individual app.state attributes
    known_models = [
        ("ecapa_model", "voice"),
        ("voice_unlock_classifier", "voice"),
        ("vad_model", "voice"),
        ("vision_analyzer", "vision"),
        ("display_monitor", "vision"),
        ("neural_mesh", "intelligence"),
        ("hybrid_orchestrator", "intelligence"),
    ]

    for model_name, category in known_models:
        if model_name not in stats:
            proxy = getattr(app_state, model_name, None)
            if proxy is not None:
                stats[model_name] = {
                    "category": category,
                    "state": get_proxy_state(proxy).value,
                    "ready": is_proxy_ready(proxy),
                    "loading": is_proxy_loading(proxy),
                }

    return stats


# FastAPI integration helpers

def create_warming_response(model_name: str, retry_after: float = 5.0) -> dict:
    """
    Create a standard "warming up" response for API endpoints.

    Args:
        model_name: Name of the model that's warming up
        retry_after: Suggested retry delay in seconds

    Returns:
        Dict suitable for FastAPI JSONResponse
    """
    return {
        "status": "warming_up",
        "message": f"The {model_name} is initializing. Please retry shortly.",
        "model": model_name,
        "retry_after": retry_after,
    }


def http_exception_for_proxy_error(error: Union[ProxyNotReadyError, ProxyNotFoundError]):
    """
    Convert a proxy error to an appropriate HTTPException.

    Args:
        error: ProxyNotReadyError or ProxyNotFoundError

    Returns:
        HTTPException with appropriate status code

    Usage:
        try:
            model = await require_model_ready(app.state, "vision")
        except (ProxyNotReadyError, ProxyNotFoundError) as e:
            raise http_exception_for_proxy_error(e)
    """
    # Import here to avoid circular imports
    from fastapi import HTTPException

    if isinstance(error, ProxyNotFoundError):
        return HTTPException(
            status_code=500,
            detail={
                "error": "model_not_registered",
                "model": error.model_name,
                "message": str(error),
            }
        )

    if isinstance(error, ProxyNotReadyError):
        headers = {}
        if error.retry_after:
            headers["Retry-After"] = str(int(error.retry_after))

        return HTTPException(
            status_code=503,
            detail={
                "error": "model_warming_up",
                "model": error.model_name,
                "state": error.state.value,
                "message": str(error),
                "retry_after": error.retry_after,
            },
            headers=headers,
        )

    return HTTPException(status_code=500, detail=str(error))


# =============================================================================
# FastAPI Dependency Injection
# =============================================================================

class RequiresModel:
    """
    FastAPI Dependency that ensures a model is ready before route execution.

    This is the RECOMMENDED way to access Ghost Proxies in FastAPI routes.
    It integrates seamlessly with FastAPI's dependency injection system and
    provides automatic waiting, error handling, and OpenAPI documentation.

    Args:
        model_name: Name of the required model
        timeout: Max seconds to wait for model (default: 30s)
        allow_warming: If True, returns proxy even if still loading
        return_none_if_missing: If True, returns None instead of raising 503

    Usage:
        from core.proxy_helpers import RequiresModel
        from fastapi import Depends

        @router.post("/analyze")
        async def analyze_image(
            request: Request,
            vision: Any = Depends(RequiresModel("vision_analyzer"))
        ):
            # vision is guaranteed to be ready here
            return await vision.analyze(image)

        # Multiple models
        @router.post("/transcribe")
        async def transcribe(
            request: Request,
            whisper: Any = Depends(RequiresModel("whisper_model")),
            vad: Any = Depends(RequiresModel("vad_silero")),
        ):
            return await whisper.transcribe(audio, vad=vad)

        # Optional model (won't fail if missing)
        @router.post("/analyze")
        async def analyze(
            request: Request,
            yolo: Optional[Any] = Depends(RequiresModel("yolo_detector", return_none_if_missing=True))
        ):
            if yolo:
                return await yolo.detect(image)
            return {"status": "yolo not available"}
    """

    def __init__(
        self,
        model_name: str,
        timeout: float = 30.0,
        allow_warming: bool = False,
        return_none_if_missing: bool = False,
    ):
        self.model_name = model_name
        self.timeout = timeout
        self.allow_warming = allow_warming
        self.return_none_if_missing = return_none_if_missing

    async def __call__(self, request: "Request") -> Any:
        """
        FastAPI calls this when resolving the dependency.

        Args:
            request: FastAPI Request object (injected automatically)

        Returns:
            The ready model/proxy

        Raises:
            HTTPException 503: If model not ready after timeout
            HTTPException 500: If model not registered (unless return_none_if_missing)
        """
        from fastapi import HTTPException

        try:
            proxy = get_model_proxy(
                request.app.state,
                self.model_name,
                raise_if_missing=not self.return_none_if_missing,
            )
        except ProxyNotFoundError:
            if self.return_none_if_missing:
                return None
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "model_not_registered",
                    "model": self.model_name,
                    "message": f"Model '{self.model_name}' is not registered",
                }
            )

        if proxy is None:
            return None

        # If already ready, return immediately
        if is_proxy_ready(proxy):
            return proxy

        # If warming is allowed and model is loading, return the proxy
        if self.allow_warming and is_proxy_loading(proxy):
            return proxy

        # Wait for the model to become ready
        try:
            ready = await wait_for_proxy(proxy, timeout=self.timeout)
        except asyncio.TimeoutError:
            ready = False

        if not ready:
            state = get_proxy_state(proxy)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "model_warming_up",
                    "model": self.model_name,
                    "state": state.value,
                    "message": f"Model '{self.model_name}' not ready after {self.timeout}s",
                    "retry_after": 5.0,
                },
                headers={"Retry-After": "5"},
            )

        return proxy


def requires_model_dependency(
    model_name: str,
    timeout: float = 30.0,
) -> RequiresModel:
    """
    Factory function for creating model dependencies.

    This is an alternative syntax for RequiresModel that some may prefer.

    Usage:
        vision_dep = requires_model_dependency("vision_analyzer")

        @router.post("/analyze")
        async def analyze(vision: Any = Depends(vision_dep)):
            return await vision.analyze(image)
    """
    return RequiresModel(model_name, timeout=timeout)


# =============================================================================
# Decorator for routes that require a model
# =============================================================================

def requires_model(model_name: str, timeout: float = 30.0):
    """
    Decorator that ensures a model is ready before route execution.

    Note: For new code, prefer using RequiresModel with Depends() instead.
    This decorator is provided for backward compatibility and simpler cases.

    Args:
        model_name: Name of the required model
        timeout: Max seconds to wait for model

    Usage:
        @router.post("/vision/analyze")
        @requires_model("vision_analyzer")
        async def analyze(request: Request):
            vision = request.app.state.vision_analyzer
            return await vision.analyze(...)
    """
    def decorator(func):
        import functools

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Find the request object
            request = None
            for arg in args:
                if hasattr(arg, 'app') and hasattr(arg.app, 'state'):
                    request = arg
                    break

            if request is None:
                for key, val in kwargs.items():
                    if hasattr(val, 'app') and hasattr(val.app, 'state'):
                        request = val
                        break

            if request is None:
                raise RuntimeError("Could not find Request object in route arguments")

            try:
                await require_model_ready(request.app.state, model_name, timeout=timeout)
            except (ProxyNotReadyError, ProxyNotFoundError) as e:
                raise http_exception_for_proxy_error(e)

            return await func(*args, **kwargs)

        return wrapper
    return decorator


# =============================================================================
# Optimization Stats for Health Endpoints
# =============================================================================

def get_optimization_stats(app_state: Any) -> dict:
    """
    Get comprehensive optimization statistics for all registered proxies.

    This is designed for the /api/optimization/stats endpoint.

    Args:
        app_state: FastAPI app.state object

    Returns:
        Dict with detailed stats about AI loader, router, and all proxies
    """
    result = {
        "ai_loader": {"available": False},
        "router": {"available": False},
        "proxies": {},
        "summary": {
            "total": 0,
            "ready": 0,
            "loading": 0,
            "failed": 0,
            "lazy": 0,
        },
    }

    # Get AI Manager stats
    ai_manager = getattr(app_state, 'ai_manager', None)
    if ai_manager:
        try:
            stats = ai_manager.get_stats()
            result["ai_loader"] = {
                "available": True,
                "initialized": True,
                "config": stats.get("config", {}),
                "memory_mb": stats["summary"]["total_memory_mb"],
            }
            result["router"] = stats.get("router", {"available": False})
            result["summary"] = {
                "total": stats["summary"]["total"],
                "ready": stats["summary"]["ready"],
                "loading": stats["summary"]["loading"],
                "failed": stats["summary"]["failed"],
                "lazy": 0,  # Will count below
            }

            # Get detailed proxy info
            for name, model_stats in stats.get("models", {}).items():
                engine = model_stats.get("engine", "unknown")
                result["proxies"][name] = {
                    "status": model_stats["status"],
                    "ready": model_stats["status"] == "READY",
                    "engine": engine,
                    "priority": model_stats.get("priority", "NORMAL"),
                    "quantized": model_stats.get("quantized", False),
                    "load_time_ms": model_stats.get("load_duration_ms", 0),
                    "memory_mb": model_stats.get("memory_mb", 0),
                    "calls": model_stats.get("calls_total", 0),
                    "calls_while_warming": model_stats.get("calls_while_warming", 0),
                }

                # Count lazy proxies
                if model_stats.get("priority") == "LAZY":
                    result["summary"]["lazy"] += 1

        except Exception as e:
            result["ai_loader"]["error"] = str(e)

    # Also include stats from app.state proxies dicts for completeness
    for category, proxies_attr in [
        ("voice", "voice_model_proxies"),
        ("vision", "vision_model_proxies"),
        ("intelligence", "intelligence_proxies"),
    ]:
        proxies = getattr(app_state, proxies_attr, {})
        for name, proxy in proxies.items():
            if name not in result["proxies"] and proxy is not None:
                result["proxies"][name] = {
                    "status": get_proxy_state(proxy).value,
                    "ready": is_proxy_ready(proxy),
                    "category": category,
                    "engine": "unknown",
                }

    return result


def get_engine_breakdown(app_state: Any) -> dict:
    """
    Get breakdown of models by optimization engine.

    Args:
        app_state: FastAPI app.state object

    Returns:
        Dict with engine names as keys and model lists as values
    """
    breakdown = {}

    ai_manager = getattr(app_state, 'ai_manager', None)
    if not ai_manager:
        return {"error": "AI Manager not available"}

    try:
        stats = ai_manager.get_stats()
        for name, model_stats in stats.get("models", {}).items():
            engine = model_stats.get("engine", "unknown")
            if engine not in breakdown:
                breakdown[engine] = {
                    "count": 0,
                    "models": [],
                    "total_memory_mb": 0,
                }
            breakdown[engine]["count"] += 1
            breakdown[engine]["models"].append(name)
            breakdown[engine]["total_memory_mb"] += model_stats.get("memory_mb", 0)

    except Exception as e:
        breakdown["error"] = str(e)

    return breakdown
