"""
Weather Bridge for JARVIS
Provides intelligent weather data using macOS WeatherKit or fallback to APIs
"""

import os
import asyncio
import subprocess
import logging
from typing import Dict, Optional, List
from datetime import datetime
import re

logger = logging.getLogger(__name__)

class WeatherBridge:
    """Intelligent weather bridge that prioritizes macOS WeatherKit over external APIs"""
    
    def __init__(self):
        # NO CACHING for real-time data
        
        # Pattern recognition for weather queries
        self._weather_patterns = self._compile_weather_patterns()
        
        # Initialize fallback API service first (needed for hybrid)
        self.api_weather_service = None
        self._init_fallback_service()
        
        # Initialize vision extractor (needed for hybrid)
        self.vision_extractor = None
        try:
            from .vision_weather_extractor import VisionWeatherExtractor
            self.vision_extractor = VisionWeatherExtractor()
            logger.info("Vision weather extractor initialized - bypasses location permissions!")
        except Exception as e:
            logger.warning(f"Could not initialize vision weather extractor: {e}")
        
        # HIGHEST PRIORITY: Hybrid weather provider (API + Vision fallback)
        try:
            from .hybrid_weather_provider import HybridWeatherProvider
            self.hybrid_provider = HybridWeatherProvider(
                api_weather_service=self.api_weather_service,
                vision_extractor=self.vision_extractor
            )
            logger.info("Hybrid weather provider initialized - API primary with vision fallback!")
        except Exception as e:
            logger.warning(f"Could not initialize hybrid weather provider: {e}")
            self.hybrid_provider = None
        
        # SECOND PRIORITY: Precise location weather with Core Location
        try:
            from .precise_weather_provider import PreciseWeatherProvider
            self.precise_provider = PreciseWeatherProvider()
            logger.info("Precise weather provider with Core Location initialized")
        except Exception as e:
            logger.warning(f"Could not initialize precise weather provider: {e}")
            self.precise_provider = None
        
        # SECOND PRIORITY: Swift native weather tool
        try:
            from .swift_weather_provider import SwiftWeatherProvider
            self.swift_provider = SwiftWeatherProvider()
            logger.info("Swift weather provider initialized")
        except Exception as e:
            logger.warning(f"Could not initialize Swift weather provider: {e}")
            self.swift_provider = None
        
        # Initialize weather widget extractor (second priority)
        from .weather_widget_extractor import WeatherWidgetExtractor
        self.widget_extractor = WeatherWidgetExtractor()
        
        # Initialize NEW macOS system integration (third)
        from .macos_system_integration import MacOSSystemIntegration
        self.system_integration = MacOSSystemIntegration()
        
        # Initialize macOS Weather app integration (fourth)
        from .macos_weather_app import MacOSWeatherApp
        self.macos_weather_app = MacOSWeatherApp()
        
        # Check temperature unit preference
        from .temperature_units import should_use_fahrenheit
        self.use_fahrenheit = should_use_fahrenheit()
        
        # Initialize macOS weather provider as backup
        try:
            from .macos_weather_provider import MacOSWeatherProvider
            self.macos_provider = MacOSWeatherProvider()
        except Exception as e:
            logger.warning(f"Could not initialize macOS weather provider: {e}")
            self.macos_provider = None
        
        # Vision extractor is now initialized earlier for hybrid provider
    
    def _compile_weather_patterns(self) -> List[re.Pattern]:
        """Compile regex patterns for weather query detection"""
        patterns = [
            # Direct weather queries
            r'\b(what|whats|what\'s|how|hows|how\'s)\s*(is|are)?\s*(the)?\s*weather\b',
            r'\bweather\s*(forecast|report|update|conditions?|today|tomorrow|now)\b',
            r'\b(current|today\'s|todays|tomorrow\'s|tomorrows)\s*weather\b',
            
            # Temperature queries
            r'\b(what|whats|what\'s|how)\s*(is|are)?\s*(the)?\s*temperature\b',
            r'\b(how\s*)?(hot|cold|warm|cool)\s*(is\s*it|outside)\b',
            r'\btemperature\s*(outside|now|today)\b',
            
            # Condition queries
            r'\b(is\s*it|will\s*it)\s*(rain|raining|snow|snowing|sunny|cloudy|foggy|windy)\b',
            r'\b(rain|snow|sun|cloud|fog|wind|storm)\s*(today|tomorrow|now)\b',
            
            # Location-specific weather
            r'\bweather\s*(in|at|for)\s*(\w+[\w\s]*)\b',
            r'\b(what|whats|what\'s)\s*(the)?\s*weather\s*(like)?\s*(in|at)\s*(\w+[\w\s]*)\b',
            
            # Forecast queries
            r'\b(weather|temperature)\s*forecast\b',
            r'\b(will|going\s*to)\s*(rain|snow|be\s*sunny|be\s*cloudy)\b',
            
            # Natural language variations
            r'\bdo\s*i\s*need\s*(an?)?\s*(umbrella|jacket|coat|sunscreen)\b',
            r'\bshould\s*i\s*(wear|bring|take)\s*(an?)?\s*(umbrella|jacket|coat)\b',
            r'\bnice\s*(day|weather)\s*(outside|today)\b',
            
            # UV and other conditions
            r'\b(uv|ultraviolet)\s*(index|level)\b',
            r'\b(humidity|wind\s*speed|visibility|pressure)\b',
            r'\b(sunrise|sunset)\s*(time|today)\b',
        ]
        
        return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    
    def _init_fallback_service(self):
        """Initialize fallback weather service if available"""
        try:
            # Try different import paths
            try:
                from backend.services.weather_service import WeatherService
            except ImportError:
                try:
                    from services.weather_service import WeatherService
                except ImportError:
                    from ..services.weather_service import WeatherService
            
            api_key = os.getenv("OPENWEATHER_API_KEY")
            if api_key:
                self.api_weather_service = WeatherService(api_key)
                logger.info("Initialized fallback weather service")
        except Exception as e:
            logger.debug(f"Fallback weather service not available: {e}")
    
    
    def is_weather_query(self, text: str) -> bool:
        """Check if text is a weather-related query using pattern matching"""
        return any(pattern.search(text) for pattern in self._weather_patterns)
    
    def extract_location_from_query(self, text: str) -> Optional[str]:
        """Extract location from weather query"""
        text_lower = text.lower()
        
        # Check if this is asking about current location (time-based queries)
        current_location_patterns = [
            r'weather\s+(?:for\s+)?today',
            r'weather\s+(?:for\s+)?tomorrow',
            r'weather\s+(?:for\s+)?tonight',
            r'weather\s+(?:for\s+)?now',
            r'weather\s+(?:for\s+)?this\s+(?:week|weekend|morning|afternoon|evening)',
            r'current\s+weather',
            r'weather\s+outside',
            r'weather\s+here',
            r"what's\s+(?:the\s+)?weather\s*$",  # Just "what's the weather" with nothing after
            r"what\s+is\s+(?:the\s+)?weather\s*$",  # Just "what is the weather" with nothing after
        ]
        
        # Check if it matches current location patterns
        for pattern in current_location_patterns:
            if re.search(pattern, text_lower):
                return None  # Current location
        
        # Patterns to extract specific location
        location_patterns = [
            # "what's the weather in Tokyo?", "weather in Tokyo", etc.
            r'weather\s+(?:in|at|for)\s+([A-Za-z]+(?:\s+[A-Za-z]+)*?)(?:\s*\?|$)',
            # "what's the weather like in New York"
            r'weather\s+(?:like\s+)?(?:in|at)\s+([A-Za-z]+(?:\s+[A-Za-z]+)*?)(?:\s*\?|$)',
            # "Tokyo weather" at start of query
            r'^([A-Za-z]+(?:\s+[A-Za-z]+)*?)\s+weather',
            # "weather Tokyo" pattern
            r'weather\s+([A-Za-z]+(?:\s+[A-Za-z]+)*?)(?:\s*\?|$)',
        ]
        
        for pattern in location_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                location = match.group(1).strip()
                # Filter out common words, time-related words, and query words
                excluded_words = [
                    'the', 'a', 'an', 'it', 'there', 'is', 'are', 'was', 'were',
                    'today', 'tomorrow', 'tonight', 'yesterday', 'now',
                    'current', 'this', 'that', 'here', 'outside'
                ]
                
                # Check if location is meaningful (not just excluded words)
                location_words = location.lower().split()
                meaningful_words = [w for w in location_words if w not in excluded_words]
                
                if meaningful_words and len(location) > 2:
                    return location
        
        return None
    
    
    async def get_current_weather(self, use_cache: bool = False) -> Dict:
        """Get weather for current location - ALWAYS REAL-TIME"""
        # NO CACHING - always get fresh data
        
        # FIRST PRIORITY: Try Hybrid provider (OpenWeatherMap with vision fallback)
        if self.hybrid_provider:
            try:
                hybrid_data = await self.hybrid_provider.get_current_weather()
                if hybrid_data and self._is_valid_weather_data(hybrid_data):
                    logger.info(f"Got weather from HYBRID provider: {hybrid_data.get('location')} - "
                               f"{hybrid_data.get('temperature')}°C, {hybrid_data.get('condition')}")
                    # Enhance with additional fields
                    hybrid_data = self._enhance_weather_data(hybrid_data)
                    return hybrid_data
            except Exception as e:
                logger.error(f"Hybrid weather provider failed: {e}")
        
        # SECOND PRIORITY: Try Precise weather provider with Core Location
        if self.precise_provider:
            try:
                precise_data = await self.precise_provider.get_weather_data()
                if precise_data and self._is_valid_weather_data(precise_data):
                    logger.info(f"Got precise weather from Core Location: {precise_data.get('location')} - "
                               f"{precise_data.get('temperature')}°C, {precise_data.get('condition')}")
                    return precise_data
            except Exception as e:
                logger.error(f"Precise weather provider failed: {e}")
        
        # SECOND PRIORITY: Try Swift native weather tool
        if self.swift_provider:
            try:
                swift_data = await self.swift_provider.get_weather_data()
                if swift_data and self._is_valid_weather_data(swift_data):
                    logger.info(f"Got weather from Swift tool: {swift_data.get('location')} - "
                               f"{swift_data.get('temperature')}°C, {swift_data.get('condition')}")
                    return swift_data
            except Exception as e:
                logger.error(f"Swift weather provider failed: {e}")
        
        # THIRD: Try weather widget extractor for accurate data
        try:
            widget_data = await self.widget_extractor.get_weather_with_fallback()
            if widget_data:
                logger.info(f"Got weather from widget: {widget_data}")
                return widget_data
        except Exception as e:
            logger.error(f"Widget extraction failed: {e}")
        
        # Third: try NEW system integration for accurate data
        try:
            weather_data = await self.system_integration.get_accurate_weather()
            if weather_data and self._is_valid_weather_data(weather_data):
                logger.info(f"Got real-time weather: {weather_data}")
                return weather_data
        except Exception as e:
            logger.error(f"System integration weather failed: {e}")
        
        # Try macOS Weather app directly
        try:
            weather_data = await self.macos_weather_app.get_weather_with_location()
            if weather_data and weather_data.get("source") != "fallback" and self._is_valid_weather_data(weather_data):
                logger.info("Got weather from macOS Weather app")
                return weather_data
        except Exception as e:
            logger.error(f"macOS Weather app failed: {e}")
        
        # Try macOS weather provider
        if self.macos_provider:
            try:
                weather_data = await self.macos_provider.get_weather_data()
                if weather_data and weather_data.get("source") != "fallback" and self._is_valid_weather_data(weather_data):
                    return weather_data
            except Exception as e:
                logger.error(f"macOS weather provider failed: {e}")
        
        # Fallback to API service if available
        if self.api_weather_service:
            logger.info("Using fallback weather API")
            try:
                api_data = await self.api_weather_service.get_current_weather()
                if api_data and self._is_valid_weather_data(api_data):
                    return api_data
            except Exception as e:
                logger.error(f"Fallback weather API failed: {e}")
        
        # Last resort - return informative message
        return self._get_fallback_weather_response()
    
    async def get_weather_by_city(self, city: str, use_cache: bool = False) -> Dict:
        """Get weather for specific city - ALWAYS REAL-TIME"""
        # NO CACHING - always get fresh data
        city_normalized = city.strip().title()
        
        # FIRST: Try Hybrid provider (OpenWeatherMap with vision fallback)
        if self.hybrid_provider:
            try:
                hybrid_data = await self.hybrid_provider.get_weather_by_city(city)
                if hybrid_data and self._is_valid_weather_data(hybrid_data):
                    logger.info(f"Got city weather from HYBRID provider: {city} - "
                               f"{hybrid_data.get('temperature')}°C")
                    # Enhance with additional fields
                    hybrid_data = self._enhance_weather_data(hybrid_data)
                    return hybrid_data
            except Exception as e:
                logger.error(f"Hybrid provider city weather failed: {e}")
        
        # SECOND: Try Precise weather provider
        if self.precise_provider:
            try:
                precise_data = await self.precise_provider.get_weather_data(city)
                if precise_data and self._is_valid_weather_data(precise_data):
                    logger.info(f"Got city weather from precise provider: {city} - "
                               f"{precise_data.get('temperature')}°C")
                    return precise_data
            except Exception as e:
                logger.error(f"Precise provider city weather failed: {e}")
        
        # SECOND: Try Swift native weather tool
        if self.swift_provider:
            try:
                swift_data = await self.swift_provider.get_weather_data(city)
                if swift_data and self._is_valid_weather_data(swift_data):
                    logger.info(f"Got city weather from Swift tool: {city} - "
                               f"{swift_data.get('temperature')}°C, {swift_data.get('condition')}")
                    return swift_data
            except Exception as e:
                logger.error(f"Swift weather provider failed for {city}: {e}")
        
        # Try system integration second
        try:
            # For specific city, we need to use provider
            if self.macos_provider:
                weather_data = await self.macos_provider.get_weather_data(city_normalized)
                if weather_data and weather_data.get("source") != "fallback":
                    return weather_data
        except Exception as e:
            logger.error(f"macOS weather provider failed for {city}: {e}")
        
        # Fallback to API
        if self.api_weather_service:
            try:
                return await self.api_weather_service.get_weather_by_city(city)
            except Exception as e:
                logger.error(f"Fallback weather API failed for {city}: {e}")
        
        return self._get_fallback_weather_response(city)
    
    def _format_weatherkit_data(self, data: Dict, city_override: Optional[str] = None) -> Dict:
        """Format WeatherKit data to match expected structure"""
        formatted = {
            "location": city_override or data.get("location", "Current Location"),
            "temperature": data.get("temperature", 20),
            "temperature_unit": data.get("temperatureUnit", "°C"),
            "feels_like": data.get("feelsLike", data.get("temperature", 20)),
            "description": data.get("description", "unknown"),
            "condition": data.get("condition", "unknown"),
            "humidity": data.get("humidity", 50),
            "wind_speed": data.get("windSpeed", 0),
            "wind_direction": data.get("windDirection", "N"),
            "pressure": data.get("pressure", 1013),
            "visibility": data.get("visibility", 10),
            "uv_index": data.get("uvIndex", 0),
            "cloud_cover": data.get("cloudCover", 0),
            "is_daylight": data.get("isDaylight", True),
            "sunrise": data.get("sunrise", "06:00"),
            "sunset": data.get("sunset", "18:00"),
            "moon_phase": data.get("moonPhase", ""),
            "timestamp": data.get("timestamp", datetime.now().isoformat()),
            "source": "WeatherKit"
        }
        
        # Add insights if available
        if "insights" in data:
            formatted["insights"] = data["insights"]
        
        # Add hourly forecast if available
        if "hourlyForecast" in data:
            formatted["hourly_forecast"] = data["hourlyForecast"]
        
        # Add alerts if any
        if "alerts" in data:
            formatted["alerts"] = data["alerts"]
        
        return formatted
    
    def _get_fallback_weather_response(self, city: str = "your location") -> Dict:
        """Get fallback weather response when no service is available"""
        return {
            "location": city,
            "temperature": None,  # No fake temperature
            "temperature_unit": "°C",
            "feels_like": None,
            "description": "Weather data temporarily unavailable",
            "condition": "unavailable",
            "humidity": None,
            "wind_speed": None,
            "wind_direction": None,
            "source": "unavailable",
            "message": "Weather services are currently unavailable. Please check your internet connection or try again later.",
            "timestamp": datetime.now().isoformat(),
            "error": True  # Flag to indicate this is an error response
        }
    
    def format_for_speech(self, weather_data: Dict, query_type: str = "current") -> str:
        """Format weather data for natural speech output"""
        location = weather_data.get("location", "your location")
        
        # Check if this is an error response
        if weather_data.get("error", False):
            return f"I'm unable to get weather data for {location} right now. Please try again in a moment or check the Weather app directly."
        
        # Handle temperature based on system preference
        temp_c = weather_data.get("temperature")
        
        # If no temperature data, return a different response
        if temp_c is None:
            condition = weather_data.get("condition", "current conditions")
            if condition and condition != "unavailable" and condition != "unknown":
                return f"The weather in {location} shows {condition}, but I couldn't get the temperature data."
            else:
                return f"I'm having trouble accessing complete weather data for {location} right now. Please check the Weather app for current conditions."
        
        feels_like_c = weather_data.get("feels_like", temp_c)
        
        # Convert to user's preferred unit
        if self.use_fahrenheit:
            # Convert to Fahrenheit
            temp = weather_data.get("temperature_f", round(temp_c * 9/5 + 32))
            feels_like = weather_data.get("feels_like_f", round(feels_like_c * 9/5 + 32))
            temp_display = f"{temp}°F"
            feels_display = f"{feels_like}°F"
            temp_unit = "°F"
        else:
            # Use Celsius
            temp = temp_c
            feels_like = feels_like_c
            temp_display = f"{temp}°C"
            feels_display = f"{feels_like}°C"
            temp_unit = "°C"
        
        description = weather_data.get("description", "unknown conditions")
        condition = weather_data.get("condition", description)
        wind_speed = weather_data.get("wind_speed", 0)
        humidity = weather_data.get("humidity", 0)
        
        # Clean up location - remove "Lat X and Lon Y" format
        if location.startswith("Lat ") and " and Lon " in location:
            # Try to get actual city name from weather data
            city = weather_data.get("detected_location") or weather_data.get("city", "your area")
            location = city
        
        # Make description more natural
        description = description.lower()
        if description in ["clear", "clear sky", "clear skies"]:
            description = "clear skies"
        elif description in ["partly cloudy", "partially cloudy"]:
            description = "partly cloudy"
        elif description in ["cloudy", "overcast"]:
            description = "cloudy"
        
        # Build conversational response
        hour = datetime.now().hour
        
        # Start with a natural greeting based on time
        if 5 <= hour < 12:
            time_greeting = "this morning"
        elif 12 <= hour < 17:
            time_greeting = "this afternoon"
        elif 17 <= hour < 21:
            time_greeting = "this evening"
        else:
            time_greeting = "tonight"
        
        # Build natural response based on query type
        if query_type == "temperature":
            # Convert temp thresholds based on unit
            if temp_unit == "°F" and temp is not None:
                if temp > 86:  # 30°C
                    response = f"It's quite hot in {location} at {temp_display}"
                elif temp > 77:  # 25°C
                    response = f"It's a warm {temp_display} in {location}"
                elif temp > 68:  # 20°C
                    response = f"It's a pleasant {temp_display} in {location}"
                elif temp > 59:  # 15°C
                    response = f"It's {temp_display} in {location} - quite mild"
                elif temp > 50:  # 10°C
                    response = f"It's a cool {temp_display} in {location}"
                elif temp > 41:  # 5°C
                    response = f"It's rather chilly at {temp_display} in {location}"
                else:
                    response = f"It's quite cold in {location} at {temp_display}"
            elif temp_unit == "°C" and temp is not None:
                # Celsius thresholds
                if temp > 30:
                    response = f"It's quite hot in {location} at {temp_display}"
                elif temp > 25:
                    response = f"It's a warm {temp_display} in {location}"
                elif temp > 20:
                    response = f"It's a pleasant {temp_display} in {location}"
                elif temp > 15:
                    response = f"It's {temp_display} in {location} - quite mild"
                elif temp > 10:
                    response = f"It's a cool {temp_display} in {location}"
                elif temp > 5:
                    response = f"It's rather chilly at {temp_display} in {location}"
                else:
                    response = f"It's quite cold in {location} at {temp_display}"
            
            if temp is not None and feels_like is not None and abs(feels_like - temp) > 3:
                if feels_like > temp:
                    response += f", but it feels warmer at {feels_display}"
                else:
                    response += f", but it feels colder at {feels_display}"
        
        elif query_type == "condition":
            response = f"We have {description} in {location} {time_greeting}"
            response += f" with temperatures around {temp_display}"
        
        else:  # General weather query for "today"
            # Make it more conversational
            if "rain" in description:
                response = f"Looks like we have {description} in {location} today"
            elif "clear" in description or "sunny" in description:
                response = f"It's a beautiful day in {location} with {description}"
            elif "cloud" in description:
                response = f"We're seeing {description} in {location} today"
            elif "snow" in description:
                response = f"We have {description} in {location}"
            else:
                response = f"The weather in {location} today is {description}"
            
            # Add temperature in a natural way
            response += f", currently {temp_display}"
            
            if temp is not None and feels_like is not None and abs(feels_like - temp) > 3:
                if feels_like > temp:
                    response += f" but feeling more like {feels_display}"
                else:
                    response += f" but feeling closer to {feels_display}"
        
        # Add contextual advice based on conditions
        advice_added = False
        
        # Use appropriate thresholds based on unit
        if temp_unit == "°F" and temp is not None:
            if temp < 41:  # 5°C
                response += ". Bundle up warmly today"
                advice_added = True
            elif temp > 86:  # 30°C
                response += ". Stay cool and hydrated"
                advice_added = True
        elif temp is not None:
            if temp < 5:
                response += ". Bundle up warmly today"
                advice_added = True
            elif temp > 30:
                response += ". Stay cool and hydrated"
                advice_added = True
        
        if "rain" in description.lower() and not advice_added:
            response += ". Don't forget your umbrella"
            advice_added = True
        elif "snow" in description.lower() and not advice_added:
            response += ". Drive carefully if you're heading out"
            advice_added = True
        elif wind_speed > 30 and not advice_added:
            response += f". It's quite windy at {wind_speed} kilometers per hour"
            advice_added = True
        elif humidity > 85 and not advice_added:
            response += ". The humidity is quite high today"
            advice_added = True
        
        # Add a pleasant closing for nice weather
        if not advice_added and temp is not None:
            if temp_unit == "°F":
                # 68-82°F is pleasant
                if temp >= 68 and temp <= 82 and "clear" in description:
                    response += ". Perfect weather to be outside"
            else:
                # 20-28°C is pleasant
                if temp >= 20 and temp <= 28 and "clear" in description:
                    response += ". Perfect weather to be outside"
        
        # Add insights if available and relevant
        insights = weather_data.get("insights", [])
        if insights and len(insights) > 0 and not advice_added:
            # Only add the first insight if it's not redundant
            insight = insights[0]
            if "hydrated" not in response and "hydrated" in insight:
                response += f". {insight}"
            elif "umbrella" not in response and "umbrella" in insight:
                response += f". {insight}"
        
        # Add critical alerts
        alerts = weather_data.get("alerts", [])
        if alerts and alerts[0].get("severity") in ["high", "extreme"]:
            response += f". Weather alert: {alerts[0].get('summary', 'Please check weather warnings')}"
        
        return response
    
    async def process_weather_query(self, query: str) -> str:
        """Process a weather query and return formatted response"""
        try:
            # If user wants to open Weather app
            if "open" in query.lower() and "weather" in query.lower():
                try:
                    subprocess.run(['open', '-a', 'Weather'], check=True)
                    return "I've opened the Weather app for you"
                except Exception:
                    return "I couldn't open the Weather app. Please try opening it manually"
            
            # First, try to get weather from macOS Weather app directly
            try:
                weather_data = await self.macos_weather_app.get_weather_with_location()
                if weather_data and weather_data.get("source") != "fallback":
                    logger.info("Got weather from macOS Weather app")
                    # Determine query type and format response
                    query_lower = query.lower()
                    query_type = "current"
                    
                    if "temperature" in query_lower or "hot" in query_lower or "cold" in query_lower:
                        query_type = "temperature"
                    elif any(word in query_lower for word in ["rain", "snow", "sunny", "cloudy", "foggy"]):
                        query_type = "condition"
                    
                    return self.format_for_speech(weather_data, query_type)
            except Exception as e:
                logger.debug(f"Weather app access failed: {e}")
            
            # Determine query type
            query_lower = query.lower()
            query_type = "current"
            
            if "temperature" in query_lower or "hot" in query_lower or "cold" in query_lower:
                query_type = "temperature"
            elif any(word in query_lower for word in ["rain", "snow", "sunny", "cloudy", "foggy"]):
                query_type = "condition"
            
            # Extract location if specified
            location = self.extract_location_from_query(query)
            
            # Get weather data with longer timeout for system integration
            weather_data = None
            try:
                if location:
                    logger.info(f"Getting weather for city: {location}")
                    weather_data = await asyncio.wait_for(
                        self.get_weather_by_city(location), 
                        timeout=10.0  # Increased timeout
                    )
                else:
                    logger.info("Getting current location weather")
                    weather_data = await asyncio.wait_for(
                        self.get_current_weather(), 
                        timeout=10.0  # Increased timeout
                    )
            except asyncio.TimeoutError:
                logger.warning("Weather data fetch timed out")
                return "I'm having trouble getting the weather data right now. The Weather app might have more current information."
            
            # If we got weather data, format it properly
            if weather_data and self._is_valid_weather_data(weather_data):
                return self.format_for_speech(weather_data, query_type)
            else:
                # Try to provide helpful guidance
                return "I'm having difficulty accessing current weather data. You might want to check the Weather app directly for the most accurate information."
                
        except Exception as e:
            logger.error(f"Error processing weather query: {e}")
            return "I'm having trouble accessing weather information right now. You can check the Weather app for current conditions"
    
    def clear_cache(self):
        """NO CACHING - this method is deprecated"""
        logger.info("Cache clearing not needed - using real-time data")
    
    def _enhance_weather_data(self, data: Dict) -> Dict:
        """Enhance weather data with additional fields and insights"""
        # Ensure all required fields exist
        data.setdefault('humidity', 50)
        data.setdefault('wind_speed', 0)
        data.setdefault('wind_speed_mph', 0)
        data.setdefault('pressure', 1013)
        data.setdefault('visibility', 10)
        data.setdefault('uv_index', 0)
        
        # Add temperature unit
        if 'temperature' in data:
            data['temperature_unit'] = '°C'
        
        # Add weather insights
        insights = []
        if data.get('humidity', 0) > 80:
            insights.append("High humidity may make it feel warmer")
        if data.get('humidity', 0) < 30:
            insights.append("Low humidity - stay hydrated")
        if data.get('uv_index', 0) >= 6:
            insights.append("High UV - consider sunscreen")
        if data.get('wind_speed_mph', 0) > 20:
            insights.append("Windy conditions")
        
        data['insights'] = insights
        
        # Add emoji for condition
        condition_emojis = {
            'clear': '☀️',
            'sunny': '☀️',
            'partly cloudy': '⛅',
            'cloudy': '☁️',
            'overcast': '☁️',
            'rain': '🌧️',
            'drizzle': '🌦️',
            'snow': '🌨️',
            'thunderstorm': '⛈️',
            'fog': '🌫️'
        }
        
        condition = data.get('condition', '').lower()
        for key, emoji in condition_emojis.items():
            if key in condition:
                data['condition_icon'] = emoji
                break
        
        return data
    
    def _is_valid_weather_data(self, data: Dict) -> bool:
        """Check if weather data is valid and not a fallback"""
        # Must have real temperature data
        if data.get("temperature") is None:
            return False
        
        # Must not be error or unavailable
        if data.get("error", False) or data.get("source") == "unavailable":
            return False
        
        # Must have some meaningful data
        has_location = bool(data.get("location") and data["location"] != "your location")
        has_condition = bool(data.get("condition") and data["condition"] not in ["unknown", "unavailable"])
        has_description = bool(data.get("description") and data["description"] not in ["unknown", "unavailable", "Weather data temporarily unavailable"])
        
        return has_location or has_condition or has_description
    
    async def close(self):
        """Clean up resources"""
        if self.api_weather_service:
            await self.api_weather_service.close()