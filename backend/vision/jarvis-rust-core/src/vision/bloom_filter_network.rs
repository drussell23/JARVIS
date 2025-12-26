//! High-Performance Bloom Filter Network for JARVIS Vision System
//! Hierarchical bloom filter system optimized for duplicate detection with SIMD acceleration

use std::sync::Arc;
use parking_lot::RwLock;
use rayon::prelude::*;
use serde::{Serialize, Deserialize};
use std::collections::HashMap;
use std::time::{Instant, Duration};
use std::hash::{Hash, Hasher};
use fnv::FnvHasher;
use std::sync::atomic::{AtomicU64, AtomicU32, Ordering};
use pyo3::prelude::*;

// SIMD support for x86_64
#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

/// Bloom filter hierarchy levels
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BloomFilterLevel {
    Global = 0,
    Regional = 1,
    Element = 2,
}

/// Performance metrics for bloom filters
#[derive(Debug)]
pub struct BloomFilterMetrics {
    pub total_insertions: AtomicU64,
    pub total_queries: AtomicU64,
    pub probable_hits: AtomicU64,
    pub confirmed_hits: AtomicU64,
    pub false_positives: AtomicU64,
    pub reset_count: AtomicU32,
    pub last_reset: Instant,
}

impl Clone for BloomFilterMetrics {
    fn clone(&self) -> Self {
        Self {
            total_insertions: AtomicU64::new(self.total_insertions.load(Ordering::Relaxed)),
            total_queries: AtomicU64::new(self.total_queries.load(Ordering::Relaxed)),
            probable_hits: AtomicU64::new(self.probable_hits.load(Ordering::Relaxed)),
            confirmed_hits: AtomicU64::new(self.confirmed_hits.load(Ordering::Relaxed)),
            false_positives: AtomicU64::new(self.false_positives.load(Ordering::Relaxed)),
            reset_count: AtomicU32::new(self.reset_count.load(Ordering::Relaxed)),
            last_reset: self.last_reset,
        }
    }
}

impl Default for BloomFilterMetrics {
    fn default() -> Self {
        Self {
            total_insertions: AtomicU64::new(0),
            total_queries: AtomicU64::new(0),
            probable_hits: AtomicU64::new(0),
            confirmed_hits: AtomicU64::new(0),
            false_positives: AtomicU64::new(0),
            reset_count: AtomicU32::new(0),
            last_reset: Instant::now(),
        }
    }
}

impl BloomFilterMetrics {
    pub fn false_positive_rate(&self) -> f64 {
        let probable = self.probable_hits.load(Ordering::Relaxed);
        let false_pos = self.false_positives.load(Ordering::Relaxed);
        if probable == 0 {
            0.0
        } else {
            false_pos as f64 / probable as f64
        }
    }

    pub fn hit_rate(&self) -> f64 {
        let total = self.total_queries.load(Ordering::Relaxed);
        let hits = self.probable_hits.load(Ordering::Relaxed);
        if total == 0 {
            0.0
        } else {
            hits as f64 / total as f64
        }
    }
}

/// High-performance adaptive bloom filter with SIMD acceleration
pub struct AdaptiveBloomFilter {
    size_bits: usize,
    size_mb: f64,
    expected_elements: usize,
    max_false_positive_rate: f64,
    level: BloomFilterLevel,
    
    // Bit array - using u64 for SIMD operations
    bit_array: Vec<AtomicU64>,
    bit_array_len: usize,
    
    // Hash parameters
    num_hashes: usize,
    hash_seeds: Vec<u64>,
    
    // Metrics and state
    metrics: BloomFilterMetrics,
    saturation_threshold: f64,
}

