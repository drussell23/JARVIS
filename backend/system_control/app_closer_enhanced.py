"""
Enhanced App Closer
===================
Handles stubborn apps that don't respond to normal quit commands
"""

import subprocess
import logging
import time
import os
import signal
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

class EnhancedAppCloser:
    """
    Robust app closing with multiple strategies
    """
    
    def __init__(self):
        self.strategies = [
            self._try_graceful_quit,
            self._try_force_quit,
            self._try_kill_process,
            self._try_killall
        ]
    
    def close_app(self, app_name: str, timeout: int = 10) -> Tuple[bool, str]:
        """
        Close an app using multiple strategies
        
        Args:
            app_name: Name of the application
            timeout: Timeout for each strategy
            
        Returns:
            (success, message)
        """
        logger.info(f"Attempting to close {app_name}")
        
        # Clean app name (remove .app if present)
        clean_name = app_name.replace('.app', '').strip()
        
        # Try each strategy
        for strategy in self.strategies:
            try:
                success, message = strategy(clean_name, timeout)
                if success:
                    logger.info(f"Successfully closed {app_name} using {strategy.__name__}")
                    return True, message
                else:
                    logger.warning(f"Strategy {strategy.__name__} failed: {message}")
            except Exception as e:
                logger.error(f"Error in {strategy.__name__}: {str(e)}")
        
        return False, f"All strategies failed to close {app_name}"
    
    def _try_graceful_quit(self, app_name: str, timeout: int) -> Tuple[bool, str]:
        """Try graceful quit with AppleScript"""
        script = f'''
        tell application "{app_name}"
            quit
        end tell
        '''
        
        try:
            # Use shorter timeout for graceful quit
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=min(timeout, 5)  # Max 5 seconds for graceful
            )
            
            if result.returncode == 0:
                # Wait a moment to ensure it's closed
                time.sleep(0.5)
                if not self._is_app_running(app_name):
                    return True, f"Gracefully closed {app_name}"
                
            return False, f"Graceful quit failed: {result.stderr}"
            
        except subprocess.TimeoutExpired:
            return False, "Graceful quit timed out"
        except Exception as e:
            return False, f"Graceful quit error: {str(e)}"
    
    def _try_force_quit(self, app_name: str, timeout: int) -> Tuple[bool, str]:
        """Try force quit with System Events"""
        script = f'''
        tell application "System Events"
            set appList to name of every application process
            if "{app_name}" is in appList then
                tell application process "{app_name}"
                    set frontmost to true
                    keystroke "q" using {{command down, option down}}
                end tell
            else
                error "Application not running"
            end if
        end tell
        '''
        
        try:
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=min(timeout, 5)
            )
            
            time.sleep(1)  # Give it time to close
            
            if not self._is_app_running(app_name):
                return True, f"Force quit {app_name}"
                
            return False, "Force quit didn't close the app"
            
        except subprocess.TimeoutExpired:
            return False, "Force quit timed out"
        except Exception as e:
            return False, f"Force quit error: {str(e)}"
    
    def _try_kill_process(self, app_name: str, timeout: int) -> Tuple[bool, str]:
        """Try to kill process by PID"""
        try:
            # Find PIDs for the app
            pids = self._find_pids(app_name)
            
            if not pids:
                # App might already be closed
                if not self._is_app_running(app_name):
                    return True, "App already closed"
                return False, "Could not find process ID"
            
            # Kill each PID
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"Sent SIGTERM to PID {pid}")
                except ProcessLookupError:
                    continue
                except PermissionError:
                    logger.warning(f"Permission denied to kill PID {pid}")
            
            # Wait and check
            time.sleep(1)
            
            # If still running, try SIGKILL
            if self._is_app_running(app_name):
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        logger.info(f"Sent SIGKILL to PID {pid}")
                    except Exception:
                        pass
                
                time.sleep(0.5)
            
            if not self._is_app_running(app_name):
                return True, f"Killed {app_name} process"
                
            return False, "Process kill failed"
            
        except Exception as e:
            return False, f"Process kill error: {str(e)}"
    
    def _try_killall(self, app_name: str, timeout: int) -> Tuple[bool, str]:
        """Try killall command as last resort"""
        try:
            # First try exact name
            subprocess.run(
                ['killall', app_name],
                capture_output=True,
                timeout=3
            )
            
            time.sleep(0.5)
            
            if not self._is_app_running(app_name):
                return True, f"Killed {app_name} with killall"
            
            # Try with force
            subprocess.run(
                ['killall', '-9', app_name],
                capture_output=True,
                timeout=3
            )
            
            time.sleep(0.5)
            
            if not self._is_app_running(app_name):
                return True, f"Force killed {app_name} with killall"
                
            return False, "killall failed"
            
        except subprocess.TimeoutExpired:
            return False, "killall timed out"
        except Exception as e:
            return False, f"killall error: {str(e)}"
    
    def _is_app_running(self, app_name: str) -> bool:
        """Check if app is running"""
        try:
            script = f'''
            tell application "System Events"
                set appList to name of every application process
                return "{app_name}" is in appList
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            return result.stdout.strip() == "true"
            
        except Exception:
            # Fallback to ps
            try:
                result = subprocess.run(
                    ['pgrep', '-i', app_name],
                    capture_output=True,
                    timeout=1
                )
                return result.returncode == 0
            except Exception:
                return False
    
    def _find_pids(self, app_name: str) -> List[int]:
        """Find process IDs for app"""
        pids = []
        
        try:
            # Try pgrep first
            result = subprocess.run(
                ['pgrep', '-i', app_name],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if result.returncode == 0:
                pids.extend([int(pid) for pid in result.stdout.strip().split('\n') if pid])
            
            # Also try ps aux
            result = subprocess.run(
                ['ps', 'aux'],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            for line in result.stdout.split('\n'):
                if app_name.lower() in line.lower() and 'grep' not in line:
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            pids.append(int(parts[1]))
                        except Exception:
                            pass
            
            # Remove duplicates
            return list(set(pids))
            
        except Exception:
            return []

# Global instance
app_closer = EnhancedAppCloser()

def close_app_enhanced(app_name: str) -> Tuple[bool, str]:
    """
    Enhanced app closing with multiple strategies
    
    Args:
        app_name: Name of the application to close
        
    Returns:
        (success, message)
    """
    return app_closer.close_app(app_name)