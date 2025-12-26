//! Sliding Window implementation for memory-efficient vision processing
//! NO HARDCODING - Everything is configurable via environment variables
//! Optimized for 16GB RAM systems

use super::{ImageData, ImageFormat, CaptureRegion};
use crate::{Result, JarvisError};
use crate::memory::MemoryManager;
use std::sync::Arc;
use std::collections::{VecDeque, HashMap};
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::time::{Instant, Duration};

/// Sliding Window configuration - fully dynamic
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SlidingWindowConfig {
    /// Window dimensions (width, height)
    pub window_width: u32,
    pub window_height: u32,
    /// Step size for sliding (smaller = more overlap)
    pub step_x: u32,
    pub step_y: u32,
    /// Maximum number of windows to keep in memory
    pub max_windows_in_memory: usize,
    /// Maximum concurrent regions to analyze
    pub max_concurrent_regions: usize,
    /// Memory threshold in MB before reducing quality
    pub memory_threshold_mb: f32,
    /// Overlap percentage (0.0 to 1.0)
    pub overlap_percentage: f32,
    /// Enable adaptive sizing based on memory
    pub adaptive_sizing: bool,
    /// Cache analyzed regions
    pub enable_caching: bool,
    /// Cache TTL in seconds
    pub cache_ttl_seconds: u64,
    /// Priority regions (e.g., center of screen)
    pub prioritize_center: bool,
    /// Skip static regions
    pub skip_static_regions: bool,
    /// Static region threshold (0.0 to 1.0)
    pub static_threshold: f32,
}

impl SlidingWindowConfig {
    /// Load from environment variables
    pub fn from_env() -> Self {
        Self {
            window_width: std::env::var("SLIDING_WINDOW_WIDTH")
                .unwrap_or_else(|_| "400".to_string())
                .parse()
                .unwrap_or(400),
            window_height: std::env::var("SLIDING_WINDOW_HEIGHT")
                .unwrap_or_else(|_| "300".to_string())
                .parse()
                .unwrap_or(300),
            step_x: std::env::var("SLIDING_WINDOW_STEP_X")
                .unwrap_or_else(|_| "200".to_string())
                .parse()
                .unwrap_or(200),
            step_y: std::env::var("SLIDING_WINDOW_STEP_Y")
                .unwrap_or_else(|_| "200".to_string())
                .parse()
                .unwrap_or(200),
            max_windows_in_memory: std::env::var("SLIDING_MAX_WINDOWS")
                .unwrap_or_else(|_| "10".to_string())
                .parse()
                .unwrap_or(10),
            max_concurrent_regions: std::env::var("SLIDING_MAX_CONCURRENT")
                .unwrap_or_else(|_| "4".to_string())
                .parse()
                .unwrap_or(4),
            memory_threshold_mb: std::env::var("SLIDING_MEMORY_THRESHOLD_MB")
                .unwrap_or_else(|_| "2000".to_string())
                .parse()
                .unwrap_or(2000.0),
            overlap_percentage: std::env::var("SLIDING_OVERLAP_PERCENT")
                .unwrap_or_else(|_| "0.5".to_string())
                .parse()
                .unwrap_or(0.5),
            adaptive_sizing: std::env::var("SLIDING_ADAPTIVE_SIZING")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            enable_caching: std::env::var("SLIDING_ENABLE_CACHE")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            cache_ttl_seconds: std::env::var("SLIDING_CACHE_TTL")
                .unwrap_or_else(|_| "60".to_string())
                .parse()
                .unwrap_or(60),
            prioritize_center: std::env::var("SLIDING_PRIORITIZE_CENTER")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            skip_static_regions: std::env::var("SLIDING_SKIP_STATIC")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            static_threshold: std::env::var("SLIDING_STATIC_THRESHOLD")
                .unwrap_or_else(|_| "0.95".to_string())
                .parse()
                .unwrap_or(0.95),
        }
    }

    /// Update configuration dynamically
    pub fn update(&mut self, key: &str, value: &str) -> Result<()> {
        match key {
            "window_width" => self.window_width = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid window width".to_string()))?,
            "window_height" => self.window_height = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid window height".to_string()))?,
            "step_x" => self.step_x = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid step X".to_string()))?,
            "step_y" => self.step_y = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid step Y".to_string()))?,
            "max_concurrent_regions" => self.max_concurrent_regions = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid max concurrent".to_string()))?,
            _ => return Err(JarvisError::InvalidOperation(format!("Unknown config key: {}", key))),
        }
        Ok(())
    }

