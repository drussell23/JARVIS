#!/usr/bin/env python3
"""
Minimal Disruption Space Switching for JARVIS
Provides controlled, user-permitted space switching with minimal visual disruption
"""

import asyncio
import subprocess
import time
import logging
from typing import Optional, Dict, Any, Tuple, Callable, List
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class SwitchMethod(Enum):
    """Available space switching methods"""
    CONTROL_ARROW = "control_arrow"      # Control+Arrow keys
    CONTROL_NUMBER = "control_number"    # Control+Number
    MISSION_CONTROL = "mission_control"  # Via Mission Control
    SWIPE_GESTURE = "swipe_gesture"      # Trackpad gesture (if available)

@dataclass
class SwitchRequest:
    """Request to switch spaces"""
    target_space: int
    reason: str
    requester: str
    priority: int = 5  # 1-10, higher is more important
    max_wait_time: float = 5.0
    require_permission: bool = True
    callback: Optional[Callable] = None

@dataclass 
class SwitchResult:
    """Result of space switch attempt"""
    success: bool
    method_used: Optional[SwitchMethod]
    duration: float
    permission_granted: bool
    error: Optional[str] = None
    screenshot_captured: bool = False

class SpaceSwitchPermission:
    """Manages user permissions for space switching"""
    
    def __init__(self):
        self.permission_cache = {}  # reason -> (granted, timestamp)
        self.cache_duration = timedelta(minutes=5)
        self.always_allow_reasons = {
            'user_explicit',
            'emergency',
            'scheduled_task'
        }
        self.always_deny_reasons = {
            'background_scan',
            'low_priority'
        }
        
    async def request_permission(self, request: SwitchRequest) -> bool:
        """Request permission to switch spaces"""
        
        # Check cache first
        if request.reason in self.permission_cache:
            granted, timestamp = self.permission_cache[request.reason]
            if datetime.now() - timestamp < self.cache_duration:
                return granted
                
        # Check always allow/deny lists
        if request.reason in self.always_allow_reasons:
            return True
        if request.reason in self.always_deny_reasons:
            return False
            
        # High priority requests get different handling
        if request.priority >= 8:
            return await self._request_urgent_permission(request)
        else:
            return await self._request_normal_permission(request)
            
    async def _request_urgent_permission(self, request: SwitchRequest) -> bool:
        """Handle urgent permission requests"""
        # For urgent requests, use system notification
        script = f'''
        display dialog "JARVIS needs to briefly switch to Desktop {request.target_space} to {request.reason}. This is marked as urgent." ¬
            buttons {{"Deny", "Allow"}} default button "Allow" ¬
            with title "JARVIS Space Switch Request" ¬
            giving up after {int(request.max_wait_time)}
        '''
        
        try:
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=request.max_wait_time + 1
            )
            
            granted = 'Allow' in result.stdout or 'gave up' in result.stdout
            self.permission_cache[request.reason] = (granted, datetime.now())
            return granted
            
        except Exception as e:
            logger.error(f"Failed to request permission: {e}")
            return False
            
    async def _request_normal_permission(self, request: SwitchRequest) -> bool:
        """Handle normal permission requests"""
        # For normal requests, could use less intrusive notification
        # For now, simplified version
        logger.info(f"Permission request for space switch: {request.reason}")
        
        # In production, this would integrate with your UI
        # For now, check environment variable for testing
        if os.getenv('JARVIS_AUTO_APPROVE_SPACE_SWITCH', 'false').lower() == 'true':
            return True
            
        return False

