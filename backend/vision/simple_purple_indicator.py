#!/usr/bin/env python3
"""
Simple purple indicator implementation
Directly runs Swift capture for purple indicator
"""

import subprocess
import os
import logging
import asyncio
from pathlib import Path
import threading
import time

logger = logging.getLogger(__name__)

class SimplePurpleIndicator:
    """Simple implementation that just runs Swift for purple indicator"""
    
    def __init__(self):
        # Use infinite capture for true persistence
        self.swift_script = Path(__file__).parent / "infinite_purple_capture.swift"
        self.capture_process = None
        self.is_capturing = False
        
    async def start(self) -> bool:
        """Start capture with purple indicator"""
        if self.is_capturing:
            logger.info("[PURPLE] Already capturing")
            return True
            
        logger.info("[PURPLE] Starting Swift capture for purple indicator...")
        
        try:
            # Check if script exists
            if not self.swift_script.exists():
                logger.error(f"[PURPLE] Swift script not found: {self.swift_script}")
                return False
                
            # Run Swift capture
            self.capture_process = subprocess.Popen(
                ["swift", str(self.swift_script), "--start"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Give it more time to start and stabilize
            await asyncio.sleep(3.0)
            
            # Check if still running
            if self.capture_process.poll() is None:
                self.is_capturing = True
                logger.info("[PURPLE] ✅ Swift capture process is running!")
                logger.info("[PURPLE] 🟣 PURPLE INDICATOR SHOULD BE VISIBLE AND STAY ON!")
                
                # Log initial output
                try:
                    # Read any initial output without blocking
                    import select
                    if select.select([self.capture_process.stdout], [], [], 0)[0]:
                        initial_output = self.capture_process.stdout.readline()
                        if initial_output:
                            logger.info(f"[PURPLE] Swift output: {initial_output.strip()}")
                except Exception:
                    pass

                # Start monitoring thread
                monitor_thread = threading.Thread(target=self._monitor_process, daemon=True)
                monitor_thread.start()
                logger.info("[PURPLE] Monitor thread started")
                
                return True
            else:
                # Process ended
                stdout, stderr = self.capture_process.communicate()
                logger.error(f"[PURPLE] Swift process ended immediately")
                logger.error(f"[PURPLE] STDOUT: {stdout}")
                logger.error(f"[PURPLE] STDERR: {stderr}")
                return False
                
        except Exception as e:
            logger.error(f"[PURPLE] Failed to start: {e}")
            return False
            
    def _monitor_process(self):
        """Monitor Swift process and restart if needed"""
        restart_count = 0
        while self.is_capturing:
            if self.capture_process and self.capture_process.poll() is not None:
                # Process ended unexpectedly
                stdout, stderr = self.capture_process.communicate()
                logger.warning(f"[PURPLE] Swift process ended unexpectedly (restart #{restart_count})")
                logger.warning(f"[PURPLE] Exit code: {self.capture_process.returncode}")
                if stdout:
                    logger.info(f"[PURPLE] Last output: {stdout[-500:]}")  # Last 500 chars
                if stderr:
                    logger.error(f"[PURPLE] Error: {stderr}")
                
                # Try to restart
                if self.is_capturing and restart_count < 3:
                    restart_count += 1
                    logger.info(f"[PURPLE] Attempting restart #{restart_count}...")
                    
                    # Restart the process
                    try:
                        self.capture_process = subprocess.Popen(
                            ["swift", str(self.swift_script), "--start"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True
                        )
                        time.sleep(2)  # Give it time to start
                        
                        if self.capture_process.poll() is None:
                            logger.info("[PURPLE] ✅ Restart successful!")
                            restart_count = 0  # Reset counter on success
                        else:
                            logger.error("[PURPLE] Restart failed")
                            
                    except Exception as e:
                        logger.error(f"[PURPLE] Failed to restart: {e}")
                        self.is_capturing = False
                        break
                else:
                    logger.error("[PURPLE] Too many restarts, giving up")
                    self.is_capturing = False
                    break
                    
            time.sleep(5)  # Check every 5 seconds
            
    def stop(self):
        """Stop capture"""
        if self.capture_process and self.is_capturing:
            logger.info("[PURPLE] Stopping Swift capture...")
            self.capture_process.terminate()
            try:
                self.capture_process.wait(timeout=5)
            except Exception:
                self.capture_process.kill()
            self.capture_process = None
            self.is_capturing = False
            logger.info("[PURPLE] Stopped")

# Global instance
_purple_indicator = None

def get_purple_indicator():
    global _purple_indicator
    if _purple_indicator is None:
        _purple_indicator = SimplePurpleIndicator()
    return _purple_indicator

async def start_purple_indicator() -> bool:
    """Start purple indicator"""
    indicator = get_purple_indicator()
    return await indicator.start()

def stop_purple_indicator():
    """Stop purple indicator"""
    indicator = get_purple_indicator()
    indicator.stop()

def is_purple_active() -> bool:
    """Check if purple indicator is active"""
    indicator = get_purple_indicator()
    return indicator.is_capturing

# Test
if __name__ == "__main__":
    async def test():
        print("Testing purple indicator...")
        success = await start_purple_indicator()
        print(f"Started: {success}")
        if success:
            print("Purple indicator should be visible for 10 seconds...")
            await asyncio.sleep(10)
            stop_purple_indicator()
            print("Done!")
            
    asyncio.run(test())