impl AdaptiveBloomFilter {
    pub fn new(
        size_mb: f64,
        expected_elements: usize,
        max_false_positive_rate: f64,
        level: BloomFilterLevel,
    ) -> Self {
        let size_bits = (size_mb * 1024.0 * 1024.0 * 8.0) as usize;
        let num_hashes = match level {
            BloomFilterLevel::Global => 10,     // 10 hash functions as per spec
            BloomFilterLevel::Regional => 7,    // 7 hash functions as per spec
            BloomFilterLevel::Element => 5,     // 5 hash functions as per spec
        };
        
        // Align to u64 boundaries for SIMD
        let bit_array_len = (size_bits + 63) / 64;
        let mut bit_array = Vec::with_capacity(bit_array_len);
        for _ in 0..bit_array_len {
            bit_array.push(AtomicU64::new(0));
        }
        
        // Generate diverse hash seeds
        let hash_seeds: Vec<u64> = (0..num_hashes)
            .map(|i| 2654435761u64.wrapping_mul(i as u64 + 1))
            .collect();
        
        Self {
            size_bits,
            size_mb,
            expected_elements,
            max_false_positive_rate,
            level,
            bit_array,
            bit_array_len,
            num_hashes,
            hash_seeds,
            metrics: BloomFilterMetrics::default(),
            saturation_threshold: 0.8,
        }
    }
    
    fn calculate_optimal_hash_functions(size_bits: usize, expected_elements: usize) -> usize {
        // k = (m/n) * ln(2)
        let optimal_k = (size_bits as f64 / expected_elements as f64) * std::f64::consts::LN_2;
        // Clamp between 1 and 12 for optimal performance
        (optimal_k.round() as usize).max(1).min(12)
    }
    
    /// Override number of hashes based on level (as per spec)
    fn get_hash_count_for_level(&self) -> usize {
        match self.level {
            BloomFilterLevel::Global => 10,     // 10 hash functions
            BloomFilterLevel::Regional => 7,    // 7 hash functions
            BloomFilterLevel::Element => 5,     // 5 hash functions
        }
    }
    
    /// Fast hash function optimized for bloom filters
    #[inline]
    fn fast_hash(&self, data: &[u8], seed: u64) -> usize {
        // Use FNV hash with seed for good distribution
        let mut hasher = FnvHasher::with_key(seed);
        hasher.write(data);
        (hasher.finish() as usize) % self.size_bits
    }
    
    /// SIMD-optimized hash function for multiple seeds
    #[cfg(target_arch = "x86_64")]
    fn simd_multi_hash(&self, data: &[u8]) -> Vec<usize> {
        if is_x86_feature_detected!("avx2") {
            self.simd_multi_hash_avx2(data)
        } else {
            // Fallback to sequential hashing
            self.hash_seeds.iter()
                .map(|&seed| self.fast_hash(data, seed))
                .collect()
        }
    }
    
    #[cfg(target_arch = "x86_64")]
    fn simd_multi_hash_avx2(&self, data: &[u8]) -> Vec<usize> {
        // For simplicity, use sequential for now - full SIMD implementation would be more complex
        self.hash_seeds.iter()
            .map(|&seed| self.fast_hash(data, seed))
            .collect()
    }
    
    #[cfg(not(target_arch = "x86_64"))]
    fn simd_multi_hash(&self, data: &[u8]) -> Vec<usize> {
        self.hash_seeds.iter()
            .map(|&seed| self.fast_hash(data, seed))
            .collect()
    }
    
    pub fn add(&self, data: &[u8]) -> bool {
        let positions = self.simd_multi_hash(data);
        
        // Set bits atomically
        for &pos in &positions {
            let word_index = pos / 64;
            let bit_index = pos % 64;
            let mask = 1u64 << bit_index;
            
            if word_index < self.bit_array_len {
                self.bit_array[word_index].fetch_or(mask, Ordering::Relaxed);
            }
        }
        
        self.metrics.total_insertions.fetch_add(1, Ordering::Relaxed);
        true
    }
    
    pub fn contains(&self, data: &[u8]) -> bool {
        self.metrics.total_queries.fetch_add(1, Ordering::Relaxed);
        
        let positions = self.simd_multi_hash(data);
        
        // Check all positions
        for &pos in &positions {
            let word_index = pos / 64;
            let bit_index = pos % 64;
            let mask = 1u64 << bit_index;
            
            if word_index >= self.bit_array_len {
                return false;
            }
            
            let word = self.bit_array[word_index].load(Ordering::Relaxed);
            if (word & mask) == 0 {
                return false; // Definitely not in set
            }
        }
        
        // All positions are set - probably in set
        self.metrics.probable_hits.fetch_add(1, Ordering::Relaxed);
        true
    }
    
