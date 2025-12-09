#!/usr/bin/env python3
"""
Standalone Loading Page Server
Serves the loading page independently from frontend/backend during restart.
Starts on a dedicated port (3001) to avoid conflicts.
Includes WebSocket support for real-time progress updates.
"""

import asyncio
import logging
import json
from pathlib import Path
from aiohttp import web
from datetime import datetime
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Port for loading server (separate from frontend:3000 and backend:8010)
LOADING_SERVER_PORT = 3001

# Global state for progress tracking with monotonic enforcement
progress_state = {
    "stage": "init",
    "message": "Initializing...",
    "progress": 0,
    "timestamp": datetime.now().isoformat(),
    "metadata": {}
}

# Track the highest progress seen (for monotonic enforcement)
max_progress_seen = 0

# WebSocket connections
active_connections = set()


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
    """Health check endpoint"""
    return web.json_response({"status": "ok", "service": "loading_server"})


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
    """Start the standalone loading server"""
    app = web.Application()

    # Routes
    app.router.add_get('/', serve_loading_page)
    app.router.add_get('/loading.html', serve_loading_page)
    app.router.add_get('/loading-manager.js', serve_loading_manager)
    app.router.add_get('/health', health_check)

    # WebSocket and progress endpoints
    app.router.add_get('/ws/startup-progress', websocket_handler)
    app.router.add_get('/api/startup-progress', get_progress)
    app.router.add_post('/api/update-progress', update_progress_endpoint)

    # Store app in module for external access
    app['update_progress'] = update_progress

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"✅ Loading server started on http://{host}:{port}")
    logger.info(f"   WebSocket: ws://{host}:{port}/ws/startup-progress")
    logger.info(f"   HTTP API: http://{host}:{port}/api/startup-progress")
    return runner


async def shutdown_server(runner):
    """Gracefully shutdown the server"""
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
