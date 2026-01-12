#!/usr/bin/env python3
"""
Model Loading Status API
Provides real-time status updates for ML model loading progress
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from typing import Dict, List, Any
import asyncio
import logging
import time
from datetime import datetime
import json

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_model_loader import get_loader_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["Model Management"])

# WebSocket connections for real-time updates
class ModelStatusManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        
        # Send initial status
        status = get_loader_status()
        await websocket.send_json({
            "type": "status_update",
            "timestamp": datetime.now().isoformat(),
            "data": status
        })
        
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        
    async def broadcast_status(self, status: Dict[str, Any]):
        """Broadcast status update to all connected clients"""
        message = {
            "type": "status_update",
            "timestamp": datetime.now().isoformat(),
            "data": status
        }
        
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting: {e}")

# Global manager instance
status_manager = ModelStatusManager()

@router.get("/status")
async def get_model_status() -> JSONResponse:
    """Get current model loading status"""
    status = get_loader_status()
    
    # Add summary information
    if 'total' in status:
        status['summary'] = {
            'percentage': (status['loaded'] / status['total'] * 100) if status['total'] > 0 else 0,
            'is_complete': status['loaded'] == status['total'],
            'has_failures': status['failed'] > 0
        }
    
    return JSONResponse(content={
        "status": "success",
        "data": status,
        "timestamp": datetime.now().isoformat()
    })

@router.websocket("/ws")
async def model_status_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time model loading updates"""
    await status_manager.connect(websocket)
    
    # Connection timeout protection (SSE-style long connection)
    connection_start = time.monotonic()
    max_connection_time = float(os.getenv("TIMEOUT_SSE_CONNECTION", "3600.0"))  # 1 hour default

    try:
        # Keep connection alive and send periodic updates
        while True:
            # Check connection timeout
            if time.monotonic() - connection_start > max_connection_time:
                logger.info("Model status WebSocket connection timeout, closing")
                break

            # Wait for client messages or send updates
            try:
                # Check for client messages (with timeout)
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=1.0
                )

                # Handle client commands
                if message == "get_status":
                    status = get_loader_status()
                    await websocket.send_json({
                        "type": "status_response",
                        "data": status
                    })

            except asyncio.TimeoutError:
                # Send periodic status updates
                status = get_loader_status()
                await status_manager.broadcast_status(status)

    except WebSocketDisconnect:
        status_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        status_manager.disconnect(websocket)

@router.get("/progress")
async def get_loading_progress() -> JSONResponse:
    """Get simplified loading progress for UI display"""
    status = get_loader_status()
    
    # Calculate progress
    total = status.get('total', 0)
    loaded = status.get('loaded', 0)
    failed = status.get('failed', 0)
    loading = status.get('loading', 0)
    
    progress = {
        'percentage': (loaded / total * 100) if total > 0 else 0,
        'loaded': loaded,
        'total': total,
        'failed': failed,
        'currently_loading': loading,
        'status': 'complete' if loaded == total else 'loading' if loading > 0 else 'waiting'
    }
    
    # Add top slow models
    if 'models' in status:
        slow_models = sorted(
            [(name, info['load_time']) for name, info in status['models'].items() 
             if info['status'] == 'loaded' and info['load_time'] > 0],
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        progress['slowest_models'] = [
            {'name': name, 'time': time} for name, time in slow_models
        ]
    
    return JSONResponse(content=progress)

# Global status update callback
async def broadcast_model_status():
    """Broadcast model status to all connected clients"""
    status = get_loader_status()
    await status_manager.broadcast_status(status)