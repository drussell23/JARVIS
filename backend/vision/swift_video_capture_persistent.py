"""
Persistent Swift Video Capture with actual purple indicator
"""

import asyncio
import socket
import subprocess
import os
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

class PersistentSwiftVideoCapture:
    """Manages persistent Swift video capture server for purple indicator"""
    
    def __init__(self):
        self.server_process = None
        self.server_port = 9876
        self.server_started = False
        self.swift_script = Path(__file__).parent / "SwiftVideoCaptureServer.swift"
        
    async def ensure_server_running(self) -> bool:
        """Ensure the Swift capture server is running"""
        # Check if server is already responding
        if await self._ping_server():
            logger.info("Swift capture server already running")
            return True
            
        # Start the server
        logger.info("Starting Swift capture server...")
        try:
            self.server_process = subprocess.Popen(
                ["swift", str(self.swift_script), "server"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait for server to start
            for _ in range(10):  # 5 seconds timeout
                await asyncio.sleep(0.5)
                if await self._ping_server():
                    logger.info("Swift capture server started successfully")
                    self.server_started = True
                    return True
                    
            logger.error("Swift capture server failed to start")
            return False
            
        except Exception as e:
            logger.error(f"Failed to start Swift capture server: {e}")
            return False
    
    async def _ping_server(self) -> bool:
        """Check if server is responding"""
        try:
            response = await self._send_command("PING")
            return response == "OK:PONG"
        except Exception:
            return False

    async def _send_command(self, command: str) -> str:
        """Send command to Swift server"""
        try:
            # Create socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            
            # Connect
            sock.connect(("127.0.0.1", self.server_port))
            
            # Send command
            sock.send(command.encode())
            
            # Get response
            response = sock.recv(1024).decode().strip()
            
            sock.close()
            return response
            
        except Exception as e:
            logger.error(f"Failed to send command {command}: {e}")
            raise
    
    async def start_capture(self) -> bool:
        """Start video capture (shows purple indicator)"""
        logger.info("Starting persistent video capture...")
        
        # Ensure server is running
        if not await self.ensure_server_running():
            return False
            
        # Send start command
        response = await self._send_command("START")
        success = response == "OK:STARTED"
        
        if success:
            logger.info("✅ Video capture started - purple indicator should be visible")
        else:
            logger.error(f"Failed to start capture: {response}")
            
        return success
    
    async def stop_capture(self) -> bool:
        """Stop video capture (hides purple indicator)"""
        logger.info("Stopping video capture...")
        
        try:
            response = await self._send_command("STOP")
            success = response == "OK:STOPPED"
            
            if success:
                logger.info("✅ Video capture stopped - purple indicator should disappear")
                
            return success
        except Exception:
            return False

    async def is_capturing(self) -> bool:
        """Check if currently capturing"""
        try:
            response = await self._send_command("STATUS")
            if response.startswith("OK:CAPTURING="):
                return response.split("=")[1] == "true"
        except Exception:
            pass
        return False
    
    def cleanup(self):
        """Stop server process"""
        if self.server_process:
            self.server_process.terminate()
            self.server_process = None
            self.server_started = False

# Global instance
_persistent_capture = PersistentSwiftVideoCapture()

async def start_persistent_video_capture() -> bool:
    """Start video capture with purple indicator"""
    return await _persistent_capture.start_capture()

async def stop_persistent_video_capture() -> bool:
    """Stop video capture"""
    return await _persistent_capture.stop_capture()

async def is_video_capturing() -> bool:
    """Check if video is capturing"""
    return await _persistent_capture.is_capturing()