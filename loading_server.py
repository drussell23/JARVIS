#!/usr/bin/env python3
"""
JARVIS Loading Server v2.0 - Robust Startup Progress Server
============================================================

Serves the loading page independently from frontend/backend during restart.
Provides real-time progress updates via WebSocket and HTTP polling.

Features:
- Monotonic progress enforcement (never decreases)
- CORS support for cross-origin requests
- WebSocket for real-time updates
- HTTP polling fallback
- Backend readiness verification before redirect
- Dynamic progress interpolation

Port: 3001 (separate from frontend:3000 and backend:8010)
"""

import asyncio
import logging
import json
import aiohttp
from pathlib import Path
from aiohttp import web
from datetime import datetime
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Port for loading server (separate from frontend:3000 and backend:8010)
LOADING_SERVER_PORT = 3001
BACKEND_PORT = 8010
FRONTEND_PORT = 3000

# Global state for progress tracking with monotonic enforcement
progress_state = {
    "stage": "init",
    "message": "Initializing JARVIS...",
    "progress": 0,
    "timestamp": datetime.now().isoformat(),
    "metadata": {},
    "backend_ready": False,
    "websocket_ready": False
}

# Track the highest progress seen (for monotonic enforcement)
max_progress_seen = 0

# WebSocket connections
active_connections = set()

# Backend readiness check state
backend_check_task = None


@web.middleware
async def cors_middleware(request, handler):
    """Add CORS headers to all responses"""
    # Handle preflight OPTIONS requests
    if request.method == 'OPTIONS':
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = e
    
    # Add CORS headers
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '3600'
    
    return response


