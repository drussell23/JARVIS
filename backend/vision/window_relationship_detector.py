#!/usr/bin/env python3
"""
Enhanced Window Relationship Detection for JARVIS Multi-Window Intelligence
Memory-optimized relationship detection with no hardcoded values
Optimized for 16GB RAM macOS systems
"""

import re
import logging
import os
import json
import gc
import time
import psutil
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass, field
from collections import defaultdict, deque
import difflib
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@dataclass
class WindowRelationship:
    """Represents a relationship between two windows"""
    window1_id: int
    window2_id: int
    relationship_type: str  # 'ide_documentation', 'ide_terminal', 'browser_reference', etc.
    confidence: float  # 0.0 to 1.0
    evidence: List[str]  # Reasons for the relationship
    timestamp: datetime = field(default_factory=datetime.now)
    memory_size_bytes: int = 0
    
    def __post_init__(self):
        """Calculate memory size after initialization"""
        self.memory_size_bytes = self._calculate_memory_size()
    
    def _calculate_memory_size(self) -> int:
        """Estimate memory usage"""
        size = len(self.relationship_type.encode())
        size += sum(len(e.encode()) for e in self.evidence)
        size += 32  # IDs and float
        return size

@dataclass
class WindowGroup:
    """A group of related windows with memory tracking"""
    group_id: str
    windows: List[Any]  # WindowInfo objects
    group_type: str  # 'project', 'communication', 'research', etc.
    confidence: float
    common_elements: List[str]
    timestamp: datetime = field(default_factory=datetime.now)
    memory_size_bytes: int = 0
    
    def __post_init__(self):
        """Calculate memory size after initialization"""
        self.memory_size_bytes = self._calculate_memory_size()
    
    def _calculate_memory_size(self) -> int:
        """Estimate memory usage"""
        size = len(self.group_id.encode()) + len(self.group_type.encode())
        size += sum(len(e.encode()) for e in self.common_elements)
        size += len(self.windows) * 100  # Estimate per window
        return size

