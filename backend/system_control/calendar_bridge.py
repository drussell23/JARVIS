#!/usr/bin/env python3
"""
Calendar Bridge - Python interface to Swift Calendar Context Provider
Provides seamless integration with macOS calendar for JARVIS
"""

import json
import subprocess
import asyncio
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of calendar events"""
    MEETING = "meeting"
    APPOINTMENT = "appointment"
    DEADLINE = "deadline"
    REMINDER = "reminder"
    OTHER = "other"


class TimeOfDay(Enum):
    """Time of day categories"""
    EARLY_MORNING = "early_morning"  # 5-8 AM
    MORNING = "morning"              # 8-12 PM
    AFTERNOON = "afternoon"          # 12-5 PM
    EVENING = "evening"              # 5-9 PM
    NIGHT = "night"                  # 9 PM-5 AM


@dataclass
class CalendarEvent:
    """Represents a calendar event"""
    event_id: str
    title: str
    start_time: datetime
    end_time: datetime
    is_all_day: bool
    location: Optional[str]
    notes: Optional[str]
    attendees: List[str]
    event_type: EventType
    
    @property
    def duration_minutes(self) -> int:
        """Get event duration in minutes"""
        return int((self.end_time - self.start_time).total_seconds() / 60)
    
    @property
    def is_happening_now(self) -> bool:
        """Check if event is currently happening"""
        now = datetime.now()
        return self.start_time <= now <= self.end_time
    
    def time_until(self, from_time: Optional[datetime] = None) -> timedelta:
        """Get time until event starts"""
        from_time = from_time or datetime.now()
        return self.start_time - from_time
    
    def time_until_minutes(self, from_time: Optional[datetime] = None) -> int:
        """Get minutes until event starts"""
        return int(self.time_until(from_time).total_seconds() / 60)


class CalendarBridge:
    """Bridge to Swift Calendar Context Provider"""
    
    def __init__(self):
        """Initialize calendar bridge"""
        self.swift_path = Path(__file__).parent.parent / "vision" / "calendar_context_provider.swift"
        self._compiled_path = None
        self._compile_if_needed()
    
    def _compile_if_needed(self):
        """Compile Swift calendar provider if needed"""
        try:
            # Check if already compiled
            compiled_name = self.swift_path.stem
            compiled_path = self.swift_path.parent / compiled_name
            
            # Compile if not exists or source is newer
            if not compiled_path.exists() or self.swift_path.stat().st_mtime > compiled_path.stat().st_mtime:
                logger.info("Compiling Swift calendar provider...")
                subprocess.run([
                    'swiftc', '-O', 
                    str(self.swift_path),
                    '-o', str(compiled_path)
                ], check=True, capture_output=True, text=True)
                logger.info("Swift calendar provider compiled successfully")
            
            self._compiled_path = compiled_path
            
        except Exception as e:
            logger.warning(f"Failed to compile Swift calendar provider: {e}")
            self._compiled_path = None
    
    async def get_calendar_context(self, hours_ahead: int = 24, max_events: int = 50) -> Optional[Dict[str, Any]]:
        """Get calendar context from Swift provider"""
        if not self._compiled_path:
            return None
        
        try:
            # Run Swift provider
            process = await asyncio.create_subprocess_exec(
                str(self._compiled_path),
                '--hours', str(hours_ahead),
                '--max-events', str(max_events),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
            
            if process.returncode != 0:
                error_output = stderr.decode().strip()
                # Try to parse error JSON for better error message
                try:
                    output_data = json.loads(stdout.decode())
                    if 'error' in output_data:
                        logger.warning(f"Calendar access issue: {output_data['error']}")
                except Exception:
                    logger.error(f"Calendar provider error: {error_output}")
                return None
            
            # Parse JSON output
            return json.loads(stdout.decode())
            
        except asyncio.TimeoutError:
            logger.error("Calendar provider timed out")
            return None
        except Exception as e:
            logger.error(f"Failed to get calendar context: {e}")
            return None
    
    async def get_events(self, hours_ahead: int = 4) -> List[CalendarEvent]:
        """Get upcoming calendar events"""
        context = await self.get_calendar_context(hours_ahead=hours_ahead)
        if not context:
            return []
        
        events = []
        for event_data in context.get('upcomingEvents', []):
            try:
                event = CalendarEvent(
                    event_id=event_data['eventId'],
                    title=event_data['title'],
                    start_time=datetime.fromisoformat(event_data['startDate']),
                    end_time=datetime.fromisoformat(event_data['endDate']),
                    is_all_day=event_data.get('isAllDay', False),
                    location=event_data.get('location'),
                    notes=event_data.get('notes'),
                    attendees=event_data.get('attendees', []),
                    event_type=EventType(event_data.get('eventType', 'other'))
                )
                events.append(event)
            except Exception as e:
                logger.warning(f"Failed to parse event: {e}")
        
        return events
    
    async def get_contextual_info(self, current_time: datetime) -> Tuple[
        Optional[CalendarEvent],  # Current event
        Optional[CalendarEvent],  # Next event
        List[CalendarEvent]       # Upcoming events
    ]:
        """Get contextual calendar information"""
        events = await self.get_events(hours_ahead=8)  # Look 8 hours ahead
        
        current_event = None
        next_event = None
        upcoming_events = []
        
        for event in events:
            if event.is_happening_now:
                current_event = event
            elif event.start_time > current_time and not next_event:
                next_event = event
            
            # Add to upcoming if within next 4 hours
            if 0 < event.time_until_minutes() <= 240:  # 4 hours
                upcoming_events.append(event)
        
        return current_event, next_event, upcoming_events
    
    def format_event_context(self, 
                           event: CalendarEvent, 
                           current_time: datetime,
                           context_type: str = "upcoming") -> str:
        """Format event information dynamically based on context"""
        minutes_until = event.time_until_minutes(current_time)
        
        # Dynamic formatting based on time and context
        if context_type == "current":
            # Currently in progress
            minutes_remaining = int((event.end_time - current_time).total_seconds() / 60)
            
            if minutes_remaining < 5:
                return f"Your {event.title} is ending soon"
            elif event.duration_minutes > 60:
                return f"You're currently in {event.title}, which ends in {minutes_remaining} minutes"
            else:
                return f"You're in {event.title}"
        
        elif context_type == "next":
            # Upcoming event formatting
            if minutes_until <= 0:
                return f"{event.title} should have started"
            elif minutes_until <= 5:
                urgency = "starting now" if minutes_until <= 2 else f"starts in {minutes_until} minutes"
                
                # Add location if available and nearby
                if event.location and minutes_until <= 10:
                    return f"Your {event.title} {urgency} at {event.location}"
                else:
                    return f"Your {event.title} {urgency}"
                    
            elif minutes_until <= 15:
                # Add preparation suggestions for certain event types
                if event.event_type == EventType.MEETING:
                    if event.attendees:
                        attendee_count = len(event.attendees)
                        attendee_info = f"with {attendee_count} {'person' if attendee_count == 1 else 'people'}"
                        return f"{event.title} {attendee_info} starts in {minutes_until} minutes"
                    else:
                        return f"{event.title} starts in {minutes_until} minutes. Time to prepare"
                else:
                    return f"{event.title} starts in {minutes_until} minutes"
                    
            elif minutes_until <= 30:
                return f"You have {event.title} in {minutes_until} minutes"
                
            elif minutes_until <= 60:
                time_desc = "half an hour" if 25 <= minutes_until <= 35 else f"about {minutes_until} minutes"
                return f"{event.title} coming up in {time_desc}"
                
            else:
                # For events more than an hour away
                hours = minutes_until // 60
                remaining_minutes = minutes_until % 60
                
                if hours == 1 and remaining_minutes < 15:
                    return f"{event.title} in about an hour"
                elif remaining_minutes < 15:
                    hour_text = "hour" if hours == 1 else "hours"
                    return f"{event.title} in {hours} {hour_text}"
                else:
                    hour_text = "hour" if hours == 1 else "hours"
                    return f"{event.title} in {hours} {hour_text} and {remaining_minutes} minutes"
        
        elif context_type == "reminder":
            # Gentle reminder format
            if minutes_until <= 30:
                return f"Don't forget about {event.title} at {event.start_time.strftime('%-I:%M %p')}"
            else:
                return f"Reminder: {event.title} scheduled for {event.start_time.strftime('%-I:%M %p')}"
        
        # Default format
        return f"{event.title} at {event.start_time.strftime('%-I:%M %p')}"
    
    async def get_smart_time_context(self, current_time: datetime) -> Optional[str]:
        """Get smart, context-aware time information"""
        current_event, next_event, upcoming_events = await self.get_contextual_info(current_time)
        
        # Build contextual response
        contexts = []
        
        # Current event context
        if current_event:
            contexts.append(self.format_event_context(current_event, current_time, "current"))
        
        # Next event context - prioritize based on urgency
        if next_event:
            minutes_until = next_event.time_until_minutes(current_time)
            
            # Only mention if within 2 hours or is important
            if minutes_until <= 120 or next_event.event_type in [EventType.MEETING, EventType.APPOINTMENT]:
                contexts.append(self.format_event_context(next_event, current_time, "next"))
        
        # Multiple upcoming events warning
        if len(upcoming_events) > 2:
            # Check if busy period ahead
            events_in_next_hour = sum(1 for e in upcoming_events if e.time_until_minutes() <= 60)
            if events_in_next_hour >= 2:
                contexts.append(f"You have a busy schedule with {events_in_next_hour} events in the next hour")
        
        # Join contexts naturally
        if not contexts:
            return None
        elif len(contexts) == 1:
            return contexts[0]
        elif len(contexts) == 2:
            return f"{contexts[0]}. {contexts[1]}"
        else:
            # Multiple contexts - be concise
            return f"{contexts[0]}. {contexts[1]}"


# Singleton instance
_calendar_bridge = None

def get_calendar_bridge() -> CalendarBridge:
    """Get singleton calendar bridge instance"""
    global _calendar_bridge
    if _calendar_bridge is None:
        _calendar_bridge = CalendarBridge()
    return _calendar_bridge