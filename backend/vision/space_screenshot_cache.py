#!/usr/bin/env python3
"""
Smart Screenshot Caching System for JARVIS Multi-Space Awareness
Implements predictive caching, pattern learning, and confidence scoring
"""

import asyncio
import os
import time
import logging
import pickle
import json
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict, deque
from enum import Enum
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

class CacheConfidence(Enum):
    """Confidence levels for cached data"""
    FRESH = "fresh"              # < 30 seconds
    RECENT = "recent"            # < 2 minutes  
    USABLE = "usable"           # < 5 minutes
    STALE = "stale"             # < 15 minutes
    OUTDATED = "outdated"       # > 15 minutes

@dataclass
class CachedScreenshot:
    """Cached screenshot with metadata"""
    space_id: int
    screenshot: Image.Image
    timestamp: datetime
    window_count: int
    active_apps: List[str]
    triggered_by: str  # "natural_switch", "requested", "predictive"
    access_count: int = 0
    last_accessed: Optional[datetime] = None
    
    def age_seconds(self) -> float:
        """Get age in seconds"""
        return (datetime.now() - self.timestamp).total_seconds()
        
    def confidence_level(self) -> CacheConfidence:
        """Determine confidence level based on age"""
        age = self.age_seconds()
        if age < 30:
            return CacheConfidence.FRESH
        elif age < 120:
            return CacheConfidence.RECENT
        elif age < 300:
            return CacheConfidence.USABLE
        elif age < 900:
            return CacheConfidence.STALE
        else:
            return CacheConfidence.OUTDATED
            
    def confidence_score(self) -> float:
        """Calculate numeric confidence score (0-1)"""
        age = self.age_seconds()
        # Exponential decay with half-life of 5 minutes
        half_life = 300  # 5 minutes
        return 0.5 ** (age / half_life)

@dataclass
class SpaceSwitchEvent:
    """Record of space switching event"""
    from_space: int
    to_space: int
    timestamp: datetime
    reason: str  # "user", "app_launch", "notification", etc.
    
@dataclass
class SpaceUsagePattern:
    """Learned patterns of space usage"""
    space_id: int
    access_times: List[datetime] = field(default_factory=list)
    avg_duration: float = 0.0  # Average time spent on space
    common_transitions: Dict[int, int] = field(default_factory=dict)  # to_space -> count
    time_of_day_histogram: np.ndarray = field(default_factory=lambda: np.zeros(24))
    app_associations: Dict[str, int] = field(default_factory=dict)  # app -> count
    
    def predict_next_access(self) -> Optional[datetime]:
        """Predict when this space might be accessed next"""
        if len(self.access_times) < 3:
            return None
            
        # Simple prediction based on average interval
        intervals = []
        for i in range(1, len(self.access_times)):
            interval = (self.access_times[i] - self.access_times[i-1]).total_seconds()
            intervals.append(interval)
            
        avg_interval = np.mean(intervals)
        last_access = self.access_times[-1]
        
        return last_access + timedelta(seconds=avg_interval)
        
    def likelihood_score(self, current_hour: int) -> float:
        """Score how likely this space is to be needed now"""
        # Base score from time of day
        time_score = self.time_of_day_histogram[current_hour] / max(1, np.sum(self.time_of_day_histogram))
        
        # Recency bonus
        if self.access_times:
            last_access = self.access_times[-1]
            hours_since = (datetime.now() - last_access).total_seconds() / 3600
            recency_score = 0.5 ** (hours_since / 2)  # Half-life of 2 hours
        else:
            recency_score = 0
            
        return (time_score * 0.6) + (recency_score * 0.4)

