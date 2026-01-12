#!/usr/bin/env python3
"""
Robust Backend Starter
Starts both TypeScript WebSocket Router and Python Backend with proper error handling
"""

import os
import sys
import subprocess
import time
import signal
import asyncio
import json
from pathlib import Path


class RobustBackendStarter:
    def __init__(self):
        self.backend_dir = Path(__file__).parent
        self.websocket_dir = self.backend_dir / "websocket"
        self.processes = []

    def check_port(self, port):
        """Check if a port is in use"""
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t"], capture_output=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def kill_port(self, port):
        """Kill process on a specific port"""
        try:
            subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(
                ["lsof", "-ti", f":{port}", "|", "xargs", "kill", "-9"], shell=True
            )
            time.sleep(1)
        except Exception:
            pass

    def ensure_dependencies(self):
        """Ensure Node.js dependencies are installed"""
        node_modules = self.websocket_dir / "node_modules"
        if not node_modules.exists():
            print("📦 Installing TypeScript dependencies...")
            result = subprocess.run(
                ["npm", "install"],
                cwd=self.websocket_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"❌ Failed to install dependencies: {result.stderr}")
                return False
            print("✅ Dependencies installed")
        return True

    def build_typescript(self):
        """Build TypeScript code"""
        print("🔨 Building TypeScript WebSocket Router...")
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=self.websocket_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"❌ Build failed: {result.stderr}")
            return False
        print("✅ TypeScript built successfully")
        return True

    def start_websocket_router(self):
        """Start TypeScript WebSocket Router"""
        print("🚀 Starting TypeScript WebSocket Router on port 8001...")

        # Start the TypeScript server
        process = subprocess.Popen(
            ["npm", "start"],
            cwd=self.websocket_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )

        self.processes.append(process)

        # Wait for it to start
        time.sleep(3)

        if process.poll() is not None:
            print("❌ TypeScript router failed to start")
            return None

        print("✅ TypeScript router started (PID: {})".format(process.pid))
        return process

    def start_python_backend(self):
        """Start Python FastAPI backend with fallback to minimal"""
        print("🚀 Starting Python Backend on port 8000...")

        # Set environment
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        # Try to start main.py first
        process = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=self.backend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
            env=env,
        )

        self.processes.append(process)

        # Wait for it to start
        time.sleep(5)

        if process.poll() is not None:
            print("❌ Python backend (main.py) failed to start")
            print("🔄 Attempting fallback to minimal backend...")

            # Remove failed process from list
            self.processes.remove(process)

            # Try minimal backend as fallback
            return self.start_minimal_backend()

        print("✅ Python backend started (PID: {})".format(process.pid))
        return process

    def start_minimal_backend(self):
        """Start minimal backend as fallback"""
        print("🚀 Starting Minimal Backend on port 8000...")

        # Check if main_minimal.py exists
        minimal_path = self.backend_dir / "main_minimal.py"
        if not minimal_path.exists():
            print("❌ main_minimal.py not found")
            return None

        # Set environment
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        # Start minimal backend
        process = subprocess.Popen(
            [sys.executable, "main_minimal.py"],
            cwd=self.backend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
            env=env,
        )

        self.processes.append(process)

        # Wait for it to start
        time.sleep(3)

        if process.poll() is not None:
            print("❌ Minimal backend also failed to start")
            # Print last few lines of output for debugging
            output, _ = process.communicate()
            print(f"Error output: {output[-500:]}")  # Last 500 chars
            return None

        print("✅ Minimal backend started (PID: {})".format(process.pid))
        print("⚠️  Running in minimal mode - some features may be limited")
        return process

    def cleanup(self, signum=None, frame=None):
        """Clean up all processes"""
        print("\n🛑 Shutting down...")
        for process in self.processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        print("✅ All processes stopped")
        sys.exit(0)

    def monitor_processes(self):
        """Monitor and restart processes if they crash"""
        while True:
            time.sleep(5)

            # Check TypeScript router
            if self.processes[0].poll() is not None:
                print("⚠️ TypeScript router crashed, restarting...")
                self.processes[0] = self.start_websocket_router()
                if not self.processes[0]:
                    print("❌ Failed to restart TypeScript router")
                    self.cleanup()

            # Check Python backend
            if len(self.processes) > 1 and self.processes[1].poll() is not None:
                print("⚠️ Python backend crashed, restarting...")
                self.processes[1] = self.start_python_backend()
                if not self.processes[1]:
                    print("❌ Failed to restart Python backend")
                    self.cleanup()

    def run(self):
        """Main execution"""
        print("🤖 JARVIS Unified Backend Starter")
        print("=" * 50)

        # Set up signal handlers
        signal.signal(signal.SIGINT, self.cleanup)
        signal.signal(signal.SIGTERM, self.cleanup)

        # Clean up existing processes
        print("🧹 Cleaning up existing processes...")
        self.kill_port(8000)
        self.kill_port(8001)

        # Ensure dependencies
        if not self.ensure_dependencies():
            return

        # Build TypeScript
        if not self.build_typescript():
            return

        # Start TypeScript router
        ts_process = self.start_websocket_router()
        if not ts_process:
            return

        # Start Python backend
        py_process = self.start_python_backend()
        if not py_process:
            self.cleanup()
            return

        print("\n✅ Unified Backend System Running!")
        print("=" * 50)
        print("📍 Endpoints:")
        print("  • Python API: http://localhost:8000")
        print("  • API Docs: http://localhost:8000/docs")
        print("  • TypeScript Router: ws://localhost:8001")
        print("  • Vision WebSocket: ws://localhost:8001/ws/vision")
        print("\nPress Ctrl+C to stop all services")

        try:
            # Monitor processes
            self.monitor_processes()
        except KeyboardInterrupt:
            self.cleanup()


if __name__ == "__main__":
    starter = RobustBackendStarter()
    starter.run()
