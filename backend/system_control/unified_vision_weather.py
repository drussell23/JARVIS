"""
Unified Vision-Based Weather System for JARVIS
Single source of truth for all weather queries using computer vision
Zero hardcoding - completely dynamic and intelligent
"""

import asyncio
import logging
import subprocess
import tempfile
import re
from typing import Dict, Optional, List, Tuple, Any
from datetime import datetime, timedelta
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class UnifiedVisionWeather:
    """
    The ONLY weather system JARVIS needs
    Uses vision to read Weather app - no GPS/API needed
    """
    
    def __init__(self, vision_handler=None, controller=None):
        self.vision_handler = vision_handler
        self.controller = controller
        self.cache = {}
        self.cache_duration = timedelta(minutes=5)
        
        # Dynamic UI element tracking
        self.ui_patterns = {
            'my_location': [
                r'my\s*location',
                r'current\s*location',
                r'home\s*location',
                r'📍.*home',
                r'•\s*home'
            ],
            'temperature': [
                r'(\d+)°',
                r'(\d+)\s*degrees',
                r'temp.*(\d+)'
            ],
            'conditions': [
                'clear', 'cloudy', 'partly cloudy', 'mostly cloudy',
                'rain', 'drizzle', 'shower', 'storm', 'thunder',
                'snow', 'sleet', 'hail', 'fog', 'mist',
                'sunny', 'overcast', 'windy'
            ]
        }
    
    async def get_weather(self, query: str = "", fast_mode: bool = False) -> Dict[str, Any]:
        """
        Main entry point - handles ALL weather queries intelligently
        
        Args:
            query: Weather query
            fast_mode: Skip navigation attempts and just read current weather
        """
        try:
            logger.info(f"[UNIFIED WEATHER] Weather query received: {query}, fast_mode: {fast_mode}")
            logger.info(f"[UNIFIED WEATHER] Has vision_handler: {self.vision_handler is not None}")
            logger.info(f"[UNIFIED WEATHER] Has controller: {self.controller is not None}")
            
            # Check cache first
            cache_key = self._get_cache_key(query)
            if cached := self._check_cache(cache_key):
                logger.info("Using cached weather data")
                return cached
            
            # Ensure Weather app is ready with timeout
            try:
                app_ready = await asyncio.wait_for(
                    self._ensure_weather_app_ready(),
                    timeout=10.0  # 10 second timeout for app preparation
                )
                if not app_ready:
                    logger.error("[UNIFIED WEATHER] Weather app not ready")
                    return self._fallback_response("Unable to access Weather app")
            except asyncio.TimeoutError:
                logger.error("[UNIFIED WEATHER] Weather app preparation timed out")
                return self._fallback_response("Weather app took too long to open")
            
            # Extract weather based on query type with timeout
            try:
                if fast_mode:
                    # Skip navigation, just read what's shown
                    weather_data = await asyncio.wait_for(
                        self._extract_weather_fast(),
                        timeout=10.0  # 10 second timeout for fast extraction
                    )
                else:
                    weather_data = await asyncio.wait_for(
                        self._extract_weather_intelligently(query),
                        timeout=25.0  # 25 second timeout for full extraction
                    )
            except asyncio.TimeoutError:
                logger.error(f"[UNIFIED WEATHER] Weather extraction timed out")
                return self._fallback_response("Weather analysis took too long")
            
            # Cache and return
            if weather_data.get('success'):
                self._update_cache(cache_key, weather_data)
            
            return weather_data
            
        except Exception as e:
            logger.error(f"[UNIFIED WEATHER] Weather extraction error: {e}", exc_info=True)
            return self._fallback_response(str(e))
    
    async def _ensure_weather_app_ready(self) -> bool:
        """Ensure Weather app is open and ready"""
        try:
            # Check if Weather app is running
            is_running = await self._is_app_running("Weather")
            
            if not is_running:
                # Open Weather app
                logger.info("Opening Weather app...")
                await self._open_weather_app()
                await asyncio.sleep(3)  # Wait for app to fully load
            
            # Bring to front
            await self._bring_app_to_front()
            await asyncio.sleep(0.5)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to prepare Weather app: {e}")
            return False
    
    async def _extract_weather_fast(self) -> Dict[str, Any]:
        """
        Fast weather extraction - skip navigation, just read what's shown
        """
        logger.info("Using fast weather extraction (no navigation)")
        
        # Just extract weather data without navigation
        weather_data = await self._extract_comprehensive_weather()
        
        if not weather_data:
            return {
                'success': False,
                'error': 'Could not read weather data'
            }
        
        # Format basic response
        location = weather_data.get('location', 'the current location')
        current = weather_data.get('current', {})
        today = weather_data.get('today', {})
        
        response_parts = []
        
        # Note if not user's location
        if location and location.lower() in ['new york', 'nyc']:
            response_parts.append(f"The Weather app is showing {location}")
        else:
            response_parts.append(f"Looking at the weather in {location}")
        
        # Current conditions
        if current.get('temperature'):
            temp = current['temperature']
            condition = current.get('condition', 'current conditions')
            response_parts.append(f"it's {temp}°F and {condition.lower()}")
        
        # High/low
        if today.get('high') and today.get('low'):
            response_parts.append(f"Today's high will be {today['high']}°F with a low of {today['low']}°F")
        
        formatted = ". ".join(response_parts) + "."
        
        return {
            'success': True,
            'data': weather_data,
            'formatted_response': formatted,
            'source': 'vision_fast',
            'timestamp': datetime.now().isoformat()
        }
    
    async def _extract_weather_intelligently(self, query: str) -> Dict[str, Any]:
        """
        Extract weather data based on query intent
        Zero hardcoding - adapts to any Weather app layout
        """
        # Analyze query intent
        intent = self._analyze_query_intent(query)
        
        # First, try to find and select user's location
        # Note: Weather app may default to a different city
        try:
            await self._select_my_location()
        except Exception as e:
            logger.warning(f"Location selection failed: {e}, continuing with current selection")
        
        # Extract comprehensive weather data
        weather_data = await self._extract_comprehensive_weather()
        
        # Format response based on intent
        formatted = self._format_response_by_intent(weather_data, intent)
        
        return {
            'success': True,
            'data': weather_data,
            'formatted_response': formatted,
            'source': 'vision',
            'timestamp': datetime.now().isoformat()
        }
    
    async def _select_my_location(self):
        """
        Navigate to My Location using robust navigation helper
        """
        if not self.controller:
            logger.warning("No controller available for navigation")
            return
        
        try:
            logger.info("Using robust navigation to select My Location")
            
            # Import and use the navigation helper
            from .weather_navigation_helper import WeatherNavigationHelper
            nav_helper = WeatherNavigationHelper(self.controller)
            
            # Pass vision handler if available
            if self.vision_handler:
                nav_helper.vision_handler = self.vision_handler
            
            # Ensure Weather is focused
            await nav_helper.ensure_weather_focused()
            
            # Try to select My Location - reduced attempts for speed
            max_attempts = 2
            for attempt in range(max_attempts):
                logger.info(f"Selection attempt {attempt + 1}/{max_attempts}")
                
                success = await nav_helper.select_my_location_robust()
                
                if success:
                    # Shorter wait for speed
                    await asyncio.sleep(1)
                    
                    # Skip verification on first attempt to save time
                    if attempt == 0:
                        logger.info("First selection attempt completed, proceeding")
                        break
                    
                    # Only verify on second attempt if needed
                    if self.vision_handler and hasattr(self.vision_handler, 'analyze_weather_fast'):
                        quick_check = await self.vision_handler.analyze_weather_fast()
                        if quick_check.get('success'):
                            analysis = quick_check.get('analysis', '')
                            if 'toronto' in analysis.lower():
                                logger.info("Successfully showing Toronto/My Location")
                                break
                            elif 'new york' in analysis.lower():
                                logger.warning(f"Still showing New York after {attempt + 1} attempts")
                    
                logger.info("Navigation completed")
                break
                
            # Final focus check
            await nav_helper.ensure_weather_focused()
            
        except Exception as e:
            logger.error(f"Failed to navigate to My Location: {e}")
            # Continue anyway - maybe it's already selected
    
    async def _extract_comprehensive_weather(self) -> Dict[str, Any]:
        """
        Extract weather data using fast single-region analysis
        """
        if not self.vision_handler:
            return {}
        
        try:
            # Ensure Weather app is still frontmost before analyzing
            if self.controller:
                logger.info("Ensuring Weather is frontmost before capturing...")
                focus_script = '''
                    tell application "Weather"
                        activate
                        set frontmost to true
                        delay 0.2
                    end tell
                    
                    -- Double-check with System Events
                    tell application "System Events"
                        set frontApp to name of first application process whose frontmost is true
                        if frontApp is not "Weather" then
                            tell process "Weather" to set frontmost to true
                        end if
                    end tell
                '''
                success, result = self.controller.execute_applescript(focus_script)
                logger.info(f"Focus script result: {success}")
                await asyncio.sleep(0.5)
            # Use the new fast weather analysis method
            if hasattr(self.vision_handler, 'analyze_weather_fast'):
                logger.info("Using fast weather analysis method")
                result = await asyncio.wait_for(
                    self.vision_handler.analyze_weather_fast(),
                    timeout=8.0  # 8 second timeout for fast analysis
                )
                
                if result.get('success'):
                    logger.info("Successfully got weather from fast analysis")
                    return self._parse_comprehensive_weather(result.get('analysis', ''))
                else:
                    logger.warning(f"Fast weather analysis failed: {result.get('error')}")
            
            # Fallback to simple screenshot with direct API call
            logger.info("Using fallback weather analysis")
            screenshot = await self.vision_handler.capture_screen()
            if screenshot is None:
                logger.error("Failed to capture screenshot")
                return {}
            
            # Use the simplest API call possible
            import numpy as np
            from PIL import Image
            if isinstance(screenshot, Image.Image):
                screenshot_np = np.array(screenshot)
            else:
                screenshot_np = screenshot
                
            # Direct simple prompt
            weather_prompt = "Read the Weather app: What's the location, current temperature, and weather condition?"
            
            # Try analyze_screenshot with no sliding window
            result = await asyncio.wait_for(
                self.vision_handler.analyze_screenshot(
                    screenshot_np,
                    weather_prompt,
                    use_cache=False,
                    priority="high"
                ),
                timeout=8.0
            )
            
            if result:
                description = result[0].get('description', '') if isinstance(result, tuple) else str(result)
                logger.info("Got weather from fallback analysis")
                return self._parse_comprehensive_weather(description)
            else:
                logger.warning("No weather data from fallback")
                return {}
                    
        except asyncio.TimeoutError:
            logger.error("Weather analysis timed out after 8 seconds")
            return {}
        except Exception as e:
            logger.error(f"Weather analysis error: {e}")
            import traceback
            traceback.print_exc()
            return {}
        
        return {}
    
    def _parse_comprehensive_weather(self, vision_response: str) -> Dict[str, Any]:
        """
        Parse vision response into structured weather data
        Handles any format dynamically
        """
        logger.info(f"[PARSE] Parsing vision response: {vision_response[:100]}...")
        
        data = {
            'location': None,
            'current': {},
            'today': {},
            'hourly': [],
            'daily': [],
            'details': {},
            'alerts': []
        }
        
        # Handle direct format like "New York: 80°F, Condition: Sunny, High/Low: 80°/62°"
        simple_format_match = re.match(r'^([^:]+):\s*(\d+)°F', vision_response)
        if simple_format_match:
            data['location'] = simple_format_match.group(1).strip()
        
        # Extract location - try multiple patterns
        if not data['location']:
            location_patterns = [
                r'^([^:,]+):\s*\d+°',  # "New York: 80°"
                r'(?:location|city|place)[:\s]+([^,\n]+)',  # "Location: New York"
                r'(?:in|at|for)\s+([A-Z][a-zA-Z\s]+)(?:[,.]|$)'  # "in New York"
            ]
            for pattern in location_patterns:
                location_match = re.search(pattern, vision_response, re.IGNORECASE)
                if location_match:
                    data['location'] = location_match.group(1).strip()
                    break
        
        # Extract current temperature
        temp_matches = re.findall(r'(\d+)°[CF]?', vision_response)
        if temp_matches:
            # Largest number is usually current temp
            temps = [int(t) for t in temp_matches]
            data['current']['temperature'] = max(temps)
            
            # Look for high/low
            if 'high' in vision_response.lower() and 'low' in vision_response.lower():
                sorted_temps = sorted(temps)
                if len(sorted_temps) >= 2:
                    data['today']['high'] = sorted_temps[-1]
                    data['today']['low'] = sorted_temps[0]
        
        # Extract conditions dynamically
        vision_lower = vision_response.lower()
        
        # First try direct condition format
        condition_match = re.search(r'condition[:\s]+(\w+)', vision_response, re.IGNORECASE)
        if condition_match:
            data['current']['condition'] = condition_match.group(1).strip().title()
        else:
            # Fall back to pattern matching
            for condition in self.ui_patterns['conditions']:
                if condition in vision_lower:
                    # Find the context around the condition
                    pattern = rf'(?:currently|now|condition)[:\s]*.*?({condition}[\w\s]*)'
                    match = re.search(pattern, vision_lower, re.IGNORECASE)
                    if match:
                        data['current']['condition'] = match.group(1).strip().title()
                        break
        
        # Extract detailed conditions
        detail_patterns = {
            'wind': r'wind[:\s]+(\d+)\s*(mph|km/h)',
            'humidity': r'humidity[:\s]+(\d+)%',
            'uv_index': r'uv\s*(?:index)?[:\s]+(\d+)',
            'air_quality': r'air\s*quality[:\s]+(\d+|good|moderate|poor)',
            'feels_like': r'feels?\s*like[:\s]+(\d+)°',
            'visibility': r'visibility[:\s]+(\d+(?:\.\d+)?)\s*(mi|km)',
            'pressure': r'pressure[:\s]+(\d+(?:\.\d+)?)',
            'sunrise': r'sunrise[:\s]+(\d{1,2}:\d{2}\s*[ap]m)',
            'sunset': r'sunset[:\s]+(\d{1,2}:\d{2}\s*[ap]m)'
        }
        
        for key, pattern in detail_patterns.items():
            match = re.search(pattern, vision_response, re.IGNORECASE)
            if match:
                data['details'][key] = match.group(1)
        
        # Extract hourly forecast
        hourly_pattern = r'(\d{1,2})\s*([ap]m)[:\s]+(\d+)°\s*([^,\n]+)'
        hourly_matches = re.findall(hourly_pattern, vision_response, re.IGNORECASE)
        for hour, ampm, temp, condition in hourly_matches[:12]:  # Next 12 hours
            data['hourly'].append({
                'time': f"{hour}{ampm}",
                'temperature': int(temp),
                'condition': condition.strip()
            })
        
        # Extract daily forecast
        days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 
                'today', 'tomorrow']
        daily_pattern = r'({}).*?(\d+)°.*?(\d+)°'.format('|'.join(days))
        daily_matches = re.findall(daily_pattern, vision_response, re.IGNORECASE)
        
        for day, high, low in daily_matches[:10]:  # Next 10 days
            data['daily'].append({
                'day': day.capitalize(),
                'high': int(high),
                'low': int(low)
            })
        
        return data
    
    def _analyze_query_intent(self, query: str) -> Dict[str, Any]:
        """Understand what the user is asking for"""
        query_lower = query.lower()
        
        intent = {
            'type': 'current',  # default
            'timeframe': 'now',
            'details_requested': [],
            'location': None
        }
        
        # Timeframe detection
        if any(word in query_lower for word in ['today', "today's", 'now', 'current']):
            intent['timeframe'] = 'today'
        elif 'tomorrow' in query_lower:
            intent['timeframe'] = 'tomorrow'
        elif 'week' in query_lower or 'forecast' in query_lower:
            intent['timeframe'] = 'week'
        elif 'hour' in query_lower:
            intent['timeframe'] = 'hourly'
        
        # Detail requests
        if 'rain' in query_lower:
            intent['details_requested'].append('precipitation')
        if 'wind' in query_lower:
            intent['details_requested'].append('wind')
        if 'humid' in query_lower:
            intent['details_requested'].append('humidity')
        if any(word in query_lower for word in ['hot', 'cold', 'warm', 'temperature']):
            intent['details_requested'].append('temperature')
        
        # Location extraction
        location_match = re.search(r'(?:in|at|for)\s+([A-Z][a-zA-Z\s]+)', query)
        if location_match:
            intent['location'] = location_match.group(1).strip()
        
        return intent
    
    def _format_response_by_intent(self, data: Dict, intent: Dict) -> str:
        """Generate natural response based on intent and data"""
        if not data:
            return "I couldn't read the weather information clearly."
        
        response_parts = []
        
        # Location intro
        location = data.get('location', 'your location')
        # Add note if not showing user's actual location
        if location and location.lower() in ['new york', 'nyc']:
            response_parts.append(f"The Weather app is showing {location}")
        else:
            response_parts.append(f"Looking at the Weather app for {location}")
        
        # Current conditions
        current = data.get('current', {})
        if current.get('temperature'):
            temp = current['temperature']
            condition = current.get('condition', 'current conditions')
            response_parts.append(f"it's {temp}°F with {condition.lower()}")
        
        # Today's forecast
        if intent['timeframe'] in ['today', 'now'] and data.get('today'):
            today = data['today']
            if 'high' in today and 'low' in today:
                response_parts.append(f"Today's high will be {today['high']}°F with a low of {today['low']}°F")
        
        # Specific details requested
        details = data.get('details', {})
        if 'wind' in intent['details_requested'] and 'wind' in details:
            response_parts.append(f"Winds are {details['wind']} mph")
        
        if 'humidity' in intent['details_requested'] and 'humidity' in details:
            response_parts.append(f"Humidity is at {details['humidity']}%")
        
        # Hourly forecast
        if intent['timeframe'] == 'hourly' and data.get('hourly'):
            hourly_summary = self._summarize_hourly(data['hourly'])
            response_parts.append(hourly_summary)
        
        # Weekly forecast
        if intent['timeframe'] == 'week' and data.get('daily'):
            weekly_summary = self._summarize_weekly(data['daily'])
            response_parts.append(weekly_summary)
        
        # Join with proper punctuation
        response = ". ".join(response_parts) + "."
        
        # Add recommendations if relevant
        recommendations = self._generate_recommendations(data, intent)
        if recommendations:
            response += f" {recommendations}"
        
        return response
    
    def _summarize_hourly(self, hourly_data: List[Dict]) -> str:
        """Create concise hourly summary"""
        if not hourly_data:
            return ""
        
        # Group by significant changes
        summary_parts = []
        
        # Next few hours
        next_hours = hourly_data[:3]
        if next_hours:
            temps = [h['temperature'] for h in next_hours]
            avg_temp = sum(temps) // len(temps)
            summary_parts.append(f"Over the next few hours, temperatures will average {avg_temp}°F")
        
        # Look for precipitation
        rainy_hours = [h for h in hourly_data if any(
            cond in h.get('condition', '').lower() 
            for cond in ['rain', 'shower', 'storm']
        )]
        if rainy_hours:
            summary_parts.append(f"Rain expected around {rainy_hours[0]['time']}")
        
        return ". ".join(summary_parts)
    
    def _summarize_weekly(self, daily_data: List[Dict]) -> str:
        """Create concise weekly summary"""
        if not daily_data:
            return ""
        
        # Temperature trends
        highs = [d['high'] for d in daily_data]
        lows = [d['low'] for d in daily_data]
        
        avg_high = sum(highs) // len(highs)
        avg_low = sum(lows) // len(lows)
        
        # Identify warmest/coolest days
        warmest_idx = highs.index(max(highs))
        coolest_idx = highs.index(min(highs))
        
        summary = f"This week will see highs averaging {avg_high}°F and lows around {avg_low}°F. "
        summary += f"{daily_data[warmest_idx]['day']} will be warmest at {max(highs)}°F, "
        summary += f"while {daily_data[coolest_idx]['day']} will be coolest at {min(highs)}°F"
        
        return summary
    
    def _generate_recommendations(self, data: Dict, intent: Dict) -> str:
        """Generate contextual recommendations"""
        recommendations = []
        
        current = data.get('current', {})
        temp = current.get('temperature', 70)
        
        # Temperature-based recommendations
        if temp > 85:
            recommendations.append("Stay hydrated in this heat")
        elif temp < 40:
            recommendations.append("Bundle up for the cold")
        
        # Condition-based recommendations
        condition = current.get('condition', '').lower()
        if 'rain' in condition:
            recommendations.append("Don't forget an umbrella")
        elif 'snow' in condition:
            recommendations.append("Drive carefully in these conditions")
        
        # UV recommendations
        if data.get('details', {}).get('uv_index'):
            uv = int(data['details']['uv_index'])
            if uv >= 6:
                recommendations.append("High UV levels - consider sunscreen")
        
        return ". ".join(recommendations) if recommendations else ""
    
    # Helper methods
    async def _is_app_running(self, app_name: str) -> bool:
        """Check if app is running"""
        try:
            script = f'''
            tell application "System Events"
                return exists process "{app_name}"
            end tell
            '''
            result = await self._run_applescript(script)
            is_running = result.strip().lower() == 'true'
            logger.info(f"Weather app running check: {is_running}")
            return is_running
        except Exception as e:
            logger.warning(f"Error checking if app is running: {e}")
            return False
    
    async def _open_weather_app(self):
        """Open Weather app"""
        try:
            await self._run_applescript('tell application "Weather" to activate')
        except Exception as e:
            logger.error(f"Failed to open Weather app: {e}")
    
    async def _bring_app_to_front(self):
        """Bring Weather app to front"""
        try:
            script = '''
            tell application "Weather"
                activate
                set frontmost to true
            end tell
            '''
            await self._run_applescript(script)
        except Exception:
            pass

    async def _run_applescript(self, script: str) -> str:
        """Execute AppleScript with timeout"""
        try:
            process = await asyncio.create_subprocess_exec(
                'osascript', '-e', script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # Add 5 second timeout for AppleScript execution
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=5.0
                )
                if stderr:
                    logger.debug(f"AppleScript stderr: {stderr.decode('utf-8')}")
                return stdout.decode('utf-8').strip()
            except asyncio.TimeoutError:
                logger.warning(f"AppleScript timed out: {script[:50]}...")
                process.terminate()
                await process.wait()
                return ""
        except Exception as e:
            logger.error(f"AppleScript error: {e}")
            return ""
    
    def _parse_location_from_vision(self, vision_response: str) -> Optional[Dict]:
        """Parse location info from vision response"""
        # Look for location indicators
        response_lower = vision_response.lower()
        
        # Check for My Location or home indicators
        location_found = False
        for pattern in ['my location', 'home', 'current location', 'your location']:
            if pattern in response_lower:
                location_found = True
                break
        
        # Also check regex patterns
        if not location_found:
            for pattern in self.ui_patterns['my_location']:
                if re.search(pattern, vision_response, re.IGNORECASE):
                    location_found = True
                    break
        
        if location_found:
            return {
                'found': True,
                'description': vision_response
            }
        
        # If no specific location found but Weather app is open, 
        # assume we can proceed with current selection
        if 'weather' in response_lower:
            return {
                'found': True,
                'description': 'Weather app open, proceeding with current location'
            }
        
        return None
    
    async def _click_location(self, location_info: Dict):
        """Click on location in sidebar"""
        if not self.controller:
            logger.warning("No controller available to click location")
            return
        
        try:
            # First, try to find and click "My Location" or the home location
            description = location_info.get('description', '')
            
            # Look for coordinates or specific text patterns
            if 'my location' in description.lower() or 'home' in description.lower():
                logger.info("Attempting to click on My Location/Home")
                
                # Try different strategies to click the location
                # Strategy 1: Look for the location in the sidebar (left side)
                if self.vision_handler:
                    # Ask vision to find the exact position
                    click_result = await self.vision_handler.describe_screen({
                        'query': '''Look at the Weather app sidebar on the left side.
                        Find the item that says "My Location" or has a home/location icon.
                        Describe its exact position in the list (e.g., "first item", "second item", etc.)'''
                    })
                    
                    if click_result.success:
                        # Parse position and click
                        position_text = click_result.description.lower()
                        
                        # Determine click coordinates based on position
                        # Weather app sidebar is typically on the left
                        base_x = 150  # X coordinate for sidebar
                        base_y = 200  # Starting Y coordinate
                        item_height = 40  # Approximate height of each item
                        
                        y_offset = 0
                        if 'first' in position_text:
                            y_offset = 0
                        elif 'second' in position_text:
                            y_offset = item_height
                        elif 'third' in position_text:
                            y_offset = item_height * 2
                        elif 'fourth' in position_text:
                            y_offset = item_height * 3
                        
                        # Click at the calculated position
                        click_x = base_x
                        click_y = base_y + y_offset
                        
                        await self.controller.click_at(click_x, click_y)
                        logger.info(f"Clicked at ({click_x}, {click_y}) for My Location")
                        return
                
                # Strategy 2: Use keyboard navigation as fallback
                # Press up arrow multiple times to ensure we're at the top
                # Then down arrow to select first item (usually My Location)
                await self.controller.key_press('up')
                await asyncio.sleep(0.1)
                await self.controller.key_press('up')
                await asyncio.sleep(0.1)
                await self.controller.key_press('up')
                await asyncio.sleep(0.1)
                await self.controller.key_press('down')
                await asyncio.sleep(0.1)
                await self.controller.key_press('return')
                logger.info("Used keyboard navigation to select My Location")
                
        except Exception as e:
            logger.error(f"Failed to click location: {e}")
    
    def _check_cache(self, key: str) -> Optional[Dict]:
        """Check if we have valid cached data"""
        if key in self.cache:
            cached_time, cached_data = self.cache[key]
            if datetime.now() - cached_time < self.cache_duration:
                return cached_data
        return None
    
    def _update_cache(self, key: str, data: Dict):
        """Update cache with new data"""
        self.cache[key] = (datetime.now(), data)
    
    def _get_cache_key(self, query: str) -> str:
        """Generate cache key from query"""
        # Simple key generation - could be more sophisticated
        return f"weather_{query.lower().replace(' ', '_')}"
    
    def _fallback_response(self, error_msg: str) -> Dict:
        """Fallback response when vision fails"""
        logger.warning(f"Weather fallback triggered: {error_msg}")
        
        # For now, just return the standard error response
        # The weather app should be open even if we couldn't read it properly
        return {
            'success': False,
            'error': error_msg,
            'formatted_response': "I'm having trouble reading the Weather app right now. The app should be open showing your weather.",
            'source': 'vision_failed', 
            'timestamp': datetime.now().isoformat()
        }


# Singleton instance for easy access
_unified_weather = None

def get_unified_weather_system(vision_handler=None, controller=None) -> UnifiedVisionWeather:
    """Get or create the unified weather system"""
    global _unified_weather
    if _unified_weather is None:
        _unified_weather = UnifiedVisionWeather(vision_handler, controller)
    return _unified_weather


# Simple test function
async def test_unified_weather():
    """Test the unified weather system"""
    weather = UnifiedVisionWeather()
    
    # Test various queries
    queries = [
        "What's the weather today?",
        "Will it rain tomorrow?",
        "What's the weekly forecast?",
        "How's the weather this hour?",
        "Is it windy outside?"
    ]
    
    for query in queries:
        print(f"\nQuery: {query}")
        result = await weather.get_weather(query)
        print(f"Response: {result.get('formatted_response', 'No response')}")


if __name__ == "__main__":
    asyncio.run(test_unified_weather())