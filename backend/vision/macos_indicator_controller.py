"""
macOS Indicator Controller - Manages the purple screen recording indicator
Part of Screen Monitoring Activation & macOS Purple Indicator System
"""

import asyncio
import subprocess
import logging
import os
from typing import Optional, Dict, Any, Callable
from pathlib import Path
from datetime import datetime
import threading

logger = logging.getLogger(__name__)


class MacOSIndicatorController:
    """
    Controls the macOS purple screen recording indicator
    Ensures it stays visible throughout monitoring sessions
    """
    
    def __init__(self):
        self.indicator_active = False
        self.capture_process: Optional[subprocess.Popen] = None
        self.swift_script = Path(__file__).parent / "infinite_purple_capture.swift"
        self.monitoring_thread: Optional[threading.Thread] = None
        self.should_monitor = False
        self.status_callbacks = []
        self.last_status_change = None
        self.activation_attempts = 0
        self.max_activation_attempts = 3
        
    def add_status_callback(self, callback: Callable[[bool], None]):
        """Add a callback to be notified of indicator status changes"""
        self.status_callbacks.append(callback)
        
    def _notify_status_change(self, active: bool):
        """Notify all callbacks of status change"""
        for callback in self.status_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    asyncio.create_task(callback(active))
                else:
                    callback(active)
            except Exception as e:
                logger.error(f"Error in status callback: {e}")
    
    async def activate_indicator(self) -> Dict[str, Any]:
        """
        Activate the macOS purple indicator
        
        Returns:
            Dictionary with activation status and details
        """
        logger.info("[MACOS] Activating purple indicator...")
        
        # Check if already active
        if self.indicator_active and self._is_process_running():
            logger.info("[MACOS] Indicator already active")
            return {
                'success': True,
                'already_active': True,
                'message': 'Purple indicator is already active'
            }
        
        # Kill any stale processes
        await self._cleanup_stale_processes()
        
        # Attempt activation
        for attempt in range(self.max_activation_attempts):
            self.activation_attempts = attempt + 1
            
            try:
                # Start the Swift capture process
                self.capture_process = subprocess.Popen(
                    ["swift", str(self.swift_script), "--start"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1  # Line buffered
                )
                
                # Wait for confirmation
                await asyncio.sleep(1.0)
                
                if self._is_process_running():
                    self.indicator_active = True
                    self.last_status_change = datetime.now()
                    
                    # Start monitoring thread
                    self._start_monitoring_thread()
                    
                    # Notify callbacks
                    self._notify_status_change(True)
                    
                    logger.info("[MACOS] ✅ Purple indicator activated successfully")
                    return {
                        'success': True,
                        'already_active': False,
                        'message': 'Purple indicator activated',
                        'attempts': self.activation_attempts
                    }
                else:
                    logger.warning(f"[MACOS] Activation attempt {attempt + 1} failed")
                    
            except Exception as e:
                logger.error(f"[MACOS] Activation error on attempt {attempt + 1}: {e}")
        
        # All attempts failed
        return {
            'success': False,
            'already_active': False,
            'message': 'Failed to activate purple indicator',
            'error': 'All activation attempts failed',
            'attempts': self.activation_attempts
        }
    
    async def deactivate_indicator(self) -> Dict[str, Any]:
        """
        Deactivate the macOS purple indicator
        
        Returns:
            Dictionary with deactivation status
        """
        logger.info("[MACOS] Deactivating purple indicator...")
        
        if not self.indicator_active:
            logger.info("[MACOS] Indicator already inactive")
            return {
                'success': True,
                'was_active': False,
                'message': 'Purple indicator was not active'
            }
        
        # Stop monitoring
        self.should_monitor = False
        
        # Terminate the process
        if self.capture_process:
            try:
                self.capture_process.terminate()
                # Wait for clean shutdown
                self.capture_process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                # Force kill if needed
                self.capture_process.kill()
                self.capture_process.wait()
            except Exception as e:
                logger.error(f"[MACOS] Error terminating process: {e}")
        
        self.indicator_active = False
        self.capture_process = None
        self.last_status_change = datetime.now()
        
        # Notify callbacks
        self._notify_status_change(False)
        
        logger.info("[MACOS] ✅ Purple indicator deactivated")
        return {
            'success': True,
            'was_active': True,
            'message': 'Purple indicator deactivated'
        }
    
    def get_indicator_status(self) -> Dict[str, Any]:
        """Get current indicator status"""
        is_running = self._is_process_running()
        
        return {
            'active': self.indicator_active and is_running,
            'process_running': is_running,
            'last_change': self.last_status_change.isoformat() if self.last_status_change else None,
            'monitoring_thread_active': self.monitoring_thread and self.monitoring_thread.is_alive(),
            'activation_attempts': self.activation_attempts
        }
    
    def _is_process_running(self) -> bool:
        """Check if the capture process is still running"""
        if not self.capture_process:
            return False
        
        return self.capture_process.poll() is None
    
    def _start_monitoring_thread(self):
        """Start thread to monitor process output"""
        self.should_monitor = True
        self.monitoring_thread = threading.Thread(
            target=self._monitor_process_output,
            daemon=True
        )
        self.monitoring_thread.start()
    
    def _monitor_process_output(self):
        """Monitor Swift process output for status updates"""
        if not self.capture_process:
            return
        
        try:
            while self.should_monitor and self.capture_process:
                line = self.capture_process.stdout.readline()
                if not line:
                    break
                    
                line = line.strip()
                if line:
                    logger.debug(f"[SWIFT] {line}")
                    
                    # Check for status signals
                    if "[VISION_STATUS] connected" in line:
                        logger.info("[MACOS] Vision status: CONNECTED")
                    elif "[VISION_STATUS] disconnected" in line:
                        logger.info("[MACOS] Vision status: DISCONNECTED")
                    elif "Session stopped" in line or "Failed" in line:
                        logger.warning(f"[MACOS] Issue detected: {line}")
                        # Auto-restart logic could go here
                        
        except Exception as e:
            logger.error(f"[MACOS] Error monitoring output: {e}")
        finally:
            logger.info("[MACOS] Monitoring thread ended")
    
    async def _cleanup_stale_processes(self):
        """Kill any stale Swift capture processes"""
        try:
            # Find existing processes
            result = subprocess.run(
                ["pgrep", "-f", "infinite_purple_capture.swift"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout:
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if pid:
                        logger.info(f"[MACOS] Killing stale process: {pid}")
                        try:
                            os.kill(int(pid), 15)  # SIGTERM
                        except Exception:
                            pass
                
                # Wait for cleanup
                await asyncio.sleep(0.5)
                
        except Exception as e:
            logger.debug(f"[MACOS] Error during cleanup: {e}")
    
    async def ensure_permissions(self) -> Dict[str, Any]:
        """Check and guide user through screen recording permissions"""
        try:
            # Run permission check
            result = subprocess.run(
                ["swift", str(self.swift_script), "--test"],
                capture_output=True,
                text=True,
                timeout=5.0
            )
            
            if "Screen recording permission granted" in result.stdout:
                return {
                    'granted': True,
                    'message': 'Screen recording permission is granted'
                }
            else:
                return {
                    'granted': False,
                    'message': 'Screen recording permission required',
                    'instructions': [
                        'Open System Preferences > Security & Privacy > Screen Recording',
                        'Enable permission for Terminal (or your terminal app)',
                        'Restart the terminal application'
                    ]
                }
                
        except Exception as e:
            logger.error(f"[MACOS] Permission check error: {e}")
            return {
                'granted': False,
                'message': 'Could not check permissions',
                'error': str(e)
            }


# Global instance
_indicator_controller = None


def get_indicator_controller():
    """Get or create the global indicator controller"""
    global _indicator_controller
    if _indicator_controller is None:
        _indicator_controller = MacOSIndicatorController()
    return _indicator_controller