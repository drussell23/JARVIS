//! High-performance Quadtree spatial operations for JARVIS Vision
//! Optimized with SIMD for fast region processing

use super::{ImageData, CaptureRegion};
use crate::{Result, JarvisError};
use std::sync::Arc;
use parking_lot::RwLock;
use rayon::prelude::*;
use serde::{Serialize, Deserialize};
use std::collections::{HashMap, BinaryHeap};
use std::cmp::Ordering;

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

/// Quadtree node for spatial subdivision
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuadNode {
    pub bounds: CaptureRegion,
    pub level: u8,
    pub importance: f32,
    pub complexity: f32,
    pub hash: u64,
    pub children: Option<Box<[QuadNode; 4]>>,
}

impl QuadNode {
    /// Create new node
    pub fn new(bounds: CaptureRegion, level: u8) -> Self {
        Self {
            bounds,
            level,
            importance: 0.5,
            complexity: 0.5,
            hash: 0,
            children: None,
        }
    }

    /// Check if point is within bounds
    #[inline]
    pub fn contains_point(&self, x: u32, y: u32) -> bool {
        x >= self.bounds.x && x < self.bounds.x + self.bounds.width &&
        y >= self.bounds.y && y < self.bounds.y + self.bounds.height
    }

    /// Check if bounds intersect
    #[inline]
    pub fn intersects(&self, other: &CaptureRegion) -> bool {
        self.bounds.x < other.x + other.width &&
        self.bounds.x + self.bounds.width > other.x &&
        self.bounds.y < other.y + other.height &&
        self.bounds.y + self.bounds.height > other.y
    }

    /// Calculate area
    #[inline]
    pub fn area(&self) -> u32 {
        self.bounds.width * self.bounds.height
    }
}

/// SIMD-accelerated importance calculator
pub struct ImportanceCalculator {
    edge_threshold: u8,
    variance_scale: f32,
}

impl ImportanceCalculator {
    pub fn new() -> Self {
        Self {
            edge_threshold: 50,
            variance_scale: 0.01,
        }
    }

    /// Calculate importance using SIMD operations
    #[cfg(target_arch = "x86_64")]
    pub unsafe fn calculate_importance_simd(&self, data: &[u8], width: usize, height: usize) -> f32 {
        if !is_x86_feature_detected!("avx2") {
            return self.calculate_importance_scalar(data, width, height);
        }

        let mut edge_count = 0u32;
        let mut sum = 0u32;
        let mut sum_sq = 0u64;
        
        // Process pixels in chunks of 32 (AVX2 width)
        let chunks = data.chunks_exact(32);
        let remainder = chunks.remainder();
        
        // Edge detection threshold
        let threshold = _mm256_set1_epi8(self.edge_threshold as i8);
        
        for chunk in chunks {
            let pixels = _mm256_loadu_si256(chunk.as_ptr() as *const __m256i);
            
            // Calculate sum for variance
            let zero = _mm256_setzero_si256();
            let sad = _mm256_sad_epu8(pixels, zero);
            sum += _mm256_extract_epi32(sad, 0) as u32 + 
                   _mm256_extract_epi32(sad, 2) as u32 +
                   _mm256_extract_epi32(sad, 4) as u32 +
                   _mm256_extract_epi32(sad, 6) as u32;
            
            // Simple edge detection
            let shifted = _mm256_srli_si256(pixels, 1);
            let diff = _mm256_sub_epi8(pixels, shifted);
            let abs_diff = _mm256_abs_epi8(diff);
            let edges = _mm256_cmpgt_epi8(abs_diff, threshold);
            
            // Count edge pixels
            let edge_mask = _mm256_movemask_epi8(edges);
            edge_count += edge_mask.count_ones();
            
            // Calculate sum of squares for variance
            // Note: This is simplified for performance
            for &byte in chunk {
                sum_sq += (byte as u64) * (byte as u64);
            }
        }
        
        // Process remainder
        for i in 0..remainder.len() {
            let byte = remainder[i];
            sum += byte as u32;
            sum_sq += (byte as u64) * (byte as u64);

            // Edge check: compare adjacent elements
            if i + 1 < remainder.len() {
                let diff = (byte as i16 - remainder[i + 1] as i16).abs();
                if diff > self.edge_threshold as i16 {
                    edge_count += 1;
                }
            }
        }
        
        // Calculate metrics
        let total_pixels = (width * height) as f32;
        let edge_density = edge_count as f32 / total_pixels;
        
        let mean = sum as f32 / total_pixels;
        let variance = (sum_sq as f32 / total_pixels) - (mean * mean);
        let normalized_variance = (variance * self.variance_scale).min(1.0);
        
        // Combine factors
        0.6 * edge_density + 0.4 * normalized_variance
    }