class SpaceScreenshotCache:
    """Intelligent screenshot cache with pattern learning and predictive caching"""
    
    def __init__(self, cache_dir: Optional[Path] = None, max_cache_size_mb: int = 500):
        # Cache storage
        self.cache: Dict[int, CachedScreenshot] = {}
        self.cache_dir = cache_dir or Path.home() / ".jarvis" / "space_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Limits
        self.max_cache_size_mb = max_cache_size_mb
        self.max_entries_per_space = 3  # Keep last 3 screenshots per space
        self.ttl_minutes = 15
        
        # Pattern learning
        self.usage_patterns: Dict[int, SpaceUsagePattern] = defaultdict(SpaceUsagePattern)
        self.switch_history: deque[SpaceSwitchEvent] = deque(maxlen=100)
        self.current_space: Optional[int] = None
        self.space_entry_time: Optional[datetime] = None
        
        # Performance tracking
        self.cache_hits = 0
        self.cache_misses = 0
        self.total_requests = 0
        
        # Background tasks
        self.predictive_cache_task = None
        self._load_patterns()
        
    def _load_patterns(self):
        """Load saved usage patterns"""
        pattern_file = self.cache_dir / "usage_patterns.pkl"
        if pattern_file.exists():
            try:
                with open(pattern_file, 'rb') as f:
                    saved_data = pickle.load(f)
                    self.usage_patterns = saved_data['patterns']
                    self.switch_history = saved_data['history']
                    logger.info(f"Loaded usage patterns for {len(self.usage_patterns)} spaces")
            except Exception as e:
                logger.error(f"Failed to load usage patterns: {e}")
                
    def _save_patterns(self):
        """Save usage patterns for persistence"""
        pattern_file = self.cache_dir / "usage_patterns.pkl"
        try:
            saved_data = {
                'patterns': self.usage_patterns,
                'history': self.switch_history
            }
            with open(pattern_file, 'wb') as f:
                pickle.dump(saved_data, f)
        except Exception as e:
            logger.error(f"Failed to save usage patterns: {e}")
            
    async def start_predictive_caching(self):
        """Start background task for predictive caching"""
        if self.predictive_cache_task is None:
            self.predictive_cache_task = asyncio.create_task(self._predictive_cache_loop())
            
    async def stop_predictive_caching(self):
        """Stop predictive caching"""
        if self.predictive_cache_task:
            self.predictive_cache_task.cancel()
            try:
                await self.predictive_cache_task
            except asyncio.CancelledError:
                pass
            self.predictive_cache_task = None
            
    async def _predictive_cache_loop(self):
        """Background loop for predictive caching"""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            try:
                # Check patterns every minute
                await asyncio.sleep(60)

                # Get spaces likely to be needed soon
                likely_spaces = self._get_likely_spaces()

                for space_id in likely_spaces[:3]:  # Pre-cache top 3
                    if space_id not in self.cache or self.cache[space_id].confidence_level() == CacheConfidence.OUTDATED:
                        logger.info(f"Predictive caching for space {space_id}")
                        # Request cache update (implementation depends on your capture method)
                        # await self.request_cache_update(space_id, "predictive")

            except Exception as e:
                logger.error(f"Error in predictive cache loop: {e}")
        else:
            logger.info("Space screenshot predictive cache loop timeout, stopping")
                
    def _get_likely_spaces(self) -> List[int]:
        """Get spaces likely to be needed soon"""
        current_hour = datetime.now().hour
        
        # Score each space
        space_scores = []
        for space_id, pattern in self.usage_patterns.items():
            score = pattern.likelihood_score(current_hour)
            space_scores.append((space_id, score))
            
        # Sort by score
        space_scores.sort(key=lambda x: x[1], reverse=True)
        
        return [space_id for space_id, _ in space_scores]
        
    def add_screenshot(self, 
                      space_id: int, 
                      screenshot: Image.Image,
                      window_count: int,
                      active_apps: List[str],
                      triggered_by: str = "natural_switch") -> CachedScreenshot:
        """Add screenshot to cache"""
        
        # Create cached entry
        cached = CachedScreenshot(
            space_id=space_id,
            screenshot=screenshot,
            timestamp=datetime.now(),
            window_count=window_count,
            active_apps=active_apps,
            triggered_by=triggered_by
        )
        
        # Update cache
        self.cache[space_id] = cached
        
        # Update patterns
        self._update_patterns(space_id, active_apps)
        
        # Clean old entries
        self._clean_cache()
        
        # Save to disk for persistence
        self._save_screenshot_to_disk(cached)
        
        return cached
        
    def get_screenshot(self, 
                      space_id: int, 
                      max_age_seconds: Optional[float] = None) -> Optional[CachedScreenshot]:
        """Get cached screenshot if available and fresh enough"""
        self.total_requests += 1
        
        if space_id not in self.cache:
            self.cache_misses += 1
            # Try to load from disk
            cached = self._load_screenshot_from_disk(space_id)
            if cached:
                self.cache[space_id] = cached
            else:
                return None
                
        cached = self.cache[space_id]
        
        # Check age if specified
        if max_age_seconds and cached.age_seconds() > max_age_seconds:
            self.cache_misses += 1
            return None
            
        # Update access info
        cached.access_count += 1
        cached.last_accessed = datetime.now()
        
        self.cache_hits += 1
        return cached
        
    def record_space_switch(self, from_space: int, to_space: int, reason: str = "user"):
        """Record space switching event"""
        
        # Update duration for previous space
        if self.current_space is not None and self.space_entry_time is not None:
            duration = (datetime.now() - self.space_entry_time).total_seconds()
            pattern = self.usage_patterns[self.current_space]
            if pattern.avg_duration == 0:
                pattern.avg_duration = duration
            else:
                # Weighted average
                pattern.avg_duration = (pattern.avg_duration * 0.9) + (duration * 0.1)
                
        # Record switch
        event = SpaceSwitchEvent(
            from_space=from_space,
            to_space=to_space,
            timestamp=datetime.now(),
            reason=reason
        )
        self.switch_history.append(event)
        
        # Update patterns
        if from_space in self.usage_patterns:
            pattern = self.usage_patterns[from_space]
            pattern.common_transitions[to_space] = pattern.common_transitions.get(to_space, 0) + 1
            
        # Update current space
        self.current_space = to_space
        self.space_entry_time = datetime.now()
        
        # Save patterns periodically
        if len(self.switch_history) % 10 == 0:
            self._save_patterns()
            
    def _update_patterns(self, space_id: int, active_apps: List[str]):
        """Update usage patterns for a space"""
        pattern = self.usage_patterns[space_id]
        pattern.space_id = space_id
        pattern.access_times.append(datetime.now())
        
        # Update time of day histogram
        current_hour = datetime.now().hour
        pattern.time_of_day_histogram[current_hour] += 1
        
        # Update app associations
        for app in active_apps:
            pattern.app_associations[app] = pattern.app_associations.get(app, 0) + 1
            
    def _clean_cache(self):
        """Clean old entries from cache"""
        # Remove outdated entries
        outdated_spaces = []
        for space_id, cached in self.cache.items():
            if cached.confidence_level() == CacheConfidence.OUTDATED:
                outdated_spaces.append(space_id)
                
        for space_id in outdated_spaces:
            del self.cache[space_id]
            # Also remove from disk
            cache_file = self.cache_dir / f"space_{space_id}.pkl"
            if cache_file.exists():
                cache_file.unlink()
                
    def _save_screenshot_to_disk(self, cached: CachedScreenshot):
        """Save screenshot to disk for persistence"""
        cache_file = self.cache_dir / f"space_{cached.space_id}.pkl"
        
        # Convert PIL Image to bytes for pickling
        import io
        img_bytes = io.BytesIO()
        cached.screenshot.save(img_bytes, format='PNG')
        img_data = img_bytes.getvalue()
        
        # Save metadata and image data
        save_data = {
            'space_id': cached.space_id,
            'timestamp': cached.timestamp,
            'window_count': cached.window_count,
            'active_apps': cached.active_apps,
            'triggered_by': cached.triggered_by,
            'image_data': img_data
        }
        
        try:
            with open(cache_file, 'wb') as f:
                pickle.dump(save_data, f)
        except Exception as e:
            logger.error(f"Failed to save screenshot to disk: {e}")
            
    def _load_screenshot_from_disk(self, space_id: int) -> Optional[CachedScreenshot]:
        """Load screenshot from disk cache"""
        cache_file = self.cache_dir / f"space_{space_id}.pkl"
        
        if not cache_file.exists():
            return None
            
        try:
            with open(cache_file, 'rb') as f:
                save_data = pickle.load(f)
                
            # Reconstruct PIL Image
            import io
            img_bytes = io.BytesIO(save_data['image_data'])
            screenshot = Image.open(img_bytes)
            
            return CachedScreenshot(
                space_id=save_data['space_id'],
                screenshot=screenshot,
                timestamp=save_data['timestamp'],
                window_count=save_data['window_count'],
                active_apps=save_data['active_apps'],
                triggered_by=save_data['triggered_by']
            )
        except Exception as e:
            logger.error(f"Failed to load screenshot from disk: {e}")
            return None
            
    def get_cache_statistics(self) -> Dict[str, Any]:
        """Get cache performance statistics"""
        hit_rate = self.cache_hits / max(1, self.total_requests)
        
        # Space coverage
        cached_spaces = list(self.cache.keys())
        space_freshness = {}
        for space_id, cached in self.cache.items():
            space_freshness[space_id] = {
                'confidence': cached.confidence_level().value,
                'age_seconds': cached.age_seconds(),
                'access_count': cached.access_count
            }
            
        return {
            'hit_rate': hit_rate,
            'total_requests': self.total_requests,
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'cached_spaces': cached_spaces,
            'space_freshness': space_freshness,
            'pattern_spaces': len(self.usage_patterns)
        }
        
    def suggest_cache_actions(self, query_intent: str) -> Dict[str, Any]:
        """Suggest cache actions based on query intent"""
        suggestions = {
            'use_cache': True,
            'max_acceptable_age': 300,  # 5 minutes default
            'request_fresh': False,
            'predictive_spaces': []
        }
        
        # Adjust based on query type
        if 'error' in query_intent.lower() or 'read' in query_intent.lower():
            suggestions['use_cache'] = False
            suggestions['request_fresh'] = True
        elif 'overview' in query_intent.lower() or 'what' in query_intent.lower():
            suggestions['max_acceptable_age'] = 600  # 10 minutes OK for overview
            
        # Suggest spaces to pre-cache
        suggestions['predictive_spaces'] = self._get_likely_spaces()[:2]
        
        return suggestions