#!/usr/bin/env python3
"""
Force Weather App to Use Current Location
Alternative approach using system location services
"""

import asyncio
import logging
import subprocess

logger = logging.getLogger(__name__)


class WeatherLocationForcer:
    """Force Weather to use current location (Toronto)"""
    
    def __init__(self, controller):
        self.controller = controller
        
    async def force_current_location(self) -> bool:
        """
        Force Weather app to use current location
        This might make it default to Toronto if that's your location
        """
        try:
            logger.info("Attempting to force current location in Weather...")
            
            # Method 1: Try location services refresh
            await self._refresh_location_services()
            
            # Method 2: Try Weather menu commands
            await self._use_weather_menu_location()
            
            # Method 3: Try keyboard shortcut for current location
            await self._use_location_shortcut()
            
            return True
            
        except Exception as e:
            logger.error(f"Location forcing failed: {e}")
            return False
    
    async def _refresh_location_services(self):
        """Refresh location services"""
        logger.info("Refreshing location services...")
        
        # First ensure location services are enabled for Weather
        # This would normally require System Preferences access
        # but we can try to trigger a location update
        
        script = '''
        tell application "Weather"
            activate
        end tell
        
        -- Try to trigger location update
        tell application "System Events"
            tell process "Weather"
                -- Try Cmd+R for refresh
                keystroke "r" using command down
                delay 1
            end tell
        end tell
        '''
        
        self.controller.execute_applescript(script)
        await asyncio.sleep(2)
    
    async def _use_weather_menu_location(self):
        """Try to use Weather menu for location"""
        logger.info("Trying Weather menu location options...")
        
        script = '''
        tell application "System Events"
            tell process "Weather"
                set frontmost to true
                
                -- Try View menu
                click menu bar item "View" of menu bar 1
                delay 0.5
                
                -- Look for Current Location or My Location
                try
                    click menu item "Current Location" of menu "View" of menu bar 1
                    return true
                on error
                    try
                        click menu item "My Location" of menu "View" of menu bar 1
                        return true
                    on error
                        -- Close menu
                        key code 53  -- Escape
                        return false
                    end try
                end try
            end tell
        end tell
        '''
        
        success, _ = self.controller.execute_applescript(script)
        if success:
            await asyncio.sleep(2)
    
    async def _use_location_shortcut(self):
        """Try keyboard shortcuts for location"""
        logger.info("Trying location shortcuts...")
        
        shortcuts_to_try = [
            ("Cmd+0", "keystroke \"0\" using command down"),
            ("Cmd+L", "keystroke \"l\" using command down"),
            ("Cmd+Shift+L", "keystroke \"l\" using {command down, shift down}")
        ]
        
        for name, shortcut in shortcuts_to_try:
            logger.info(f"Trying {name}...")
            script = f'''
            tell application "System Events"
                tell process "Weather"
                    {shortcut}
                    delay 1
                end tell
            end tell
            '''
            
            self.controller.execute_applescript(script)
            await asyncio.sleep(1)


class WeatherSmartNavigator:
    """Smart navigation that combines all approaches"""
    
    def __init__(self, controller, vision_handler=None):
        self.controller = controller
        self.vision_handler = vision_handler
        
    async def navigate_to_toronto(self) -> bool:
        """
        Navigate to Toronto using the smartest available method
        """
        logger.info("Smart navigation to Toronto...")
        
        # First check if Toronto is already showing
        if await self._is_toronto_showing():
            logger.info("Toronto already selected!")
            return True
            
        # Try different approaches in order of likelihood
        approaches = [
            ("Keyboard Navigation", self._keyboard_navigate_to_top),
            ("Direct Click", self._direct_click_toronto),
            ("Force Location", self._force_location),
            ("Search", self._search_for_toronto)
        ]
        
        for name, method in approaches:
            logger.info(f"Trying {name}...")
            try:
                if await method():
                    await asyncio.sleep(2)  # Let it load
                    if await self._is_toronto_showing():
                        logger.info(f"✅ {name} succeeded!")
                        return True
            except Exception as e:
                logger.error(f"{name} failed: {e}")
        
        logger.warning("All navigation methods failed")
        return False
    
    async def _is_toronto_showing(self) -> bool:
        """Check if Toronto weather is showing"""
        if not self.vision_handler:
            return False
            
        try:
            result = await self.vision_handler.analyze_weather_fast()
            if result.get('success'):
                analysis = result.get('analysis', '').lower()
                return 'toronto' in analysis or ('68' in analysis and 'canada' in analysis)
        except Exception:
            return False

    async def _keyboard_navigate_to_top(self) -> bool:
        """Use keyboard to navigate to top of list (Toronto)"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                -- Focus the sidebar
                click at {100, 200}
                delay 0.3
                
                -- Go to top with Home key or multiple up arrows
                key code 115  -- Home key
                delay 0.5
                
                -- If Home didn't work, use up arrows
                repeat 10 times
                    key code 126  -- Up arrow
                    delay 0.1
                end repeat
                
                -- Select with Enter
                key code 36  -- Return
                delay 1
            end tell
        end tell
        '''
        
        self.controller.execute_applescript(script)
        return True
    
    async def _direct_click_toronto(self) -> bool:
        """Direct click on Toronto position"""
        # Multiple clicks with slight variations
        positions = [(125, 65), (125, 60), (130, 65)]
        
        for x, y in positions:
            await self.controller.click_at(x, y)
            await asyncio.sleep(0.2)
            await self.controller.click_at(x, y)  # Double-click
            await asyncio.sleep(1)
            
        return True
    
    async def _force_location(self) -> bool:
        """Force current location"""
        forcer = WeatherLocationForcer(self.controller)
        await forcer.force_current_location()
        return True
    
    async def _search_for_toronto(self) -> bool:
        """Search for Toronto"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                -- Try search
                keystroke "f" using command down
                delay 0.5
                
                -- Type Toronto
                keystroke "Toronto, Canada"
                delay 1
                
                -- Select first result
                key code 36  -- Return
                delay 2
            end tell
        end tell
        '''
        
        self.controller.execute_applescript(script)
        return True