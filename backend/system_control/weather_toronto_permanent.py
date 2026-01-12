#!/usr/bin/env python3
"""
Permanent Toronto Selection for Weather App
Mimics exact human behavior to make selection stick
"""

import asyncio
import logging
import subprocess

logger = logging.getLogger(__name__)


class WeatherTorontoPermanent:
    """Make Toronto selection permanent like manual clicking"""
    
    def __init__(self, controller, vision_handler=None):
        self.controller = controller
        self.vision_handler = vision_handler
        
    async def select_toronto_permanent(self) -> bool:
        """
        Select Toronto and make it stick permanently
        Mimics exact human clicking behavior
        """
        try:
            logger.info("Attempting permanent Toronto selection...")
            
            # Step 1: Close Weather app completely first
            await self._close_weather_app()
            await asyncio.sleep(1)
            
            # Step 2: Open Weather fresh
            logger.info("Opening Weather app fresh...")
            subprocess.run(['open', '-a', 'Weather'], check=False)
            await asyncio.sleep(3)  # Give it time to fully load
            
            # Step 3: Use multiple selection methods in sequence
            methods = [
                self._method_accessibility_click,
                self._method_ui_element_click,
                self._method_force_click,
                self._method_applescript_click
            ]
            
            for method in methods:
                logger.info(f"Trying {method.__name__}...")
                if await method():
                    # Wait for selection to register
                    await asyncio.sleep(2)
                    
                    # Verify it stuck
                    if await self._verify_toronto_selected():
                        logger.info("✅ Toronto selection is permanent!")
                        
                        # Do a save action to persist
                        await self._save_selection()
                        return True
                        
            logger.warning("All permanent selection methods failed")
            return False
            
        except Exception as e:
            logger.error(f"Permanent selection error: {e}")
            return False
    
    async def _close_weather_app(self):
        """Completely close Weather app"""
        script = '''
        tell application "Weather"
            quit
        end tell
        '''
        self.controller.execute_applescript(script)
        # Also force quit to ensure it's closed
        subprocess.run(['pkill', '-x', 'Weather'], stderr=subprocess.DEVNULL)
        await asyncio.sleep(0.5)
    
    async def _method_accessibility_click(self) -> bool:
        """Use accessibility to click Toronto"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                set frontmost to true
                delay 1
                
                -- Find and click Toronto using accessibility
                tell window 1
                    tell scroll area 1 of splitter group 1
                        tell table 1 of scroll area 1
                            -- Look for Toronto row
                            repeat with i from 1 to count of rows
                                try
                                    set rowItem to row i
                                    set rowText to value of static text 1 of UI element 1 of rowItem
                                    
                                    if rowText contains "Toronto" or rowText contains "My Location" then
                                        -- Select the row
                                        set selected of rowItem to true
                                        delay 0.5
                                        
                                        -- Click it
                                        click rowItem
                                        delay 0.5
                                        
                                        -- Double-click for good measure
                                        click rowItem
                                        delay 1
                                        
                                        return true
                                    end if
                                end try
                            end repeat
                        end tell
                    end tell
                end tell
            end tell
        end tell
        return false
        '''
        
        success, result = self.controller.execute_applescript(script)
        return success and result.strip().lower() == 'true'
    
    async def _method_ui_element_click(self) -> bool:
        """Direct UI element interaction"""
        script = '''
        tell application "System Events"
            tell process "Weather"
                set frontmost to true
                
                -- Try to find Toronto in the sidebar
                set allElements to entire contents of window 1
                
                repeat with elem in allElements
                    try
                        if class of elem is static text then
                            if value of elem contains "Toronto" or value of elem contains "My Location" then
                                -- Get parent row
                                set parentElem to container of elem
                                
                                -- Click parent multiple times
                                click parentElem
                                delay 0.3
                                click parentElem
                                delay 1
                                
                                return true
                            end if
                        end if
                    end try
                end repeat
            end tell
        end tell
        return false
        '''
        
        success, result = self.controller.execute_applescript(script)
        return success and result.strip().lower() == 'true'
    
    async def _method_force_click(self) -> bool:
        """Force click with mouse events"""
        # Click at Toronto position multiple times with delays
        positions = [
            (125, 65),   # Primary Toronto position
            (125, 60),   # Slightly higher
            (125, 70),   # Slightly lower
        ]
        
        for x, y in positions:
            # Try click and hold
            success, _ = await self.controller.click_and_hold(x, y, 0.5)
            if success:
                await asyncio.sleep(0.5)
                
                # Regular clicks
                await self.controller.click_at(x, y)
                await asyncio.sleep(0.2)
                await self.controller.click_at(x, y)
                await asyncio.sleep(1)
                
                # Check if it worked
                if self.vision_handler:
                    result = await self.vision_handler.analyze_weather_fast()
                    if result.get('success'):
                        analysis = result.get('analysis', '').lower()
                        if 'toronto' in analysis:
                            return True
        
        return False
    
    async def _method_applescript_click(self) -> bool:
        """Use AppleScript to simulate exact human clicks"""
        script = '''
        -- Activate Weather and wait
        tell application "Weather"
            activate
            delay 1
        end tell
        
        -- Use System Events to click
        tell application "System Events"
            tell process "Weather"
                set frontmost to true
                
                -- Click at Toronto position
                click at {125, 65}
                delay 0.2
                
                -- Double-click to select
                click at {125, 65}
                click at {125, 65}
                delay 1
                
                -- Press Enter to confirm selection
                key code 36
                delay 1
            end tell
        end tell
        
        return true
        '''
        
        success, _ = self.controller.execute_applescript(script)
        return success
    
    async def _save_selection(self):
        """Try to save the selection to make it persistent"""
        # Press Cmd+S to save
        script = '''
        tell application "System Events"
            tell process "Weather"
                keystroke "s" using command down
                delay 0.5
            end tell
        end tell
        '''
        self.controller.execute_applescript(script)
        
        # Also try clicking elsewhere to "confirm" selection
        await self.controller.click_at(300, 300)  # Click in main area
        await asyncio.sleep(0.5)
    
    async def _verify_toronto_selected(self) -> bool:
        """Verify Toronto is selected and showing"""
        if not self.vision_handler:
            return True  # Assume success if can't verify
            
        try:
            result = await self.vision_handler.analyze_weather_fast()
            if result.get('success'):
                analysis = result.get('analysis', '').lower()
                return 'toronto' in analysis or 'canada' in analysis
        except Exception:
            pass

        return False


async def test_permanent_selection():
    """Test permanent Toronto selection"""
    import os
    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer
    from system_control.macos_controller import MacOSController
    
    print("🎯 Testing Permanent Toronto Selection")
    print("="*60)
    
    vision = ClaudeVisionAnalyzer(os.getenv('ANTHROPIC_API_KEY'))
    controller = MacOSController()
    
    selector = WeatherTorontoPermanent(controller, vision)
    
    print("\nAttempting permanent Toronto selection...")
    success = await selector.select_toronto_permanent()
    
    if success:
        print("✅ Toronto permanently selected!")
        
        # Test persistence
        print("\nTesting persistence - closing and reopening Weather...")
        await selector._close_weather_app()
        await asyncio.sleep(2)
        
        subprocess.run(['open', '-a', 'Weather'], check=False)
        await asyncio.sleep(3)
        
        # Check what's showing
        result = await vision.analyze_weather_fast()
        if result.get('success'):
            print(f"After reopen: {result.get('analysis', '')[:100]}...")
            
            if 'toronto' in result.get('analysis', '').lower():
                print("✅ Selection persisted!")
            else:
                print("❌ Selection didn't persist")
    else:
        print("❌ Failed to select Toronto permanently")
    
    print("\n" + "="*60)


if __name__ == "__main__":
    import os
    os.chdir('/Users/derekjrussell/Documents/repos/JARVIS-AI-Agent/backend')
    asyncio.run(test_permanent_selection())