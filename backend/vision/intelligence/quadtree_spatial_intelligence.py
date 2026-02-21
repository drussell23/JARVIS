#!/usr/bin/env python3
"""
Quadtree-Based Spatial Intelligence
Purpose: Minimize data processing and API calls through intelligent spatial division

Memory Allocation: 50MB Total
- Quadtree Structures: 20MB (adaptive subdivision trees)
- Importance Maps: 15MB (region weighting data) 
- Query Cache: 15MB (smart query results)

Key Features:
- Adaptive subdivision based on content complexity
- Importance weighting for region prioritization
- Smart querying with caching
- Performance optimization through spatial indexing
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any, Set, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import asyncio
import logging
from collections import defaultdict, deque
import cv2
import json
from enum import Enum
import hashlib
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RegionImportance(Enum):
    """Region importance levels"""
    CRITICAL = "critical"      # Must process immediately
    HIGH = "high"             # Process with priority
    MEDIUM = "medium"         # Normal processing
    LOW = "low"               # Can skip if resources limited
    STATIC = "static"         # Rarely changes, cache heavily

@dataclass
class QuadNode:
    """Node in the quadtree structure"""
    x: int
    y: int
    width: int
    height: int
    level: int
    
    # Node properties
    importance: float = 0.5
    complexity: float = 0.5
    last_update: Optional[datetime] = None
    hash_value: Optional[str] = None
    
    # Children nodes (NW, NE, SW, SE)
    children: Optional[List['QuadNode']] = None
    
    # Cached analysis
    cached_result: Optional[Dict[str, Any]] = None
    cache_timestamp: Optional[datetime] = None
    
    # Statistics
    access_count: int = 0
    change_frequency: float = 0.0
    
    def bounds(self) -> Tuple[int, int, int, int]:
        """Get bounds as (x1, y1, x2, y2)"""
        return (self.x, self.y, self.x + self.width, self.y + self.height)
    
    def center(self) -> Tuple[int, int]:
        """Get center point"""
        return (self.x + self.width // 2, self.y + self.height // 2)
    
    def area(self) -> int:
        """Get area in pixels"""
        return self.width * self.height
    
    def should_subdivide(self, min_size: int = 50) -> bool:
        """Check if node should be subdivided"""
        return (self.children is None and 
                self.width > min_size and 
                self.height > min_size and
                self.complexity > 0.6)
    
    def subdivide(self) -> List['QuadNode']:
        """Create four child nodes"""
        if self.children is not None:
            return self.children
            
        half_w = self.width // 2
        half_h = self.height // 2
        
        self.children = [
            # Northwest
            QuadNode(self.x, self.y, half_w, half_h, self.level + 1),
            # Northeast  
            QuadNode(self.x + half_w, self.y, self.width - half_w, half_h, self.level + 1),
            # Southwest
            QuadNode(self.x, self.y + half_h, half_w, self.height - half_h, self.level + 1),
            # Southeast
            QuadNode(self.x + half_w, self.y + half_h, 
                    self.width - half_w, self.height - half_h, self.level + 1)
        ]
        
        return self.children

@dataclass
class QueryResult:
    """Result from quadtree query"""
    nodes: List[QuadNode]
    total_importance: float
    coverage_ratio: float
    from_cache: bool = False
    
class QuadtreeSpatialIntelligence:
    """Intelligent spatial division system for efficient processing"""
    
    def __init__(self, memory_allocation: Dict[str, int] = None):
        """Initialize Quadtree Spatial Intelligence"""
        self.memory_allocation = memory_allocation or {
            'quadtree_structures': 20 * 1024 * 1024,  # 20MB
            'importance_maps': 15 * 1024 * 1024,      # 15MB
            'query_cache': 15 * 1024 * 1024           # 15MB
        }
        
        # Configuration
        self.max_depth = 6  # Maximum quadtree depth
        self.min_node_size = 50  # Minimum node size in pixels
        self.cache_duration = timedelta(minutes=5)
        self.importance_threshold = 0.7
        
        # Core components
        self.quadtrees: Dict[str, QuadNode] = {}  # Image ID -> root node
        self.importance_maps: Dict[str, np.ndarray] = {}
        self.query_cache: Dict[str, QueryResult] = {}
        self.access_patterns: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        # Track last analyzed frame per stream id to avoid unnecessary rebuilds.
        self._last_frame_hashes: Dict[str, str] = {}
        
        # Performance tracking
        self.stats = {
            'total_subdivisions': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'api_calls_saved': 0,
            'processing_time_saved': 0.0
        }
        
        # Initialize ML models
        self._init_importance_model()
        
    def _init_importance_model(self):
        """Initialize importance detection model"""
        # In production, this would load a trained model
        # For now, use heuristics
        self.importance_detector = self._heuristic_importance

    def _ensure_rgb(self, region: np.ndarray) -> np.ndarray:
        """Normalize input region to HxWx3 RGB for robust OpenCV operations."""
        if region.ndim == 2:
            return np.stack([region, region, region], axis=-1)
        if region.ndim == 3 and region.shape[2] == 1:
            return np.repeat(region, 3, axis=2)
        if region.ndim == 3 and region.shape[2] >= 3:
            return region[:, :, :3]
        return np.asarray(region, dtype=np.uint8)
        
    def _heuristic_importance(self, region: np.ndarray) -> float:
        """Calculate importance using heuristics"""
        if region.size == 0:
            return 0.0
        region = self._ensure_rgb(region)
            
        # Edge density (indicates UI elements)
        gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / edges.size
        
        # Color variance (indicates content richness)
        color_variance = np.std(region)
        
        # Center bias (center regions often more important)
        h, w = region.shape[:2]
        center_weight = 1.0  # Would calculate based on position
        
        # Combine factors
        importance = (
            0.4 * edge_density +
            0.3 * min(color_variance / 100, 1.0) +
            0.3 * center_weight
        )
        
        return float(np.clip(importance, 0.0, 1.0))
    
    async def build_quadtree(self, image: np.ndarray, image_id: str,
                            focus_regions: Optional[List[Tuple[int, int, int, int]]] = None) -> QuadNode:
        """Build adaptive quadtree for image"""
        h, w = image.shape[:2]
        
        # Create root node
        root = QuadNode(0, 0, w, h, 0)
        
        # Calculate importance map
        importance_map = await self._calculate_importance_map(image, focus_regions)
        self.importance_maps[image_id] = importance_map
        
        # Build tree adaptively
        await self._build_adaptive_tree(root, image, importance_map)
        
        # Store tree
        self.quadtrees[image_id] = root
        
        # Log statistics
        node_count = self._count_nodes(root)
        logger.info(f"Built quadtree for {image_id}: {node_count} nodes, "
                   f"max depth {self._get_max_depth(root)}")
        
        return root
    
    async def _calculate_importance_map(self, image: np.ndarray,
                                      focus_regions: Optional[List[Tuple[int, int, int, int]]]) -> np.ndarray:
        """Calculate importance map for entire image"""
        h, w = image.shape[:2]
        importance_map = np.zeros((h, w), dtype=np.float32)
        
        # Base importance from image features
        # Process in tiles for efficiency
        tile_size = 64
        for y in range(0, h, tile_size):
            for x in range(0, w, tile_size):
                y2 = min(y + tile_size, h)
                x2 = min(x + tile_size, w)
                
                tile = image[y:y2, x:x2]
                importance = self.importance_detector(tile)
                importance_map[y:y2, x:x2] = importance
        
        # Apply Gaussian blur for smooth transitions
        importance_map = cv2.GaussianBlur(importance_map, (21, 21), 0)
        
        # Boost focus regions if provided
        if focus_regions:
            for x1, y1, x2, y2 in focus_regions:
                importance_map[y1:y2, x1:x2] *= 1.5
        
        # Normalize
        importance_map = np.clip(importance_map, 0.0, 1.0)
        
        return importance_map
    
    async def _build_adaptive_tree(self, node: QuadNode, image: np.ndarray,
                                 importance_map: np.ndarray, depth: int = 0):
        """Build tree adaptively based on content"""
        if depth >= self.max_depth:
            return
            
        x1, y1, x2, y2 = node.bounds()
        region = image[y1:y2, x1:x2]
        region_importance = importance_map[y1:y2, x1:x2]
        
        # Calculate node properties
        node.importance = float(np.mean(region_importance))
        node.complexity = self._calculate_complexity(region)
        node.hash_value = self._calculate_hash(region)
        
        # Decide if subdivision needed
        if node.should_subdivide(self.min_node_size):
            # Check if region has variable importance
            importance_variance = np.var(region_importance)
            
            if importance_variance > 0.1 or node.complexity > 0.6:
                children = node.subdivide()
                self.stats['total_subdivisions'] += 1
                
                # Recursively build children
                for child in children:
                    await self._build_adaptive_tree(child, image, importance_map, depth + 1)
    
    def _calculate_complexity(self, region: np.ndarray) -> float:
        """Calculate visual complexity of region"""
        if region.size == 0:
            return 0.0
        region = self._ensure_rgb(region)
            
        # Gradient magnitude
        gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        gradient_mag = np.sqrt(grad_x**2 + grad_y**2)
        
        # Texture complexity (using standard deviation)
        texture_complexity = np.std(gray) / 255.0
        
        # Color diversity
        unique_colors = len(np.unique(region.reshape(-1, 3), axis=0))
        color_diversity = min(unique_colors / 1000, 1.0)
        
        complexity = (
            0.4 * (np.mean(gradient_mag) / 255.0) +
            0.3 * texture_complexity +
            0.3 * color_diversity
        )
        
        return float(np.clip(complexity, 0.0, 1.0))
    
    def _calculate_hash(self, region: np.ndarray) -> str:
        """Calculate perceptual hash of region"""
        region = self._ensure_rgb(region)
        # Resize to 8x8 for perceptual hashing
        small = cv2.resize(region, (8, 8), interpolation=cv2.INTER_AREA)
        
        # Convert to grayscale
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        
        # Calculate average
        avg = gray.mean()
        
        # Generate binary hash
        hash_bits = (gray > avg).flatten()
        
        # Convert to hex string
        hash_int = int(''.join(['1' if b else '0' for b in hash_bits]), 2)
        return hex(hash_int)[2:].zfill(16)
    
    async def query_regions(self, image_id: str, 
                          importance_threshold: float = 0.5,
                          max_regions: int = 10,
                          query_bounds: Optional[Tuple[int, int, int, int]] = None) -> QueryResult:
        """Query important regions from quadtree"""
        # Check cache
        cache_key = f"{image_id}_{importance_threshold}_{max_regions}_{query_bounds}"
        if cache_key in self.query_cache:
            cached = self.query_cache[cache_key]
            cache_ts = None
            if cached.nodes:
                cache_ts = cached.nodes[0].cache_timestamp
            if cache_ts and datetime.now() - cache_ts < self.cache_duration:
                self.stats['cache_hits'] += 1
                cached.from_cache = True
                return cached
        
        self.stats['cache_misses'] += 1
        
        # Get quadtree
        if image_id not in self.quadtrees:
            raise ValueError(f"No quadtree found for image {image_id}")
            
        root = self.quadtrees[image_id]
        
        # Collect nodes meeting criteria
        candidate_nodes = []
        self._collect_important_nodes(root, importance_threshold, candidate_nodes, query_bounds)
        
        # Sort by importance and select top regions
        candidate_nodes.sort(key=lambda n: n.importance * n.area(), reverse=True)
        selected_nodes = candidate_nodes[:max_regions]
        
        # Merge overlapping nodes
        merged_nodes = self._merge_overlapping_nodes(selected_nodes)
        
        # Calculate metrics
        total_importance = sum(n.importance for n in merged_nodes)
        total_area = sum(n.area() for n in merged_nodes)
        image_area = root.area()
        coverage_ratio = total_area / image_area if image_area > 0 else 0
        
        # Create result
        result = QueryResult(
            nodes=merged_nodes,
            total_importance=total_importance,
            coverage_ratio=coverage_ratio
        )
        
        # Cache result
        for node in merged_nodes:
            node.cache_timestamp = datetime.now()
        self.query_cache[cache_key] = result
        
        # Track access pattern
        for node in merged_nodes:
            self.access_patterns[image_id].append(node.center())
        
        return result

    async def analyze_frame(
        self,
        frame: np.ndarray,
        image_id: str = "live_stream",
        importance_threshold: float = 0.5,
        max_regions: int = 64,
        focus_regions: Optional[List[Tuple[int, int, int, int]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Analyze a frame and return normalized region dictionaries.

        This is the stable integration contract used by the vision orchestrator.
        """
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return []

        # Normalize grayscale input to RGB for OpenCV paths expecting 3 channels.
        if frame.ndim == 2:
            frame = np.stack([frame, frame, frame], axis=-1)

        frame_hash = self._calculate_hash(frame)
        known_hash = self._last_frame_hashes.get(image_id)

        # Rebuild the quadtree when content changes to keep importance mapping accurate.
        if image_id not in self.quadtrees or frame_hash != known_hash:
            self._invalidate_cache(image_id)
            await self.build_quadtree(frame, image_id, focus_regions=focus_regions)
            self._last_frame_hashes[image_id] = frame_hash

        query = await self.query_regions(
            image_id=image_id,
            importance_threshold=importance_threshold,
            max_regions=max_regions,
        )

        regions: List[Dict[str, Any]] = []
        for node in query.nodes:
            x1, y1, x2, y2 = node.bounds()
            regions.append(
                {
                    "x1": int(x1),
                    "y1": int(y1),
                    "x2": int(x2),
                    "y2": int(y2),
                    "width": int(node.width),
                    "height": int(node.height),
                    "bounds": (int(x1), int(y1), int(x2), int(y2)),
                    "center": tuple(int(v) for v in node.center()),
                    "importance": float(node.importance),
                    "complexity": float(node.complexity),
                    "change_frequency": float(node.change_frequency),
                    "level": int(node.level),
                    "area": int(node.area()),
                    "hash": node.hash_value,
                    "last_update": (
                        node.last_update.isoformat() if node.last_update else None
                    ),
                    "source": "quadtree",
                    "image_id": image_id,
                    "analyzed_at": time.time(),
                }
            )

        return regions
    
    def _collect_important_nodes(self, node: QuadNode, threshold: float,
                               result: List[QuadNode],
                               bounds: Optional[Tuple[int, int, int, int]] = None):
        """Recursively collect nodes meeting importance threshold"""
        # Check if node intersects query bounds
        if bounds:
            x1, y1, x2, y2 = bounds
            nx1, ny1, nx2, ny2 = node.bounds()
            if not (nx1 < x2 and nx2 > x1 and ny1 < y2 and ny2 > y1):
                return
        
        # If leaf node or meets criteria
        if node.children is None:
            if node.importance >= threshold:
                result.append(node)
                node.access_count += 1
        else:
            # Check if entire node meets criteria
            if node.importance >= threshold + 0.2:  # Higher threshold for parent
                result.append(node)
                node.access_count += 1
            else:
                # Recurse to children
                for child in node.children:
                    self._collect_important_nodes(child, threshold, result, bounds)
    
    def _merge_overlapping_nodes(self, nodes: List[QuadNode]) -> List[QuadNode]:
        """Merge overlapping nodes to reduce redundancy"""
        if not nodes:
            return []
            
        merged = []
        used = set()
        
        for i, node1 in enumerate(nodes):
            if i in used:
                continue
                
            # Check for overlaps with remaining nodes
            merge_group = [node1]
            x1_min, y1_min, x1_max, y1_max = node1.bounds()
            
            for j, node2 in enumerate(nodes[i+1:], i+1):
                if j in used:
                    continue
                    
                x2_min, y2_min, x2_max, y2_max = node2.bounds()
                
                # Check overlap
                if (x1_min < x2_max and x1_max > x2_min and
                    y1_min < y2_max and y1_max > y2_min):
                    
                    # Merge bounds
                    x1_min = min(x1_min, x2_min)
                    y1_min = min(y1_min, y2_min)
                    x1_max = max(x1_max, x2_max)
                    y1_max = max(y1_max, y2_max)
                    
                    merge_group.append(node2)
                    used.add(j)
            
            # Create merged node
            if len(merge_group) > 1:
                merged_node = QuadNode(
                    x1_min, y1_min,
                    x1_max - x1_min, y1_max - y1_min,
                    min(n.level for n in merge_group)
                )
                merged_node.importance = max(n.importance for n in merge_group)
                merged_node.complexity = max(n.complexity for n in merge_group)
                merged.append(merged_node)
            else:
                merged.append(node1)
            
            used.add(i)
        
        return merged
    
    async def update_regions(self, image_id: str, new_image: np.ndarray,
                           changed_bounds: Optional[List[Tuple[int, int, int, int]]] = None):
        """Update quadtree with new image data"""
        if image_id not in self.quadtrees:
            # Build new tree
            await self.build_quadtree(new_image, image_id)
            return
        
        root = self.quadtrees[image_id]
        importance_map = self.importance_maps[image_id]
        
        if changed_bounds:
            # Update only changed regions
            for bounds in changed_bounds:
                await self._update_node_recursive(root, new_image, importance_map, bounds)
        else:
            # Full update
            await self._update_node_recursive(root, new_image, importance_map)
        
        # Invalidate affected cache entries
        self._invalidate_cache(image_id)
    
    async def _update_node_recursive(self, node: QuadNode, image: np.ndarray,
                                   importance_map: np.ndarray,
                                   update_bounds: Optional[Tuple[int, int, int, int]] = None):
        """Recursively update nodes"""
        x1, y1, x2, y2 = node.bounds()
        
        # Check if node intersects update region
        if update_bounds:
            ux1, uy1, ux2, uy2 = update_bounds
            if not (x1 < ux2 and x2 > ux1 and y1 < uy2 and y2 > uy1):
                return
        
        # Extract region
        region = image[y1:y2, x1:x2]
        new_hash = self._calculate_hash(region)
        
        # Check if changed
        if new_hash != node.hash_value:
            # Update node properties
            region_importance = importance_map[y1:y2, x1:x2]
            node.importance = float(np.mean(region_importance))
            node.complexity = self._calculate_complexity(region)
            node.hash_value = new_hash
            node.last_update = datetime.now()
            node.change_frequency = min(node.change_frequency + 0.1, 1.0)
            
            # Clear cache
            node.cached_result = None
            node.cache_timestamp = None
        
        # Update children if exist
        if node.children:
            for child in node.children:
                await self._update_node_recursive(child, image, importance_map, update_bounds)
    
    def _invalidate_cache(self, image_id: str):
        """Invalidate cache entries for image"""
        keys_to_remove = [k for k in self.query_cache if k.startswith(f"{image_id}_")]
        for key in keys_to_remove:
            del self.query_cache[key]
    
    async def optimize_for_patterns(self, image_id: str):
        """Optimize quadtree based on access patterns"""
        if image_id not in self.access_patterns:
            return
            
        patterns = self.access_patterns[image_id]
        if len(patterns) < 10:
            return
        
        # Cluster access points
        access_points = np.array(patterns)
        
        # Simple k-means clustering
        from sklearn.cluster import KMeans
        n_clusters = min(5, len(patterns) // 10)
        kmeans = KMeans(n_clusters=n_clusters, random_state=42)
        clusters = kmeans.fit_predict(access_points)
        
        # Adjust importance based on access frequency
        root = self.quadtrees[image_id]
        importance_map = self.importance_maps[image_id]
        
        for i, center in enumerate(kmeans.cluster_centers_):
            x, y = int(center[0]), int(center[1])
            
            # Boost importance around frequently accessed areas
            cv2.circle(importance_map, (x, y), 100, 1.2, -1)
        
        # Clip values
        importance_map = np.clip(importance_map, 0.0, 1.0)
        
        # Rebuild affected parts of tree
        await self._update_node_recursive(root, None, importance_map)
    
    def get_processing_recommendations(self, image_id: str,
                                     available_api_calls: int = 10) -> List[Dict[str, Any]]:
        """Get recommendations for efficient processing"""
        if image_id not in self.quadtrees:
            return []
            
        root = self.quadtrees[image_id]
        recommendations = []
        
        # Collect all leaf nodes
        leaf_nodes = []
        self._collect_leaf_nodes(root, leaf_nodes)
        
        # Categorize by importance and change frequency
        critical_nodes = [n for n in leaf_nodes if n.importance > 0.8]
        dynamic_nodes = [n for n in leaf_nodes if n.change_frequency > 0.5]
        static_nodes = [n for n in leaf_nodes if n.change_frequency < 0.1]
        
        # Recommendations
        if critical_nodes:
            recommendations.append({
                'type': 'process_critical',
                'nodes': critical_nodes[:available_api_calls // 2],
                'reason': 'High importance regions requiring immediate processing',
                'priority': 1.0
            })
        
        if dynamic_nodes:
            recommendations.append({
                'type': 'monitor_dynamic',
                'nodes': dynamic_nodes[:available_api_calls // 3],
                'reason': 'Frequently changing regions needing regular updates',
                'priority': 0.8
            })
        
        if len(static_nodes) > len(leaf_nodes) * 0.7:
            recommendations.append({
                'type': 'cache_static',
                'nodes': static_nodes,
                'reason': 'Majority static content - enable aggressive caching',
                'priority': 0.6
            })
        
        # API call optimization
        total_regions = len(leaf_nodes)
        if total_regions > available_api_calls:
            recommendations.append({
                'type': 'batch_process',
                'batch_size': available_api_calls,
                'reason': f'Limited API calls ({available_api_calls}) for {total_regions} regions',
                'priority': 0.9
            })
        
        return recommendations
    
    def _collect_leaf_nodes(self, node: QuadNode, result: List[QuadNode]):
        """Collect all leaf nodes"""
        if node.children is None:
            result.append(node)
        else:
            for child in node.children:
                self._collect_leaf_nodes(child, result)
    
    def _count_nodes(self, node: QuadNode) -> int:
        """Count total nodes in tree"""
        if node.children is None:
            return 1
        return 1 + sum(self._count_nodes(child) for child in node.children)
    
    def _get_max_depth(self, node: QuadNode) -> int:
        """Get maximum depth of tree"""
        if node.children is None:
            return node.level
        return max(self._get_max_depth(child) for child in node.children)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get system statistics"""
        stats = dict(self.stats)
        
        # Add tree statistics
        tree_stats = []
        for image_id, root in self.quadtrees.items():
            tree_stats.append({
                'image_id': image_id,
                'total_nodes': self._count_nodes(root),
                'max_depth': self._get_max_depth(root),
                'avg_importance': self._calculate_avg_importance(root)
            })
        
        stats['trees'] = tree_stats
        stats['cache_size'] = len(self.query_cache)
        
        # Memory usage
        quadtree_size = sum(self._estimate_node_memory(root) 
                           for root in self.quadtrees.values())
        importance_size = sum(m.nbytes for m in self.importance_maps.values())
        
        stats['memory_usage'] = {
            'quadtree_structures': quadtree_size,
            'importance_maps': importance_size,
            'query_cache': len(str(self.query_cache).encode()),
            'total': quadtree_size + importance_size
        }
        
        return stats
    
    def _calculate_avg_importance(self, node: QuadNode) -> float:
        """Calculate average importance of tree"""
        nodes = []
        self._collect_leaf_nodes(node, nodes)
        if not nodes:
            return 0.0
        return sum(n.importance for n in nodes) / len(nodes)
    
    def _estimate_node_memory(self, node: QuadNode) -> int:
        """Estimate memory usage of node and children"""
        # Base node size
        base_size = 200  # Approximate bytes per node
        
        if node.children is None:
            return base_size
        
        return base_size + sum(self._estimate_node_memory(child) 
                              for child in node.children)
    
    async def cleanup_old_data(self, max_age_hours: int = 24):
        """Clean up old cached data"""
        now = datetime.now()
        max_age = timedelta(hours=max_age_hours)
        
        # Clean query cache
        old_keys = []
        for key, result in self.query_cache.items():
            if result.nodes and result.nodes[0].cache_timestamp:
                if now - result.nodes[0].cache_timestamp > max_age:
                    old_keys.append(key)
        
        for key in old_keys:
            del self.query_cache[key]
        
        logger.info(f"Cleaned {len(old_keys)} old cache entries")
        
        # Clear old access patterns
        for image_id in list(self.access_patterns.keys()):
            self.access_patterns[image_id] = self.access_patterns[image_id][-1000:]

# Global instance management
_quadtree_instance: Optional[QuadtreeSpatialIntelligence] = None

def get_quadtree_spatial_intelligence() -> QuadtreeSpatialIntelligence:
    """Get global Quadtree instance"""
    global _quadtree_instance
    if _quadtree_instance is None:
        _quadtree_instance = QuadtreeSpatialIntelligence()
    return _quadtree_instance

# Export main classes and functions
__all__ = [
    'QuadtreeSpatialIntelligence',
    'QuadNode', 
    'QueryResult',
    'RegionImportance',
    'get_quadtree_spatial_intelligence'
]
