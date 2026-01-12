"""
Direct macOS Weather App Integration
Uses AppleScript to get weather from the native Weather app
"""

import subprocess
import json
import logging
import re
from typing import Dict, Optional, Tuple
from datetime import datetime
import asyncio

logger = logging.getLogger(__name__)

class MacOSWeatherApp:
    """Direct integration with macOS Weather app using AppleScript"""
    
    def __init__(self):
        self._weather_cache = None
        self._cache_time = None
        self._cache_duration = 300  # 5 minutes
        
    async def get_weather_from_app(self) -> Optional[Dict]:
        """Get weather directly from macOS Weather app"""
        try:
            # AppleScript to get weather from the Weather app
            # This script opens Weather app and reads the current conditions
            script = '''
            tell application "System Events"
                -- Check if Weather app exists
                if not (exists application "Weather") then
                    return "Weather app not found"
                end if
            end tell
            
            -- Activate Weather app
            tell application "Weather"
                activate
                delay 1
            end tell
            
            -- Use UI scripting to read weather information
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    delay 0.5
                    
                    -- Get all UI elements text
                    set allText to ""
                    try
                        -- Try to get text from main window
                        tell window 1
                            set allElements to entire contents
                            repeat with elem in allElements
                                try
                                    if class of elem is static text then
                                        set elemText to value of elem as string
                                        if elemText is not "" and elemText is not missing value then
                                            set allText to allText & elemText & " | "
                                        end if
                                    end if
                                end try
                            end repeat
                        end tell
                    end try
                    
                    return allText
                end tell
            end tell
            '''
            
            # Run the AppleScript
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0 and result.stdout:
                weather_text = result.stdout.strip()
                
                # Parse the weather information
                weather_data = self._parse_weather_text(weather_text)
                
                if weather_data:
                    return weather_data
            
            return None
            
        except subprocess.TimeoutError:
            logger.error("Timeout accessing Weather app")
            return None
        except Exception as e:
            logger.error(f"Error accessing Weather app: {e}")
            return None
    
    def _parse_weather_text(self, text: str) -> Optional[Dict]:
        """Parse weather information from Weather app text"""
        try:
            # Split by delimiter
            parts = text.split(' | ')
            
            # Initialize weather data
            weather_data = {
                "source": "macOS Weather app",
                "timestamp": datetime.now().isoformat()
            }
            
            # Look for temperature (both F and C)
            temp_found = False
            for part in parts:
                # Match temperature patterns like "69°", "69°F", "20°C"
                temp_match = re.search(r'(\d+)°([FC])?', part)
                if temp_match:
                    temp_value = int(temp_match.group(1))
                    temp_unit = temp_match.group(2) if temp_match.group(2) else 'F'  # Default to F
                    
                    # Convert to Celsius if needed for consistency
                    if temp_unit == 'F':
                        weather_data["temperature_f"] = temp_value
                        weather_data["temperature"] = round((temp_value - 32) * 5/9)
                        weather_data["temperature_unit"] = "°F"
                    else:
                        weather_data["temperature"] = temp_value
                        weather_data["temperature_f"] = round(temp_value * 9/5 + 32)
                        weather_data["temperature_unit"] = "°C"
                    
                    temp_found = True
                    break
            
            # Look for location
            location_found = False
            for part in parts:
                # Common location patterns in Weather app
                if len(part) > 3 and not any(char in part for char in ['°', '%', 'mph', 'km/h']):
                    # Check if it looks like a location name
                    if part[0].isupper() and not part.isupper():
                        # Found what looks like a location
                        weather_data["location"] = part.strip()
                        location_found = True
                        break
            
            # Look for conditions
            condition_keywords = ['Clear', 'Cloudy', 'Partly', 'Mostly', 'Rain', 'Snow', 'Sunny', 
                                'Overcast', 'Fog', 'Mist', 'Thunder', 'Storm']
            for part in parts:
                for keyword in condition_keywords:
                    if keyword.lower() in part.lower():
                        weather_data["description"] = part.strip()
                        break
            
            # Look for other weather data
            for part in parts:
                # Humidity
                if '%' in part and 'humid' in part.lower():
                    humid_match = re.search(r'(\d+)%', part)
                    if humid_match:
                        weather_data["humidity"] = int(humid_match.group(1))
                
                # Wind speed
                if 'mph' in part or 'km/h' in part:
                    wind_match = re.search(r'(\d+)\s*(mph|km/h)', part)
                    if wind_match:
                        weather_data["wind_speed"] = float(wind_match.group(1))
                        weather_data["wind_unit"] = wind_match.group(2)
                
                # Feels like
                if 'feels like' in part.lower():
                    feels_match = re.search(r'(\d+)°', part)
                    if feels_match:
                        feels_temp = int(feels_match.group(1))
                        if weather_data.get("temperature_unit") == "°F":
                            weather_data["feels_like_f"] = feels_temp
                            weather_data["feels_like"] = round((feels_temp - 32) * 5/9)
                        else:
                            weather_data["feels_like"] = feels_temp
            
            # If we found temperature and some data, consider it valid
            if temp_found and (location_found or "description" in weather_data):
                return weather_data
            
            return None
            
        except Exception as e:
            logger.error(f"Error parsing weather text: {e}")
            return None
    
    async def get_current_location_from_system(self) -> Optional[str]:
        """Try to get current location from system"""
        try:
            # Try using location services via shortcuts or system
            script = '''
            tell application "System Events"
                -- Try to get location from Weather app's current location
                tell application "Weather"
                    activate
                    delay 0.5
                end tell
                
                tell process "Weather"
                    -- Look for location in window title or first element
                    try
                        set windowTitle to title of window 1
                        return windowTitle
                    end try
                end tell
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0 and result.stdout:
                # Extract location from window title
                location = result.stdout.strip()
                # Clean up common patterns
                location = location.replace("Weather", "").strip()
                if location and len(location) > 2:
                    return location
                    
        except Exception as e:
            logger.debug(f"Could not get location from system: {e}")
        
        return None
    
    async def _get_location_from_ip(self) -> str:
        """Get location from IP as fallback"""
        try:
            import aiohttp
            url = "http://ip-api.com/json/?fields=city,regionName,country"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=3) as response:
                    if response.status == 200:
                        data = await response.json()
                        city = data.get("city", "")
                        region = data.get("regionName", "")
                        if city and region:
                            return f"{city}, {region}"
                        elif city:
                            return city
        except Exception:
            pass
        return "your location"
    
    async def ensure_weather_app_ready(self) -> bool:
        """Ensure Weather app is running and ready"""
        try:
            # Check if Weather app is running
            check_script = '''
            tell application "System Events"
                if (exists process "Weather") then
                    return "running"
                else
                    return "not running"
                end if
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', check_script],
                capture_output=True,
                text=True
            )
            
            if "not running" in result.stdout:
                # Open Weather app
                subprocess.run(['open', '-a', 'Weather'], check=False)
                await asyncio.sleep(2)  # Give it time to open
                
            return True
            
        except Exception as e:
            logger.error(f"Error ensuring Weather app is ready: {e}")
            return False
    
    async def get_weather_with_location(self) -> Dict:
        """Get weather with proper location detection"""
        # Check cache first
        if self._weather_cache and self._cache_time:
            age = (datetime.now() - self._cache_time).seconds
            if age < self._cache_duration:
                return self._weather_cache
        
        # Ensure Weather app is ready
        await self.ensure_weather_app_ready()
        
        # Get weather from app
        weather_data = await self.get_weather_from_app()
        
        if weather_data:
            # Try to get better location if not found
            if "location" not in weather_data or weather_data["location"] == "":
                location = await self.get_current_location_from_system()
                if location:
                    weather_data["location"] = location
                else:
                    # Get location from IP as fallback
                    weather_data["location"] = await self._get_location_from_ip()
            
            # Cache the result
            self._weather_cache = weather_data
            self._cache_time = datetime.now()
            
            return weather_data
        
        # Return fallback with dynamic location
        fallback_location = await self._get_location_from_ip()
        return {
            "location": fallback_location,
            "temperature": 20,
            "temperature_f": 68,
            "temperature_unit": "°F",
            "description": "Weather data temporarily unavailable",
            "source": "fallback",
            "message": "Unable to access Weather app. Please ensure it's installed and has location permissions.",
            "timestamp": datetime.now().isoformat()
        }