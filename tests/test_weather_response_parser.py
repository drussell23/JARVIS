# [Ouroboros] Modified by Ouroboros (op=op-01a122db-) at 2026-10-09 22:51 UTC
# Reason: `backend/utils/weather_response_parser.py` has no corresponding test module. CREATE `tests/test_weather_response_parser.

from __future__ import annotations
import pytest
from backend.utils.weather_response_parser import WeatherResponseParser

class TestWeatherResponseParser:
    def test_extract_weather_info_with_temperature_and_condition(self):
        response = "The temperature is 72degF and it's sunny. The weather app shows clear skies."
        result = WeatherResponseParser.extract_weather_info(response)
        assert "72degF" in result
        assert "sunny" in result
        
        
        
    def test_extract_weather_info_with_no_weather_keywords(self):
        response = "This is a regular sentence with no weather information."
        result = WeatherResponseParser.extract_weather_info(response)
        assert result == "I couldn't extract specific weather information from what I see."
        
        
        
    def test_format_weather_response_with_only_temperature(self):
        raw_info = "The temperature is 25degC."
        result = WeatherResponseParser.format_weather_response(raw_info)
        assert "25degC" in result
        
    def test_format_weather_response_with_only_condition(self):
        raw_info = "It's cloudy."
        result = WeatherResponseParser.format_weather_response(raw_info)
        assert "cloudy" in result
        
        
    def test_format_weather_response_with_invalid_location(self):
        raw_info = "The temperature is 72degF and it's sunny."
        location = "yes sir"
        result = WeatherResponseParser.format_weather_response(raw_info, location)
        # Should not include location in response
        assert "yes" not in result
        
    def test_format_weather_response_empty_input(self):
        raw_info = ""
        result = WeatherResponseParser.format_weather_response(raw_info)
        assert result == ""
        
        
    def test_extract_weather_info_edge_case_no_period(self):
        response = "It's sunny and 75degF"
        result = WeatherResponseParser.extract_weather_info(response)
        assert "sunny" in result
        assert "75degF" in result
        
    def test_extract_weather_info_edge_case_temperature_only(self):
        response = "The temperature is 20 degrees."
        result = WeatherResponseParser.extract_weather_info(response)
        assert "20 degrees" in result