    pub fn reset(&self) {
        // Reset all bits atomically
        for word in &self.bit_array {
            word.store(0, Ordering::Relaxed);
        }
        
        // Reset metrics (keeping historical reset count)
        let old_reset_count = self.metrics.reset_count.load(Ordering::Relaxed);
        
        self.metrics.total_insertions.store(0, Ordering::Relaxed);
        self.metrics.total_queries.store(0, Ordering::Relaxed);
        self.metrics.probable_hits.store(0, Ordering::Relaxed);
        self.metrics.confirmed_hits.store(0, Ordering::Relaxed);
        self.metrics.false_positives.store(0, Ordering::Relaxed);
        self.metrics.reset_count.store(old_reset_count + 1, Ordering::Relaxed);
    }
    
    pub fn estimate_saturation(&self) -> f64 {
        // Estimate saturation by sampling bits (for performance)
        const SAMPLE_SIZE: usize = 1000;
        let step = self.bit_array_len.max(SAMPLE_SIZE) / SAMPLE_SIZE;
        
        let mut set_bits = 0u64;
        let mut total_bits = 0u64;
        
        for i in (0..self.bit_array_len).step_by(step) {
            let word = self.bit_array[i].load(Ordering::Relaxed);
            set_bits += word.count_ones() as u64;
            total_bits += 64;
        }
        
        if total_bits == 0 {
            0.0
        } else {
            set_bits as f64 / total_bits as f64
        }
    }
    
    pub fn get_metrics(&self) -> HashMap<String, f64> {
        let mut metrics = HashMap::new();
        metrics.insert("size_mb".to_string(), self.size_mb);
        metrics.insert("total_insertions".to_string(), self.metrics.total_insertions.load(Ordering::Relaxed) as f64);
        metrics.insert("total_queries".to_string(), self.metrics.total_queries.load(Ordering::Relaxed) as f64);
        metrics.insert("hit_rate".to_string(), self.metrics.hit_rate());
        metrics.insert("false_positive_rate".to_string(), self.metrics.false_positive_rate());
        metrics.insert("saturation_level".to_string(), self.estimate_saturation());
        metrics.insert("reset_count".to_string(), self.metrics.reset_count.load(Ordering::Relaxed) as f64);
        metrics
    }
}

/// High-performance hierarchical bloom filter network
pub struct BloomFilterNetwork {
    global_filter: Arc<AdaptiveBloomFilter>,
    regional_filters: Vec<Arc<AdaptiveBloomFilter>>,  // 4 quadrant filters
    element_filter: Arc<AdaptiveBloomFilter>,
    
    // Network metrics
    network_metrics: Arc<RwLock<HashMap<String, AtomicU64>>>,
    enable_hierarchical_checking: bool,
}

impl BloomFilterNetwork {
    pub fn new(
        global_size_mb: f64,    // 4MB for global
        regional_size_mb: f64,   // 1MB × 4 for regional (total 4MB)
        element_size_mb: f64,    // 2MB for element
    ) -> Self {
        let global_filter = Arc::new(AdaptiveBloomFilter::new(
            global_size_mb,
            100000,  // Expected elements (increased for global scope)
            0.01,   // Max FP rate
            BloomFilterLevel::Global,
        ));
        
        // Create 4 regional filters (one per quadrant) - 1MB each
        let regional_filters: Vec<Arc<AdaptiveBloomFilter>> = (0..4)
            .map(|_| Arc::new(AdaptiveBloomFilter::new(
                regional_size_mb,
                5000,   // Elements per quadrant
                0.01,
                BloomFilterLevel::Regional,
            )))
            .collect();
        
        let element_filter = Arc::new(AdaptiveBloomFilter::new(
            element_size_mb,
            20000,  // UI elements (increased for element scope)
            0.01,
            BloomFilterLevel::Element,
        ));
        
        let mut network_metrics = HashMap::new();
        network_metrics.insert("total_checks".to_string(), AtomicU64::new(0));
        network_metrics.insert("global_hits".to_string(), AtomicU64::new(0));
        network_metrics.insert("regional_hits".to_string(), AtomicU64::new(0));
        network_metrics.insert("element_hits".to_string(), AtomicU64::new(0));
        network_metrics.insert("total_misses".to_string(), AtomicU64::new(0));
        network_metrics.insert("hierarchical_shortcuts".to_string(), AtomicU64::new(0));
        
        Self {
            global_filter,
            regional_filters,
            element_filter,
            network_metrics: Arc::new(RwLock::new(network_metrics)),
            enable_hierarchical_checking: true,
        }
    }
    