class ConfigurableWindowRelationshipDetector:
    """Memory-aware relationship detector with full configurability"""
    
    def __init__(self):
        # Load configuration
        self.config = self._load_config()
        
        # Load configurable app lists
        self.ide_apps = self._load_app_list('ide_apps')
        self.doc_apps = self._load_app_list('doc_apps')
        self.terminal_apps = self._load_app_list('terminal_apps')
        self.comm_apps = self._load_app_list('comm_apps')
        
        # Load patterns
        self.project_indicators = self._load_patterns('project_indicators')
        self.doc_domains = self._load_patterns('doc_domains')
        
        # Memory management
        self.relationship_cache = deque(maxlen=self.config['max_cached_relationships'])
        self.group_cache = deque(maxlen=self.config['max_cached_groups'])
        self.cache_timestamps = {}
        self.total_memory_used = 0
        
        # Stats
        self.stats = {
            'relationships_detected': 0,
            'groups_formed': 0,
            'cache_hits': 0,
            'memory_cleanups': 0,
            'current_memory_mb': 0,
            'peak_memory_mb': 0
        }
        
        # Start cleanup task
        self._last_cleanup = time.time()
        
        logger.info(f"Configurable Window Relationship Detector initialized with config: {self.config}")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        return {
            # Memory limits
            'max_memory_mb': int(os.getenv('WINDOW_REL_MAX_MEMORY_MB', '50')),  # 50MB limit
            'max_cached_relationships': int(os.getenv('WINDOW_REL_MAX_CACHED', '100')),
            'max_cached_groups': int(os.getenv('WINDOW_REL_MAX_GROUPS', '20')),
            'cache_ttl_seconds': int(os.getenv('WINDOW_REL_CACHE_TTL', '300')),  # 5 minutes
            
            # Analysis settings
            'min_confidence': float(os.getenv('WINDOW_REL_MIN_CONFIDENCE', '0.5')),
            'group_min_confidence': float(os.getenv('WINDOW_REL_GROUP_MIN_CONF', '0.6')),
            'max_windows_to_analyze': int(os.getenv('WINDOW_REL_MAX_ANALYZE', '50')),
            
            # Thresholds
            'title_similarity_threshold': float(os.getenv('WINDOW_REL_TITLE_SIM', '0.6')),
            'common_word_min_length': int(os.getenv('WINDOW_REL_WORD_MIN_LEN', '3')),
            'group_common_word_ratio': float(os.getenv('WINDOW_REL_WORD_RATIO', '0.4')),
            
            # Memory pressure
            'low_memory_mb': int(os.getenv('WINDOW_REL_LOW_MEMORY_MB', '2000')),
            'critical_memory_mb': int(os.getenv('WINDOW_REL_CRITICAL_MEMORY_MB', '1000')),
            
            # Cleanup
            'cleanup_interval_seconds': int(os.getenv('WINDOW_REL_CLEANUP_INTERVAL', '60'))
        }
    
    def _load_app_list(self, list_name: str) -> Set[str]:
        """Load app list from environment or defaults"""
        env_var = f'WINDOW_REL_{list_name.upper()}'
        apps_json = os.getenv(env_var)
        
        if apps_json:
            try:
                return set(json.loads(apps_json))
            except Exception as e:
                logger.warning(f"Failed to load {list_name} from env: {e}")
        
        # Defaults
        defaults = {
            'ide_apps': {
                'Visual Studio Code', 'Cursor', 'Xcode', 'IntelliJ IDEA', 
                'PyCharm', 'WebStorm', 'Sublime Text', 'Atom', 'TextMate'
            },
            'doc_apps': {
                'Chrome', 'Safari', 'Firefox', 'Preview', 'Books', 
                'Dash', 'DevDocs', 'Notion', 'Obsidian'
            },
            'terminal_apps': {
                'Terminal', 'iTerm', 'Alacritty', 'Hyper', 'Warp'
            },
            'comm_apps': {
                'Discord', 'Slack', 'Messages', 'Mail', 'Telegram', 
                'WhatsApp', 'Signal', 'Teams'
            }
        }
        
        return defaults.get(list_name, set())
    
    def _load_patterns(self, pattern_name: str) -> List[str]:
        """Load patterns from environment or defaults"""
        env_var = f'WINDOW_REL_{pattern_name.upper()}'
        patterns_json = os.getenv(env_var)
        
        if patterns_json:
            try:
                return json.loads(patterns_json)
            except Exception as e:
                logger.warning(f"Failed to load {pattern_name} from env: {e}")
        
        # Defaults
        defaults = {
            'project_indicators': [
                r'\.git', r'package\.json', r'requirements\.txt', r'Cargo\.toml',
                r'pom\.xml', r'build\.gradle', r'\.xcodeproj', r'\.sln'
            ],
            'doc_domains': [
                'stackoverflow.com', 'github.com', 'docs.', 'developer.',
                'api.', 'reference.', 'tutorial.', 'guide.', 'npm',
                'pypi.org', 'crates.io', 'rubygems.org'
            ]
        }
        
        return defaults.get(pattern_name, [])
    
    def _check_memory_available(self) -> bool:
        """Check if we have enough memory to continue"""
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        process_mb = psutil.Process().memory_info().rss / 1024 / 1024
        
        # Update stats
        self.stats['current_memory_mb'] = process_mb
        self.stats['peak_memory_mb'] = max(self.stats['peak_memory_mb'], process_mb)
        
        # Check limits
        if available_mb < self.config['critical_memory_mb']:
            logger.warning(f"Critical system memory: {available_mb}MB")
            self._emergency_cleanup()
            return False
        
        if process_mb > self.config['max_memory_mb']:
            logger.warning(f"Process memory {process_mb}MB exceeds limit")
            self._cleanup_caches()
            return False
        
        return True
    
    def _cleanup_caches(self):
        """Clean up old cached data"""
        current_time = time.time()
        ttl = self.config['cache_ttl_seconds']
        
        # Clean relationships
        new_relationships = []
        for rel in self.relationship_cache:
            if (current_time - rel.timestamp.timestamp()) < ttl:
                new_relationships.append(rel)
        
        self.relationship_cache = deque(new_relationships, maxlen=self.config['max_cached_relationships'])
        
        # Clean groups
        new_groups = []
        for group in self.group_cache:
            if (current_time - group.timestamp.timestamp()) < ttl:
                new_groups.append(group)
        
        self.group_cache = deque(new_groups, maxlen=self.config['max_cached_groups'])
        
        # Update memory tracking
        self.total_memory_used = sum(r.memory_size_bytes for r in self.relationship_cache)
        self.total_memory_used += sum(g.memory_size_bytes for g in self.group_cache)
        
        self.stats['memory_cleanups'] += 1
        gc.collect()
    
    def _emergency_cleanup(self):
        """Emergency cleanup when memory is critical"""
        logger.warning("Emergency cleanup triggered")
        self.relationship_cache.clear()
        self.group_cache.clear()
        self.cache_timestamps.clear()
        self.total_memory_used = 0
        gc.collect()
    
    def _maybe_cleanup(self):
        """Periodic cleanup check"""
        current_time = time.time()
        if current_time - self._last_cleanup > self.config['cleanup_interval_seconds']:
            self._cleanup_caches()
            self._last_cleanup = current_time
    
    def detect_relationships(self, windows: List[Any]) -> List[WindowRelationship]:
        """Detect all relationships between windows with memory management"""
        # Check memory
        if not self._check_memory_available():
            logger.warning("Skipping relationship detection due to memory pressure")
            return []
        
        # Limit windows to analyze
        if len(windows) > self.config['max_windows_to_analyze']:
            # Prioritize visible windows
            windows = sorted(windows, key=lambda w: not getattr(w, 'is_visible', True))
            windows = windows[:self.config['max_windows_to_analyze']]
        
        relationships = []
        
        # Check cache for existing relationships
        cached_pairs = set()
        for rel in self.relationship_cache:
            cached_pairs.add((rel.window1_id, rel.window2_id))
            cached_pairs.add((rel.window2_id, rel.window1_id))
            if self._is_cache_valid(rel):
                relationships.append(rel)
                self.stats['cache_hits'] += 1
        
        # Check each pair of windows
        for i, window1 in enumerate(windows):
            for j, window2 in enumerate(windows[i+1:], i+1):
                # Skip if already in cache
                if (window1.window_id, window2.window_id) in cached_pairs:
                    continue
                
                relationship = self._analyze_window_pair(window1, window2)
                if relationship and relationship.confidence >= self.config['min_confidence']:
                    relationships.append(relationship)
                    # Add to cache
                    self._add_to_cache(relationship)
                    self.stats['relationships_detected'] += 1
        
        # Periodic cleanup
        self._maybe_cleanup()
        
        return relationships
    
    def _add_to_cache(self, relationship: WindowRelationship):
        """Add relationship to cache with memory management"""
        # Check memory before adding
        if self.total_memory_used + relationship.memory_size_bytes > self.config['max_memory_mb'] * 1024 * 1024:
            # Remove oldest entries
            while self.relationship_cache and self.total_memory_used + relationship.memory_size_bytes > self.config['max_memory_mb'] * 1024 * 1024:
                removed = self.relationship_cache.popleft()
                self.total_memory_used -= removed.memory_size_bytes
        
        self.relationship_cache.append(relationship)
        self.total_memory_used += relationship.memory_size_bytes
    
    def _is_cache_valid(self, item: Any) -> bool:
        """Check if cached item is still valid"""
        age = datetime.now() - item.timestamp
        return age.total_seconds() < self.config['cache_ttl_seconds']
    
    def group_windows(self, windows: List[Any], 
                     relationships: List[WindowRelationship]) -> List[WindowGroup]:
        """Group windows by project or task with memory management"""
        # Check memory
        if not self._check_memory_available():
            logger.warning("Skipping window grouping due to memory pressure")
            return []
        
        groups = []
        
        # Build adjacency list from relationships
        window_graph = defaultdict(list)
        for rel in relationships:
            if rel.confidence >= self.config['group_min_confidence']:
                window_graph[rel.window1_id].append(rel.window2_id)
                window_graph[rel.window2_id].append(rel.window1_id)
        
        # Find connected components (groups)
        visited = set()
        for window in windows[:self.config['max_windows_to_analyze']]:
            if window.window_id not in visited:
                group = self._find_connected_windows(
                    window.window_id, window_graph, windows, visited
                )
                if len(group) > 1:  # Only groups with 2+ windows
                    window_group = self._analyze_group(group)
                    if window_group:
                        groups.append(window_group)
                        self.stats['groups_formed'] += 1
                        
                        # Add to cache with memory management
                        if self.total_memory_used + window_group.memory_size_bytes <= self.config['max_memory_mb'] * 1024 * 1024:
                            self.group_cache.append(window_group)
                            self.total_memory_used += window_group.memory_size_bytes
        
        return groups
    
    def _analyze_window_pair(self, window1: Any, window2: Any) -> Optional[WindowRelationship]:
        """Analyze relationship between two windows"""
        evidence = []
        relationship_type = None
        confidence = 0.0
        
        # Load configurable confidence weights
        weights = {
            'project_match': float(os.getenv('WINDOW_REL_WEIGHT_PROJECT', '0.4')),
            'language_match': float(os.getenv('WINDOW_REL_WEIGHT_LANGUAGE', '0.3')),
            'documentation': float(os.getenv('WINDOW_REL_WEIGHT_DOC', '0.2')),
            'base_ide_doc': float(os.getenv('WINDOW_REL_WEIGHT_BASE_IDE_DOC', '0.2')),
            'base_ide_term': float(os.getenv('WINDOW_REL_WEIGHT_BASE_IDE_TERM', '0.1')),
            'dev_pair': float(os.getenv('WINDOW_REL_WEIGHT_DEV_PAIR', '0.6')),
            'comm_pair': float(os.getenv('WINDOW_REL_WEIGHT_COMM_PAIR', '0.7'))
        }
        
        # Check IDE + Documentation relationship
        if (self._is_ide(window1) and self._is_documentation(window2)) or \
           (self._is_ide(window2) and self._is_documentation(window1)):
            ide_window = window1 if self._is_ide(window1) else window2
            doc_window = window2 if self._is_documentation(window2) else window1
            
            # Check for project name match
            project_match = self._find_common_project(ide_window, doc_window)
            if project_match:
                evidence.append(f"Common project: {project_match[:50]}")  # Limit length
                confidence += weights['project_match']
            
            # Check for language/framework match
            lang_match = self._find_common_language(ide_window, doc_window)
            if lang_match:
                evidence.append(f"Common language: {lang_match}")
                confidence += weights['language_match']
            
            # Check if documentation is relevant
            if self._is_relevant_documentation(doc_window):
                evidence.append("Technical documentation")
                confidence += weights['documentation']
            
            if confidence > 0:
                relationship_type = "ide_documentation"
                confidence = min(confidence + weights['base_ide_doc'], 1.0)
        
        # Check IDE + Terminal relationship
        elif (self._is_ide(window1) and self._is_terminal(window2)) or \
             (self._is_ide(window2) and self._is_terminal(window1)):
            ide_window = window1 if self._is_ide(window1) else window2
            terminal_window = window2 if self._is_terminal(window2) else window1
            
            # Check for project path match
            project_match = self._find_common_project(ide_window, terminal_window)
            if project_match:
                evidence.append(f"Same project: {project_match[:50]}")
                confidence += weights['project_match'] * 1.25  # Higher weight for terminal
            
            # Check for common commands/files
            if self._has_related_content(ide_window, terminal_window):
                evidence.append("Related commands/files")
                confidence += weights['language_match']
            
            # If same user and both are development tools, likely related
            if not project_match and not evidence:
                evidence.append("Development environment pair")
                confidence = weights['dev_pair']
            
            if confidence > 0:
                relationship_type = "ide_terminal"
                confidence = min(confidence + weights['base_ide_term'], 0.95)
        
        # Check Browser + Browser relationship
        elif self._is_documentation(window1) and self._is_documentation(window2):
            # Check for similar titles or domains
            similarity = self._calculate_title_similarity(window1, window2)
            if similarity > self.config['title_similarity_threshold']:
                evidence.append(f"Similar content ({similarity:.0%})")
                confidence = similarity
                relationship_type = "related_documentation"
        
        # Check Communication app relationships
        elif self._is_communication(window1) and self._is_communication(window2):
            evidence.append("Multiple communication channels")
            confidence = weights['comm_pair']
            relationship_type = "communication_group"
        
        # Create relationship if found
        if relationship_type and confidence >= self.config['min_confidence']:
            return WindowRelationship(
                window1_id=window1.window_id,
                window2_id=window2.window_id,
                relationship_type=relationship_type,
                confidence=confidence,
                evidence=evidence[:5]  # Limit evidence items
            )
        
        return None
    
    def _is_ide(self, window: Any) -> bool:
        """Check if window is an IDE or code editor"""
        return any(ide in getattr(window, 'app_name', '') for ide in self.ide_apps)
    
    def _is_documentation(self, window: Any) -> bool:
        """Check if window is documentation or reference"""
        if any(app in getattr(window, 'app_name', '') for app in self.doc_apps):
            # Additional check for browser windows
            window_title = getattr(window, 'window_title', '')
            if window_title:
                title_lower = window_title.lower()
                # Check for documentation indicators (configurable)
                doc_keywords = json.loads(os.getenv('WINDOW_REL_DOC_KEYWORDS', 
                    '["docs", "documentation", "api", "reference", "guide", "tutorial", "stackoverflow", "github"]'))
                return any(keyword in title_lower for keyword in doc_keywords)
            return True
        return False
    
    def _is_terminal(self, window: Any) -> bool:
        """Check if window is a terminal"""
        return any(term in getattr(window, 'app_name', '') for term in self.terminal_apps)
    
    def _is_communication(self, window: Any) -> bool:
        """Check if window is a communication app"""
        return any(app in getattr(window, 'app_name', '') for app in self.comm_apps)
    
    def _find_common_project(self, window1: Any, window2: Any) -> Optional[str]:
        """Find common project name between windows"""
        title1 = getattr(window1, 'window_title', '')
        title2 = getattr(window2, 'window_title', '')
        
        if not title1 or not title2:
            return None
        
        # Extract potential project names
        title1_parts = re.findall(r'[\w\-]+', title1)
        title2_parts = re.findall(r'[\w\-]+', title2)
        
        # Special handling for common project patterns
        for title in [title1, title2]:
            # Pattern: "something — ProjectName"
            if ' — ' in title:
                parts = title.split(' — ')
                if len(parts) > 1:
                    project_candidate = parts[-1].strip()[:50]  # Limit length
                    # Check if this appears in the other window
                    other_title = title2 if title == title1 else title1
                    if project_candidate in other_title:
                        return project_candidate
        
        # Find common parts
        common_parts = set(title1_parts) & set(title2_parts)
        
        # Filter out common words (configurable)
        common_words = set(json.loads(os.getenv('WINDOW_REL_COMMON_WORDS', 
            '["the", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by", "from", "is", "are", "was", "were", "tree", "working", "file", "edit", "view"]')))
        
        min_length = self.config['common_word_min_length']
        significant_parts = [part for part in common_parts 
                           if len(part) > min_length and part.lower() not in common_words]
        
        # Prioritize hyphenated project names
        hyphenated = [part for part in significant_parts if '-' in part]
        if hyphenated:
            return max(hyphenated, key=len)[:50]  # Limit length
        
        if significant_parts:
            return max(significant_parts, key=len)[:50]  # Limit length
        
        return None
    
    def _find_common_language(self, ide_window: Any, doc_window: Any) -> Optional[str]:
        """Find common programming language/framework"""
        doc_title = getattr(doc_window, 'window_title', '')
        if not doc_title:
            return None
        
        title_lower = doc_title.lower()
        
        # Load languages from config or use defaults
        languages_json = os.getenv('WINDOW_REL_LANGUAGES')
        if languages_json:
            try:
                languages = json.loads(languages_json)
            except Exception:
                languages = self._get_default_languages()
        else:
            languages = self._get_default_languages()
        
        for lang, keywords in languages.items():
            if any(keyword in title_lower for keyword in keywords):
                return lang
        
        return None
    
    def _get_default_languages(self) -> Dict[str, List[str]]:
        """Get default language patterns"""
        return {
            'python': ['python', 'django', 'flask', 'fastapi', 'numpy', 'pandas'],
            'javascript': ['javascript', 'js', 'react', 'vue', 'angular', 'node'],
            'typescript': ['typescript', 'ts'],
            'rust': ['rust', 'cargo', 'crates'],
            'go': ['golang', 'go '],
            'java': ['java', 'spring', 'maven', 'gradle'],
            'swift': ['swift', 'swiftui', 'ios', 'macos'],
            'ruby': ['ruby', 'rails'],
        }
    
    def _is_relevant_documentation(self, window: Any) -> bool:
        """Check if window contains technical documentation"""
        window_title = getattr(window, 'window_title', '')
        if not window_title:
            return False
        
        title_lower = window_title.lower()
        
        # Check for documentation domains
        if any(domain in title_lower for domain in self.doc_domains):
            return True
        
        # Check for technical keywords (configurable)
        tech_keywords = json.loads(os.getenv('WINDOW_REL_TECH_KEYWORDS',
            '["api", "docs", "reference", "guide", "tutorial", "example", "documentation", "manual", "specification"]'))
        
        return any(keyword in title_lower for keyword in tech_keywords)
    
    def _has_related_content(self, window1: Any, window2: Any) -> bool:
        """Check if windows have related content"""
        title1 = getattr(window1, 'window_title', '')
        title2 = getattr(window2, 'window_title', '')
        
        if not title1 or not title2:
            return False
        
        # Extract file names or paths
        file_pattern = r'[\w\-]+\.\w+'
        files1 = set(re.findall(file_pattern, title1))
        files2 = set(re.findall(file_pattern, title2))
        
        # Check for common files
        return bool(files1 & files2)
    
    def _calculate_title_similarity(self, window1: Any, window2: Any) -> float:
        """Calculate similarity between window titles"""
        title1 = getattr(window1, 'window_title', '')
        title2 = getattr(window2, 'window_title', '')
        
        if not title1 or not title2:
            return 0.0
        
        # Use sequence matcher for similarity
        return difflib.SequenceMatcher(
            None, 
            title1.lower()[:200],  # Limit length
            title2.lower()[:200]
        ).ratio()
    
    def _find_connected_windows(self, start_id: int, graph: Dict[int, List[int]], 
                              all_windows: List[Any], 
                              visited: Set[int]) -> List[Any]:
        """Find all windows connected to the starting window"""
        connected = []
        stack = [start_id]
        
        while stack and len(connected) < 20:  # Limit group size
            window_id = stack.pop()
            if window_id not in visited:
                visited.add(window_id)
                
                # Find the window object
                window = next((w for w in all_windows if w.window_id == window_id), None)
                if window:
                    connected.append(window)
                
                # Add neighbors
                for neighbor_id in graph[window_id]:
                    if neighbor_id not in visited:
                        stack.append(neighbor_id)
        
        return connected
    
    def _analyze_group(self, windows: List[Any]) -> Optional[WindowGroup]:
        """Analyze a group of windows to determine its type"""
        if not windows:
            return None
        
        # Count window types
        ide_count = sum(1 for w in windows if self._is_ide(w))
        doc_count = sum(1 for w in windows if self._is_documentation(w))
        term_count = sum(1 for w in windows if self._is_terminal(w))
        comm_count = sum(1 for w in windows if self._is_communication(w))
        
        # Determine group type
        group_type = "mixed"
        confidence = 0.5
        
        if ide_count > 0 and (doc_count > 0 or term_count > 0):
            group_type = "project"
            confidence = 0.8
        elif comm_count >= 2:
            group_type = "communication"
            confidence = 0.9
        elif doc_count >= 2:
            group_type = "research"
            confidence = 0.7
        
        # Find common elements
        common_elements = []
        
        # Extract all title words (limited)
        all_words = []
        for window in windows:
            window_title = getattr(window, 'window_title', '')
            if window_title:
                words = re.findall(r'\b\w{4,}\b', window_title[:200])  # Limit title length
                all_words.extend(words[:20])  # Limit words per title
        
        # Find frequent words
        word_counts = defaultdict(int)
        excluded_words = set(json.loads(os.getenv('WINDOW_REL_EXCLUDED_WORDS',
            '["window", "file", "edit", "view", "help"]')))
        
        for word in all_words:
            word_lower = word.lower()
            if word_lower not in excluded_words:
                word_counts[word_lower] += 1
        
        # Common elements are words that appear in multiple windows
        min_appearances = len(windows) * self.config['group_common_word_ratio']
        common_elements = [word for word, count in word_counts.items() 
                         if count >= min_appearances][:5]  # Top 5
        
        return WindowGroup(
            group_id=f"{group_type}_{windows[0].window_id}",
            windows=windows,
            group_type=group_type,
            confidence=confidence,
            common_elements=common_elements
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics"""
        return {
            **self.stats,
            'cache_size_relationships': len(self.relationship_cache),
            'cache_size_groups': len(self.group_cache),
            'total_memory_used_mb': self.total_memory_used / 1024 / 1024,
            'available_system_mb': psutil.virtual_memory().available / 1024 / 1024
        }

# Backward compatibility
WindowRelationshipDetector = ConfigurableWindowRelationshipDetector

async def test_relationship_detection():
    """Test window relationship detection"""
    print("🔍 Testing Configurable Window Relationship Detection")
    print("=" * 50)
    
    detector = ConfigurableWindowRelationshipDetector()
    
    print(f"\n📊 Configuration:")
    print(f"   Max Memory: {detector.config['max_memory_mb']}MB")
    print(f"   Min Confidence: {detector.config['min_confidence']}")
    print(f"   Max Windows: {detector.config['max_windows_to_analyze']}")
    
    # Test with mock windows
    class MockWindow:
        def __init__(self, window_id, app_name, title, visible=True):
            self.window_id = window_id
            self.app_name = app_name
            self.window_title = title
            self.is_visible = visible
    
    # Create test windows
    windows = [
        MockWindow(1, "Visual Studio Code", "myproject — Visual Studio Code"),
        MockWindow(2, "Chrome", "Python Documentation - myproject API reference"),
        MockWindow(3, "Terminal", "~/projects/myproject"),
        MockWindow(4, "Slack", "Team Chat"),
        MockWindow(5, "Discord", "Development Server")
    ]
    
    print(f"\n🪟 Testing with {len(windows)} mock windows")
    
    # Detect relationships
    relationships = detector.detect_relationships(windows)
    print(f"\n🔗 Found {len(relationships)} relationships:")
    
    for rel in relationships:
        window1 = next(w for w in windows if w.window_id == rel.window1_id)
        window2 = next(w for w in windows if w.window_id == rel.window2_id)
        
        print(f"\n   Type: {rel.relationship_type}")
        print(f"   {window1.app_name} ↔ {window2.app_name}")
        print(f"   Confidence: {rel.confidence:.0%}")
        print(f"   Evidence: {', '.join(rel.evidence)}")
    
    # Group windows
    groups = detector.group_windows(windows, relationships)
    print(f"\n📁 Found {len(groups)} window groups:")
    
    for group in groups:
        print(f"\n   Group: {group.group_type}")
        print(f"   Windows: {[w.app_name for w in group.windows]}")
        print(f"   Common: {', '.join(group.common_elements)}")
        print(f"   Confidence: {group.confidence:.0%}")
    
    # Show stats
    print(f"\n📈 Statistics:")
    stats = detector.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    print("\n✅ Test complete!")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_relationship_detection())