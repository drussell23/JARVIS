#!/usr/bin/env python3
"""
Bloom Filter Network (4.4) for JARVIS Vision System
Hierarchical bloom filter system for preventing redundant processing through efficient duplicate detection.

Architecture:
- Global Filter (3MB): System-wide deduplication across all vision processing
- Regional Filter (4MB): Window/region-specific deduplication  
- Element Filter (3MB): UI element-specific deduplication

Total Memory Allocation: 10MB
Features:
- Multi-level checking with short-circuit on hit
- Intelligent reset strategies based on saturation
- Adaptive parameters for hash functions and sizing
- Integration with existing LSH caching system
"""

import hashlib
import time
import asyncio
import pickle
import numpy as np
from typing import Dict, List, Tuple, Optional, Any, Union, Set, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import OrderedDict
from enum import Enum
import logging
import threading
from abc import ABC, abstractmethod
import mmh3  # MurmurHash3 for better distribution

# Try to import Rust acceleration if available
try:
    from .rust_bridge import rust_bloom_hash
    RUST_HASH_AVAILABLE = True
except ImportError:
    RUST_HASH_AVAILABLE = False

logger = logging.getLogger(__name__)

class BloomFilterLevel(Enum):
    """Bloom filter hierarchy levels"""
    GLOBAL = "GLOBAL"
    REGIONAL = "REGIONAL"
    ELEMENT = "ELEMENT"

@dataclass
class BloomFilterMetrics:
    """Performance and accuracy metrics for bloom filters"""
    total_insertions: int = 0
    total_queries: int = 0
    probable_hits: int = 0
    confirmed_hits: int = 0
    false_positives: int = 0
    saturation_level: float = 0.0
    last_reset: datetime = field(default_factory=datetime.now)
    reset_count: int = 0
    
    @property
    def false_positive_rate(self) -> float:
        """Calculate actual false positive rate"""
        if self.probable_hits == 0:
            return 0.0
        return self.false_positives / self.probable_hits
    
    @property
    def hit_rate(self) -> float:
        """Calculate hit rate"""
        if self.total_queries == 0:
            return 0.0
        return self.probable_hits / self.total_queries

