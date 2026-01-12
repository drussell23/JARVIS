"""
Weather Widget Extractor for JARVIS
Extracts real-time weather data directly from macOS weather widget
"""

import subprocess
import re
import json
import asyncio
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime
import os

logger = logging.getLogger(__name__)

class WeatherWidgetExtractor:
    """Extract weather data from macOS weather widget using multiple methods"""
    
    def __init__(self):
        self.widget_data_cache = None
        self.last_extraction_time = None
        
    async def extract_weather_data(self) -> Optional[Dict]:
        """Extract weather data from widget using best available method"""
        methods = [
            self._extract_via_accessibility,
            self._extract_via_notification_center,
            self._extract_via_weather_app_ui,
            self._extract_via_system_events
        ]
        
        for method in methods:
            try:
                data = await method()
                if data and self._validate_weather_data(data):
                    logger.info(f"Successfully extracted weather data using {method.__name__}")
                    return data
            except Exception as e:
                logger.debug(f"Method {method.__name__} failed: {e}")
                continue
        
        return None
    
    async def _extract_via_accessibility(self) -> Optional[Dict]:
        """Extract weather data using Accessibility Inspector approach"""
        try:
            # AppleScript to read weather widget via accessibility
            script = '''
            tell application "System Events"
                -- Open Notification Center
                tell application "System Events" to key code 111 using {command down, option down}
                delay 1
                
                -- Look for weather widget elements
                tell process "NotificationCenter"
                    set weatherData to {}
                    
                    -- Find all static text elements
                    set allText to value of every static text of every UI element
                    
                    repeat with textItem in allText
                        if textItem contains "°" then
                            -- Temperature found
                            set weatherData to weatherData & {temperature: textItem}
                        else if textItem contains "Toronto" or textItem contains "My Location" then
                            -- Location found
                            set weatherData to weatherData & {location: textItem}
                        else if textItem contains "Cloudy" or textItem contains "Sunny" or textItem contains "Rainy" then
                            -- Condition found
                            set weatherData to weatherData & {condition: textItem}
                        end if
                    end repeat
                    
                    -- Close Notification Center
                    tell application "System Events" to key code 111 using {command down, option down}
                    
                    return weatherData as string
                end tell
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                return self._parse_applescript_output(result.stdout)
                
        except Exception as e:
            logger.debug(f"Accessibility extraction failed: {e}")
        
        return None
    
    async def _extract_via_notification_center(self) -> Optional[Dict]:
        """Extract from Notification Center widgets"""
        try:
            # Use shortcuts CLI to get weather if available
            result = subprocess.run(
                ['shortcuts', 'run', 'Get Current Weather'],
                capture_output=True,
                text=True,
                timeout=3
            )
            
            if result.returncode == 0:
                # Parse shortcuts output
                return self._parse_shortcuts_output(result.stdout)
                
        except FileNotFoundError:
            logger.debug("Shortcuts CLI not available")
        except Exception as e:
            logger.debug(f"Notification Center extraction failed: {e}")
        
        return None
    
    async def _extract_via_weather_app_ui(self) -> Optional[Dict]:
        """Extract directly from Weather app UI"""
        try:
            # Ensure Weather app is open
            subprocess.run(['open', '-a', 'Weather'], check=False)
            await asyncio.sleep(1)
            
            # Enhanced AppleScript to read all weather data
            script = '''
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    delay 0.5
                    
                    -- Collect all weather information
                    set weatherInfo to {}
                    
                    -- Get all text from the window
                    tell window 1
                        set allElements to entire contents
                        set textValues to {}
                        
                        repeat with elem in allElements
                            try
                                if class of elem is static text then
                                    set elemValue to value of elem
                                    if elemValue is not missing value then
                                        set end of textValues to elemValue
                                    end if
                                end if
                            end try
                        end repeat
                    end tell
                    
                    -- Parse collected text
                    set tempFound to false
                    set locationFound to false
                    set conditionFound to false
                    
                    repeat with txt in textValues
                        -- Temperature (e.g., "66°", "19°C")
                        if txt contains "°" and not tempFound then
                            set weatherInfo to weatherInfo & {temperature: txt}
                            set tempFound to true
                        
                        -- Location (capitalized multi-word strings)
                        else if (count words of txt) > 0 and (count words of txt) < 5 then
                            if character 1 of txt is in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" then
                                if not (txt is in {"Weather", "File", "Edit", "View", "Window", "Help"}) then
                                    if not locationFound then
                                        set weatherInfo to weatherInfo & {location: txt}
                                        set locationFound to true
                                    end if
                                end if
                            end if
                        
                        -- Weather conditions
                        else if txt contains "Partly" or txt contains "Cloudy" or txt contains "Clear" or txt contains "Sunny" then
                            if not conditionFound then
                                set weatherInfo to weatherInfo & {condition: txt}
                                set conditionFound to true
                            end if
                        end if
                    end repeat
                    
                    return weatherInfo as string
                end tell
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                return self._parse_applescript_output(result.stdout)
                
        except Exception as e:
            logger.debug(f"Weather app UI extraction failed: {e}")
        
        return None
    
    async def _extract_via_system_events(self) -> Optional[Dict]:
        """Extract using System Events and UI scripting"""
        try:
            # Get weather from menu bar if available
            script = '''
            tell application "System Events"
                -- Check for weather in menu bar extras
                tell application process "ControlCenter"
                    try
                        -- Click on weather in menu bar
                        click menu bar item "Weather" of menu bar 1
                        delay 0.5
                        
                        -- Get weather info from popup
                        set weatherText to value of every static text of window 1
                        
                        -- Click again to close
                        click menu bar item "Weather" of menu bar 1
                        
                        return weatherText as string
                    end try
                end tell
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=3
            )
            
            if result.returncode == 0:
                return self._parse_menu_bar_output(result.stdout)
                
        except Exception as e:
            logger.debug(f"System Events extraction failed: {e}")
        
        return None
    
    def _parse_applescript_output(self, output: str) -> Optional[Dict]:
        """Parse AppleScript output into weather data"""
        try:
            weather_data = {}
            
            # Clean output
            output = output.strip()
            
            # Parse temperature
            temp_match = re.search(r'temperature:([^,}]+)', output)
            if temp_match:
                temp_str = temp_match.group(1).strip()
                temp_num_match = re.search(r'(\d+)°([FC])?', temp_str)
                if temp_num_match:
                    temp_value = int(temp_num_match.group(1))
                    temp_unit = temp_num_match.group(2) if temp_num_match.group(2) else 'F'
                    
                    if temp_unit == 'F':
                        weather_data['temperature_f'] = temp_value
                        weather_data['temperature'] = round((temp_value - 32) * 5/9)
                    else:
                        weather_data['temperature'] = temp_value
                        weather_data['temperature_f'] = round(temp_value * 9/5 + 32)
                    
                    weather_data['temperature_unit'] = f'°{temp_unit}'
            
            # Parse location
            loc_match = re.search(r'location:([^,}]+)', output)
            if loc_match:
                location = loc_match.group(1).strip().strip('"')
                # Clean up location
                if "My Location" in location:
                    # Extract actual city name if present
                    parts = location.split("•")
                    if len(parts) > 0:
                        location = parts[0].strip()
                weather_data['location'] = location
            
            # Parse condition
            cond_match = re.search(r'condition:([^,}]+)', output)
            if cond_match:
                condition = cond_match.group(1).strip().strip('"')
                weather_data['condition'] = condition
                weather_data['description'] = condition.lower()
            
            if weather_data:
                weather_data['source'] = 'weather_widget'
                weather_data['timestamp'] = datetime.now().isoformat()
                return weather_data
                
        except Exception as e:
            logger.error(f"Error parsing AppleScript output: {e}")
        
        return None
    
    def _parse_shortcuts_output(self, output: str) -> Optional[Dict]:
        """Parse Shortcuts app output"""
        try:
            weather_data = {}
            lines = output.strip().split('\n')
            
            for line in lines:
                if '°' in line:
                    # Temperature line
                    temp_match = re.search(r'(\d+)°([FC])?', line)
                    if temp_match:
                        temp_value = int(temp_match.group(1))
                        temp_unit = temp_match.group(2) if temp_match.group(2) else 'F'
                        
                        if temp_unit == 'F':
                            weather_data['temperature_f'] = temp_value
                            weather_data['temperature'] = round((temp_value - 32) * 5/9)
                        else:
                            weather_data['temperature'] = temp_value
                            weather_data['temperature_f'] = round(temp_value * 9/5 + 32)
                            
                elif any(cond in line.lower() for cond in ['clear', 'cloudy', 'rain', 'snow', 'sunny']):
                    weather_data['condition'] = line.strip()
                    weather_data['description'] = line.strip().lower()
                elif line and len(line.split()) <= 3:
                    # Might be location
                    weather_data['location'] = line.strip()
            
            if weather_data:
                weather_data['source'] = 'shortcuts'
                weather_data['timestamp'] = datetime.now().isoformat()
                return weather_data
                
        except Exception as e:
            logger.error(f"Error parsing shortcuts output: {e}")
        
        return None
    
    def _parse_menu_bar_output(self, output: str) -> Optional[Dict]:
        """Parse menu bar weather output"""
        try:
            weather_data = {}
            
            # Parse comma-separated values from menu bar
            parts = output.split(',')
            
            for part in parts:
                part = part.strip()
                
                if '°' in part:
                    temp_match = re.search(r'(\d+)°([FC])?', part)
                    if temp_match:
                        temp_value = int(temp_match.group(1))
                        temp_unit = temp_match.group(2) if temp_match.group(2) else 'F'
                        
                        if temp_unit == 'F':
                            weather_data['temperature_f'] = temp_value
                            weather_data['temperature'] = round((temp_value - 32) * 5/9)
                        else:
                            weather_data['temperature'] = temp_value
                            weather_data['temperature_f'] = round(temp_value * 9/5 + 32)
                            
                elif any(cond in part.lower() for cond in ['clear', 'cloudy', 'rain', 'snow', 'sunny', 'partly']):
                    weather_data['condition'] = part
                    weather_data['description'] = part.lower()
                elif part and part[0].isupper() and len(part.split()) <= 3:
                    weather_data['location'] = part
            
            if weather_data:
                weather_data['source'] = 'menu_bar'
                weather_data['timestamp'] = datetime.now().isoformat()
                return weather_data
                
        except Exception as e:
            logger.error(f"Error parsing menu bar output: {e}")
        
        return None
    
    def _validate_weather_data(self, data: Dict) -> bool:
        """Validate extracted weather data"""
        # Must have at least temperature or condition
        has_temp = 'temperature' in data or 'temperature_f' in data
        has_condition = 'condition' in data or 'description' in data
        
        return has_temp or has_condition
    
    async def get_weather_with_fallback(self) -> Dict:
        """Get weather data with intelligent fallbacks"""
        # Try extraction first
        weather_data = await self.extract_weather_data()
        
        if weather_data:
            # Enhance with location if missing
            if 'location' not in weather_data:
                location = await self._get_current_location()
                if location:
                    weather_data['location'] = location
            
            return weather_data
        
        # Return None to trigger other methods in weather bridge
        return None
    
    async def _get_current_location(self) -> Optional[str]:
        """Get current location from system"""
        try:
            # Try to get from system preferences
            result = subprocess.run(
                ['defaults', 'read', 'com.apple.weather', 'WeatherCities'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                # Parse the plist output to find current city
                output = result.stdout
                # Look for city names in the output
                city_match = re.search(r'"name" = "([^"]+)"', output)
                if city_match:
                    return city_match.group(1)
        except Exception:
            pass

        return None