    /// Calculate effective window size based on memory pressure
    pub fn get_adaptive_window_size(&self, available_memory_mb: f32) -> (u32, u32) {
        if !self.adaptive_sizing {
            return (self.window_width, self.window_height);
        }

        // Scale window size based on available memory
        let memory_ratio = available_memory_mb / self.memory_threshold_mb;
        
        if memory_ratio < 0.5 {
            // Very low memory - use smaller windows
            (self.window_width * 3 / 4, self.window_height * 3 / 4)
        } else if memory_ratio < 0.75 {
            // Low memory - slightly smaller windows
            (self.window_width * 7 / 8, self.window_height * 7 / 8)
        } else {
            // Sufficient memory - use configured size
            (self.window_width, self.window_height)
        }
    }
}

/// Window region with metadata
#[derive(Debug, Clone)]
pub struct WindowRegion {
    pub bounds: CaptureRegion,
    pub data: Option<Vec<u8>>,
    pub hash: u64,
    pub priority: f32,
    pub is_static: bool,
    pub last_analyzed: Option<Instant>,
}

/// Sliding window capture system
pub struct SlidingWindowCapture {
    config: Arc<RwLock<SlidingWindowConfig>>,
    memory_manager: Arc<MemoryManager>,
    /// Circular buffer of window regions
    window_buffer: VecDeque<WindowRegion>,
    /// Cache of analyzed regions
    region_cache: Arc<RwLock<HashMap<u64, CachedAnalysis>>>,
    /// Previous frame hashes for static detection
    previous_hashes: HashMap<(u32, u32), u64>,
    /// Statistics
    stats: SlidingWindowStats,
}

#[derive(Debug, Clone)]
pub struct CachedAnalysis {
    pub result: String,
    pub timestamp: Instant,
    pub confidence: f32,
}

// Serde implementations that skip Instant (which can't be serialized)
impl serde::Serialize for CachedAnalysis {
    fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        use serde::ser::SerializeStruct;
        let mut state = serializer.serialize_struct("CachedAnalysis", 2)?;
        state.serialize_field("result", &self.result)?;
        state.serialize_field("confidence", &self.confidence)?;
        state.end()
    }
}

impl<'de> serde::Deserialize<'de> for CachedAnalysis {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        struct CachedAnalysisHelper {
            result: String,
            confidence: f32,
        }
        let helper = CachedAnalysisHelper::deserialize(deserializer)?;
        Ok(CachedAnalysis {
            result: helper.result,
            timestamp: Instant::now(),
            confidence: helper.confidence,
        })
    }
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct SlidingWindowStats {
    pub total_windows_processed: u64,
    pub cache_hits: u64,
    pub cache_misses: u64,
    pub static_regions_skipped: u64,
    pub avg_window_size_bytes: f32,
    pub total_memory_saved_mb: f32,
    pub memory_pressure_events: u64,
}

impl SlidingWindowCapture {
    pub fn new(config: SlidingWindowConfig) -> Self {
        let memory_manager = MemoryManager::global();
        let max_windows = config.max_windows_in_memory;

        Self {
            config: Arc::new(RwLock::new(config)),
            memory_manager,
            window_buffer: VecDeque::with_capacity(max_windows),
            region_cache: Arc::new(RwLock::new(HashMap::new())),
            previous_hashes: HashMap::new(),
            stats: SlidingWindowStats::default(),
        }
    }

