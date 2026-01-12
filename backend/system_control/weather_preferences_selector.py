#!/usr/bin/env python3
"""
Weather App Preferences Approach
Try to set default location through preferences
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class WeatherPreferencesSelector:
    """Set Toronto as default through Weather preferences"""
    
    def __init__(self, controller):
        self.controller = controller
        
    async def set_toronto_default(self) -> bool:
        """
        Try to set Toronto as default location through preferences
        """
        try:
            logger.info("Attempting to set Toronto as default via preferences...")
            
            # Method 1: Try Weather menu > Preferences
            if await self._via_preferences_menu():
                return True
                
            # Method 2: Try keyboard shortcut Cmd+,
            if await self._via_preferences_shortcut():
                return True
                
            # Method 3: Try to manipulate plist directly
            if await self._via_plist():
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Preferences approach failed: {e}")
            return False
    
    async def _via_preferences_menu(self) -> bool:
        """Access preferences through menu"""
        script = '''
        tell application "Weather"
            activate
        end tell
        delay 1
        
        tell application "System Events"
            tell process "Weather"
                -- Try to open preferences
                click menu item "Preferences…" of menu "Weather" of menu bar 1
                delay 1
                
                -- Look for location settings
                if exists window "General" then
                    -- Success - preferences opened
                    return true
                end if
            end tell
        end tell
        return false
        '''
        
        success, result = self.controller.execute_applescript(script)
        if success and result.strip().lower() == 'true':
            logger.info("Opened Weather preferences")
            # TODO: Navigate preferences to set default location
            
            # Close preferences for now
            await asyncio.sleep(1)
            close_script = '''
            tell application "System Events"
                key code 53 -- Escape
            end tell
            '''
            self.controller.execute_applescript(close_script)
            
        return False
    
    async def _via_preferences_shortcut(self) -> bool:
        """Try Cmd+, shortcut"""
        script = '''
        tell application "Weather"
            activate
        end tell
        delay 0.5
        
        tell application "System Events"
            tell process "Weather"
                -- Press Cmd+,
                keystroke "," using command down
                delay 1
                
                -- Check if preferences opened
                if (count windows) > 1 then
                    -- Close it
                    key code 53
                    return true
                end if
            end tell
        end tell
        return false
        '''
        
        success, result = self.controller.execute_applescript(script)
        return success and result.strip().lower() == 'true'
    
    async def _via_plist(self) -> bool:
        """Try to modify Weather app preferences directly"""
        # Weather app likely stores preferences in:
        # ~/Library/Preferences/com.apple.weather.plist
        # But modifying plists requires careful handling
        
        # For now, just check if the file exists
        import os
        plist_path = os.path.expanduser("~/Library/Preferences/com.apple.weather.plist")
        
        if os.path.exists(plist_path):
            logger.info(f"Weather preferences found at: {plist_path}")
            # Note: Modifying plists requires proper tools and app restart
            # This would need more investigation
            
        return False


class WeatherSmartSelector:
    """Smart selection that tries multiple approaches"""
    
    def __init__(self, controller, vision_handler=None):
        self.controller = controller
        self.vision_handler = vision_handler
        
    async def ensure_toronto_selected(self) -> bool:
        """
        Ensure Toronto is selected using the smartest approach
        """
        # First, check if Toronto is already selected
        if await self._is_toronto_selected():
            logger.info("Toronto is already selected")
            return True
            
        # Try different approaches
        approaches = [
            ("Direct Click", self._try_direct_click),
            ("Menu Navigation", self._try_menu_navigation),
            ("Search", self._try_search_approach),
            ("List Navigation", self._try_list_navigation)
        ]
        
        for name, method in approaches:
            logger.info(f"Trying approach: {name}")
            if await method():
                if await self._is_toronto_selected():
                    logger.info(f"✅ {name} worked!")
                    return True
                    
        return False
    
    async def _is_toronto_selected(self) -> bool:
        """Check if Toronto is currently selected"""
        if not self.vision_handler:
            return False
            
        try:
            result = await self.vision_handler.analyze_weather_fast()
            if result.get('success'):
                analysis = result.get('analysis', '').lower()
                return 'toronto' in analysis or 'canada' in analysis
        except Exception:
            return False

    async def _try_direct_click(self) -> bool:
        """Simple direct click approach"""
        await self.controller.click_at(125, 65)
        await asyncio.sleep(0.3)
        await self.controller.click_at(125, 65)
        await asyncio.sleep(2)
        return True
    
    async def _try_menu_navigation(self) -> bool:
        """Try using View menu"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                -- Try View menu
                click menu bar item "View" of menu bar 1
                delay 0.5
                
                -- Look for location options
                if exists menu item "My Location" of menu "View" of menu bar 1 then
                    click menu item "My Location" of menu "View" of menu bar 1
                    return true
                else
                    key code 53 -- Escape to close menu
                end if
            end tell
        end tell
        return false
        '''
        
        success, _ = self.controller.execute_applescript(script)
        await asyncio.sleep(2)
        return success
    
    async def _try_search_approach(self) -> bool:
        """Try using search to find Toronto"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                -- Try Cmd+F for search
                keystroke "f" using command down
                delay 0.5
                
                -- Type Toronto
                keystroke "Toronto"
                delay 1
                
                -- Press Enter
                key code 36
                delay 2
            end tell
        end tell
        '''
        
        self.controller.execute_applescript(script)
        await asyncio.sleep(2)
        return True
    
    async def _try_list_navigation(self) -> bool:
        """Navigate the location list"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                -- Click in sidebar to focus it
                click at {100, 200}
                delay 0.5
                
                -- Use arrows to navigate
                repeat 5 times
                    key code 126 -- Up arrow
                    delay 0.1
                end repeat
                
                -- Select with Enter
                key code 36
                delay 2
            end tell
        end tell
        '''
        
        self.controller.execute_applescript(script)
        return True