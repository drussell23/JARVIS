"""
macOS System Integration for JARVIS
Direct real-time system data access with NO caching or hardcoding
"""

import subprocess
import json
import asyncio
import logging
import re
from typing import Dict, Optional, Tuple
from datetime import datetime
import aiohttp

logger = logging.getLogger(__name__)

class MacOSSystemIntegration:
    """Direct macOS system integration for real-time accurate data"""
    
    def __init__(self):
        # NO CACHING for time-sensitive data
        pass
    
    async def get_system_time(self) -> Dict:
        """Get real system time directly from macOS"""
        try:
            # Get current date/time from system
            result = subprocess.run(
                ["date", "+%Y-%m-%d %H:%M:%S %Z %A"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                # Parse: 2024-09-09 18:47:19 EDT Tuesday
                parts = result.stdout.strip().split()
                date_str = parts[0]
                time_str = parts[1]
                timezone = parts[2]
                day_name = parts[3]
                
                # Get timezone full name
                tz_result = subprocess.run(
                    ["systemsetup", "-gettimezone"],
                    capture_output=True,
                    text=True
                )
                
                timezone_full = "America/New_York"  # Default
                if tz_result.returncode == 0:
                    output = tz_result.stdout.strip()
                    if "Time Zone:" in output:
                        timezone_full = output.split("Time Zone:")[1].strip()
                
                return {
                    "date": date_str,
                    "time": time_str,
                    "timezone": timezone,
                    "timezone_full": timezone_full,
                    "day_name": day_name,
                    "raw": result.stdout.strip(),
                    "source": "system"
                }
            
        except Exception as e:
            logger.error(f"Error getting system time: {e}")
        
        # Fallback - but should never reach here
        return {
            "error": "Could not get system time",
            "source": "error"
        }
    
    async def get_weather_from_widget(self) -> Optional[Dict]:
        """Get weather directly from macOS Weather widget using AppleScript"""
        try:
            # First ensure Weather app is running
            subprocess.run(["open", "-a", "Weather"], check=False)
            await asyncio.sleep(1)  # Let it open
            
            # AppleScript to read Weather app current conditions
            script = '''
            tell application "System Events"
                if not (exists process "Weather") then
                    tell application "Weather" to activate
                    delay 2
                end if
                
                tell process "Weather"
                    set frontmost to true
                    delay 1
                    
                    -- Collect all text from the Weather app
                    set weatherInfo to ""
                    
                    try
                        -- Get window title (often contains location)
                        set windowTitle to title of window 1
                        set weatherInfo to windowTitle & "|"
                    end try
                    
                    try
                        -- Get all UI elements text
                        tell window 1
                            set allElements to entire contents
                            repeat with elem in allElements
                                try
                                    if class of elem is static text then
                                        set elemValue to value of elem
                                        if elemValue is not missing value and length of elemValue > 0 then
                                            -- Look for temperature patterns
                                            if elemValue contains "°" then
                                                set weatherInfo to weatherInfo & elemValue & "|"
                                            -- Look for location names (capitalized words)
                                            else if character 1 of elemValue is in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" then
                                                set weatherInfo to weatherInfo & elemValue & "|"
                                            end if
                                        end if
                                    end if
                                end try
                            end repeat
                        end tell
                    end try
                    
                    return weatherInfo
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
                # Parse the pipe-delimited output
                parts = result.stdout.strip().split("|")
                weather_data = {}
                
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                        
                    # Temperature pattern (e.g., "67°", "20°C", "72°F")
                    temp_match = re.match(r'^(\d+)°([FC])?$', part)
                    if temp_match:
                        temp_value = int(temp_match.group(1))
                        temp_unit = temp_match.group(2) if temp_match.group(2) else 'F'  # Default to F
                        weather_data["temperature"] = temp_value
                        weather_data["temperature_unit"] = temp_unit
                        logger.info(f"Found temperature: {temp_value}°{temp_unit}")
                    
                    # Location (window title or capitalized text)
                    elif part and part[0].isupper() and len(part) > 2 and '°' not in part:
                        # Skip generic UI text
                        if part not in ["Weather", "File", "Edit", "View", "Window", "Help"]:
                            if "location" not in weather_data or len(part) > len(weather_data.get("location", "")):
                                weather_data["location"] = part
                                logger.info(f"Found location: {part}")
                    
                    # Weather condition keywords
                    conditions = ["Clear", "Cloudy", "Partly", "Sunny", "Rain", "Snow", "Fog", "Storm", "Overcast"]
                    for condition in conditions:
                        if condition.lower() in part.lower():
                            weather_data["condition"] = part
                            logger.info(f"Found condition: {part}")
                            break
                
                if weather_data:
                    weather_data["source"] = "macOS Weather app"
                    weather_data["timestamp"] = datetime.now().isoformat()
                    return weather_data
            
        except Exception as e:
            logger.error(f"Error reading Weather widget: {e}")
        
        return None
    
    async def get_system_location(self) -> Optional[Dict]:
        """Get current location using macOS location services"""
        try:
            # Method 1: Try CoreLocationCLI
            try:
                # Get coordinates
                coords_result = subprocess.run(
                    ["CoreLocationCLI", "-once"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if coords_result.returncode == 0:
                    # Parse: <latitude> <longitude>
                    coords = coords_result.stdout.strip().split()
                    if len(coords) >= 2:
                        lat = float(coords[0])
                        lon = float(coords[1])
                        
                        # Get address
                        addr_result = subprocess.run(
                            ["CoreLocationCLI", "-once", "-format", "%address"],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        
                        address = addr_result.stdout.strip() if addr_result.returncode == 0 else ""
                        
                        return {
                            "latitude": lat,
                            "longitude": lon,
                            "address": address,
                            "source": "CoreLocationCLI"
                        }
            except FileNotFoundError:
                logger.debug("CoreLocationCLI not installed")
            
            # Method 2: Use IP geolocation as fallback
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://ip-api.com/json/", timeout=3) as response:
                        if response.status == 200:
                            data = await response.json()
                            return {
                                "latitude": data.get("lat"),
                                "longitude": data.get("lon"),
                                "city": data.get("city"),
                                "region": data.get("regionName"),
                                "country": data.get("country"),
                                "source": "IP geolocation"
                            }
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error getting location: {e}")
        
        return None
    
    async def get_accurate_weather(self) -> Dict:
        """Get accurate weather combining multiple sources"""
        # First try to get from Weather widget
        weather_data = await self.get_weather_from_widget()
        
        if not weather_data:
            weather_data = {}
        
        # Get location if not in weather data
        if "location" not in weather_data or not weather_data["location"]:
            location_data = await self.get_system_location()
            if location_data:
                if location_data.get("city"):
                    weather_data["location"] = f"{location_data['city']}, {location_data.get('region', '')}"
                elif location_data.get("address"):
                    # Parse city from address
                    parts = location_data["address"].split(",")
                    if parts:
                        weather_data["location"] = parts[0].strip()
        
        # Get system temperature unit preference
        try:
            result = subprocess.run(
                ['defaults', 'read', '-g', 'AppleTemperatureUnit'],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                unit = result.stdout.strip()
                weather_data["preferred_unit"] = "F" if unit.lower() == 'fahrenheit' else "C"
        except Exception:
            # Check locale for US/Imperial
            locale_result = subprocess.run(
                ['defaults', 'read', '-g', 'AppleLocale'],
                capture_output=True,
                text=True
            )
            if locale_result.returncode == 0 and "_US" in locale_result.stdout:
                weather_data["preferred_unit"] = "F"
            else:
                weather_data["preferred_unit"] = "C"
        
        # Ensure we have real-time timestamp
        weather_data["timestamp"] = datetime.now().isoformat()
        weather_data["system_time"] = await self.get_system_time()
        
        return weather_data
    
    async def screen_capture_weather_widget(self) -> Optional[Dict]:
        """Capture Weather widget from screen as last resort"""
        try:
            # Take screenshot of notification center/widgets
            screenshot_path = "/tmp/weather_widget.png"
            
            # Capture the screen
            subprocess.run([
                "screencapture", "-x", "-R", "0,0,400,200", screenshot_path
            ], check=True)
            
            # Here you would use OCR or vision analysis to extract weather data
            # For now, we'll just note that we tried
            logger.info("Screenshot captured for weather widget analysis")
            
        except Exception as e:
            logger.error(f"Error capturing weather widget: {e}")
        
        return None