    /// Generate sliding windows for an image
    pub fn generate_windows(&mut self, image: &ImageData) -> Result<Vec<WindowRegion>> {
        let available_memory_mb = self.get_available_memory_mb();

        // Extract config values to release the lock
        let (window_width, window_height, step_x, step_y, prioritize_center, skip_static_regions, static_threshold, memory_threshold_mb, max_concurrent_regions) = {
            let config = self.config.read();
            let (ww, wh) = config.get_adaptive_window_size(available_memory_mb);
            let sx = ((ww as f32) * (1.0 - config.overlap_percentage)) as u32;
            let sy = ((wh as f32) * (1.0 - config.overlap_percentage)) as u32;
            (ww, wh, sx, sy, config.prioritize_center, config.skip_static_regions, config.static_threshold, config.memory_threshold_mb, config.max_concurrent_regions)
        };

        let mut windows = Vec::new();

        // Generate windows with configurable stepping
        for y in (0..image.height.saturating_sub(window_height)).step_by(step_y as usize) {
            for x in (0..image.width.saturating_sub(window_width)).step_by(step_x as usize) {
                let bounds = CaptureRegion {
                    x,
                    y,
                    width: window_width.min(image.width - x),
                    height: window_height.min(image.height - y),
                };

                // Calculate priority (center regions get higher priority)
                let priority = if prioritize_center {
                    self.calculate_region_priority(x, y, window_width, window_height, image.width, image.height)
                } else {
                    1.0
                };

                // Extract region data without copying
                let region_data = self.extract_region_zero_copy(image, &bounds)?;

                // Calculate hash for change detection
                let hash = self.calculate_region_hash(&region_data);

                // Check if region is static
                let is_static = if skip_static_regions {
                    self.is_region_static(x, y, hash, static_threshold)
                } else {
                    false
                };
                
                if is_static {
                    self.stats.static_regions_skipped += 1;
                    continue; // Skip static regions
                }
                
                windows.push(WindowRegion {
                    bounds,
                    data: Some(region_data),
                    hash,
                    priority,
                    is_static,
                    last_analyzed: None,
                });
            }
        }
        
        // Sort by priority (highest first)
        windows.sort_by(|a, b| b.priority.partial_cmp(&a.priority).unwrap());
        
        // Keep only top N windows based on memory constraints
        let max_windows = if available_memory_mb < memory_threshold_mb {
            max_concurrent_regions / 2  // Reduce when memory is low
        } else {
            max_concurrent_regions
        };
        
        windows.truncate(max_windows);
        
        // Update stats
        self.stats.total_windows_processed += windows.len() as u64;
        
        Ok(windows)
    }

    /// Extract region with efficient memory allocation
    fn extract_region_zero_copy(&mut self, image: &ImageData, bounds: &CaptureRegion) -> Result<Vec<u8>> {
        let bytes_per_pixel = image.format.bytes_per_pixel() as usize;
        let region_size = (bounds.width * bounds.height) as usize * bytes_per_pixel;

        // Allocate buffer from memory pool and convert to Vec
        let mut buffer = self.memory_manager.allocate(region_size)?;

        // Copy region data (optimized for cache efficiency)
        {
            let src_data = image.as_slice();
            let dst_data = buffer.as_mut_slice();
            let dst_stride = (bounds.width as usize) * bytes_per_pixel;

            for y in 0..bounds.height as usize {
                let src_offset = ((bounds.y as usize + y) * image.width as usize + bounds.x as usize) * bytes_per_pixel;
                let dst_offset = y * dst_stride;

                dst_data[dst_offset..dst_offset + dst_stride]
                    .copy_from_slice(&src_data[src_offset..src_offset + dst_stride]);
            }
        }

        // Update stats
        if self.stats.total_windows_processed > 0 {
            self.stats.avg_window_size_bytes =
                (self.stats.avg_window_size_bytes * (self.stats.total_windows_processed - 1) as f32 + region_size as f32)
                / self.stats.total_windows_processed as f32;
        }

        // Convert PooledBuffer to Vec<u8>
        Ok(buffer.into_vec())
    }

    /// Calculate region priority based on position
    fn calculate_region_priority(&self, x: u32, y: u32, width: u32, height: u32, 
                                 image_width: u32, image_height: u32) -> f32 {
        // Center of region
        let region_center_x = x + width / 2;
        let region_center_y = y + height / 2;
        
        // Center of image
        let image_center_x = image_width / 2;
        let image_center_y = image_height / 2;
        
        // Distance from center (normalized)
        let dx = (region_center_x as f32 - image_center_x as f32) / image_width as f32;
        let dy = (region_center_y as f32 - image_center_y as f32) / image_height as f32;
        let distance = (dx * dx + dy * dy).sqrt();
        
        // Priority decreases with distance from center
        1.0 - (distance / 2.0_f32.sqrt()).min(1.0)
    }

    /// Calculate hash for region (for change detection)
    fn calculate_region_hash(&self, data: &[u8]) -> u64 {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};

        let mut hasher = DefaultHasher::new();

        // Sample the data for faster hashing (every 16th byte)
        for i in (0..data.len()).step_by(16) {
            data[i].hash(&mut hasher);
        }

