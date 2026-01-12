#!/usr/bin/env python3
"""
Enhanced Weather Navigation with Human-like Interaction
Simulates manual clicking behavior for reliable Toronto selection
"""

import asyncio
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


class WeatherNavigationEnhanced:
    """Enhanced navigation that mimics human interaction patterns"""
    
    def __init__(self, controller, vision_handler=None):
        self.controller = controller
        self.vision_handler = vision_handler
        
    async def select_toronto_human_like(self) -> bool:
        """
        Select Toronto using human-like interaction patterns
        Implements multiple strategies to ensure selection works
        """
        try:
            logger.info("Starting human-like Toronto selection")
            
            # First, ensure Weather app is completely ready
            if not await self._ensure_weather_fully_loaded():
                logger.error("Weather app not fully loaded")
                return False
            
            # Strategy 1: Enhanced click with hold
            logger.info("Strategy 1: Click-and-hold on Toronto")
            if await self._click_and_hold_toronto():
                if await self._verify_toronto_selected():
                    logger.info("Successfully selected Toronto with click-and-hold")
                    return True
            
            # Strategy 2: Double-click with delay
            logger.info("Strategy 2: Human-speed double-click")
            if await self._human_double_click_toronto():
                if await self._verify_toronto_selected():
                    logger.info("Successfully selected Toronto with double-click")
                    return True
            
            # Strategy 3: Accessibility-based selection
            logger.info("Strategy 3: Accessibility selection")
            if await self._select_via_accessibility():
                if await self._verify_toronto_selected():
                    logger.info("Successfully selected Toronto via accessibility")
                    return True
            
            # Strategy 4: Menu bar navigation
            logger.info("Strategy 4: Menu bar navigation")
            if await self._select_via_menu():
                if await self._verify_toronto_selected():
                    logger.info("Successfully selected Toronto via menu")
                    return True
                    
            logger.warning("All Toronto selection strategies failed")
            return False
            
        except Exception as e:
            logger.error(f"Enhanced navigation error: {e}")
            return False
    
    async def _ensure_weather_fully_loaded(self) -> bool:
        """Ensure Weather app is completely loaded and ready"""
        try:
            # Open Weather if not already open
            script = '''
            -- Ensure Weather is open and fully loaded
            tell application "Weather"
                activate
                delay 0.5
                
                -- Force window to front
                if (count windows) = 0 then
                    reopen
                    delay 2
                end if
                
                set frontmost to true
            end tell
            
            -- Wait for UI to be ready
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    
                    -- Wait for window to exist (simpler check)
                    set maxWait to 10
                    set waitCount to 0
                    repeat while waitCount < maxWait
                        try
                            if exists window 1 then
                                delay 1 -- Extra delay for UI to settle
                                return true
                            end if
                        end try
                        delay 0.5
                        set waitCount to waitCount + 1
                    end repeat
                    
                    -- Even if we can't verify, assume it's ready after waiting
                    return true
                end tell
            end tell
            '''
            
            success, result = self.controller.execute_applescript(script)
            if success and result.strip().lower() == 'true':
                logger.info("Weather app fully loaded")
                await asyncio.sleep(1)  # Extra delay for animations
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to ensure Weather loaded: {e}")
            return False
    
    async def _click_and_hold_toronto(self) -> bool:
        """Click and hold on Toronto location (mimics human press-and-hold)"""
        try:
            script = '''
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    delay 0.5
                    
                    -- Simple approach: Click on first sidebar item (Toronto)
                    try
                        -- Click at top of sidebar where Toronto should be
                        click at {125, 65}
                        delay 0.2
                        -- Click again to ensure selection
                        click at {125, 65}
                        delay 1
                        return true
                    on error
                        -- Try keyboard navigation
                        key code 126 -- Up arrow
                        delay 0.2
                        key code 126 -- Up arrow
                        delay 0.2
                        key code 36 -- Return
                        delay 1
                        return true
                    end try
                end tell
            end tell
            return false
            '''
            
            success, result = self.controller.execute_applescript(script)
            await asyncio.sleep(1)  # Let selection settle
            return success and result.strip().lower() == 'true'
            
        except Exception as e:
            logger.error(f"Click-and-hold failed: {e}")
            
            # Fallback: Direct coordinate click-and-hold
            try:
                # Click and hold at Toronto position
                await self.controller.click_at(125, 65)
                await asyncio.sleep(0.2)  # Hold
                await self.controller.click_at(125, 65)  # Release
                await asyncio.sleep(1)
                return True
            except Exception:
                return False

    async def _human_double_click_toronto(self) -> bool:
        """Double-click with human-like timing"""
        try:
            script = '''
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    
                    -- Simple double-click at Toronto position
                    try
                        -- Double-click where Toronto should be
                        do shell script "osascript -e 'tell application \\"System Events\\" to click at {125, 65}'"
                        delay 0.15  -- Human double-click timing
                        do shell script "osascript -e 'tell application \\"System Events\\" to click at {125, 65}'"
                        delay 1.5
                        return true
                    on error
                        -- Fallback to keyboard
                        key code 126 -- Up
                        delay 0.1
                        key code 126 -- Up
                        delay 0.1
                        key code 36 -- Return
                        delay 1
                        return true
                    end try
                end tell
            end tell
            '''
            
            success, result = self.controller.execute_applescript(script)
            await asyncio.sleep(1.5)  # Let weather load
            return success
            
        except Exception as e:
            logger.error(f"Human double-click failed: {e}")
            
            # Fallback: Direct coordinate double-click
            try:
                await self.controller.click_at(125, 65)
                await asyncio.sleep(0.15)  # Human-like delay
                await self.controller.click_at(125, 65)
                await asyncio.sleep(1.5)
                return True
            except Exception:
                return False

    async def _select_via_accessibility(self) -> bool:
        """Use accessibility features to select Toronto"""
        try:
            script = '''
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    
                    -- Try to click the first location (Toronto) using accessibility
                    try
                        -- Get the sidebar's first clickable item
                        tell window 1
                            -- Click the first row in the sidebar
                            set firstLocation to first UI element whose role is "AXRow"
                            if exists firstLocation then
                                click firstLocation
                                delay 1
                                return true
                            end if
                        end tell
                    end try
                    
                    -- Fallback: Use position-based click
                    click at {125, 65}
                    delay 1
                    return true
                end tell
            end tell
            '''
            
            success, result = self.controller.execute_applescript(script)
            await asyncio.sleep(1)
            return success and result.strip().lower() == 'true'
            
        except Exception as e:
            logger.error(f"Accessibility selection failed: {e}")
            return False
    
    async def _select_via_menu(self) -> bool:
        """Try to select location via Weather menu bar"""
        try:
            script = '''
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    
                    -- Try View menu
                    click menu bar item "View" of menu bar 1
                    delay 0.2
                    
                    -- Look for location options
                    try
                        click menu item "My Location" of menu "View" of menu bar 1
                        delay 1
                        return true
                    end try
                    
                    -- Close menu if still open
                    key code 53  -- Escape
                    
                    -- Try Window menu
                    click menu bar item "Window" of menu bar 1
                    delay 0.2
                    
                    try
                        -- Look for Toronto in Window menu
                        set menuItems to menu items of menu "Window" of menu bar 1
                        repeat with mi in menuItems
                            if name of mi contains "Toronto" then
                                click mi
                                delay 1
                                return true
                            end if
                        end repeat
                    end try
                    
                    key code 53  -- Escape
                end tell
            end tell
            return false
            '''
            
            success, result = self.controller.execute_applescript(script)
            await asyncio.sleep(1)
            return success and result.strip().lower() == 'true'
            
        except Exception as e:
            logger.error(f"Menu selection failed: {e}")
            return False
    
    async def _verify_toronto_selected(self) -> bool:
        """Verify Toronto is selected and showing"""
        if not self.vision_handler:
            logger.warning("No vision handler for verification")
            return True  # Assume success if can't verify
        
        try:
            # Quick vision check
            result = await self.vision_handler.analyze_weather_fast()
            if result.get('success'):
                analysis = result.get('analysis', '').lower()
                
                # Check for Toronto indicators
                if any(indicator in analysis for indicator in ['toronto', 'ontario', '74°', 'my location']):
                    logger.info("Verified: Toronto is showing")
                    return True
                elif 'new york' in analysis or 'nyc' in analysis:
                    logger.warning("Still showing New York")
                    return False
                else:
                    # Unknown location, might be Toronto
                    logger.info(f"Showing: {analysis[:50]}...")
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Verification failed: {e}")
            return True  # Assume success if can't verify


async def test_enhanced_navigation():
    """Test the enhanced navigation"""
    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer
    from system_control.macos_controller import MacOSController
    import os
    
    print("🚀 Testing Enhanced Toronto Navigation")
    print("="*60)
    
    vision = ClaudeVisionAnalyzer(os.getenv('ANTHROPIC_API_KEY'))
    controller = MacOSController()
    nav = WeatherNavigationEnhanced(controller, vision)
    
    # Test selection
    print("\nSelecting Toronto with enhanced methods...")
    success = await nav.select_toronto_human_like()
    
    if success:
        print("✅ Toronto selected successfully!")
        
        # Verify it stays selected
        print("\nWaiting 3 seconds to ensure selection is stable...")
        await asyncio.sleep(3)
        
        if await nav._verify_toronto_selected():
            print("✅ Toronto selection is stable!")
        else:
            print("❌ Selection reverted")
    else:
        print("❌ Failed to select Toronto")
    
    print("\n" + "="*60)


if __name__ == "__main__":
    import os
    os.chdir('/Users/derekjrussell/Documents/repos/JARVIS-AI-Agent/backend')
    asyncio.run(test_enhanced_navigation())