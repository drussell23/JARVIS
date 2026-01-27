#!/usr/bin/env python3
"""
JARVIS Loading Server v124.0 - Advanced Startup Progress Server
================================================================

This server provides:
1. Loading page during startup
2. Supervisor API endpoints for health/heartbeat
3. WebSocket support for real-time progress updates
4. Unified health endpoint for frontend polling

API Endpoints:
- GET /api/supervisor/health - Supervisor health status
- GET /api/supervisor/heartbeat - Heartbeat for keep-alive
- GET /api/health/unified - Unified system health
- GET /api/supervisor/status - Full supervisor status
- WS /ws/progress - Real-time progress updates

Author: JARVIS System
Version: 124.0.0
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger("LoadingServer.v124")


# =============================================================================
# v124.0: ASYNC HTTP SERVER WITH API ROUTES
# =============================================================================

class LoadingServerHandler:
    """
    v124.0: Handler for loading server requests.

    Provides API endpoints and static file serving for the loading page.
    """

    def __init__(self, static_dir: Path, supervisor_state_file: Path):
        self.static_dir = static_dir
        self.supervisor_state_file = supervisor_state_file
        self._startup_time = time.time()
        self._progress = 0
        self._phase = "initializing"
        self._components: Dict[str, Dict[str, Any]] = {}

    def get_supervisor_state(self) -> Dict[str, Any]:
        """Read current supervisor state from file."""
        try:
            if self.supervisor_state_file.exists():
                with open(self.supervisor_state_file) as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"Could not read supervisor state: {e}")
        return {}

    def get_health_response(self) -> Dict[str, Any]:
        """Generate health response with supervisor state."""
        state = self.get_supervisor_state()
        uptime = time.time() - self._startup_time

        return {
            "status": "starting" if uptime < 60 else "healthy",
            "uptime": round(uptime, 2),
            "supervisor": {
                "pid": state.get("pid"),
                "started_at": state.get("started_at"),
                "health_level": state.get("health_level", 0),
                "entry_point": state.get("entry_point"),
            },
            "loading_server": {
                "version": "124.0.0",
                "progress": self._progress,
                "phase": self._phase,
            },
            "timestamp": datetime.now().isoformat(),
        }

    def get_unified_health(self) -> Dict[str, Any]:
        """Generate unified health response for frontend."""
        state = self.get_supervisor_state()

        return {
            "status": "starting",
            "components": {
                "supervisor": {
                    "status": "healthy" if state.get("pid") else "starting",
                    "pid": state.get("pid"),
                },
                "backend": {
                    "status": "starting",
                    "port": 8010,
                },
                "frontend": {
                    "status": "starting",
                    "port": 3000,
                },
                "loading_server": {
                    "status": "healthy",
                    "port": int(os.getenv("LOADING_SERVER_PORT", "3001")),
                },
            },
            "progress": self._progress,
            "phase": self._phase,
            "timestamp": datetime.now().isoformat(),
        }

    def get_heartbeat(self) -> Dict[str, Any]:
        """Generate heartbeat response."""
        return {
            "alive": True,
            "timestamp": time.time(),
            "uptime": time.time() - self._startup_time,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get full supervisor status."""
        state = self.get_supervisor_state()
        return {
            "supervisor": state,
            "loading_server": {
                "version": "124.0.0",
                "startup_time": self._startup_time,
                "progress": self._progress,
                "phase": self._phase,
                "components": self._components,
            },
        }

    def update_progress(self, progress: int, phase: str, components: Optional[Dict] = None):
        """Update startup progress."""
        self._progress = progress
        self._phase = phase
        if components:
            self._components.update(components)