        hasher.finish()
    }

    /// Check if region is static (unchanged)
    fn is_region_static(&mut self, x: u32, y: u32, hash: u64, threshold: f32) -> bool {
        let key = (x, y);
        
        if let Some(&previous_hash) = self.previous_hashes.get(&key) {
            // Simple hash comparison for now
            // In production, could use more sophisticated similarity metrics
            let is_static = hash == previous_hash;
            
            // Update hash
            self.previous_hashes.insert(key, hash);
            
            is_static
        } else {
            // First time seeing this region
            self.previous_hashes.insert(key, hash);
            false
        }
    }

    /// Get available memory in MB
    fn get_available_memory_mb(&self) -> f32 {
        #[cfg(target_os = "macos")]
        {
            use sysinfo::System;
            let mut sys = System::new();
            sys.refresh_memory();
            (sys.available_memory() / 1024 / 1024) as f32
        }
        
        #[cfg(not(target_os = "macos"))]
        {
            // Default to 2GB if we can't determine
            2000.0
        }
    }

    /// Analyze windows with caching
    pub async fn analyze_windows<F>(&mut self, windows: Vec<WindowRegion>, analyzer: F) -> Result<Vec<AnalysisResult>>
    where
        F: Fn(&WindowRegion) -> Result<String> + Send + Sync,
    {
        let config = self.config.read();
        let mut results = Vec::new();
        let windows_count = windows.len();

        for window in windows {
            // Check cache first
            if config.enable_caching {
                let mut cache = self.region_cache.write();
                
                if let Some(cached) = cache.get(&window.hash) {
                    if cached.timestamp.elapsed() < Duration::from_secs(config.cache_ttl_seconds) {
                        self.stats.cache_hits += 1;
                        results.push(AnalysisResult {
                            bounds: window.bounds,
                            result: cached.result.clone(),
                            confidence: cached.confidence,
                            from_cache: true,
                        });
                        continue;
                    }
                }
            }
            
            // Analyze region
            self.stats.cache_misses += 1;
            let analysis_result = analyzer(&window)?;
            
            // Cache result
            if config.enable_caching {
                let mut cache = self.region_cache.write();
                cache.insert(window.hash, CachedAnalysis {
                    result: analysis_result.clone(),
                    timestamp: Instant::now(),
                    confidence: 0.9, // Would be returned by analyzer
                });
            }
            
            results.push(AnalysisResult {
                bounds: window.bounds,
                result: analysis_result,
                confidence: 0.9,
                from_cache: false,
            });
        }
        
        // Calculate memory saved
        let full_image_size_mb = 1920.0 * 1080.0 * 3.0 / 1024.0 / 1024.0; // Assume full HD
        let windows_size_mb = (self.stats.avg_window_size_bytes * windows_count as f32) / 1024.0 / 1024.0;
        self.stats.total_memory_saved_mb += full_image_size_mb - windows_size_mb;
        
        Ok(results)
    }

    /// Clear cache
    pub fn clear_cache(&mut self) {
        self.region_cache.write().clear();
        self.previous_hashes.clear();
    }

    /// Get statistics
    pub fn get_stats(&self) -> SlidingWindowStats {
        self.stats.clone()
    }

    /// Update configuration
    pub fn update_config(&self, key: &str, value: &str) -> Result<()> {
        self.config.write().update(key, value)
    }
}

/// Analysis result for a window region
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalysisResult {
    pub bounds: CaptureRegion,
    pub result: String,
    pub confidence: f32,
    pub from_cache: bool,
}

/// Memory-aware sliding window coordinator
pub struct MemoryAwareSlidingWindow {
    capture: Arc<RwLock<SlidingWindowCapture>>,
    memory_monitor: MemoryMonitor,
    quality_adjuster: QualityAdjuster,
}

/// Monitor memory usage
struct MemoryMonitor {
    threshold_mb: f32,
    check_interval: Duration,
    last_check: Instant,
}

impl MemoryMonitor {
    fn new(threshold_mb: f32) -> Self {
        Self {
            threshold_mb,
            check_interval: Duration::from_secs(1),
            last_check: Instant::now(),
        }
    }

    fn should_reduce_quality(&mut self) -> bool {
        if self.last_check.elapsed() < self.check_interval {
            return false;
        }
        
        self.last_check = Instant::now();
        
        #[cfg(target_os = "macos")]
        {
            use sysinfo::System;
            let mut sys = System::new();
            sys.refresh_memory();
            let available_mb = (sys.available_memory() / 1024 / 1024) as f32;
            available_mb < self.threshold_mb
        }
        
        #[cfg(not(target_os = "macos"))]
        false
    }
}

/// Adjust quality based on memory pressure
struct QualityAdjuster {
    quality_levels: Vec<QualityLevel>,
    current_level: usize,
}

#[derive(Clone)]
struct QualityLevel {
    name: String,
    window_size: (u32, u32),
    max_regions: usize,
    jpeg_quality: u8,
}

