"""
Vision-Based Weather Extractor for JARVIS
Extracts weather data by visually reading the macOS Weather app
Bypasses Core Location restrictions using computer vision
"""

import re
import logging
import subprocess
import asyncio
from typing import Dict, Optional, List, Tuple
from datetime import datetime
import json

logger = logging.getLogger(__name__)


class VisionWeatherExtractor:
    """Extract weather data using vision instead of GPS/APIs"""
    
    def __init__(self):
        self.weather_app_name = "Weather"
        self.last_extraction = None
        self.cache_duration = 300  # 5 minutes
        
    async def get_weather_from_vision(self) -> Optional[Dict]:
        """Main method to get weather using vision"""
        try:
            # First, ensure Weather app is running
            if not await self._is_weather_app_running():
                await self._open_weather_app()
                await asyncio.sleep(2)  # Wait for app to load
            
            # Extract weather data from screen
            weather_data = await self._extract_weather_data()
            
            if weather_data:
                # Cache the result
                self.last_extraction = {
                    'data': weather_data,
                    'timestamp': datetime.now()
                }
                logger.info(f"Extracted weather via vision: {weather_data.get('location')} - {weather_data.get('temperature')}°")
                
            return weather_data
            
        except Exception as e:
            logger.error(f"Vision weather extraction failed: {e}")
            return None
    
    async def _is_weather_app_running(self) -> bool:
        """Check if Weather app is running"""
        try:
            script = '''
            tell application "System Events"
                return exists process "Weather"
            end tell
            '''
            result = await self._run_applescript(script)
            return result.strip().lower() == 'true'
        except Exception:
            return False

    async def _open_weather_app(self):
        """Open Weather app"""
        try:
            script = 'tell application "Weather" to activate'
            await self._run_applescript(script)
            logger.info("Opened Weather app")
        except Exception as e:
            logger.error(f"Failed to open Weather app: {e}")
    
    async def _extract_weather_data(self) -> Optional[Dict]:
        """Extract weather data from Weather app using AppleScript"""
        try:
            # AppleScript to extract text from Weather app
            script = '''
            tell application "System Events"
                tell process "Weather"
                    set frontmost to true
                    delay 0.5
                    
                    -- Get all static text elements
                    set allTexts to value of every static text of window 1
                    
                    -- Get window title (often contains location)
                    set windowTitle to title of window 1
                    
                    -- Combine all text
                    set weatherInfo to windowTitle & "|"
                    repeat with txt in allTexts
                        if txt is not missing value then
                            set weatherInfo to weatherInfo & txt & "|"
                        end if
                    end repeat
                    
                    return weatherInfo
                end tell
            end tell
            '''
            
            result = await self._run_applescript(script)
            
            if result:
                return self._parse_weather_text(result)
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to extract weather data: {e}")
            return None
    
    def _parse_weather_text(self, text: str) -> Optional[Dict]:
        """Parse extracted text into structured weather data"""
        try:
            # Split text into components
            parts = [p.strip() for p in text.split('|') if p.strip()]
            
            weather_data = {
                'source': 'vision',
                'timestamp': datetime.now().isoformat()
            }
            
            # Extract location (usually first part or contains "Location")
            for part in parts:
                if 'location' in part.lower() or '•' in part:
                    weather_data['location'] = self._clean_location(part)
                    break
            else:
                # Use first part as location if no clear location found
                if parts:
                    weather_data['location'] = self._clean_location(parts[0])
            
            # Extract temperatures (look for ° symbol)
            temps = []
            for part in parts:
                if '°' in part:
                    # Extract number before degree symbol
                    temp_match = re.search(r'(\d+)°', part)
                    if temp_match:
                        temps.append(int(temp_match.group(1)))
            
            if temps:
                # First/largest temperature is usually current
                weather_data['temperature_f'] = max(temps)
                weather_data['temperature'] = round((weather_data['temperature_f'] - 32) * 5/9)
                
                # Look for high/low
                if len(temps) >= 3:
                    sorted_temps = sorted(temps)
                    weather_data['high_f'] = sorted_temps[-1]
                    weather_data['low_f'] = sorted_temps[0]
            
            # Extract conditions
            condition_keywords = ['clear', 'cloudy', 'partly cloudy', 'rain', 'snow', 
                                'fog', 'sunny', 'overcast', 'thunderstorm', 'drizzle']
            
            for part in parts:
                part_lower = part.lower()
                for keyword in condition_keywords:
                    if keyword in part_lower:
                        weather_data['condition'] = keyword.title()
                        weather_data['description'] = keyword
                        break
            
            # Extract additional data
            for part in parts:
                # Humidity
                if 'humidity' in part.lower():
                    hum_match = re.search(r'(\d+)%', part)
                    if hum_match:
                        weather_data['humidity'] = int(hum_match.group(1))
                
                # Wind
                if 'wind' in part.lower() or 'mph' in part.lower():
                    wind_match = re.search(r'(\d+)\s*mph', part.lower())
                    if wind_match:
                        weather_data['wind_speed_mph'] = int(wind_match.group(1))
                        weather_data['wind_speed'] = round(weather_data['wind_speed_mph'] * 1.60934)
                
                # UV Index
                if 'uv' in part.lower():
                    uv_match = re.search(r'(\d+)', part)
                    if uv_match:
                        weather_data['uv_index'] = int(uv_match.group(1))
            
            # Set defaults for missing data
            weather_data.setdefault('condition', 'Unknown')
            weather_data.setdefault('description', 'unknown')
            weather_data.setdefault('humidity', 50)
            weather_data.setdefault('wind_speed', 0)
            weather_data.setdefault('wind_speed_mph', 0)
            
            return weather_data
            
        except Exception as e:
            logger.error(f"Failed to parse weather text: {e}")
            return None
    
    def _clean_location(self, location: str) -> str:
        """Clean location string"""
        # Remove "My Location" indicator
        location = location.replace('My Location', '').replace('•', '').strip()
        # Remove extra spaces
        location = ' '.join(location.split())
        # Handle common patterns
        if location.lower() == 'home':
            location = 'Current Location'
        return location
    
    async def _run_applescript(self, script: str) -> str:
        """Run AppleScript and return output"""
        try:
            process = await asyncio.create_subprocess_exec(
                'osascript', '-e', script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                return stdout.decode('utf-8').strip()
            else:
                logger.error(f"AppleScript error: {stderr.decode('utf-8')}")
                return ""
                
        except Exception as e:
            logger.error(f"Failed to run AppleScript: {e}")
            return ""
    
    async def get_weather_with_cache(self) -> Optional[Dict]:
        """Get weather with caching to avoid excessive screen reading"""
        # Check cache
        if self.last_extraction:
            time_diff = (datetime.now() - self.last_extraction['timestamp']).seconds
            if time_diff < self.cache_duration:
                logger.info("Using cached vision weather data")
                return self.last_extraction['data']
        
        # Extract fresh data
        return await self.get_weather_from_vision()
    
    async def extract_forecast(self) -> Optional[List[Dict]]:
        """Extract forecast data from Weather app"""
        # TODO: Implement forecast extraction
        # This would analyze the 10-day forecast section
        pass
    
    async def extract_hourly(self) -> Optional[List[Dict]]:
        """Extract hourly forecast from Weather app"""
        # TODO: Implement hourly extraction
        # This would analyze the hourly section
        pass


# Test function
async def test_vision_weather():
    """Test the vision weather extractor"""
    extractor = VisionWeatherExtractor()
    
    print("Testing Vision Weather Extractor...")
    print("=" * 50)
    
    weather = await extractor.get_weather_from_vision()
    
    if weather:
        print(f"Location: {weather.get('location', 'Unknown')}")
        print(f"Temperature: {weather.get('temperature', 'N/A')}°C / {weather.get('temperature_f', 'N/A')}°F")
        print(f"Condition: {weather.get('condition', 'Unknown')}")
        print(f"Source: {weather.get('source', 'Unknown')}")
        
        if 'high_f' in weather:
            print(f"High/Low: {weather['high_f']}°F / {weather.get('low_f', 'N/A')}°F")
        
        print("\nFull data:")
        print(json.dumps(weather, indent=2))
    else:
        print("Failed to extract weather data")


if __name__ == "__main__":
    # Run test
    asyncio.run(test_vision_weather())