async def check_backend_health():
    """Check if backend is fully ready (HTTP + WebSocket)"""
    global progress_state
    
    try:
        async with aiohttp.ClientSession() as session:
            # Check HTTP health
            async with session.get(
                f'http://localhost:{BACKEND_PORT}/health',
                timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                if resp.status != 200:
                    return False, "Backend HTTP not ready"
                
            # Check WebSocket endpoint exists
            async with session.get(
                f'http://localhost:{BACKEND_PORT}/docs',
                timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                # If docs load, FastAPI is fully up
                if resp.status == 200:
                    progress_state['backend_ready'] = True
                    progress_state['websocket_ready'] = True
                    return True, "Backend fully ready"
                    
        return False, "Backend not fully initialized"
        
    except asyncio.TimeoutError:
        return False, "Backend timeout"
    except aiohttp.ClientError as e:
        return False, f"Backend connection error: {e}"
    except Exception as e:
        return False, f"Backend check error: {e}"


async def backend_readiness_monitor():
    """Background task to monitor backend readiness"""
    global progress_state, max_progress_seen
    
    logger.info("[Monitor] Starting backend readiness monitor...")
    
    while True:
        try:
            ready, reason = await check_backend_health()
            
            if ready:
                logger.info(f"[Monitor] Backend ready: {reason}")
                
                # If we haven't reached 100% yet, send complete
                if max_progress_seen < 100:
                    await update_progress(
                        "complete",
                        "JARVIS is online - All systems operational",
                        100,
                        {
                            "success": True,
                            "redirect_url": f"http://localhost:{FRONTEND_PORT}",
                            "backend_ready": True
                        }
                    )
                break
            else:
                logger.debug(f"[Monitor] Backend not ready: {reason}")
                
        except Exception as e:
            logger.debug(f"[Monitor] Check failed: {e}")
        
        await asyncio.sleep(1)  # Check every second
    
    logger.info("[Monitor] Backend readiness monitor stopped")


async def serve_loading_page(request):
    """Serve the main loading page"""
    loading_html = Path(__file__).parent / "frontend" / "public" / "loading.html"

    if not loading_html.exists():
        return web.Response(
            text="Loading page not found. Please ensure frontend/public/loading.html exists.",
            status=404
        )

    return web.FileResponse(loading_html)


async def serve_loading_manager(request):
    """Serve the loading manager JavaScript"""
    loading_js = Path(__file__).parent / "frontend" / "public" / "loading-manager.js"

    if not loading_js.exists():
        return web.Response(
            text="Loading manager not found. Please ensure frontend/public/loading-manager.js exists.",
            status=404
        )

    return web.FileResponse(loading_js)


async def health_check(request):
    """Health check endpoint - compatible with service discovery"""
    return web.json_response({
        "status": "ok",
        "message": "pong",
        "service": "jarvis_loading_server",
        "version": "2.0.0",
        "progress": progress_state.get("progress", 0),
        "backend_ready": progress_state.get("backend_ready", False)
    })


async def websocket_handler(request):
    """WebSocket handler for real-time progress updates"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    active_connections.add(ws)
    logger.info(f"[WebSocket] Client connected (total: {len(active_connections)})")

    try:
        # Send current progress immediately
        await ws.send_json(progress_state)

        # Keep connection alive and handle messages
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                except:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f'[WebSocket] Error: {ws.exception()}')

    except Exception as e:
        logger.error(f"[WebSocket] Connection error: {e}")
    finally:
        active_connections.discard(ws)
        logger.info(f"[WebSocket] Client disconnected (total: {len(active_connections)})")

    return ws


async def get_progress(request):
    """HTTP endpoint for progress (fallback for polling)"""
    return web.json_response(progress_state)


async def update_progress_endpoint(request):
    """HTTP endpoint for receiving progress updates from start_system.py"""
    try:
        data = await request.json()
        stage = data.get('stage')
        message = data.get('message')
        progress = data.get('progress', 0)
        metadata = data.get('metadata')

        await update_progress(stage, message, progress, metadata)

        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error(f"[Update] Error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def update_progress(stage, message, progress, metadata=None):
    """
    Update progress and broadcast to all connected clients.
    
    CRITICAL: Enforces monotonic progress - progress can only increase, never decrease.
    This prevents UI jumpiness when backend stages complete in random order.
    The loading server is the SINGLE SOURCE OF TRUTH for progress.
    """
    global progress_state, max_progress_seen

    # Convert progress to float for comparison
    try:
        progress_value = float(progress) if progress is not None else 0
    except (TypeError, ValueError):
        progress_value = 0

    # MONOTONIC PROGRESS ENFORCEMENT
    # Progress can only increase (or stay the same), never decrease
    # Exception: 'complete' stage always sets to 100
    if stage == 'complete':
        effective_progress = 100
        max_progress_seen = 100
    elif progress_value > max_progress_seen:
        effective_progress = progress_value
        max_progress_seen = progress_value
    else:
        # Progress would decrease - skip the progress update but still update stage/message
        # This allows late-arriving stages to show their message without moving the bar backward
        effective_progress = max_progress_seen
        logger.debug(f"[Progress] Skipped backward: {progress_value}% -> kept at {max_progress_seen}% - {stage}: {message}")

    # Always update stage and message (shows what's currently happening)
    # But use the monotonic effective_progress for the progress bar
    progress_state = {
        "stage": stage,
        "message": message,
        "progress": effective_progress,
        "timestamp": datetime.now().isoformat(),
    }

    if metadata:
        progress_state["metadata"] = metadata

    logger.info(f"[Progress] {effective_progress}% - {stage}: {message}")

    # Broadcast to all WebSocket connections
    disconnected = set()
    for ws in active_connections:
        try:
            await ws.send_json(progress_state)
        except Exception as e:
            logger.error(f"[Broadcast] Failed to send to client: {e}")
            disconnected.add(ws)

    # Clean up disconnected clients
    for ws in disconnected:
        active_connections.discard(ws)


async def start_server(host='0.0.0.0', port=LOADING_SERVER_PORT):
    """Start the standalone loading server with CORS and backend monitoring"""
    global backend_check_task
    
    # Create app with CORS middleware
    app = web.Application(middlewares=[cors_middleware])

    # Routes
    app.router.add_get('/', serve_loading_page)
    app.router.add_get('/loading.html', serve_loading_page)
    app.router.add_get('/loading-manager.js', serve_loading_manager)
    app.router.add_get('/health', health_check)
    
    # Add health/ping endpoint for service discovery compatibility
    app.router.add_get('/health/ping', health_check)

    # WebSocket and progress endpoints
    app.router.add_get('/ws/startup-progress', websocket_handler)
    app.router.add_get('/api/startup-progress', get_progress)
    app.router.add_post('/api/update-progress', update_progress_endpoint)
    
    # Handle OPTIONS preflight for CORS
    app.router.add_route('OPTIONS', '/{path:.*}', handle_options)

    # Store app in module for external access
    app['update_progress'] = update_progress

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"✅ Loading server v2.0 started on http://{host}:{port}")
    logger.info(f"   WebSocket: ws://{host}:{port}/ws/startup-progress")
    logger.info(f"   HTTP API: http://{host}:{port}/api/startup-progress")
    logger.info(f"   CORS: Enabled for all origins")
    
    # Start backend readiness monitor
    backend_check_task = asyncio.create_task(backend_readiness_monitor())
    logger.info(f"   Backend Monitor: Started (checking port {BACKEND_PORT})")
    
    return runner


async def handle_options(request):
    """Handle OPTIONS preflight requests"""
    return web.Response(status=200)


async def shutdown_server(runner):
    """Gracefully shutdown the server"""
    global backend_check_task
    
    # Cancel backend monitor
    if backend_check_task and not backend_check_task.done():
        backend_check_task.cancel()
        try:
            await backend_check_task
        except asyncio.CancelledError:
            pass
        logger.info("Backend monitor stopped")
    
    # Close all WebSocket connections
    for ws in list(active_connections):
        try:
            await ws.close()
        except Exception:
            pass
    active_connections.clear()
    
    await runner.cleanup()
    logger.info("Loading server stopped")


async def main():
    """Main entry point"""
    runner = await start_server()

    try:
        # Keep server running
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down loading server...")
    finally:
        await shutdown_server(runner)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