class MinimalSpaceSwitcher:
    """Handles space switching with minimal disruption"""
    
    def __init__(self):
        self.permission_manager = SpaceSwitchPermission()
        self.current_space = 1
        self.switching = False
        self.switch_history = []
        
        # Optimization settings
        self.disable_animations = True
        self.use_fastest_method = True
        self.pre_switch_delay = 0.1  # Small delay to prepare
        self.post_switch_delay = 0.3  # Wait for space to settle
        
    async def switch_to_space(self, request: SwitchRequest) -> SwitchResult:
        """Switch to specified space with minimal disruption"""
        
        start_time = time.time()
        
        # Check if already on target space
        if self.current_space == request.target_space:
            return SwitchResult(
                success=True,
                method_used=None,
                duration=0,
                permission_granted=True,
                error="Already on target space"
            )
            
        # Check if already switching
        if self.switching:
            return SwitchResult(
                success=False,
                method_used=None,
                duration=0,
                permission_granted=False,
                error="Another switch in progress"
            )
            
        # Request permission if needed
        permission_granted = True
        if request.require_permission:
            permission_granted = await self.permission_manager.request_permission(request)
            if not permission_granted:
                return SwitchResult(
                    success=False,
                    method_used=None,
                    duration=time.time() - start_time,
                    permission_granted=False,
                    error="Permission denied"
                )
                
        # Perform the switch
        try:
            self.switching = True
            
            # Choose method based on configuration
            method = self._choose_switch_method(request.target_space)
            
            # Prepare for switch (save state, etc.)
            await self._prepare_for_switch()
            
            # Execute switch
            success = await self._execute_switch(request.target_space, method)
            
            if success:
                self.current_space = request.target_space
                self.switch_history.append({
                    'from': self.current_space,
                    'to': request.target_space,
                    'timestamp': datetime.now(),
                    'reason': request.reason,
                    'method': method
                })
                
                # Execute callback if provided
                if request.callback:
                    await request.callback()
                    
                # Small delay to let space settle
                await asyncio.sleep(self.post_switch_delay)
                
            return SwitchResult(
                success=success,
                method_used=method if success else None,
                duration=time.time() - start_time,
                permission_granted=permission_granted,
                error=None if success else "Switch failed"
            )
            
        except Exception as e:
            logger.error(f"Error during space switch: {e}")
            return SwitchResult(
                success=False,
                method_used=None,
                duration=time.time() - start_time,
                permission_granted=permission_granted,
                error=str(e)
            )
            
        finally:
            self.switching = False
            
    def _choose_switch_method(self, target_space: int) -> SwitchMethod:
        """Choose the best method for switching"""
        
        # For adjacent spaces, use arrow keys (fastest)
        if abs(target_space - self.current_space) == 1:
            return SwitchMethod.CONTROL_ARROW
            
        # For specific numbered spaces (1-9), use direct number
        if 1 <= target_space <= 9:
            return SwitchMethod.CONTROL_NUMBER
            
        # For others, use Mission Control
        return SwitchMethod.MISSION_CONTROL
        
    async def _prepare_for_switch(self):
        """Prepare system for space switch"""
        
        if self.disable_animations:
            # Temporarily disable animations (requires accessibility)
            try:
                subprocess.run([
                    'defaults', 'write', 'com.apple.dock',
                    'workspaces-swoosh-animation-off', '-bool', 'YES'
                ], capture_output=True)
            except Exception:
                pass

        # Small delay to prepare
        await asyncio.sleep(self.pre_switch_delay)
        
    async def _execute_switch(self, target_space: int, method: SwitchMethod) -> bool:
        """Execute the actual space switch"""
        
        try:
            if method == SwitchMethod.CONTROL_ARROW:
                return await self._switch_with_arrows(target_space)
            elif method == SwitchMethod.CONTROL_NUMBER:
                return await self._switch_with_number(target_space)
            elif method == SwitchMethod.MISSION_CONTROL:
                return await self._switch_with_mission_control(target_space)
            else:
                return False
                
        except Exception as e:
            logger.error(f"Switch execution failed: {e}")
            return False
            
    async def _switch_with_arrows(self, target_space: int) -> bool:
        """Switch using Control+Arrow keys"""
        
        direction = 'right' if target_space > self.current_space else 'left'
        steps = abs(target_space - self.current_space)
        
        script = f'''
        tell application "System Events"
            repeat {steps} times
                key code {123 if direction == 'left' else 124} using control down
                delay 0.1
            end repeat
        end tell
        '''
        
        result = subprocess.run(['osascript', '-e', script], capture_output=True)
        return result.returncode == 0
        
    async def _switch_with_number(self, target_space: int) -> bool:
        """Switch using Control+Number"""
        
        # Key codes for numbers 1-9
        key_codes = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23, 6: 22, 7: 26, 8: 28, 9: 25}
        
        if target_space not in key_codes:
            return False
            
        script = f'''
        tell application "System Events"
            key code {key_codes[target_space]} using control down
        end tell
        '''
        
        result = subprocess.run(['osascript', '-e', script], capture_output=True)
        return result.returncode == 0
        
    async def _switch_with_mission_control(self, target_space: int) -> bool:
        """Switch using Mission Control"""
        
        script = f'''
        tell application "System Events"
            -- Open Mission Control
            key code 126 using control down
            delay 0.5
            
            -- Click on the target space (simplified)
            -- In production, would calculate exact position
            click at {{200 * {target_space}, 100}}
        end tell
        '''
        
        result = subprocess.run(['osascript', '-e', script], capture_output=True)
        return result.returncode == 0
        
    async def quick_capture_and_return(self, target_space: int, capture_callback: Callable) -> Optional[Any]:
        """Quick switch, capture, and return"""
        
        original_space = self.current_space
        
        # Create high-priority request
        request = SwitchRequest(
            target_space=target_space,
            reason="quick_capture",
            requester="vision_system",
            priority=7,
            require_permission=True,
            max_wait_time=3.0
        )
        
        # Switch to target
        switch_result = await self.switch_to_space(request)
        if not switch_result.success:
            return None
            
        try:
            # Capture
            capture_result = await capture_callback()
            
            # Switch back
            return_request = SwitchRequest(
                target_space=original_space,
                reason="return_from_capture",
                requester="vision_system",
                priority=9,
                require_permission=False  # No permission for return
            )
            
            await self.switch_to_space(return_request)
            
            return capture_result
            
        except Exception as e:
            logger.error(f"Error during quick capture: {e}")
            
            # Try to return to original space
            try:
                return_request = SwitchRequest(
                    target_space=original_space,
                    reason="error_recovery",
                    requester="vision_system", 
                    priority=10,
                    require_permission=False
                )
                await self.switch_to_space(return_request)
            except Exception:
                pass

            return None
            
    def restore_animation_settings(self):
        """Restore animation settings after switching"""
        
        if self.disable_animations:
            try:
                subprocess.run([
                    'defaults', 'delete', 'com.apple.dock',
                    'workspaces-swoosh-animation-off'
                ], capture_output=True)
                
                # Restart Dock to apply changes
                subprocess.run(['killall', 'Dock'], capture_output=True)
            except Exception:
                pass