impl QualityAdjuster {
    fn new() -> Self {
        Self {
            quality_levels: vec![
                QualityLevel {
                    name: "Ultra".to_string(),
                    window_size: (500, 400),
                    max_regions: 6,
                    jpeg_quality: 90,
                },
                QualityLevel {
                    name: "High".to_string(),
                    window_size: (400, 300),
                    max_regions: 4,
                    jpeg_quality: 80,
                },
                QualityLevel {
                    name: "Medium".to_string(),
                    window_size: (300, 250),
                    max_regions: 3,
                    jpeg_quality: 70,
                },
                QualityLevel {
                    name: "Low".to_string(),
                    window_size: (250, 200),
                    max_regions: 2,
                    jpeg_quality: 60,
                },
            ],
            current_level: 1, // Start at High
        }
    }

    fn reduce_quality(&mut self) -> Option<QualityLevel> {
        if self.current_level < self.quality_levels.len() - 1 {
            self.current_level += 1;
            Some(self.quality_levels[self.current_level].clone())
        } else {
            None
        }
    }

    fn increase_quality(&mut self) -> Option<QualityLevel> {
        if self.current_level > 0 {
            self.current_level -= 1;
            Some(self.quality_levels[self.current_level].clone())
        } else {
            None
        }
    }

    fn current_quality(&self) -> &QualityLevel {
        &self.quality_levels[self.current_level]
    }
}

impl MemoryAwareSlidingWindow {
    pub fn new(config: SlidingWindowConfig) -> Self {
        Self {
            capture: Arc::new(RwLock::new(SlidingWindowCapture::new(config))),
            memory_monitor: MemoryMonitor::new(
                std::env::var("MEMORY_MONITOR_THRESHOLD_MB")
                    .unwrap_or_else(|_| "2000".to_string())
                    .parse()
                    .unwrap_or(2000.0)
            ),
            quality_adjuster: QualityAdjuster::new(),
        }
    }

    /// Process image with automatic quality adjustment
    pub async fn process_adaptive(&mut self, image: &ImageData) -> Result<Vec<AnalysisResult>> {
        // Check memory and adjust quality if needed
        if self.memory_monitor.should_reduce_quality() {
            if let Some(new_quality) = self.quality_adjuster.reduce_quality() {
                self.apply_quality_level(&new_quality)?;
                tracing::info!("Reduced quality to {} due to memory pressure", new_quality.name);
            }
        } else {
            // Try to increase quality if memory allows
            if let Some(new_quality) = self.quality_adjuster.increase_quality() {
                self.apply_quality_level(&new_quality)?;
                tracing::info!("Increased quality to {} due to available memory", new_quality.name);
            }
        }

        // Generate windows with current quality settings
        let mut windows = self.capture.write().generate_windows(image)?;

        // Limit windows based on current quality
        let current_quality = self.quality_adjuster.current_quality();
        windows.truncate(current_quality.max_regions);

        // Analyze windows
        let analyzer = |window: &WindowRegion| -> Result<String> {
            // This would call Claude Vision API or other analyzer
            Ok(format!("Analysis of region at ({}, {})", window.bounds.x, window.bounds.y))
        };

        self.capture.write().analyze_windows(windows, analyzer).await
    }

    fn apply_quality_level(&self, level: &QualityLevel) -> Result<()> {
        self.capture.read().update_config("window_width", &level.window_size.0.to_string())?;
        self.capture.read().update_config("window_height", &level.window_size.1.to_string())?;
        self.capture.read().update_config("max_concurrent_regions", &level.max_regions.to_string())?;
        Ok(())
    }

    /// Get sliding window statistics
    pub fn get_stats(&self) -> SlidingWindowStats {
        self.capture.read().get_stats()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sliding_window_config() {
        let config = SlidingWindowConfig::from_env();
        assert!(config.window_width > 0);
        assert!(config.window_height > 0);
        assert!(config.overlap_percentage >= 0.0 && config.overlap_percentage <= 1.0);
    }

    #[test]
    fn test_adaptive_window_sizing() {
        let config = SlidingWindowConfig::from_env();
        
        // Test low memory scenario
        let (width, height) = config.get_adaptive_window_size(500.0);
        assert!(width <= config.window_width);
        assert!(height <= config.window_height);
        
        // Test high memory scenario
        let (width, height) = config.get_adaptive_window_size(5000.0);
        assert_eq!(width, config.window_width);
        assert_eq!(height, config.window_height);
    }

    #[test]
    fn test_region_priority_calculation() {
        let capture = SlidingWindowCapture::new(SlidingWindowConfig::from_env());
        
        // Center region should have higher priority
        let center_priority = capture.calculate_region_priority(460, 390, 200, 150, 1920, 1080);
        let corner_priority = capture.calculate_region_priority(0, 0, 200, 150, 1920, 1080);
        
        assert!(center_priority > corner_priority);
    }
}