    pub fn check_and_add(&self, data: &[u8], level: BloomFilterLevel) -> (bool, BloomFilterLevel) {
        {
            let metrics = self.network_metrics.read();
            metrics.get("total_checks").unwrap().fetch_add(1, Ordering::Relaxed);
        }
        
        if self.enable_hierarchical_checking {
            // Hierarchical checking with short-circuit
            
            // Check Global first
            if self.global_filter.contains(data) {
                let metrics = self.network_metrics.read();
                metrics.get("global_hits").unwrap().fetch_add(1, Ordering::Relaxed);
                metrics.get("hierarchical_shortcuts").unwrap().fetch_add(1, Ordering::Relaxed);
                return (true, BloomFilterLevel::Global);
            }
            
            // Check Regional filters (check all quadrants)
            if matches!(level, BloomFilterLevel::Regional | BloomFilterLevel::Element) {
                for regional_filter in &self.regional_filters {
                    if regional_filter.contains(data) {
                        let metrics = self.network_metrics.read();
                        metrics.get("regional_hits").unwrap().fetch_add(1, Ordering::Relaxed);
                        // Promote to global
                        self.global_filter.add(data);
                        return (true, BloomFilterLevel::Regional);
                    }
                }
            }
            
            // Check Element
            if level == BloomFilterLevel::Element {
                if self.element_filter.contains(data) {
                    let metrics = self.network_metrics.read();
                    metrics.get("element_hits").unwrap().fetch_add(1, Ordering::Relaxed);
                    // Promote to higher levels
                    // Add to all regional filters for promotion
                    for regional_filter in &self.regional_filters {
                        regional_filter.add(data);
                    }
                    self.global_filter.add(data);
                    return (true, BloomFilterLevel::Element);
                }
            }
        }
        
        // Not found - add to appropriate levels
        {
            let metrics = self.network_metrics.read();
            metrics.get("total_misses").unwrap().fetch_add(1, Ordering::Relaxed);
        }
        
        match level {
            BloomFilterLevel::Global => {
                self.global_filter.add(data);
            }
            BloomFilterLevel::Regional => {
                // Add to all regional filters for simplicity
                // In a real implementation, you'd determine the correct quadrant
                for regional_filter in &self.regional_filters {
                    regional_filter.add(data);
                }
            }
            BloomFilterLevel::Element => {
                self.element_filter.add(data);
            }
        }
        
        (false, level)
    }
    
    pub fn get_network_stats(&self) -> HashMap<String, f64> {
        let mut stats = HashMap::new();
        
        // Network metrics
        {
            let metrics = self.network_metrics.read();
            for (key, value) in metrics.iter() {
                stats.insert(key.clone(), value.load(Ordering::Relaxed) as f64);
            }
        }
        
        // Individual filter stats
        let global_stats = self.global_filter.get_metrics();
        for (key, value) in global_stats {
            stats.insert(format!("global_{}", key), value);
        }
        
        // Aggregate regional stats
        for (idx, regional_filter) in self.regional_filters.iter().enumerate() {
            let regional_stats = regional_filter.get_metrics();
            for (key, value) in regional_stats {
                stats.insert(format!("regional_q{}_{}", idx, key), value);
            }
        }
        
        let element_stats = self.element_filter.get_metrics();
        for (key, value) in element_stats {
            stats.insert(format!("element_{}", key), value);
        }
        
        // Calculate derived metrics
        let total_checks = *stats.get("total_checks").unwrap_or(&0.0);
        let total_hits = *stats.get("global_hits").unwrap_or(&0.0) +
                        *stats.get("regional_hits").unwrap_or(&0.0) +
                        *stats.get("element_hits").unwrap_or(&0.0);
        let hierarchical_shortcuts = *stats.get("hierarchical_shortcuts").unwrap_or(&0.0);

        if total_checks > 0.0 {
            stats.insert("overall_hit_rate".to_string(), total_hits / total_checks);
            stats.insert("hierarchical_efficiency".to_string(), hierarchical_shortcuts / total_checks);
        }
        
        stats
    }
    