class AdaptiveBloomFilter:
    """
    High-performance bloom filter with adaptive parameters and intelligent resetting
    """
    
    def __init__(
        self,
        size_mb: float,
        expected_elements: int,
        max_false_positive_rate: float = 0.01,
        level: BloomFilterLevel = BloomFilterLevel.GLOBAL,
        enable_rust_hashing: bool = True
    ):
        self.size_mb = size_mb
        self.size_bits = int(size_mb * 1024 * 1024 * 8)  # Convert MB to bits
        self.expected_elements = expected_elements
        self.max_false_positive_rate = max_false_positive_rate
        self.level = level
        self.enable_rust_hashing = enable_rust_hashing and RUST_HASH_AVAILABLE
        
        # Calculate optimal number of hash functions based on level
        # Global: 10 hash functions, Regional: 7, Element: 5 (as per spec)
        if level == BloomFilterLevel.GLOBAL:
            self.num_hashes = 10
        elif level == BloomFilterLevel.REGIONAL:
            self.num_hashes = 7
        elif level == BloomFilterLevel.ELEMENT:
            self.num_hashes = 5
        else:
            self.num_hashes = self._calculate_optimal_hash_functions()
        
        # Initialize bit array
        self.bit_array = np.zeros(self.size_bits, dtype=np.uint8)
        
        # Metrics and state tracking
        self.metrics = BloomFilterMetrics()
        self.lock = threading.RLock()
        self.saturation_threshold = 0.8  # Reset when 80% full
        
        # Hash function seeds for diversity
        # Use different seed patterns for each level
        if level == BloomFilterLevel.GLOBAL:
            # Global uses prime multipliers for maximum diversity
            self.hash_seeds = [
                (2654435761 * (i + 1)) & 0xFFFFFFFF
                for i in range(self.num_hashes)
            ]
        elif level == BloomFilterLevel.REGIONAL:
            # Regional uses Fibonacci-based seeds
            self.hash_seeds = [(11400714819323198485 * (i + 1)) & 0xFFFFFFFF for i in range(self.num_hashes)]
        else:
            # Element uses simple linear seeds
            self.hash_seeds = [
                (i * 2654435761) & 0xFFFFFFFF
                for i in range(self.num_hashes)
            ]
        
        logger.info(f"Initialized {level.value} bloom filter: "
                   f"{size_mb}MB, {self.num_hashes} hash functions, "
                   f"{expected_elements} expected elements")
    
    def _calculate_optimal_hash_functions(self) -> int:
        """Calculate optimal number of hash functions based on filter size and expected elements"""
        # k = (m/n) * ln(2) where m = bits, n = expected elements
        optimal_k = (self.size_bits / self.expected_elements) * np.log(2)
        # Clamp between 1 and 10 for practical performance
        return max(1, min(10, int(round(optimal_k))))
    
    def _hash_element(self, element: Union[str, bytes], seed: int) -> int:
        """Generate hash for element with given seed"""
        if isinstance(element, str):
            element = element.encode('utf-8')
        
        if self.enable_rust_hashing:
            try:
                return rust_bloom_hash(element, seed) % self.size_bits
            except Exception:
                # Fallback to Python hashing
                pass
        
        # Use MurmurHash3 for better distribution
        return mmh3.hash(element, seed, signed=False) % self.size_bits
    
    def add(self, element: Union[str, bytes]) -> bool:
        """Add element to bloom filter"""
        with self.lock:
            # Check saturation before adding
            if self.metrics.saturation_level > self.saturation_threshold:
                if self._should_reset():
                    self.reset()
            
            # Generate hash positions
            positions = [self._hash_element(element, seed) for seed in self.hash_seeds]
            
            # Set bits
            for pos in positions:
                self.bit_array[pos] = 1
            
            self.metrics.total_insertions += 1
            self._update_saturation_estimate()
            
            return True
    
    def contains(self, element: Union[str, bytes]) -> bool:
        """Check if element might be in the set (may have false positives)"""
        with self.lock:
            self.metrics.total_queries += 1
            
            # Generate hash positions
            positions = [self._hash_element(element, seed) for seed in self.hash_seeds]
            
            # Check all positions
            for pos in positions:
                if self.bit_array[pos] == 0:
                    return False  # Definitely not in set
            
            # All positions are set - probably in set
            self.metrics.probable_hits += 1
            return True
    
    def _update_saturation_estimate(self):
        """Update saturation level estimate"""
        # Estimate based on set bits (approximation for performance)
        if self.metrics.total_insertions % 1000 == 0:  # Check every 1000 insertions
            set_bits = np.sum(self.bit_array)
            self.metrics.saturation_level = set_bits / self.size_bits
    
    def _should_reset(self) -> bool:
        """Determine if filter should be reset based on intelligent criteria"""
        # Reset if saturation too high
        if self.metrics.saturation_level > self.saturation_threshold:
            return True
        
        # Reset if false positive rate too high
        if (self.metrics.false_positive_rate > self.max_false_positive_rate * 2 and 
            self.metrics.probable_hits > 100):
            return True
        
        # Reset based on level-specific schedules
        time_since_reset = (datetime.now() - self.metrics.last_reset).total_seconds()
        
        # Weekly reset for Global (604800 seconds = 7 days)
        if self.level == BloomFilterLevel.GLOBAL and time_since_reset > 604800:
            return True
        # Daily reset for Regional (86400 seconds = 24 hours)    
        elif self.level == BloomFilterLevel.REGIONAL and time_since_reset > 86400:
            return True
        # Hourly reset for Element (3600 seconds = 1 hour)
        elif self.level == BloomFilterLevel.ELEMENT and time_since_reset > 3600:
            return True
        
        # Also reset if saturation too high
        if self.metrics.saturation_level > 0.5:
            return True
        
        return False
    
    def reset(self):
        """Reset the bloom filter"""
        with self.lock:
            self.bit_array.fill(0)
            old_metrics = self.metrics
            self.metrics = BloomFilterMetrics()
            self.metrics.reset_count = old_metrics.reset_count + 1
            
            logger.info(f"Reset {self.level.value} bloom filter "
                       f"(saturation: {old_metrics.saturation_level:.2%}, "
                       f"FP rate: {old_metrics.false_positive_rate:.2%})")
    
    def get_memory_usage(self) -> Dict[str, Any]:
        """Get current memory usage statistics"""
        bit_array_mb = self.bit_array.nbytes / 1024 / 1024
        return {
            'allocated_mb': self.size_mb,
            'actual_mb': bit_array_mb,
            'saturation_level': self.metrics.saturation_level,
            'total_insertions': self.metrics.total_insertions,
            'total_queries': self.metrics.total_queries,
            'hit_rate': self.metrics.hit_rate,
            'false_positive_rate': self.metrics.false_positive_rate
        }