# Integration helper for vision system
class SpaceCaptureIntegration:
    """Integrates space switching with screenshot capture"""
    
    def __init__(self, switcher: MinimalSpaceSwitcher, capture_func: Callable):
        self.switcher = switcher
        self.capture_func = capture_func
        
    async def capture_space(self, 
                          space_id: int, 
                          reason: str,
                          cache_callback: Optional[Callable] = None) -> Optional[Any]:
        """Capture screenshot from specific space"""
        
        async def capture_and_cache():
            # Capture screenshot
            screenshot = await self.capture_func()
            
            # Cache if callback provided
            if cache_callback and screenshot:
                await cache_callback(space_id, screenshot)
                
            return screenshot
            
        # Use quick capture method
        return await self.switcher.quick_capture_and_return(
            space_id,
            capture_and_cache
        )
        
    async def update_all_spaces(self, 
                              space_ids: List[int],
                              priority: int = 3) -> Dict[int, Any]:
        """Update screenshots for multiple spaces"""
        
        results = {}
        
        for space_id in space_ids:
            # Lower priority for bulk updates
            request = SwitchRequest(
                target_space=space_id,
                reason="bulk_space_update",
                requester="cache_system",
                priority=priority,
                require_permission=True
            )
            
            result = await self.capture_space(
                space_id,
                f"update_space_{space_id}"
            )
            
            if result:
                results[space_id] = result
                
        return results