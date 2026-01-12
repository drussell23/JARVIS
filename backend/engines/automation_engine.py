import asyncio
import aiohttp
from typing import Dict, List, Optional, Any, Callable, Union
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from enum import Enum
import json
import os
from abc import ABC, abstractmethod
import calendar
from icalendar import Calendar, Event
import pytz
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

class TaskStatus(Enum):
    """Task execution status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TaskPriority(Enum):
    """Task priority levels"""
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4

@dataclass
class ScheduledEvent:
    """Represents a scheduled calendar event"""
    id: str
    title: str
    start_time: datetime
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    attendees: List[str] = field(default_factory=list)
    reminders: List[int] = field(default_factory=list)  # Minutes before event
    recurring: bool = False
    recurrence_rule: Optional[str] = None

@dataclass
class AutomationTask:
    """Represents an automation task"""
    id: str
    name: str
    action: str
    parameters: Dict[str, Any]
    trigger: Optional[Dict[str, Any]] = None
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    created_at: datetime = field(default_factory=datetime.now)
    executed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None

class AutomationAction(ABC):
    """Base class for automation actions"""
    
    @abstractmethod
    async def execute(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the automation action"""
        pass
    
    @abstractmethod
    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """Validate action parameters"""
        pass

class CalendarManager:
    """Manages calendar and scheduling functionality"""
    
    def __init__(self):
        self.events: Dict[str, ScheduledEvent] = {}
        self.scheduler = AsyncIOScheduler()
        self.reminder_callbacks: List[Callable] = []
        
    def start(self):
        """Start the scheduler"""
        self.scheduler.start()
        
    def stop(self):
        """Stop the scheduler"""
        self.scheduler.shutdown()
        
    def add_event(self, event: ScheduledEvent) -> str:
        """Add a new calendar event"""
        self.events[event.id] = event
        
        # Schedule reminders
        for reminder_minutes in event.reminders:
            reminder_time = event.start_time - timedelta(minutes=reminder_minutes)
            if reminder_time > datetime.now():
                self.scheduler.add_job(
                    self._trigger_reminder,
                    trigger=DateTrigger(run_date=reminder_time),
                    args=[event.id, reminder_minutes],
                    id=f"reminder_{event.id}_{reminder_minutes}"
                )
        
        return event.id
        
    def remove_event(self, event_id: str) -> bool:
        """Remove a calendar event"""
        if event_id in self.events:
            del self.events[event_id]
            
            # Remove associated reminders
            for job in self.scheduler.get_jobs():
                if job.id.startswith(f"reminder_{event_id}"):
                    job.remove()
                    
            return True
        return False
        
    def get_events(self, start_date: datetime, end_date: datetime) -> List[ScheduledEvent]:
        """Get events within a date range"""
        events = []
        for event in self.events.values():
            if start_date <= event.start_time <= end_date:
                events.append(event)
                
        return sorted(events, key=lambda e: e.start_time)
        
    def get_today_events(self) -> List[ScheduledEvent]:
        """Get today's events"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        return self.get_events(today, tomorrow)
        
    def get_upcoming_events(self, hours: int = 24) -> List[ScheduledEvent]:
        """Get upcoming events within specified hours"""
        now = datetime.now()
        future = now + timedelta(hours=hours)
        return self.get_events(now, future)
        
    async def _trigger_reminder(self, event_id: str, minutes_before: int):
        """Trigger event reminder"""
        if event_id in self.events:
            event = self.events[event_id]
            for callback in self.reminder_callbacks:
                await callback(event, minutes_before)
                
    def add_reminder_callback(self, callback: Callable):
        """Add callback for reminders"""
        self.reminder_callbacks.append(callback)
        
    def create_event_from_text(self, text: str) -> Optional[ScheduledEvent]:
        """Parse natural language to create event"""
        # Simple parsing logic - can be enhanced with NLP
        import re
        from dateutil import parser
        import uuid
        
        # Extract date/time
        time_patterns = [
            r"(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
            r"at (\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
            r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
        ]
        
        title = text
        start_time = None
        
        # Try to extract time information
        for pattern in time_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    time_str = match.group(0)
                    start_time = parser.parse(time_str, fuzzy=True)
                    # Remove time from title
                    title = text.replace(time_str, "").strip()
                    break
                except Exception:
                    pass

        if not start_time:
            # Default to tomorrow at 9 AM
            start_time = datetime.now().replace(hour=9, minute=0, second=0) + timedelta(days=1)
            
        # Create event
        event = ScheduledEvent(
            id=str(uuid.uuid4()),
            title=title,
            start_time=start_time,
            reminders=[15, 60]  # 15 min and 1 hour reminders
        )
        
        return event
        
    def export_to_ical(self) -> str:
        """Export calendar to iCal format"""
        cal = Calendar()
        cal.add('prodid', '-//AI Assistant Calendar//EN')
        cal.add('version', '2.0')
        
        for event in self.events.values():
            ical_event = Event()
            ical_event.add('summary', event.title)
            ical_event.add('dtstart', event.start_time)
            if event.end_time:
                ical_event.add('dtend', event.end_time)
            if event.location:
                ical_event.add('location', event.location)
            if event.description:
                ical_event.add('description', event.description)
                
            cal.add_component(ical_event)
            
        return cal.to_ical().decode('utf-8')

class WeatherService:
    """Weather information service"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENWEATHER_API_KEY")
        self.base_url = "https://api.openweathermap.org/data/2.5"
        self.cache = {}
        self.cache_duration = 600  # 10 minutes
        
    async def get_current_weather(self, location: str) -> Dict[str, Any]:
        """Get current weather for a location"""
        cache_key = f"current_{location}"
        
        # Check cache
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if datetime.now().timestamp() - timestamp < self.cache_duration:
                return cached_data
                
        # Fetch from API
        async with aiohttp.ClientSession() as session:
            params = {
                "q": location,
                "appid": self.api_key,
                "units": "metric"
            }
            
            async with session.get(f"{self.base_url}/weather", params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Parse relevant information
                    weather_info = {
                        "location": data["name"],
                        "country": data["sys"]["country"],
                        "temperature": data["main"]["temp"],
                        "feels_like": data["main"]["feels_like"],
                        "humidity": data["main"]["humidity"],
                        "pressure": data["main"]["pressure"],
                        "description": data["weather"][0]["description"],
                        "icon": data["weather"][0]["icon"],
                        "wind_speed": data["wind"]["speed"],
                        "clouds": data["clouds"]["all"],
                        "timestamp": datetime.now().isoformat()
                    }
                    
                    # Cache result
                    self.cache[cache_key] = (weather_info, datetime.now().timestamp())
                    
                    return weather_info
                else:
                    raise Exception(f"Weather API error: {response.status}")
                    
    async def get_forecast(self, location: str, days: int = 5) -> List[Dict[str, Any]]:
        """Get weather forecast"""
        cache_key = f"forecast_{location}_{days}"
        
        # Check cache
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if datetime.now().timestamp() - timestamp < self.cache_duration * 6:  # 1 hour cache
                return cached_data
                
        # Fetch from API
        async with aiohttp.ClientSession() as session:
            params = {
                "q": location,
                "appid": self.api_key,
                "units": "metric",
                "cnt": days * 8  # 8 forecasts per day (3-hour intervals)
            }
            
            async with session.get(f"{self.base_url}/forecast", params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Group by day
                    daily_forecasts = {}
                    for item in data["list"]:
                        date = datetime.fromtimestamp(item["dt"]).date()
                        if date not in daily_forecasts:
                            daily_forecasts[date] = []
                        daily_forecasts[date].append(item)
                        
                    # Summarize daily
                    forecast = []
                    for date, items in sorted(daily_forecasts.items())[:days]:
                        temps = [item["main"]["temp"] for item in items]
                        forecast.append({
                            "date": date.isoformat(),
                            "temp_min": min(temps),
                            "temp_max": max(temps),
                            "temp_avg": sum(temps) / len(temps),
                            "description": items[len(items)//2]["weather"][0]["description"],
                            "humidity": sum(item["main"]["humidity"] for item in items) / len(items),
                            "rain_probability": max(item.get("pop", 0) for item in items)
                        })
                        
                    # Cache result
                    self.cache[cache_key] = (forecast, datetime.now().timestamp())
                    
                    return forecast
                else:
                    raise Exception(f"Weather API error: {response.status}")
                    
    def format_weather_report(self, weather: Dict[str, Any]) -> str:
        """Format weather data as readable text"""
        return (
            f"Weather in {weather['location']}, {weather['country']}:\n"
            f"Temperature: {weather['temperature']}°C (feels like {weather['feels_like']}°C)\n"
            f"Conditions: {weather['description'].capitalize()}\n"
            f"Humidity: {weather['humidity']}%\n"
            f"Wind: {weather['wind_speed']} m/s\n"
            f"Cloudiness: {weather['clouds']}%"
        )

class InformationService:
    """General information service aggregator"""
    
    def __init__(self):
        self.services = {
            "news": self._get_news,
            "stock": self._get_stock_info,
            "crypto": self._get_crypto_info,
            "wikipedia": self._get_wikipedia_summary
        }
        
    async def get_information(self, query_type: str, query: str) -> Dict[str, Any]:
        """Get information based on query type"""
        if query_type in self.services:
            return await self.services[query_type](query)
        else:
            raise ValueError(f"Unknown information type: {query_type}")
            
    async def _get_news(self, topic: str) -> Dict[str, Any]:
        """Get news headlines"""
        # Using NewsAPI (requires API key)
        api_key = os.getenv("NEWS_API_KEY")
        if not api_key:
            return {"error": "News API key not configured"}
            
        async with aiohttp.ClientSession() as session:
            params = {
                "q": topic,
                "apiKey": api_key,
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 5
            }
            
            async with session.get("https://newsapi.org/v2/everything", params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    articles = []
                    for article in data.get("articles", [])[:5]:
                        articles.append({
                            "title": article["title"],
                            "source": article["source"]["name"],
                            "url": article["url"],
                            "published": article["publishedAt"],
                            "description": article.get("description", "")[:200]
                        })
                        
                    return {
                        "topic": topic,
                        "articles": articles,
                        "total": data.get("totalResults", 0)
                    }
                    
        return {"error": "Failed to fetch news"}
        
    async def _get_stock_info(self, symbol: str) -> Dict[str, Any]:
        """Get stock market information"""
        # Using Alpha Vantage API (requires API key)
        api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
        if not api_key:
            return {"error": "Stock API key not configured"}
            
        async with aiohttp.ClientSession() as session:
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": symbol.upper(),
                "apikey": api_key
            }
            
            async with session.get("https://www.alphavantage.co/query", params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if "Global Quote" in data:
                        quote = data["Global Quote"]
                        return {
                            "symbol": quote["01. symbol"],
                            "price": float(quote["05. price"]),
                            "change": float(quote["09. change"]),
                            "change_percent": quote["10. change percent"],
                            "volume": int(quote["06. volume"]),
                            "latest_trading_day": quote["07. latest trading day"]
                        }
                        
        return {"error": f"Failed to fetch stock info for {symbol}"}
        
    async def _get_crypto_info(self, symbol: str) -> Dict[str, Any]:
        """Get cryptocurrency information"""
        # Using CoinGecko API (free tier)
        async with aiohttp.ClientSession() as session:
            # Get coin ID from symbol
            async with session.get("https://api.coingecko.com/api/v3/coins/list") as response:
                if response.status == 200:
                    coins = await response.json()
                    coin_id = None
                    for coin in coins:
                        if coin["symbol"].lower() == symbol.lower():
                            coin_id = coin["id"]
                            break
                            
                    if coin_id:
                        # Get price data
                        params = {
                            "ids": coin_id,
                            "vs_currencies": "usd",
                            "include_24hr_change": "true",
                            "include_market_cap": "true",
                            "include_24hr_vol": "true"
                        }
                        
                        async with session.get("https://api.coingecko.com/api/v3/simple/price", params=params) as response:
                            if response.status == 200:
                                data = await response.json()
                                coin_data = data[coin_id]
                                
                                return {
                                    "symbol": symbol.upper(),
                                    "name": coin_id.replace("-", " ").title(),
                                    "price": coin_data["usd"],
                                    "change_24h": coin_data.get("usd_24h_change", 0),
                                    "market_cap": coin_data.get("usd_market_cap", 0),
                                    "volume_24h": coin_data.get("usd_24h_vol", 0)
                                }
                                
        return {"error": f"Failed to fetch crypto info for {symbol}"}
        
    async def _get_wikipedia_summary(self, topic: str) -> Dict[str, Any]:
        """Get Wikipedia summary"""
        async with aiohttp.ClientSession() as session:
            params = {
                "action": "query",
                "format": "json",
                "titles": topic,
                "prop": "extracts",
                "exintro": True,
                "explaintext": True,
                "exsentences": 3
            }
            
            async with session.get("https://en.wikipedia.org/w/api.php", params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    pages = data["query"]["pages"]
                    
                    for page_id, page_data in pages.items():
                        if "extract" in page_data:
                            return {
                                "topic": page_data["title"],
                                "summary": page_data["extract"],
                                "url": f"https://en.wikipedia.org/wiki/{page_data['title'].replace(' ', '_')}"
                            }
                            
        return {"error": f"No Wikipedia article found for {topic}"}

class HomeAutomationController:
    """Basic home automation control interface"""
    
    def __init__(self):
        self.devices = {}
        self.scenes = {}
        self.automations = []
        
    def register_device(self, device_id: str, device_info: Dict[str, Any]):
        """Register a smart home device"""
        self.devices[device_id] = {
            "id": device_id,
            "name": device_info.get("name", device_id),
            "type": device_info.get("type", "unknown"),
            "capabilities": device_info.get("capabilities", []),
            "state": device_info.get("initial_state", {}),
            "online": True
        }
        
    async def control_device(self, device_id: str, action: str, parameters: Dict[str, Any] = None) -> Dict[str, Any]:
        """Control a smart home device"""
        if device_id not in self.devices:
            return {"error": f"Device {device_id} not found"}
            
        device = self.devices[device_id]
        
        # Simulate device control
        if action == "turn_on":
            device["state"]["power"] = "on"
        elif action == "turn_off":
            device["state"]["power"] = "off"
        elif action == "set_brightness":
            if "brightness" in device["capabilities"]:
                device["state"]["brightness"] = parameters.get("level", 100)
        elif action == "set_temperature":
            if "temperature" in device["capabilities"]:
                device["state"]["temperature"] = parameters.get("temperature", 20)
        elif action == "set_color":
            if "color" in device["capabilities"]:
                device["state"]["color"] = parameters.get("color", "#FFFFFF")
                
        return {
            "device_id": device_id,
            "action": action,
            "new_state": device["state"],
            "success": True
        }
        
    def create_scene(self, scene_name: str, devices_config: List[Dict[str, Any]]):
        """Create a scene with multiple device configurations"""
        self.scenes[scene_name] = {
            "name": scene_name,
            "devices": devices_config,
            "created_at": datetime.now().isoformat()
        }
        
    async def activate_scene(self, scene_name: str) -> List[Dict[str, Any]]:
        """Activate a predefined scene"""
        if scene_name not in self.scenes:
            return [{"error": f"Scene {scene_name} not found"}]
            
        scene = self.scenes[scene_name]
        results = []
        
        for device_config in scene["devices"]:
            device_id = device_config["device_id"]
            for action, params in device_config["actions"].items():
                result = await self.control_device(device_id, action, params)
                results.append(result)
                
        return results
        
    def get_device_status(self, device_id: Optional[str] = None) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """Get status of one or all devices"""
        if device_id:
            return self.devices.get(device_id, {"error": f"Device {device_id} not found"})
        else:
            return list(self.devices.values())

class TaskExecutor:
    """Executes automation tasks with planning capabilities"""
    
    def __init__(self):
        self.actions: Dict[str, AutomationAction] = {}
        self.tasks: Dict[str, AutomationTask] = {}
        self.scheduler = AsyncIOScheduler()
        self.running_tasks = set()
        
        # Register built-in actions
        self._register_builtin_actions()
        
    def _register_builtin_actions(self):
        """Register built-in automation actions"""
        # Will be implemented with specific action classes
        pass
        
    def register_action(self, name: str, action: AutomationAction):
        """Register a new automation action"""
        self.actions[name] = action
        
    async def create_task(self, task: AutomationTask) -> str:
        """Create a new automation task"""
        self.tasks[task.id] = task
        
        # Schedule if trigger is specified
        if task.trigger:
            self._schedule_task(task)
            
        return task.id
        
    def _schedule_task(self, task: AutomationTask):
        """Schedule a task based on its trigger"""
        trigger_type = task.trigger.get("type")
        
        if trigger_type == "time":
            # One-time execution at specific time
            run_time = datetime.fromisoformat(task.trigger["time"])
            self.scheduler.add_job(
                self.execute_task,
                trigger=DateTrigger(run_date=run_time),
                args=[task.id],
                id=f"task_{task.id}"
            )
        elif trigger_type == "interval":
            # Recurring execution
            self.scheduler.add_job(
                self.execute_task,
                trigger=IntervalTrigger(**task.trigger["interval"]),
                args=[task.id],
                id=f"task_{task.id}"
            )
        elif trigger_type == "cron":
            # Cron-based execution
            self.scheduler.add_job(
                self.execute_task,
                trigger=CronTrigger(**task.trigger["cron"]),
                args=[task.id],
                id=f"task_{task.id}"
            )
            
    async def execute_task(self, task_id: str) -> Dict[str, Any]:
        """Execute a specific task"""
        if task_id not in self.tasks:
            return {"error": f"Task {task_id} not found"}
            
        task = self.tasks[task_id]
        
        # Check if already running
        if task_id in self.running_tasks:
            return {"error": f"Task {task_id} is already running"}
            
        # Mark as running
        self.running_tasks.add(task_id)
        task.status = TaskStatus.RUNNING
        
        try:
            # Get action
            if task.action not in self.actions:
                raise ValueError(f"Unknown action: {task.action}")
                
            action = self.actions[task.action]
            
            # Validate parameters
            if not action.validate_parameters(task.parameters):
                raise ValueError("Invalid parameters for action")
                
            # Execute action
            result = await action.execute(task.parameters)
            
            # Update task
            task.status = TaskStatus.COMPLETED
            task.executed_at = datetime.now()
            task.result = result
            
            return {
                "task_id": task_id,
                "status": "completed",
                "result": result
            }
            
        except Exception as e:
            # Handle failure
            task.status = TaskStatus.FAILED
            task.error = str(e)
            
            return {
                "task_id": task_id,
                "status": "failed",
                "error": str(e)
            }
            
        finally:
            # Remove from running set
            self.running_tasks.discard(task_id)
            
    def get_task_status(self, task_id: str) -> Optional[AutomationTask]:
        """Get task status"""
        return self.tasks.get(task_id)
        
    def list_tasks(self, status: Optional[TaskStatus] = None) -> List[AutomationTask]:
        """List tasks, optionally filtered by status"""
        tasks = list(self.tasks.values())
        
        if status:
            tasks = [t for t in tasks if t.status == status]
            
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)
        
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending or running task"""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            
            # Remove from scheduler if scheduled
            try:
                self.scheduler.remove_job(f"task_{task_id}")
            except Exception:
                pass

            # Update status
            task.status = TaskStatus.CANCELLED
            
            return True
            
        return False
        
    def create_task_plan(self, goal: str, context: Dict[str, Any]) -> List[AutomationTask]:
        """Create a task plan to achieve a goal"""
        # This is a simplified version - can be enhanced with AI planning
        tasks = []
        
        # Parse goal and create tasks
        goal_lower = goal.lower()
        
        if "meeting" in goal_lower or "appointment" in goal_lower:
            # Create calendar event task
            tasks.append(AutomationTask(
                id=f"task_{datetime.now().timestamp()}",
                name="Schedule meeting",
                action="create_calendar_event",
                parameters={"text": goal}
            ))
            
        if "weather" in goal_lower:
            # Create weather check task
            location = context.get("default_location", "London")
            tasks.append(AutomationTask(
                id=f"task_{datetime.now().timestamp()}",
                name="Check weather",
                action="get_weather",
                parameters={"location": location}
            ))
            
        if "reminder" in goal_lower or "remind" in goal_lower:
            # Create reminder task
            tasks.append(AutomationTask(
                id=f"task_{datetime.now().timestamp()}",
                name="Set reminder",
                action="create_reminder",
                parameters={"text": goal}
            ))
            
        if "lights" in goal_lower or "turn on" in goal_lower or "turn off" in goal_lower:
            # Create home automation task
            tasks.append(AutomationTask(
                id=f"task_{datetime.now().timestamp()}",
                name="Control lights",
                action="control_device",
                parameters={"command": goal}
            ))
            
        return tasks

class AutomationEngine:
    """Main automation engine integrating all services"""
    
    def __init__(self):
        self.calendar = CalendarManager()
        self.weather = WeatherService()
        self.information = InformationService()
        self.home_automation = HomeAutomationController()
        self.task_executor = TaskExecutor()
        
        # Start services
        self.calendar.start()
        self.task_executor.scheduler.start()
        
    async def process_command(self, command: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Process an automation command"""
        command_lower = command.lower()
        context = context or {}
        
        # Calendar commands
        if any(word in command_lower for word in ["schedule", "meeting", "appointment", "calendar"]):
            event = self.calendar.create_event_from_text(command)
            if event:
                event_id = self.calendar.add_event(event)
                return {
                    "type": "calendar",
                    "action": "event_created",
                    "event": event,
                    "message": f"Scheduled '{event.title}' for {event.start_time.strftime('%B %d at %I:%M %p')}"
                }
                
        # Weather commands
        if any(word in command_lower for word in ["weather", "temperature", "forecast"]):
            location = context.get("location", "London")
            
            # Extract location from command
            import re
            location_match = re.search(r"(?:in|at|for)\s+([A-Za-z\s]+)", command)
            if location_match:
                location = location_match.group(1).strip()
                
            if "forecast" in command_lower:
                forecast = await self.weather.get_forecast(location)
                return {
                    "type": "weather",
                    "action": "forecast",
                    "data": forecast,
                    "message": f"Weather forecast for {location}"
                }
            else:
                weather = await self.weather.get_current_weather(location)
                return {
                    "type": "weather",
                    "action": "current",
                    "data": weather,
                    "message": self.weather.format_weather_report(weather)
                }
                
        # Information commands
        if any(word in command_lower for word in ["news", "stock", "crypto", "bitcoin", "information"]):
            query_type = None
            query = ""
            
            if "news" in command_lower:
                query_type = "news"
                query = command.replace("news", "").strip()
            elif "stock" in command_lower:
                query_type = "stock"
                # Extract stock symbol
                words = command.split()
                for i, word in enumerate(words):
                    if word.lower() == "stock" and i + 1 < len(words):
                        query = words[i + 1].upper()
                        break
            elif any(crypto in command_lower for crypto in ["crypto", "bitcoin", "ethereum"]):
                query_type = "crypto"
                if "bitcoin" in command_lower:
                    query = "BTC"
                elif "ethereum" in command_lower:
                    query = "ETH"
                else:
                    # Extract crypto symbol
                    words = command.split()
                    for word in words:
                        if word.upper() in ["BTC", "ETH", "ADA", "DOT", "LINK"]:
                            query = word.upper()
                            break
                            
            if query_type and query:
                info = await self.information.get_information(query_type, query)
                return {
                    "type": "information",
                    "action": query_type,
                    "data": info,
                    "message": f"Here's the {query_type} information for {query}"
                }
                
        # Home automation commands
        if any(word in command_lower for word in ["lights", "temperature", "turn on", "turn off", "dim", "brighten"]):
            # Simple device control
            action = None
            device_type = None
            
            if "turn on" in command_lower:
                action = "turn_on"
            elif "turn off" in command_lower:
                action = "turn_off"
            elif "dim" in command_lower:
                action = "set_brightness"
            elif "brighten" in command_lower:
                action = "set_brightness"
                
            if "lights" in command_lower:
                device_type = "light"
            elif "temperature" in command_lower or "thermostat" in command_lower:
                device_type = "thermostat"
                
            if action and device_type:
                # Find matching devices
                matching_devices = [
                    device_id for device_id, device in self.home_automation.devices.items()
                    if device["type"] == device_type
                ]
                
                if matching_devices:
                    results = []
                    for device_id in matching_devices:
                        result = await self.home_automation.control_device(device_id, action)
                        results.append(result)
                        
                    return {
                        "type": "home_automation",
                        "action": action,
                        "results": results,
                        "message": f"Executed {action} for {len(results)} {device_type}(s)"
                    }
                    
        # Task planning
        if any(word in command_lower for word in ["plan", "create task", "automate"]):
            # Create task plan
            tasks = self.task_executor.create_task_plan(command, context)

            # Execute tasks
            results = []
            for task in tasks:
                task_id = await self.task_executor.create_task(task)
                result = await self.task_executor.execute_task(task_id)
                results.append(result)

            return {
                "type": "task_plan",
                "action": "executed",
                "tasks": [t.__dict__ for t in tasks],
                "results": results,
                "message": f"Created and executed {len(tasks)} tasks"
            }

        # Display connection commands - route to JARVIS voice API
        if any(word in command_lower for word in ["tv", "display", "monitor", "screen mirroring", "airplay", "living room", "bedroom", "kitchen"]):
            # Route to JARVIS voice API for proper handling
            try:
                from api.jarvis_voice_api import unified_command_processor
                from pydantic import BaseModel

                class CommandRequest(BaseModel):
                    text: str

                request = CommandRequest(text=command)
                result = await unified_command_processor(request)

                return {
                    "type": "display",
                    "action": "routed_to_jarvis",
                    "result": result,
                    "message": result.get("message", "Display command processed")
                }
            except Exception as e:
                return {
                    "type": "error",
                    "message": f"Failed to process display command: {str(e)}"
                }

        return {
            "type": "unknown",
            "message": "I couldn't understand that command. Try asking about weather, calendar, news, home automation, or display connections."
        }
        
    def shutdown(self):
        """Shutdown automation engine"""
        self.calendar.stop()
        self.task_executor.scheduler.shutdown()

# Example usage
if __name__ == "__main__":
    async def test_automation():
        engine = AutomationEngine()
        
        # Test calendar
        result = await engine.process_command("Schedule meeting with John tomorrow at 3pm")
        print(f"Calendar: {result}")
        
        # Test weather
        result = await engine.process_command("What's the weather in New York?")
        print(f"Weather: {result}")
        
        # Test information
        result = await engine.process_command("Get news about technology")
        print(f"News: {result}")
        
        engine.shutdown()
        
    # Run test
    asyncio.run(test_automation())