    /// Fallback scalar implementation
    pub fn calculate_importance_scalar(&self, data: &[u8], width: usize, height: usize) -> f32 {
        let mut edge_count = 0;
        let mut sum = 0u64;
        let mut sum_sq = 0u64;
        
        // Process each pixel
        for y in 0..height {
            for x in 0..width {
                let idx = y * width + x;
                if idx >= data.len() {
                    break;
                }
                
                let pixel = data[idx];
                sum += pixel as u64;
                sum_sq += (pixel as u64) * (pixel as u64);
                
                // Simple edge detection
                if x > 0 {
                    let diff = (pixel as i16 - data[idx - 1] as i16).abs();
                    if diff > self.edge_threshold as i16 {
                        edge_count += 1;
                    }
                }
                
                if y > 0 {
                    let diff = (pixel as i16 - data[idx - width] as i16).abs();
                    if diff > self.edge_threshold as i16 {
                        edge_count += 1;
                    }
                }
            }
        }
        
        let total_pixels = (width * height) as f32;
        let edge_density = edge_count as f32 / total_pixels;
        
        let mean = sum as f32 / total_pixels;
        let variance = (sum_sq as f32 / total_pixels) - (mean * mean);
        let normalized_variance = (variance * self.variance_scale).min(1.0);
        
        0.6 * edge_density + 0.4 * normalized_variance
    }
}

/// Spatial quadtree for efficient region processing
pub struct SpatialQuadtree {
    root: Arc<RwLock<QuadNode>>,
    max_depth: u8,
    min_size: u32,
    importance_calculator: Arc<ImportanceCalculator>,
    node_cache: Arc<RwLock<HashMap<u64, Arc<QuadNode>>>>,
}