async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, handler: LoadingServerHandler):
    """Handle incoming HTTP request."""
    try:
        # Read request line
        request_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not request_line:
            return

        request_str = request_line.decode('utf-8', errors='ignore').strip()
        parts = request_str.split(' ')
        if len(parts) < 2:
            return

        method, path = parts[0], parts[1]

        # Read headers (discard for now)
        while True:
            line = await reader.readline()
            if not line or line == b'\r\n':
                break

        # Route request
        response_body = ""
        content_type = "application/json"
        status = "200 OK"

        if path == "/api/supervisor/health":
            response_body = json.dumps(handler.get_health_response())

        elif path == "/api/supervisor/heartbeat":
            response_body = json.dumps(handler.get_heartbeat())

        elif path == "/api/health/unified":
            response_body = json.dumps(handler.get_unified_health())

        elif path == "/api/supervisor/status":
            response_body = json.dumps(handler.get_status())

        elif path == "/" or path == "/index.html":
            # Serve loading page
            content_type = "text/html"
            response_body = get_loading_page_html()

        elif path.endswith(".js"):
            content_type = "application/javascript"
            static_path = handler.static_dir / path.lstrip("/")
            if static_path.exists():
                response_body = static_path.read_text()
            else:
                status = "404 Not Found"
                response_body = "Not Found"

        elif path.endswith(".css"):
            content_type = "text/css"
            static_path = handler.static_dir / path.lstrip("/")
            if static_path.exists():
                response_body = static_path.read_text()
            else:
                status = "404 Not Found"
                response_body = "Not Found"

        else:
            # Try static file
            static_path = handler.static_dir / path.lstrip("/")
            if static_path.exists() and static_path.is_file():
                response_body = static_path.read_text()
            else:
                status = "404 Not Found"
                content_type = "text/plain"
                response_body = f"Not Found: {path}"

        # Send response
        response = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(response_body.encode())}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{response_body}"
        )

        writer.write(response.encode())
        await writer.drain()

    except asyncio.TimeoutError:
        logger.debug("Request timeout")
    except Exception as e:
        logger.debug(f"Request error: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def get_loading_page_html() -> str:
    """Generate the loading page HTML."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JARVIS - Initializing</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 50%, #0a0a0a 100%);
            color: #00ff88;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }
        .container {
            text-align: center;
            padding: 2rem;
        }
        .logo {
            font-size: 4rem;
            font-weight: bold;
            letter-spacing: 0.5rem;
            margin-bottom: 2rem;
            text-shadow: 0 0 20px rgba(0, 255, 136, 0.5);
        }
        .status {
            font-size: 1.2rem;
            margin-bottom: 2rem;
            opacity: 0.8;
        }
        .progress-container {
            width: 400px;
            height: 10px;
            background: rgba(0, 255, 136, 0.1);
            border-radius: 5px;
            overflow: hidden;
            margin: 0 auto 2rem;
        }
        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, #00ff88, #00ccff);
            border-radius: 5px;
            transition: width 0.5s ease;
            animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        .phase {
            font-size: 0.9rem;
            opacity: 0.6;
            margin-bottom: 1rem;
        }
        .dots {
            display: inline-block;
            animation: dots 1.5s infinite;
        }
        @keyframes dots {
            0%, 20% { content: '.'; }
            40% { content: '..'; }
            60% { content: '...'; }
            80%, 100% { content: ''; }
        }
        .components {
            margin-top: 2rem;
            text-align: left;
            display: inline-block;
            font-size: 0.85rem;
            opacity: 0.7;
        }
        .component {
            margin: 0.5rem 0;
            padding: 0.5rem;
            background: rgba(0, 255, 136, 0.05);
            border-radius: 4px;
        }
        .component.ready { color: #00ff88; }
        .component.pending { color: #ffcc00; }
        .component.error { color: #ff4444; }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">J.A.R.V.I.S.</div>
        <div class="status">Initializing System<span class="dots">...</span></div>
        <div class="progress-container">
            <div class="progress-bar" id="progress" style="width: 0%"></div>
        </div>
        <div class="phase" id="phase">Starting supervisor...</div>
        <div class="components" id="components"></div>
    </div>

    <script>
        let pollInterval;

        async function checkHealth() {
            try {
                const response = await fetch('/api/health/unified');
                const data = await response.json();

                // Update progress
                const progress = data.progress || 0;
                document.getElementById('progress').style.width = progress + '%';

                // Update phase
                document.getElementById('phase').textContent = data.phase || 'Initializing...';

                // Update components
                const componentsDiv = document.getElementById('components');
                if (data.components) {
                    componentsDiv.innerHTML = Object.entries(data.components)
                        .map(([name, info]) => {
                            const status = info.status || 'pending';
                            return `<div class="component ${status}">${name}: ${status}</div>`;
                        })
                        .join('');
                }

                // If system is ready, redirect to main UI
                if (data.status === 'healthy' && progress >= 100) {
                    clearInterval(pollInterval);
                    setTimeout(() => {
                        window.location.href = 'http://localhost:3000';
                    }, 1000);
                }
            } catch (e) {
                console.log('Waiting for supervisor...', e);
            }
        }

        // Start polling
        checkHealth();
        pollInterval = setInterval(checkHealth, 2000);
    </script>
</body>
</html>'''


async def run_server(port: int):
    """Run the loading server."""
    # Setup paths
    jarvis_home = Path.home() / ".jarvis"
    static_dir = Path(__file__).parent.parent / "frontend" / "public"
    supervisor_state_file = jarvis_home / "locks" / "supervisor.state"

    handler = LoadingServerHandler(static_dir, supervisor_state_file)

    async def client_handler(reader, writer):
        await handle_request(reader, writer, handler)

    server = await asyncio.start_server(
        client_handler,
        host='0.0.0.0',
        port=port,
        reuse_address=True,
    )

    addr = server.sockets[0].getsockname()
    logger.info(f"[v124.0] Loading server started on {addr[0]}:{addr[1]}")

    async with server:
        await server.serve_forever()


def main():
    """Main entry point."""
    port = int(os.getenv("LOADING_SERVER_PORT", "3001"))
    logger.info(f"[v124.0] Starting loading server on port {port}")

    try:
        asyncio.run(run_server(port))
    except KeyboardInterrupt:
        logger.info("[v124.0] Loading server stopped")
    except Exception as e:
        logger.error(f"[v124.0] Loading server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