class BloomFilterNetwork:
    """
    Hierarchical bloom filter system with multi-level checking and intelligent management
    """
    
    def __init__(
        self,
        global_size_mb: float = 4.0,  # Changed from 3.0 to 4.0 (4MB total)
        regional_size_mb: float = 1.0,  # Changed from 4.0 to 1.0 (1MB × 4 = 4MB total)
        element_size_mb: float = 2.0,  # Changed from 3.0 to 2.0 (2MB total)
        enable_hierarchical_checking: bool = True,
        enable_rust_hashing: bool = True
    ):
        self.enable_hierarchical_checking = enable_hierarchical_checking
        self.enable_rust_hashing = enable_rust_hashing
        
        # Initialize bloom filters for each level
        self.global_filter = AdaptiveBloomFilter(
            size_mb=global_size_mb,
            expected_elements=100000,  # System-wide elements (increased for global scope)
            level=BloomFilterLevel.GLOBAL,
            enable_rust_hashing=enable_rust_hashing
        )
        
        # Create 4 regional filters (one per quadrant) - 1MB each
        self.regional_filters = [
            AdaptiveBloomFilter(
                size_mb=regional_size_mb,
                expected_elements=5000,  # Elements per quadrant
                level=BloomFilterLevel.REGIONAL,
                max_false_positive_rate=0.01,
                enable_rust_hashing=enable_rust_hashing
            ) for _ in range(4)  # 4 quadrants
        ]
        
        self.element_filter = AdaptiveBloomFilter(
            size_mb=element_size_mb,
            expected_elements=20000,  # UI elements (increased for element scope)
            level=BloomFilterLevel.ELEMENT,
            enable_rust_hashing=enable_rust_hashing
        )
        
        # Network-level metrics
        self.network_metrics = {
            'total_checks': 0,
            'global_hits': 0,
            'regional_hits': 0,
            'element_hits': 0,
            'total_misses': 0,
            'hierarchical_shortcuts': 0
        }
        
        self.lock = threading.RLock()
        
        logger.info(f"Initialized Bloom Filter Network: "
                   f"Total {global_size_mb + regional_size_mb + element_size_mb}MB allocated")
    
    def _generate_element_key(
        self,
        element_data: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Generate consistent key for vision element"""
        if isinstance(element_data, dict):
            # Sort keys for consistent hashing
            sorted_data = json.dumps(element_data, sort_keys=True)
        elif isinstance(element_data, (list, tuple)): # Handle lists/tuples of primitives consistently 
            # Sort if all primitives else keep order intact 
            sorted_data = str(sorted(element_data) if all(isinstance(x, (str, int, float)) for x in element_data) else element_data) 
        else:
            sorted_data = str(element_data)
        
        # Add context if provided
        if context:
            context_str = json.dumps(context, sort_keys=True)
            sorted_data = f"{sorted_data}|{context_str}"
        
        return hashlib.sha256(sorted_data.encode('utf-8')).hexdigest()
    
    def check_and_add(
        self,
        element_data: Any,
        context: Optional[Dict[str, Any]] = None,
        check_level: BloomFilterLevel = BloomFilterLevel.GLOBAL
    ) -> Tuple[bool, BloomFilterLevel]:
        """
        Check if element exists and add if not found
        Returns: (is_duplicate, level_where_found)
        """
        with self.lock:
            self.network_metrics['total_checks'] += 1
            
            element_key = self._generate_element_key(element_data, context)
            
            if self.enable_hierarchical_checking:
                # Hierarchical checking with short-circuit
                
                # Check Global first (most comprehensive)
                if self.global_filter.contains(element_key):
                    self.network_metrics['global_hits'] += 1
                    self.network_metrics['hierarchical_shortcuts'] += 1
                    return True, BloomFilterLevel.GLOBAL
                
                # Check Regional filters if not in Global
                if check_level in [BloomFilterLevel.REGIONAL, BloomFilterLevel.ELEMENT]:
                    # Determine quadrant based on context (if provided)
                    quadrant_idx = self._get_quadrant_index(context)
                    
                    if quadrant_idx is not None:
                        # Check specific quadrant
                        if self.regional_filters[quadrant_idx].contains(element_key):
                            self.network_metrics['regional_hits'] += 1
                            # Add to global for future shortcuts
                            self.global_filter.add(element_key)
                            return True, BloomFilterLevel.REGIONAL
                    else:
                        # Check all quadrants
                        for idx, regional_filter in enumerate(self.regional_filters):
                            if regional_filter.contains(element_key):
                                self.network_metrics['regional_hits'] += 1
                                # Add to global for future shortcuts
                                self.global_filter.add(element_key)
                                return True, BloomFilterLevel.REGIONAL
                
                # Check Element if not in Regional
                if check_level == BloomFilterLevel.ELEMENT:
                    if self.element_filter.contains(element_key):
                        self.network_metrics['element_hits'] += 1
                        # Promote to higher levels
                        quadrant_idx = self._get_quadrant_index(context)
                        if quadrant_idx is not None:
                            self.regional_filters[quadrant_idx].add(element_key)
                        self.global_filter.add(element_key)
                        return True, BloomFilterLevel.ELEMENT
            else:
                # Direct level checking
                target_filter = self._get_filter_for_level(check_level)
                if target_filter.contains(element_key):
                    self.network_metrics[f'{check_level.value.lower()}_hits'] += 1
                    return True, check_level
            
            # Not found - add to appropriate levels
            self.network_metrics['total_misses'] += 1
            self._add_to_appropriate_levels(element_key, check_level, context)
            
            return False, check_level

    def check_duplicate(
        self,
        element_data: Any,
        context: Optional[Dict[str, Any]] = None,
        check_level: BloomFilterLevel = BloomFilterLevel.GLOBAL,
    ) -> bool:
        """
        Backward-compatible duplicate check contract used by orchestrators.

        Returns True when the element has been seen before at the selected
        hierarchy level, otherwise records it and returns False.
        """
        is_duplicate, _ = self.check_and_add(
            element_data=element_data,
            context=context,
            check_level=check_level,
        )
        return is_duplicate
    
    def _get_filter_for_level(self, level: BloomFilterLevel, quadrant: Optional[int] = None) -> Union[AdaptiveBloomFilter, List[AdaptiveBloomFilter]]:
        """Get bloom filter(s) for specific level"""
        if level == BloomFilterLevel.GLOBAL:
            return self.global_filter
        elif level == BloomFilterLevel.REGIONAL:
            if quadrant is not None and 0 <= quadrant < 4:
                return self.regional_filters[quadrant]
            return self.regional_filters
        elif level == BloomFilterLevel.ELEMENT:
            return self.element_filter
        else:
            raise ValueError(f"Unknown bloom filter level: {level}")
    
    def _add_to_appropriate_levels(self, element_key: str, level: BloomFilterLevel, 
                                  context: Optional[Dict[str, Any]] = None):
        """Add element to appropriate filter levels"""
        if level == BloomFilterLevel.ELEMENT:
            self.element_filter.add(element_key)
        elif level == BloomFilterLevel.REGIONAL:
            quadrant_idx = self._get_quadrant_index(context)
            if quadrant_idx is not None:
                self.regional_filters[quadrant_idx].add(element_key)
            else:
                # Add to all quadrants if no context
                for regional_filter in self.regional_filters:
                    regional_filter.add(element_key)
        elif level == BloomFilterLevel.GLOBAL:
            self.global_filter.add(element_key)
    
    def _get_quadrant_index(self, context: Optional[Dict[str, Any]]) -> Optional[int]:
        """Determine quadrant index from context (0-3 for quadrants)"""
        if not context:
            return None
            
        # Check for explicit quadrant
        if 'quadrant' in context:
            return int(context['quadrant']) % 4
            
        # Determine from coordinates if available
        if 'x' in context and 'y' in context:
            x, y = context['x'], context['y']
            # Assuming screen divided into 4 quadrants
            # You might want to get actual screen dimensions
            if 'screen_width' in context and 'screen_height' in context:
                width = context['screen_width']
                height = context['screen_height']
                quadrant = 0
                if x > width / 2:
                    quadrant += 1
                if y > height / 2:
                    quadrant += 2
                return quadrant
        
        # Check for window/region info
        if 'region' in context:
            region = context['region'].lower()
            if 'top' in region and 'left' in region:
                return 0
            elif 'top' in region and 'right' in region:
                return 1
            elif 'bottom' in region and 'left' in region:
                return 2
            elif 'bottom' in region and 'right' in region:
                return 3
                
        return None
    
    def get_network_stats(self) -> Dict[str, Any]:
        """Get comprehensive network statistics"""
        with self.lock:
            regional_memory = sum(rf.size_mb for rf in self.regional_filters)
            total_memory = (
                self.global_filter.size_mb + 
                regional_memory + 
                self.element_filter.size_mb
            )
            
            # Get stats for each regional filter
            regional_stats = []
            for idx, rf in enumerate(self.regional_filters):
                stats = rf.get_memory_usage()
                stats['quadrant'] = idx
                regional_stats.append(stats)
            
            return {
                'network_metrics': self.network_metrics.copy(),
                'total_memory_mb': total_memory,
                'global_filter': self.global_filter.get_memory_usage(),
                'regional_filters': regional_stats,
                'element_filter': self.element_filter.get_memory_usage(),
                'efficiency_stats': {
                    'total_checks': self.network_metrics['total_checks'],
                    'hit_rate': (
                        (self.network_metrics['global_hits'] + 
                         self.network_metrics['regional_hits'] + 
                         self.network_metrics['element_hits']) / 
                        max(1, self.network_metrics['total_checks'])
                    ),
                    'hierarchical_efficiency': (
                        self.network_metrics['hierarchical_shortcuts'] / 
                        max(1, self.network_metrics['total_checks'])
                    )
                }
            }
    
    def reset_network(self, level: Optional[BloomFilterLevel] = None):
        """Reset bloom filters at specified level or all levels"""
        with self.lock:
            if level is None:
                # Reset all levels
                self.global_filter.reset()
                for idx, rf in enumerate(self.regional_filters):
                    rf.reset()
                    logger.info(f"Reset regional filter {idx}")
                self.element_filter.reset()
                logger.info("Reset entire Bloom Filter Network")
            elif level == BloomFilterLevel.REGIONAL:
                # Reset all regional filters
                for idx, rf in enumerate(self.regional_filters):
                    rf.reset()
                logger.info("Reset all regional bloom filters")
            else:
                target_filter = self._get_filter_for_level(level)
                if isinstance(target_filter, list):
                    for f in target_filter:
                        f.reset()
                else:
                    target_filter.reset()
                logger.info(f"Reset {level.value} bloom filter")
    
    def optimize_network(self):
        """Optimize network performance based on usage patterns"""
        with self.lock:
            stats = self.get_network_stats()
            
            # Check if any filter needs attention
            filter_map = {
                'global_filter': BloomFilterLevel.GLOBAL,
                'element_filter': BloomFilterLevel.ELEMENT,
            }
            for filter_name, level in filter_map.items():
                filter_stats = stats.get(filter_name, {})
                if filter_stats.get('saturation_level', 0.0) > 0.85:
                    self.reset_network(level)

            # Regional filters are stored as a list of filter stats.
            for regional_stats in stats.get('regional_filters', []):
                if regional_stats.get('saturation_level', 0.0) > 0.85:
                    self.reset_network(BloomFilterLevel.REGIONAL)
                    break
                    
            logger.info("Bloom Filter Network optimization completed")


# Singleton instance for system-wide use
_bloom_filter_network: Optional[BloomFilterNetwork] = None
_network_lock = threading.Lock()

def get_bloom_filter_network() -> BloomFilterNetwork:
    """Get singleton bloom filter network instance with correct specifications"""
    global _bloom_filter_network
    with _network_lock:
        if _bloom_filter_network is None:
            # Initialize with spec-compliant sizes
            _bloom_filter_network = BloomFilterNetwork(
                global_size_mb=4.0,    # 4MB for global
                regional_size_mb=1.0,  # 1MB × 4 for regional
                element_size_mb=2.0    # 2MB for element
            )
        return _bloom_filter_network

def reset_bloom_filter_network():
    """Reset the singleton bloom filter network"""
    global _bloom_filter_network
    with _network_lock:
        if _bloom_filter_network is not None:
            _bloom_filter_network.reset_network()


# Integration helpers for vision system
class VisionBloomFilterIntegration:
    """Integration helpers for vision system components"""
    
    def __init__(self, network: Optional[BloomFilterNetwork] = None):
        self.network = network or get_bloom_filter_network()
    
    def is_image_duplicate(
        self,
        image_hash: str,
        window_context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Check if image/screenshot is duplicate"""
        is_duplicate, _ = self.network.check_and_add(
            image_hash,
            context=window_context,
            check_level=BloomFilterLevel.GLOBAL
        )
        return is_duplicate
    
    def is_ui_element_duplicate(
        self,
        element_data: Dict[str, Any],
        region_context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Check if UI element analysis is duplicate"""
        is_duplicate, _ = self.network.check_and_add(
            element_data,
            context=region_context,
            check_level=BloomFilterLevel.ELEMENT
        )
        return is_duplicate
    
    def is_window_region_duplicate(
        self,
        region_data: Dict[str, Any],
        window_context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Check if window region analysis is duplicate"""
        is_duplicate, _ = self.network.check_and_add(
            region_data,
            context=window_context,
            check_level=BloomFilterLevel.REGIONAL
        )
        return is_duplicate


# Example usage and testing
if __name__ == "__main__":
    import json
    
    # Initialize network
    network = BloomFilterNetwork()
    integration = VisionBloomFilterIntegration(network)
    
    # Simulate vision processing with spec-compliant configuration
    print("Testing Bloom Filter Network with 4MB/1MB×4/2MB configuration...")
    
    # Test image deduplication
    for i in range(100):
        image_hash = f"image_hash_{i % 50}"  # 50% duplicates
        is_duplicate = integration.is_image_duplicate(
            image_hash,
            {"window": "test_app", "timestamp": time.time()}
        )
        if i % 20 == 0:
            print(f"Image {i}: {'DUPLICATE' if is_duplicate else 'NEW'}")
    
    # Test UI element deduplication
    for i in range(50):
        element_data = {
            "type": "button",
            "text": f"Button {i % 20}",  # 40% duplicates
            "bounds": {"x": i*10, "y": i*10, "width": 100, "height": 30}
        }
        is_duplicate = integration.is_ui_element_duplicate(element_data)
        if i % 10 == 0:
            print(f"UI Element {i}: {'DUPLICATE' if is_duplicate else 'NEW'}")
    
    # Test regional filters with quadrants
    print("\nTesting quadrant-based regional filtering:")
    for quadrant in range(4):
        for i in range(10):
            region_data = {
                "region": f"quadrant_{quadrant}",
                "x": quadrant % 2 * 1000 + i * 10,
                "y": quadrant // 2 * 1000 + i * 10,
                "data": f"region_{quadrant}_{i}"
            }
            is_duplicate = integration.is_window_region_duplicate(
                region_data,
                {"quadrant": quadrant}
            )
            if i == 0:
                print(f"Quadrant {quadrant} - Item {i}: {'DUPLICATE' if is_duplicate else 'NEW'}")
    
    # Print network statistics
    stats = network.get_network_stats()
    print("\nBloom Filter Network Statistics:")
    print(json.dumps(stats, indent=2, default=str))
    
    # Show memory allocation
    print("\nMemory Allocation Compliance:")
    print(f"Global Filter: {stats['global_filter']['allocated_mb']}MB (target: 4MB)")
    total_regional = sum(rf['allocated_mb'] for rf in stats['regional_filters'])
    print(f"Regional Filters: {total_regional}MB (target: 4MB total, 1MB each)")
    print(f"Element Filter: {stats['element_filter']['allocated_mb']}MB (target: 2MB)")
    print(f"Total: {stats['total_memory_mb']}MB (target: 10MB)")
