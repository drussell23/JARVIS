#!/usr/bin/env python3
"""
Direct Swift capture execution for purple indicator
This bypasses all the complex bridging and runs Swift directly
"""

import subprocess
import os
import signal
import logging
import asyncio
from pathlib import Path

logger = logging.getLogger(__name__)

class DirectSwiftCapture:
    """Execute Swift capture directly for purple indicator"""
    
    def __init__(self):
        # Use infinite capture script for permanent purple indicator
        self.swift_script = Path(__file__).parent / "infinite_purple_capture.swift"
        self.capture_process = None
        self.is_capturing = False
        self.vision_status_callback = None
        self.callback_loop = None
        
    async def start_capture(self) -> bool:
        """Start Swift capture - shows purple indicator"""
        if self.is_capturing and self.capture_process and self.capture_process.poll() is None:
            logger.info("[DIRECT] Capture already running with active process")
            return True
            
        logger.info("[DIRECT] Starting Swift capture for purple indicator...")
        
        # Kill any existing Swift capture processes first
        try:
            # Check for any Swift capture processes
            existing_pids = subprocess.check_output(
                ["pgrep", "-f", "(persistent_capture|infinite_purple_capture).swift"],
                text=True
            ).strip().split('\n')
            
            for pid in existing_pids:
                if pid:
                    logger.info(f"[DIRECT] Found existing Swift capture process: {pid}, terminating...")
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception:
                        pass
            
            # Give processes time to terminate
            await asyncio.sleep(0.5)
        except subprocess.CalledProcessError:
            # No existing processes
            pass
        except Exception as e:
            logger.warning(f"[DIRECT] Error checking for existing processes: {e}")
        
        try:
            # Run the Swift script with --start flag
            self.capture_process = subprocess.Popen(
                ["swift", str(self.swift_script), "--start"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            self.is_capturing = True
            
            # Give Swift a moment to start
            import time
            time.sleep(1.0)
            
            # Check if process started successfully
            if self.capture_process.poll() is None:
                logger.info("[DIRECT] ✅ Swift capture process running - purple indicator should be visible!")
                
                # Start a thread to monitor output
                import threading
                monitor_thread = threading.Thread(target=self._monitor_swift_output, daemon=True)
                monitor_thread.start()
                
                return True
            else:
                # Process ended already - check error
                stderr = self.capture_process.stderr.read()
                logger.error(f"[DIRECT] Swift process ended immediately: {stderr}")
                self.is_capturing = False
                return False
            
        except Exception as e:
            logger.error(f"[DIRECT] Failed to start Swift capture: {e}")
            return False
    
    
    def _monitor_swift_output(self):
        """Monitor Swift output in background"""
        if not self.capture_process:
            return
        
        # Store the main event loop reference
        loop = None
        try:
            # Try to get the running loop from the main thread
            import threading
            main_thread = None
            for thread in threading.enumerate():
                if thread.name == 'MainThread':
                    main_thread = thread
                    break
                    
            if main_thread and hasattr(main_thread, '_target') and hasattr(main_thread._target, '__self__'):
                # Try to find loop in various places
                if hasattr(asyncio, '_running_loop'):
                    loop = asyncio._running_loop
            
            # Fallback: create new event loop for callbacks
            if not loop:
                loop = asyncio.new_event_loop()
                
        except Exception as e:
            logger.debug(f"[DIRECT] Could not get event loop: {e}")
            
        try:
            for line in iter(self.capture_process.stdout.readline, ''):
                if line:
                    logger.info(f"[SWIFT] {line.strip()}")
                    
                    # Check for vision status signals
                    if "[VISION_STATUS] connected" in line:
                        logger.info("🟢 Vision status: CONNECTED")
                        if self.vision_status_callback and (self.callback_loop or loop):
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self.vision_status_callback(True),
                                    self.callback_loop or loop
                                )
                            except Exception as e:
                                logger.debug(f"Could not run callback: {e}")
                    elif "[VISION_STATUS] disconnected" in line:
                        logger.info("🔴 Vision status: DISCONNECTED")
                        if self.vision_status_callback and (self.callback_loop or loop):
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self.vision_status_callback(False),
                                    self.callback_loop or loop
                                )
                            except Exception as e:
                                logger.debug(f"Could not run callback: {e}")
                    
                    # Check if capture stopped unexpectedly
                    if "Session stopped" in line or "Failed" in line:
                        logger.warning(f"[DIRECT] Swift capture issue detected: {line.strip()}")
                        
            # Process ended
            logger.info("[DIRECT] Swift process ended")
            return_code = self.capture_process.wait()
            
            if return_code != 0 and self.is_capturing:
                stderr = self.capture_process.stderr.read()
                logger.error(f"[DIRECT] Swift capture ended with error {return_code}: {stderr}")
                
            self.is_capturing = False
                    
        except Exception as e:
            logger.error(f"[DIRECT] Error monitoring Swift output: {e}")
            self.is_capturing = False
    
    def stop_capture(self):
        """Stop capture if running"""
        if self.capture_process and self.is_capturing:
            logger.info("[DIRECT] Stopping Swift capture...")
            self.capture_process.terminate()
            self.capture_process.wait(timeout=5.0)  # Wait for clean shutdown
            self.capture_process = None
            self.is_capturing = False
            logger.info("[DIRECT] Swift capture stopped")
            
            # Notify vision status change
            if self.vision_status_callback:
                asyncio.run_coroutine_threadsafe(
                    self.vision_status_callback(False),
                    asyncio.get_event_loop()
                )
    
    def set_vision_status_callback(self, callback):
        """Set callback for vision status changes"""
        self.vision_status_callback = callback
        # Store the current event loop for the callback
        try:
            self.callback_loop = asyncio.get_running_loop()
        except RuntimeError:
            self.callback_loop = None
            logger.warning("[DIRECT] No running event loop when setting callback")

# Global instance
_direct_capture = None

def get_direct_capture():
    """Get or create direct capture instance"""
    global _direct_capture
    if _direct_capture is None:
        _direct_capture = DirectSwiftCapture()
    return _direct_capture

async def start_direct_swift_capture() -> bool:
    """Start direct Swift capture with purple indicator"""
    capture = get_direct_capture()
    return await capture.start_capture()

def stop_direct_swift_capture():
    """Stop direct Swift capture"""
    capture = get_direct_capture()
    capture.stop_capture()

def is_direct_capturing() -> bool:
    """Check if capturing"""
    capture = get_direct_capture()
    return capture.is_capturing

# Test function
async def test_direct_capture():
    """Test the direct capture"""
    print("\n🟣 Testing Direct Swift Capture")
    print("=" * 60)
    
    success = await start_direct_swift_capture()
    
    if success:
        print("✅ Capture started - check for purple indicator!")
        print("⏳ Waiting 10 seconds...")
        await asyncio.sleep(10)
        
        print("Stopping capture...")
        stop_direct_swift_capture()
        print("✅ Test complete!")
    else:
        print("❌ Failed to start capture")

if __name__ == "__main__":
    asyncio.run(test_direct_capture())