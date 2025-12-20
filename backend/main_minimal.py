#!/usr/bin/env python3
"""
Minimal JARVIS backend for testing - bypasses import errors
"""
import os
import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent
sys.path.insert(0, str(backend_path))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Optional
import signal
import time

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global reference to upgrader
_upgrader = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    global _upgrader
    
    logger.info("=" * 60)
    logger.info("🚀 Starting JARVIS Minimal Backend")
    logger.info("=" * 60)
    logger.info("📌 MODE: MINIMAL - Basic functionality only")
    logger.info("⏳ This is temporary while full system initializes")
    logger.info("✅ Available: Basic voice commands, health checks")
    logger.info("⚠️  Unavailable: Wake word, ML audio, vision, advanced features")
    logger.info("=" * 60)
    
    # Create upgrader before yielding
    try:
        from minimal_to_full_upgrader import get_upgrader
        _upgrader = get_upgrader()
        logger.info("✅ Upgrader initialized - will monitor for full mode readiness")
    except Exception as e:
        logger.warning(f"⚠️  Could not create upgrader: {e}")
        _upgrader = None
    
    # Start the upgrader before yielding
    if _upgrader:
        # Start upgrader in background
        async def start_upgrader():
            await asyncio.sleep(2)  # Small delay to ensure API is ready
            logger.info("🔄 Starting upgrade monitor...")
            await _upgrader.start()
            
        asyncio.create_task(start_upgrader())
        logger.info("📊 Upgrade monitor will check system readiness every 5 seconds")
    
    yield
    
    # Shutdown
    logger.info("Shutting down minimal backend...")
    if _upgrader:
        await _upgrader.stop()


# Create FastAPI app
app = FastAPI(
    title="JARVIS Minimal Backend", 
    version="1.0.0",
    lifespan=lifespan
)


# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"status": "JARVIS Minimal Backend Running"}


@app.get("/health/ping")
async def health_ping():
    """Ultra-fast liveness probe - responds immediately."""
    return {"status": "ok", "message": "pong"}


@app.get("/health/startup")
async def health_startup():
    """Startup status for minimal backend."""
    return {
        "phase": "MINIMAL_MODE",
        "progress": 1.0,
        "ready_for_requests": True,
        "full_mode": False,
        "is_complete": False,
        "components": {
            "ready": ["config", "minimal_api"],
            "pending": ["full_backend"],
            "total": 3
        },
        "message": "Running in minimal mode - full features loading in background"
    }


@app.get("/health")
async def health():
    global _upgrader
    
    response = {
        "status": "healthy", 
        "service": "jarvis-minimal",
        "mode": "minimal",
        "message": "Running in minimal mode - full features loading...",
        "components": {
            "vision": False,
            "memory": False,
            "voice": False,
            "tools": False,
            "rust": False
        }
    }
    
    # Add upgrader status if available
    if _upgrader:
        response["upgrader"] = {
            "monitoring": _upgrader._running,
            "attempts": _upgrader._upgrade_attempts,
            "max_attempts": _upgrader._max_attempts,
            "status": "checking_readiness" if _upgrader._running else "stopped"
        }
        
        # Log health check with upgrade status
        if _upgrader._upgrade_attempts > 0:
            logger.debug(f"🔄 Minimal mode health check - Upgrade attempt {_upgrader._upgrade_attempts}/{_upgrader._max_attempts}")
    else:
        logger.debug("⚡ Minimal mode health check - No upgrader available")
        
    return response


@app.get("/voice/jarvis/status")
async def voice_status():
    return {
        "status": "available",
        "mode": "minimal",
        "message": "Voice system in minimal mode",
    }


@app.get("/audio/ml/config")
async def audio_config():
    return {"sample_rate": 16000, "channels": 1, "format": "int16"}


@app.post("/voice/jarvis/activate")
async def activate_jarvis():
    logger.info("✅ JARVIS activated in minimal mode")
    logger.info("  📌 Basic voice commands available")
    logger.info("  ⏳ Advanced features will activate when full mode is ready")
    return {"success": True, "message": "JARVIS activated in minimal mode"}


@app.post("/audio/ml/predict")
async def predict_audio():
    return {"prediction": "normal", "confidence": 0.9}


@app.websocket("/audio/ml/stream")
async def audio_ml_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            # Echo back minimal response
            await websocket.send_json(
                {
                    "type": "response",
                    "status": "minimal_mode",
                    "message": "Audio ML in minimal mode",
                }
            )
    except WebSocketDisconnect:
        pass


@app.get("/upgrader/status")
async def upgrader_status():
    """Get upgrader status"""
    if _upgrader:
        return {
            "running": _upgrader._running,
            "minimal_mode": _upgrader._is_minimal_mode,
            "attempts": _upgrader._upgrade_attempts,
            "max_attempts": _upgrader._max_attempts
        }
    return {"error": "Upgrader not initialized"}


@app.post("/upgrader/check")
async def trigger_upgrade_check():
    """Manually trigger an upgrade check"""
    if _upgrader and _upgrader._running:
        # Force the upgrader to check now
        logger.info("Manually triggering upgrade check...")
        _upgrader._is_minimal_mode = True  # Force it to check
        if not _upgrader._upgrade_task:
            _upgrader._upgrade_task = asyncio.create_task(_upgrader._upgrade_monitor())
        return {"status": "Upgrade check triggered"}
    return {"error": "Upgrader not running"}


@app.post("/shutdown")
async def shutdown(background_tasks: BackgroundTasks):
    """Gracefully shutdown the minimal backend."""
    
    def shutdown_server():
        """Shutdown server after response is sent."""
        time.sleep(1)  # Give time for response
        os.kill(os.getpid(), signal.SIGTERM)
    
    background_tasks.add_task(shutdown_server)
    
    return {
        "success": True,
        "message": "Minimal backend shutting down gracefully"
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8010")))
    args = parser.parse_args()

    logger.info(f"Starting JARVIS Minimal Backend on port {args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port)
