#!/usr/bin/env python3
"""
JARVIS GCP Inference Stub Server v1.0.0
========================================

Ultra-fast APARS-compatible stub server for Docker-based GCP inference.
Starts in <1 second and reports that ML dependencies are pre-baked.

This stub is automatically replaced when the real jarvis-prime server starts.
Its purpose is to provide immediate health endpoints so APARS doesn't timeout
while the real server is initializing.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                    Stub Server (Port 8000)                          │
    ├─────────────────────────────────────────────────────────────────────┤
    │  - Instant startup (<1s)                                            │
    │  - APARS progress: Phase 3 COMPLETE (deps pre-baked)                │
    │  - Signals supervisor that ml_deps phase is skipped                 │
    │  - Graceful handoff to real server                                  │
    └─────────────────────────────────────────────────────────────────────┘

Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

# Lazy imports for ultra-fast startup
_fastapi_imported = False
_uvicorn_imported = False


def _lazy_import_fastapi():
    """Lazy import FastAPI for faster initial startup."""
    global _fastapi_imported
    if not _fastapi_imported:
        global FastAPI, Response, JSONResponse, Request
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, Response
        _fastapi_imported = True


def _lazy_import_uvicorn():
    """Lazy import uvicorn for faster initial startup."""
    global _uvicorn_imported
    if not _uvicorn_imported:
        global uvicorn
        import uvicorn
        _uvicorn_imported = True


# =============================================================================
# CONFIGURATION
# =============================================================================
@dataclass
class StubConfig:
    """Configuration for the stub server."""
    port: int = field(default_factory=lambda: int(os.getenv("JARVIS_PORT", "8000")))
    host: str = field(default_factory=lambda: os.getenv("JARVIS_HOST", "0.0.0.0"))
    
    # APARS configuration
    deps_prebaked: bool = field(default_factory=lambda: os.getenv("JARVIS_DEPS_PREBAKED", "true").lower() == "true")
    skip_phase_3: bool = field(default_factory=lambda: os.getenv("APARS_SKIP_PHASE_3", "true").lower() == "true")
    
    # Server mode
    is_docker: bool = field(default_factory=lambda: os.getenv("JARVIS_DOCKER", "false").lower() == "true")
    is_gcp_inference: bool = field(default_factory=lambda: os.getenv("JARVIS_GCP_INFERENCE", "false").lower() == "true")
    
    # Paths
    progress_file: Path = field(default_factory=lambda: Path(os.getenv("JARVIS_PROGRESS_FILE", "/tmp/jarvis_progress.json")))
    handoff_signal: Path = field(default_factory=lambda: Path("/tmp/jarvis_stub_handoff"))


class ServerPhase(Enum):
    """Server initialization phases matching APARS."""
    BOOTING = 0
    FASTAPI_STUB = 1
    SYSTEM_DEPS = 2
    ML_DEPS = 3  # SKIPPED when prebaked
    REPO_CLONE = 4
    MODEL_LOAD = 5
    READY = 6


# =============================================================================
# PROGRESS TRACKER (APARS Compatible)
# =============================================================================
class ProgressTracker:
    """
    Tracks startup progress in APARS-compatible format.
    
    Key insight: When deps are pre-baked, we start at Phase 4 (repo_clone)
    instead of Phase 0. This eliminates the 5-8 minute ml_deps phase.
    """
    
    def __init__(self, config: StubConfig):
        self.config = config
        self.start_time = time.time()
        self._phase = ServerPhase.FASTAPI_STUB
        self._phase_progress = 0
        self._checkpoint = "stub_starting"
        self._model_loaded = False
        self._ready_for_inference = False
        self._error: Optional[str] = None
        
        # If deps are prebaked, we skip Phase 0-3 and start at 60% total progress
        if config.deps_prebaked and config.skip_phase_3:
            self._phase = ServerPhase.REPO_CLONE
            self._phase_progress = 0
            self._checkpoint = "deps_prebaked_starting_repo_phase"
            self._base_progress = 60  # Phases 0-3 complete = 60%
        else:
            self._base_progress = 5  # Normal start
    
    @property
    def elapsed_seconds(self) -> int:
        """Elapsed time since startup."""
        return int(time.time() - self.start_time)
    
    @property
    def total_progress(self) -> int:
        """
        Calculate total progress percentage.
        
        Phase weights (matching gcp_vm_startup.sh):
          Phase 0 (booting):     0-5%   (5%)
          Phase 1 (fastapi):     5-15%  (10%)
          Phase 2 (system_deps): 15-30% (15%)
          Phase 3 (ml_deps):     30-60% (30%) <- SKIPPED if prebaked
          Phase 4 (repo_clone):  60-80% (20%)
          Phase 5 (model_load):  80-95% (15%)
          Phase 6 (ready):       95-100% (5%)
        """
        phase_ranges = {
            ServerPhase.BOOTING: (0, 5),
            ServerPhase.FASTAPI_STUB: (5, 15),
            ServerPhase.SYSTEM_DEPS: (15, 30),
            ServerPhase.ML_DEPS: (30, 60),
            ServerPhase.REPO_CLONE: (60, 80),
            ServerPhase.MODEL_LOAD: (80, 95),
            ServerPhase.READY: (95, 100),
        }
        
        start, end = phase_ranges.get(self._phase, (0, 5))
        phase_contribution = (end - start) * (self._phase_progress / 100)
        return int(start + phase_contribution)
    
    @property
    def eta_seconds(self) -> int:
        """Estimated time to completion."""
        if self._phase == ServerPhase.READY:
            return 0
        
        # Estimate based on typical phase durations (with prebaked deps)
        phase_estimates = {
            ServerPhase.BOOTING: 5,
            ServerPhase.FASTAPI_STUB: 3,
            ServerPhase.SYSTEM_DEPS: 0,  # Skipped in Docker
            ServerPhase.ML_DEPS: 0,      # Skipped (prebaked)
            ServerPhase.REPO_CLONE: 30,  # Still need to clone/mount repo
            ServerPhase.MODEL_LOAD: 60,  # Model loading is the main time now
            ServerPhase.READY: 0,
        }
        
        # Sum remaining phases
        remaining = 0
        past_current = False
        for phase in ServerPhase:
            if phase == self._phase:
                # Partial remaining in current phase
                remaining += phase_estimates.get(phase, 10) * (1 - self._phase_progress / 100)
                past_current = True
            elif past_current:
                remaining += phase_estimates.get(phase, 10)
        
        return max(0, int(remaining))
    
    def update(
        self,
        phase: Optional[ServerPhase] = None,
        phase_progress: Optional[int] = None,
        checkpoint: Optional[str] = None,
        model_loaded: Optional[bool] = None,
        ready_for_inference: Optional[bool] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update progress state."""
        if phase is not None:
            self._phase = phase
        if phase_progress is not None:
            self._phase_progress = min(100, max(0, phase_progress))
        if checkpoint is not None:
            self._checkpoint = checkpoint
        if model_loaded is not None:
            self._model_loaded = model_loaded
        if ready_for_inference is not None:
            self._ready_for_inference = ready_for_inference
        if error is not None:
            self._error = error
        
        # Persist to file for external monitoring
        self._write_progress_file()
    
    def _write_progress_file(self) -> None:
        """Write progress to file for external monitoring."""
        try:
            data = self.to_dict()
            self.config.progress_file.parent.mkdir(parents=True, exist_ok=True)
            self.config.progress_file.write_text(json.dumps(data, indent=2))
        except Exception:
            pass  # Don't fail on progress file write errors
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to APARS-compatible dictionary."""
        return {
            # Legacy fields (backward compatibility)
            "status": "healthy" if self._ready_for_inference else "starting",
            "phase": self._phase.name.lower(),
            "mode": "inference" if self._ready_for_inference else "stub",
            "model_loaded": self._model_loaded,
            "ready_for_inference": self._ready_for_inference,
            
            # APARS v197.0 detailed progress
            "apars": {
                "phase_number": self._phase.value,
                "phase_name": self._phase.name.lower(),
                "phase_progress": self._phase_progress,
                "total_progress": self.total_progress,
                "checkpoint": self._checkpoint,
                "eta_seconds": self.eta_seconds,
                "elapsed_seconds": self.elapsed_seconds,
                "error": self._error,
                
                # Pre-baked metadata
                "deps_prebaked": self.config.deps_prebaked,
                "skipped_phases": [0, 1, 2, 3] if self.config.deps_prebaked else [],
            },
            
            # Metadata
            "version": "1.0.0",
            "server_type": "docker_stub",
            "is_docker": self.config.is_docker,
            "is_gcp_inference": self.config.is_gcp_inference,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================
config = StubConfig()
progress = ProgressTracker(config)
shutdown_event = asyncio.Event()


def create_app() -> "FastAPI":
    """Create and configure the FastAPI application."""
    _lazy_import_fastapi()
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan manager."""
        # Startup
        logging.info(f"[Stub] Starting on {config.host}:{config.port}")
        logging.info(f"[Stub] Deps prebaked: {config.deps_prebaked}")
        logging.info(f"[Stub] Skip Phase 3: {config.skip_phase_3}")
        
        # Update progress to show we're running
        progress.update(
            phase=ServerPhase.REPO_CLONE if config.deps_prebaked else ServerPhase.FASTAPI_STUB,
            phase_progress=10,
            checkpoint="stub_server_running",
        )
        
        yield
        
        # Shutdown
        logging.info("[Stub] Shutting down...")
        shutdown_event.set()
    
    app = FastAPI(
        title="JARVIS GCP Inference Stub",
        description="Ultra-fast stub server for APARS progress reporting",
        version="1.0.0",
        lifespan=lifespan,
    )
    
    @app.get("/")
    async def root():
        """Root endpoint."""
        return {
            "service": "jarvis-gcp-inference-stub",
            "status": "running",
            "message": "Stub server active. Real server will take over when ready.",
        }
    
    @app.get("/health")
    async def health():
        """
        APARS-compatible health endpoint.
        
        This is the critical endpoint that the supervisor polls.
        It reports progress showing that ml_deps phase is COMPLETE (skipped).
        """
        return JSONResponse(content=progress.to_dict())
    
    @app.get("/health/ready")
    async def health_ready():
        """Kubernetes-style readiness probe."""
        data = progress.to_dict()
        if data["ready_for_inference"]:
            return JSONResponse(content={"ready": True})
        return JSONResponse(
            content={"ready": False, "phase": data["phase"]},
            status_code=503,
        )
    
    @app.get("/health/live")
    async def health_live():
        """Kubernetes-style liveness probe."""
        return JSONResponse(content={"alive": True})
    
    @app.post("/internal/handoff")
    async def handoff():
        """
        Signal that the real server is taking over.
        
        Called by the real jarvis-prime server when it's ready to accept traffic.
        This stub server will gracefully shutdown.
        """
        logging.info("[Stub] Received handoff signal - shutting down")
        
        # Write handoff signal file
        config.handoff_signal.touch()
        
        # Trigger graceful shutdown
        shutdown_event.set()
        
        return {"status": "handoff_acknowledged", "message": "Stub server shutting down"}
    
    @app.post("/internal/update-progress")
    async def update_progress(request: Request):
        """
        Update progress from external source (e.g., startup script).
        
        This allows the startup script to report progress through the stub
        server's health endpoint.
        """
        try:
            data = await request.json()
            progress.update(
                phase=ServerPhase(data.get("phase_number", progress._phase.value)) if "phase_number" in data else None,
                phase_progress=data.get("phase_progress"),
                checkpoint=data.get("checkpoint"),
                model_loaded=data.get("model_loaded"),
                ready_for_inference=data.get("ready_for_inference"),
                error=data.get("error"),
            )
            return {"status": "updated", "progress": progress.to_dict()}
        except Exception as e:
            return JSONResponse(
                content={"error": str(e)},
                status_code=400,
            )
    
    return app


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def setup_logging():
    """Configure logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def setup_signal_handlers():
    """Setup graceful shutdown handlers."""
    def handle_signal(signum, frame):
        logging.info(f"[Stub] Received signal {signum}, initiating shutdown...")
        shutdown_event.set()
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


async def run_server():
    """Run the stub server."""
    _lazy_import_uvicorn()
    
    app = create_app()
    
    server_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,  # Reduce noise
    )
    server = uvicorn.Server(server_config)
    
    # Run server with shutdown monitoring
    server_task = asyncio.create_task(server.serve())
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    
    done, pending = await asyncio.wait(
        [server_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    
    # Cleanup
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    
    # Force server shutdown if still running
    if not server.started:
        return
    
    server.should_exit = True
    await asyncio.sleep(0.5)  # Give time for graceful shutdown


def main():
    """Main entry point."""
    setup_logging()
    setup_signal_handlers()
    
    logging.info("=" * 60)
    logging.info("JARVIS GCP Inference Stub Server v1.0.0")
    logging.info("=" * 60)
    logging.info(f"  Port:          {config.port}")
    logging.info(f"  Deps Prebaked: {config.deps_prebaked}")
    logging.info(f"  Skip Phase 3:  {config.skip_phase_3}")
    logging.info(f"  Docker Mode:   {config.is_docker}")
    logging.info(f"  GCP Inference: {config.is_gcp_inference}")
    logging.info("=" * 60)
    
    # Initial progress update
    progress.update(
        phase=ServerPhase.REPO_CLONE if config.deps_prebaked else ServerPhase.FASTAPI_STUB,
        phase_progress=5,
        checkpoint="stub_initializing",
    )
    
    # Run server
    asyncio.run(run_server())
    
    logging.info("[Stub] Server stopped")


if __name__ == "__main__":
    main()