impl SpatialQuadtree {
    pub fn new(width: u32, height: u32) -> Self {
        let bounds = CaptureRegion { x: 0, y: 0, width, height };
        let root = QuadNode::new(bounds, 0);
        
        Self {
            root: Arc::new(RwLock::new(root)),
            max_depth: 6,
            min_size: 64,
            importance_calculator: Arc::new(ImportanceCalculator::new()),
            node_cache: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Build adaptive quadtree from image data
    pub fn build_from_image(&mut self, image: &ImageData) -> Result<()> {
        let data = image.as_slice();
        let width = image.width as usize;
        let height = image.height as usize;
        
        // Clear cache
        self.node_cache.write().clear();
        
        // Build tree recursively
        let mut root = self.root.write();
        self.build_node_recursive(&mut root, data, width, height, image.width)?;
        
        Ok(())
    }

    fn build_node_recursive(
        &self,
        node: &mut QuadNode,
        data: &[u8],
        img_width: usize,
        img_height: usize,
        stride: u32,
    ) -> Result<()> {
        // Calculate importance for this region
        let region_data = self.extract_region_data(data, &node.bounds, stride)?;
        
        #[cfg(target_arch = "x86_64")]
        unsafe {
            node.importance = self.importance_calculator.calculate_importance_simd(
                &region_data,
                node.bounds.width as usize,
                node.bounds.height as usize,
            );
        }
        
        #[cfg(not(target_arch = "x86_64"))]
        {
            node.importance = self.importance_calculator.calculate_importance_scalar(
                &region_data,
                node.bounds.width as usize,
                node.bounds.height as usize,
            );
        }
        
        // Calculate hash
        node.hash = self.calculate_region_hash(&region_data);
        
        // Decide if subdivision is needed
        if self.should_subdivide(node) {
            let children = self.create_children(node);
            
            // Process children in parallel
            let child_results: Vec<_> = children
                .into_par_iter()
                .map(|mut child| {
                    self.build_node_recursive(&mut child, data, img_width, img_height, stride)
                        .map(|_| child)
                })
                .collect();
            
            // Check for errors
            let mut children_array = Vec::new();
            for result in child_results {
                match result {
                    Ok(child) => children_array.push(child),
                    Err(e) => return Err(e),
                }
            }
            
            if children_array.len() == 4 {
                node.children = Some(Box::new([
                    children_array[0].clone(),
                    children_array[1].clone(),
                    children_array[2].clone(),
                    children_array[3].clone(),
                ]));
            }
        }
        
        // Cache important nodes
        if node.importance > 0.7 {
            self.node_cache.write().insert(node.hash, Arc::new(node.clone()));
        }
        
        Ok(())
    }

    fn should_subdivide(&self, node: &QuadNode) -> bool {
        node.level < self.max_depth &&
        node.bounds.width > self.min_size &&
        node.bounds.height > self.min_size &&
        node.importance > 0.3 &&
        node.complexity > 0.5
    }

    fn create_children(&self, parent: &QuadNode) -> Vec<QuadNode> {
        let half_width = parent.bounds.width / 2;
        let half_height = parent.bounds.height / 2;
        let level = parent.level + 1;
        
        vec![
            // Top-left
            QuadNode::new(CaptureRegion {
                x: parent.bounds.x,
                y: parent.bounds.y,
                width: half_width,
                height: half_height,
            }, level),
            
            // Top-right
            QuadNode::new(CaptureRegion {
                x: parent.bounds.x + half_width,
                y: parent.bounds.y,
                width: parent.bounds.width - half_width,
                height: half_height,
            }, level),
            
            // Bottom-left
            QuadNode::new(CaptureRegion {
                x: parent.bounds.x,
                y: parent.bounds.y + half_height,
                width: half_width,
                height: parent.bounds.height - half_height,
            }, level),
            
            // Bottom-right
            QuadNode::new(CaptureRegion {
                x: parent.bounds.x + half_width,
                y: parent.bounds.y + half_height,
                width: parent.bounds.width - half_width,
                height: parent.bounds.height - half_height,
            }, level),
        ]
    }

    fn extract_region_data(&self, data: &[u8], bounds: &CaptureRegion, stride: u32) -> Result<Vec<u8>> {
        let mut region_data = Vec::with_capacity((bounds.width * bounds.height * 3) as usize);
        
        for y in 0..bounds.height {
            let src_y = bounds.y + y;
            if src_y >= stride {
                break;
            }
            
            for x in 0..bounds.width {
                let src_x = bounds.x + x;
                if src_x >= stride {
                    break;
                }
                
                let idx = (src_y * stride + src_x) as usize * 3;
                if idx + 2 < data.len() {
                    region_data.push(data[idx]);
                    region_data.push(data[idx + 1]);
                    region_data.push(data[idx + 2]);
                }
            }
        }
        
        Ok(region_data)
    }

    fn calculate_region_hash(&self, data: &[u8]) -> u64 {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        
        let mut hasher = DefaultHasher::new();
        
        // Sample data for faster hashing
        for i in (0..data.len()).step_by(16) {
            data[i].hash(&mut hasher);
        }
        
        hasher.finish()
    }

    /// Query regions by importance threshold
    pub fn query_important_regions(&self, threshold: f32, max_regions: usize) -> Vec<CaptureRegion> {
        let mut regions = Vec::new();
        let root = self.root.read();
        
        self.collect_important_regions(&root, threshold, &mut regions);
        
        // Sort by importance * area (prioritize large important regions)
        regions.sort_by(|a, b| {
            let score_a = a.1 * (a.0.area() as f32);
            let score_b = b.1 * (b.0.area() as f32);
            score_b.total_cmp(&score_a)
        });
        
        regions.truncate(max_regions);
        regions.into_iter().map(|(region, _)| region).collect()
    }

    fn collect_important_regions(
        &self,
        node: &QuadNode,
        threshold: f32,
        regions: &mut Vec<(CaptureRegion, f32)>,
    ) {
        if node.importance >= threshold {
            if node.children.is_none() {
                // Leaf node
                regions.push((node.bounds, node.importance));
            } else {
                // Check if all children meet threshold
                let children = node.children.as_ref().unwrap();
                let all_important = children.iter().all(|c| c.importance >= threshold);
                
                if all_important {
                    // Use parent region instead of children
                    regions.push((node.bounds, node.importance));
                } else {
                    // Recurse to children
                    for child in children.iter() {
                        self.collect_important_regions(child, threshold, regions);
                    }
                }
            }
        } else if let Some(children) = &node.children {
            // Check children even if parent doesn't meet threshold
            for child in children.iter() {
                self.collect_important_regions(child, threshold, regions);
            }
        }
    }

    /// Find regions containing a point
    pub fn regions_at_point(&self, x: u32, y: u32) -> Vec<(CaptureRegion, u8, f32)> {
        let mut regions = Vec::new();
        let root = self.root.read();
        
        self.find_regions_containing_point(&root, x, y, &mut regions);
        regions
    }

    fn find_regions_containing_point(
        &self,
        node: &QuadNode,
        x: u32,
        y: u32,
        regions: &mut Vec<(CaptureRegion, u8, f32)>,
    ) {
        if !node.contains_point(x, y) {
            return;
        }
        
        regions.push((node.bounds, node.level, node.importance));
        
        if let Some(children) = &node.children {
            for child in children.iter() {
                self.find_regions_containing_point(child, x, y, regions);
            }
        }
    }

    /// Get regions intersecting a bounds
    pub fn regions_intersecting(&self, bounds: &CaptureRegion) -> Vec<CaptureRegion> {
        let mut regions = Vec::new();
        let root = self.root.read();
        
        self.find_intersecting_regions(&root, bounds, &mut regions);
        regions
    }

    fn find_intersecting_regions(
        &self,
        node: &QuadNode,
        bounds: &CaptureRegion,
        regions: &mut Vec<CaptureRegion>,
    ) {
        if !node.intersects(bounds) {
            return;
        }
        
        if node.children.is_none() {
            regions.push(node.bounds);
        } else {
            for child in node.children.as_ref().unwrap().iter() {
                self.find_intersecting_regions(child, bounds, regions);
            }
        }
    }

    /// Get tree statistics
    pub fn get_stats(&self) -> QuadtreeStats {
        let root = self.root.read();
        let mut stats = QuadtreeStats::default();
        
        self.calculate_stats(&root, &mut stats);
        stats.cache_size = self.node_cache.read().len();
        
        stats
    }

    fn calculate_stats(&self, node: &QuadNode, stats: &mut QuadtreeStats) {
        stats.total_nodes += 1;
        stats.max_depth = stats.max_depth.max(node.level);
        
        if node.children.is_none() {
            stats.leaf_nodes += 1;
            stats.total_importance += node.importance;
        } else {
            for child in node.children.as_ref().unwrap().iter() {
                self.calculate_stats(child, stats);
            }
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct QuadtreeStats {
    pub total_nodes: usize,
    pub leaf_nodes: usize,
    pub max_depth: u8,
    pub total_importance: f32,
    pub cache_size: usize,
}

/// Priority queue entry for region processing
#[derive(Clone)]
struct PriorityRegion {
    region: CaptureRegion,
    priority: f32,
}

impl Eq for PriorityRegion {}

impl PartialEq for PriorityRegion {
    fn eq(&self, other: &Self) -> bool {
        self.priority == other.priority
    }
}

impl Ord for PriorityRegion {
    fn cmp(&self, other: &Self) -> Ordering {
        self.priority.total_cmp(&other.priority)
    }
}

impl PartialOrd for PriorityRegion {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        self.priority.partial_cmp(&other.priority)
    }
}

/// Batch processor for efficient region analysis
pub struct RegionBatchProcessor {
    max_batch_size: usize,
    priority_queue: BinaryHeap<PriorityRegion>,
}

impl RegionBatchProcessor {
    pub fn new(max_batch_size: usize) -> Self {
        Self {
            max_batch_size,
            priority_queue: BinaryHeap::new(),
        }
    }

    /// Add regions for processing
    pub fn add_regions(&mut self, regions: Vec<(CaptureRegion, f32)>) {
        for (region, importance) in regions {
            self.priority_queue.push(PriorityRegion {
                region,
                priority: importance,
            });
        }
    }

    /// Get next batch of regions to process
    pub fn next_batch(&mut self) -> Vec<CaptureRegion> {
        let mut batch = Vec::new();
        
        while batch.len() < self.max_batch_size && !self.priority_queue.is_empty() {
            if let Some(entry) = self.priority_queue.pop() {
                batch.push(entry.region);
            }
        }
        
        batch
    }

    /// Check if more batches available
    pub fn has_more(&self) -> bool {
        !self.priority_queue.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_quadnode_basic() {
        let bounds = CaptureRegion { x: 0, y: 0, width: 100, height: 100 };
        let node = QuadNode::new(bounds, 0);
        
        assert!(node.contains_point(50, 50));
        assert!(!node.contains_point(200, 200));
        assert_eq!(node.area(), 10000);
    }

    #[test]
    fn test_importance_calculator() {
        let calc = ImportanceCalculator::new();
        let data = vec![0u8; 1000];
        
        let importance = calc.calculate_importance_scalar(&data, 100, 10);
        assert!(importance >= 0.0 && importance <= 1.0);
    }

    #[test]
    fn test_spatial_quadtree() {
        let mut tree = SpatialQuadtree::new(1920, 1080);
        
        // Test querying
        let regions = tree.query_important_regions(0.5, 10);
        assert!(regions.len() <= 10);
        
        // Test point query
        let point_regions = tree.regions_at_point(960, 540);
        assert!(!point_regions.is_empty());
        
        // Test stats
        let stats = tree.get_stats();
        assert!(stats.total_nodes > 0);
    }
}