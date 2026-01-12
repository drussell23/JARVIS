#!/usr/bin/env python3
"""
Human-like Weather App Clicker
Replicates exact human clicking behavior for Toronto selection
"""

import asyncio
import logging
import subprocess
import time

logger = logging.getLogger(__name__)


class WeatherHumanClicker:
    """Click Toronto exactly like a human would"""
    
    def __init__(self, controller, vision_handler=None):
        self.controller = controller
        self.vision_handler = vision_handler
        
    async def click_toronto_like_human(self) -> bool:
        """
        Click Toronto with exact human-like behavior
        Including mouse movement, timing, and interaction patterns
        """
        try:
            logger.info("Starting human-like Toronto clicking...")
            
            # Step 1: Ensure Weather is truly active (like a human would check)
            await self._activate_weather_like_human()
            
            # Step 2: Move mouse to sidebar area first (humans don't teleport)
            await self._move_mouse_to_sidebar()
            
            # Step 3: Look for Toronto and click it naturally
            success = await self._click_toronto_naturally()
            
            if success:
                # Step 4: Verify like a human would (wait and check)
                await asyncio.sleep(1.5)  # Humans wait to see result
                if await self._verify_toronto_selected():
                    logger.info("✅ Successfully clicked Toronto like a human!")
                    
                    # Step 5: Click somewhere else to "confirm" (human behavior)
                    await self._confirm_selection()
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Human-like clicking failed: {e}")
            return False
    
    async def _activate_weather_like_human(self):
        """Activate Weather app like a human would"""
        # First check if it's already open
        is_running = subprocess.run(
            ['pgrep', '-x', 'Weather'], 
            capture_output=True
        ).returncode == 0
        
        if not is_running:
            # Open it fresh
            logger.info("Opening Weather app fresh...")
            subprocess.run(['open', '-a', 'Weather'], check=False)
            await asyncio.sleep(3)  # Humans wait for apps to open
        else:
            # Bring to front by clicking on it
            logger.info("Bringing Weather to front...")
            script = '''
            tell application "Weather"
                activate
                set frontmost to true
            end tell
            '''
            self.controller.execute_applescript(script)
            await asyncio.sleep(0.5)
    
    async def _move_mouse_to_sidebar(self):
        """Move mouse to sidebar area gradually"""
        # Humans don't instantly teleport mouse
        # Start from a neutral position and move to sidebar
        logger.info("Moving mouse to sidebar area...")
        
        # If we had mouse control, we'd move gradually
        # For now, just position near sidebar
        script = '''
        tell application "System Events"
            -- Move mouse to sidebar area
            -- This simulates human mouse movement
            delay 0.2
        end tell
        '''
        self.controller.execute_applescript(script)
        await asyncio.sleep(0.3)
    
    async def _click_toronto_naturally(self) -> bool:
        """Click Toronto with natural human timing and behavior"""
        logger.info("Clicking Toronto with human-like behavior...")
        
        # Method 1: Click with natural timing
        # Humans often hover briefly before clicking
        positions_to_try = [
            (125, 65, "Toronto primary position"),
            (125, 60, "Toronto alternate position"),
            (150, 65, "Toronto text area")
        ]
        
        for x, y, desc in positions_to_try:
            logger.info(f"Trying {desc}...")
            
            # Hover briefly (humans don't instantly click)
            await asyncio.sleep(0.2)
            
            # Click with human-like timing
            # Single click first
            success, _ = await self.controller.click_at(x, y)
            
            if success:
                # Humans sometimes double-click to be sure
                await asyncio.sleep(0.15)  # Human double-click timing
                await self.controller.click_at(x, y)
                
                # Wait to see result (humans don't immediately move on)
                await asyncio.sleep(1.5)
                
                # Check if it worked
                if self.vision_handler:
                    result = await self.vision_handler.analyze_weather_fast()
                    if result.get('success'):
                        analysis = result.get('analysis', '').lower()
                        if 'toronto' in analysis:
                            logger.info(f"✅ Toronto selected at {desc}")
                            return True
        
        # Method 2: Try keyboard navigation (some humans prefer keyboard)
        logger.info("Trying keyboard navigation...")
        script = '''
        tell application "System Events"
            tell process "Weather"
                -- Click in sidebar to focus it
                click at {100, 200}
                delay 0.3
                
                -- Navigate to top with arrows (like humans do)
                repeat 5 times
                    key code 126 -- Up arrow
                    delay 0.15  -- Human typing speed
                end repeat
                
                -- Select with Enter
                key code 36
                delay 1
            end tell
        end tell
        '''
        
        success, _ = self.controller.execute_applescript(script)
        await asyncio.sleep(1)
        
        return success
    
    async def _confirm_selection(self):
        """Confirm selection like a human would"""
        # Humans often click elsewhere after making a selection
        # This might help "save" the selection
        logger.info("Confirming selection...")
        
        # Click in the main weather display area
        await self.controller.click_at(500, 400)
        await asyncio.sleep(0.5)
        
        # Some humans might also press Escape to ensure focus
        script = '''
        tell application "System Events"
            key code 53 -- Escape
        end tell
        '''
        self.controller.execute_applescript(script)
    
    async def _verify_toronto_selected(self) -> bool:
        """Verify Toronto is selected"""
        if not self.vision_handler:
            return True
            
        try:
            result = await self.vision_handler.analyze_weather_fast()
            if result.get('success'):
                analysis = result.get('analysis', '').lower()
                return 'toronto' in analysis or 'canada' in analysis
        except Exception:
            pass

        return False


async def test_human_clicker():
    """Test human-like clicking"""
    import os
    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer
    from system_control.macos_controller import MacOSController
    
    print("🖱️ Testing Human-like Toronto Clicking")
    print("="*60)
    
    vision = ClaudeVisionAnalyzer(os.getenv('ANTHROPIC_API_KEY'))
    controller = MacOSController()
    
    clicker = WeatherHumanClicker(controller, vision)
    
    print("\nAttempting human-like Toronto selection...")
    success = await clicker.click_toronto_like_human()
    
    if success:
        print("✅ Successfully selected Toronto!")
        
        # Test if it persists
        print("\nChecking if selection persists...")
        await asyncio.sleep(2)
        
        result = await vision.analyze_weather_fast()
        if result.get('success'):
            print(f"Still showing: {result.get('analysis', '')[:50]}...")
    else:
        print("❌ Failed to select Toronto")
    
    print("\n" + "="*60)


if __name__ == "__main__":
    import os
    os.chdir('/Users/derekjrussell/Documents/repos/JARVIS-AI-Agent/backend')
    asyncio.run(test_human_clicker())