    pub fn reset_all(&self) {
        self.global_filter.reset();
        for regional_filter in &self.regional_filters {
            regional_filter.reset();
        }
        self.element_filter.reset();
        
        // Reset network metrics
        let metrics = self.network_metrics.read();
        for value in metrics.values() {
            value.store(0, Ordering::Relaxed);
        }
    }
    
    pub fn optimize_network(&self) {
        // Check saturation levels and reset if needed
        let global_sat = self.global_filter.estimate_saturation();
        let regional_sats: Vec<f64> = self.regional_filters.iter()
            .map(|rf| rf.estimate_saturation())
            .collect();
        let element_sat = self.element_filter.estimate_saturation();
        
        if global_sat > 0.85 {
            self.global_filter.reset();
        }
        for (idx, regional_filter) in self.regional_filters.iter().enumerate() {
            if regional_sats[idx] > 0.85 {
                regional_filter.reset();
            }
        }
        if element_sat > 0.85 {
            self.element_filter.reset();
        }
    }
}

// Python bindings
#[pymodule]
fn rust_bloom_bindings(_py: Python, m: &PyModule) -> PyResult<()> {
    #[pyfn(m)]
    #[pyo3(name = "rust_bloom_hash")]
    fn rust_bloom_hash(data: &[u8], seed: u64) -> u64 {
        let mut hasher = FnvHasher::with_key(seed);
        hasher.write(data);
        hasher.finish()
    }
    
    Ok(())
}

// Global singleton for C-style interface
use std::sync::Once;
static INIT: Once = Once::new();
static mut NETWORK: Option<Arc<BloomFilterNetwork>> = None;

pub fn get_global_bloom_network() -> Arc<BloomFilterNetwork> {
    unsafe {
        INIT.call_once(|| {
            NETWORK = Some(Arc::new(BloomFilterNetwork::new(4.0, 1.0, 2.0)));
        });
        NETWORK.as_ref().unwrap().clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_adaptive_bloom_filter() {
        let filter = AdaptiveBloomFilter::new(1.0, 1000, 0.01, BloomFilterLevel::Global);
        
        // Test basic operations
        let data = b"test_element";
        assert!(!filter.contains(data)); // Should not be present initially
        
        filter.add(data);
        assert!(filter.contains(data)); // Should be present after adding
        
        // Test different data
        let data2 = b"different_element";
        assert!(!filter.contains(data2)); // Should not have false positives for very different data
    }
    
    #[test]
    fn test_bloom_filter_network() {
        let network = BloomFilterNetwork::new(4.0, 1.0, 2.0);
        
        let data = b"test_network_element";
        
        // First check should return false (not found)
        let (found, level) = network.check_and_add(data, BloomFilterLevel::Element);
        assert!(!found);
        assert_eq!(level, BloomFilterLevel::Element);
        
        // Second check should return true (found)
        let (found, level) = network.check_and_add(data, BloomFilterLevel::Element);
        assert!(found);
    }
    
    #[test]
    fn test_hierarchical_promotion() {
        let network = BloomFilterNetwork::new(1.0, 1.0, 1.0);
        
        let data = b"hierarchical_test";
        
        // Add to element level
        network.check_and_add(data, BloomFilterLevel::Element);
        
        // Should now be found at global level due to promotion
        assert!(network.global_filter.contains(data));
        // Check at least one regional filter contains it
        let regional_found = network.regional_filters.iter()
            .any(|rf| rf.contains(data));
        assert!(regional_found);
        assert!(network.element_filter.contains(data));
    }
}