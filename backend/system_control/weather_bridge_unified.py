"""
Unified Weather Bridge for JARVIS
Single entry point for ALL weather queries using vision
Replaces all previous weather providers with one intelligent system
"""

import re
import logging
from typing import Dict, Optional
from datetime import datetime

from .unified_vision_weather import get_unified_weather_system

logger = logging.getLogger(__name__)


#: Words that mean weather and essentially nothing else — a whole-word match
#: on one of these is enough on its own.
_WEATHER_STRONG = re.compile(
    r"\b(?:weather|forecast|temperature|humidity|celsius|fahrenheit)\b",
    re.IGNORECASE,
)

#: Words that OFTEN mean weather but are ordinary English besides. These may
#: never trigger alone. "wind" and "warm" are the reason this list exists:
#: they matched "window" and "warming" as substrings.
_WEATHER_WEAK = re.compile(
    r"\b(?:rain|raining|snow|snowing|sunny|cloudy|humid|wind|windy|cold|hot|"
    r"warm|storm|storms|stormy|degrees)\b",
    re.IGNORECASE,
)

#: Interrogative shape. A weak word inside a QUESTION is a weather query; the
#: same word inside a statement is just a word.
_WEATHER_QUESTION = re.compile(
    r"\b(?:what|what's|whats|how|is|are|will|going|gonna|should|outside|"
    r"today|tomorrow|tonight)\b",
    re.IGNORECASE,
)


def _is_weather_query(text: str) -> bool:
    """Whole-word weather detection. The canonical implementation.

    Lives here because this module is the one three others already route
    through (``weather_system_config``, ``migrate_to_unified_weather``, and
    ``jarvis_agent_voice`` via the bridge) — putting the precise version
    anywhere else would leave the loose one reachable. NEVER raises."""
    try:
        t = str(text or "")
        if _WEATHER_STRONG.search(t):
            return True
        return bool(_WEATHER_WEAK.search(t) and _WEATHER_QUESTION.search(t))
    except (TypeError, AttributeError):
        return False


class UnifiedWeatherBridge:
    """
    The ONLY weather bridge JARVIS needs
    Routes all weather queries to vision-based system
    """
    
    def __init__(self, vision_handler=None, controller=None):
        # Get the unified weather system
        self.weather_system = get_unified_weather_system(vision_handler, controller)
        
        logger.info("Unified Weather Bridge initialized - using vision-based weather only")
    
    async def get_weather(self, query: str = "") -> Dict:
        """
        Single method for ALL weather queries
        Handles current weather, forecasts, specific questions
        """
        try:
            # Let the unified system handle everything
            result = await self.weather_system.get_weather(query)
            
            # Log success/failure
            if result.get('success'):
                logger.info(f"Weather query successful: {result.get('location', 'Unknown')}")
            else:
                logger.warning(f"Weather query failed: {result.get('error', 'Unknown error')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Weather bridge error: {e}")
            return {
                'success': False,
                'error': str(e),
                'formatted_response': "I encountered an error checking the weather.",
                'timestamp': datetime.now().isoformat()
            }
    
    # Convenience methods that all route to the same system
    async def get_current_weather(self) -> Dict:
        """Get current weather - routes to unified system"""
        return await self.get_weather("What's the current weather?")
    
    async def get_weather_by_city(self, city: str) -> Dict:
        """Get weather for a city - routes to unified system"""
        return await self.get_weather(f"What's the weather in {city}?")
    
    async def get_forecast(self, days: int = 7) -> Dict:
        """Get forecast - routes to unified system"""
        return await self.get_weather(f"What's the {days}-day forecast?")
    
    async def check_precipitation(self) -> Dict:
        """Check for rain/snow - routes to unified system"""
        return await self.get_weather("Will it rain or snow today?")
    
    def is_weather_query(self, text: str) -> bool:
        """Is this actually asking about the weather?

        THE BUG THIS REPLACES was a bare substring scan over short common
        words. ``in`` matches ANYWHERE in a string, so on ordinary phrases
        from this very system:

            "close the window"        -> 'wind'  -> opened the Weather app
            "open a new window"       -> 'wind'  -> opened the Weather app
            "the model is warming up" -> 'warm'  -> opened the Weather app
            "training the brain"      -> 'rain'  -> opened the Weather app
            "hotfix deployed"         -> 'hot'   -> opened the Weather app
            "scold the agent"         -> 'cold'  -> opened the Weather app

        This predicate sits upstream of ``subprocess.run(['open', '-a',
        'Weather'])`` and downstream of speech recognition, so a MISHEARD
        FRAGMENT could launch an application unbidden. That is the part that
        matters: silence is a bug, but taking an action on someone's machine
        because a substring appeared is a different category.

        Two rules. Whole words only, because the failure was entirely about
        boundaries. And ambiguous words need interrogative context: "is it
        cold outside" asks about weather, "the cold start took 12 seconds"
        does not, and both contain "cold". NEVER raises."""
        return _is_weather_query(text)