//! Enhanced vision processing module with dynamic configuration
//! Zero-copy operations and comprehensive format support

pub mod capture;  // Thread-safe implementation
pub mod compression;
pub mod processing;
pub mod sliding_window;
pub mod sliding_window_bindings;
pub mod goal_patterns;
pub mod pattern_mining;
pub mod anomaly_detection;
pub mod intervention_engine;
pub mod solution_matching;
pub mod spatial_quadtree;
pub mod semantic_cache_lsh;
pub mod predictive_engine;
pub mod bloom_filter_network;
pub mod integration_pipeline;
pub mod bloom_filter;
pub mod metal_accelerator;  // Thread-safe implementation

#[cfg(target_os = "macos")]
pub mod macos_optimization;

use crate::{Result, JarvisError};
use crate::memory::{MemoryManager, ZeroCopyBuffer};
use image::{DynamicImage, ImageBuffer, Rgb, Rgba, Luma};
use std::sync::Arc;
use std::collections::HashMap;
use serde::{Deserialize, Serialize};
use once_cell::sync::Lazy;
use parking_lot::RwLock;

// Re-export capture types
pub use capture::{
    ScreenCapture, CaptureConfig, CaptureRegion, ColorSpace,
    PixelFormat, CompressionHint, CaptureQuality, CaptureStats,
    SharedMemoryHandle, WindowInfo, AppInfo, TextDetection, BridgeMetrics
};
pub use processing::{
    ImageProcessor, ProcessingPipeline, ProcessingConfig,
    QualityPreset, ColorCorrectionMode, ResizeAlgorithm,
    ProcessingStats, ProcessingOperation
};
pub use compression::{
    ImageCompressor, CompressionFormat, CompressedImage,
    CompressionConfig, CompressionStats, AlgorithmPerformance
};

#[cfg(target_os = "macos")]
pub use macos_optimization::{
    WindowTracker, WindowPosition, AppStateDetector, AppState, AppStateChange,
    ChunkedTextExtractor, TextChunk, NotificationMonitor, NotificationEvent,
    WorkspaceOrganizer, WorkspaceRule, RuleCondition, RuleAction, WindowLayout
};

pub use sliding_window::{
    SlidingWindowConfig, SlidingWindowCapture, WindowRegion, AnalysisResult,
    MemoryAwareSlidingWindow, SlidingWindowStats, CachedAnalysis
};

pub use spatial_quadtree::{
    QuadNode, SpatialQuadtree, QuadtreeStats, RegionBatchProcessor,
    ImportanceCalculator
};

pub use semantic_cache_lsh::{
    SemanticCacheLSH, CacheEntry, LSHIndex, SimilarityComputer,
    CachePredictor, PredictiveEntry
};

pub use predictive_engine::{
    StateVector, TransitionMatrix, PredictionTask, PredictionQueue,
    SimdStateMatcher, PredictiveEngine
};

pub use bloom_filter_network::{
    BloomFilterLevel, BloomFilterMetrics, AdaptiveBloomFilter, BloomFilterNetwork,
    get_global_bloom_network
};

pub use integration_pipeline::{
    IntegrationPipeline, SystemMode, Priority, MemoryAllocation, ProcessingResult,
    MemoryStatus, ComponentMemory
};

/// Global vision configuration
static VISION_CONFIG: Lazy<RwLock<VisionGlobalConfig>> = 
    Lazy::new(|| RwLock::new(VisionGlobalConfig::from_env()));

/// Global vision configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VisionGlobalConfig {
    pub enable_hardware_acceleration: bool,
    pub default_format: ImageFormat,
    pub memory_pool_size_mb: usize,
    pub enable_profiling: bool,
    pub enable_debug_output: bool,
    pub max_concurrent_operations: usize,
    pub default_quality: QualityPreset,
}

impl VisionGlobalConfig {
    pub fn from_env() -> Self {
        Self {
            enable_hardware_acceleration: std::env::var("VISION_HW_ACCEL")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            default_format: std::env::var("VISION_DEFAULT_FORMAT")
                .unwrap_or_else(|_| "rgba8".to_string())
                .parse()
                .unwrap_or(ImageFormat::Rgba8),
            memory_pool_size_mb: std::env::var("VISION_MEMORY_POOL_MB")
                .unwrap_or_else(|_| "200".to_string())
                .parse()
                .unwrap_or(200),
            enable_profiling: std::env::var("VISION_PROFILING")
                .unwrap_or_else(|_| "false".to_string())
                .parse()
                .unwrap_or(false),
            enable_debug_output: std::env::var("VISION_DEBUG")
                .unwrap_or_else(|_| "false".to_string())
                .parse()
                .unwrap_or(false),
            max_concurrent_operations: std::env::var("VISION_MAX_CONCURRENT")
                .unwrap_or_else(|_| "4".to_string())
                .parse()
                .unwrap_or(4),
            default_quality: std::env::var("VISION_DEFAULT_QUALITY")
                .unwrap_or_else(|_| "balanced".to_string())
                .parse()
                .unwrap_or(QualityPreset::Balanced),
        }
    }
    
    pub fn update(&mut self, key: &str, value: &str) -> Result<()> {
        match key {
            "enable_hardware_acceleration" => self.enable_hardware_acceleration = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid boolean".to_string()))?,
            "default_format" => self.default_format = value.parse()
                .map_err(|e: String| JarvisError::InvalidOperation(e))?,
            "memory_pool_size_mb" => self.memory_pool_size_mb = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid size".to_string()))?,
            _ => return Err(JarvisError::InvalidOperation(format!("Unknown config key: {}", key))),
        }
        Ok(())
    }
}

/// Enhanced image data container with metadata and zero-copy support
#[derive(Clone)]
pub struct ImageData {
    pub width: u32,
    pub height: u32,
    pub channels: u8,
    pub data: Vec<u8>,
    pub format: ImageFormat,
    pub metadata: ImageMetadata,
    zero_copy_buffer: Option<ZeroCopyBuffer>,
}

/// Image metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageMetadata {
    pub timestamp: Option<std::time::SystemTime>,
    pub color_space: ColorSpace,
    pub bit_depth: u8,
    pub compression_hint: CompressionHint,
    pub dpi: Option<(f32, f32)>,
    pub icc_profile: Option<Vec<u8>>,
    pub exif: HashMap<String, String>,
    pub custom: HashMap<String, String>,
}

impl Default for ImageMetadata {
    fn default() -> Self {
        Self {
            timestamp: Some(std::time::SystemTime::now()),
            color_space: ColorSpace::Srgb,
            bit_depth: 8,
            compression_hint: CompressionHint::None,
            dpi: None,
            icc_profile: None,
            exif: HashMap::new(),
            custom: HashMap::new(),
        }
    }
}

/// Enhanced image formats with comprehensive support
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum ImageFormat {
    // 8-bit formats
    Gray8,
    GrayA8,
    Rgb8,
    Rgba8,
    Bgr8,
    Bgra8,
    // 16-bit formats
    Gray16,
    Rgb16,
    Rgba16,
    // Float formats
    GrayF32,
    RgbF32,
    RgbaF32,
    // Special formats
    YCbCr,
    Lab,
    Hsv,
}

impl std::str::FromStr for ImageFormat {
    type Err = String;
    
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "gray8" | "g8" => Ok(ImageFormat::Gray8),
            "graya8" | "ga8" => Ok(ImageFormat::GrayA8),
            "rgb8" | "rgb" => Ok(ImageFormat::Rgb8),
            "rgba8" | "rgba" => Ok(ImageFormat::Rgba8),
            "bgr8" | "bgr" => Ok(ImageFormat::Bgr8),
            "bgra8" | "bgra" => Ok(ImageFormat::Bgra8),
            "gray16" | "g16" => Ok(ImageFormat::Gray16),
            "rgb16" => Ok(ImageFormat::Rgb16),
            "rgba16" => Ok(ImageFormat::Rgba16),
            "grayf32" | "gf32" => Ok(ImageFormat::GrayF32),
            "rgbf32" => Ok(ImageFormat::RgbF32),
            "rgbaf32" => Ok(ImageFormat::RgbaF32),
            "ycbcr" | "yuv" => Ok(ImageFormat::YCbCr),
            "lab" => Ok(ImageFormat::Lab),
            "hsv" => Ok(ImageFormat::Hsv),
            _ => Err(format!("Unknown image format: {}", s)),
        }
    }
}

impl ImageData {
    /// Create new image data with default metadata
    pub fn new(width: u32, height: u32, channels: u8, format: ImageFormat) -> Self {
        let data_size = (width * height * channels as u32) as usize;
        Self {
            width,
            height,
            channels,
            data: vec![0; data_size],
            format,
            metadata: ImageMetadata::default(),
            zero_copy_buffer: None,
        }
    }
    
    /// Create with custom metadata
    pub fn with_metadata(width: u32, height: u32, channels: u8, format: ImageFormat, 
                        metadata: ImageMetadata) -> Self {
        let data_size = (width * height * channels as u32) as usize;
        Self {
            width,
            height,
            channels,
            data: vec![0; data_size],
            format,
            metadata,
            zero_copy_buffer: None,
        }
    }
    
    /// Create from zero-copy buffer
    pub fn from_zero_copy(width: u32, height: u32, format: ImageFormat, 
                         buffer: ZeroCopyBuffer) -> Result<Self> {
        let channels = format.channels();
        let expected_size = (width * height * channels as u32) as usize;
        
        unsafe {
            if buffer.as_slice().len() < expected_size {
                return Err(JarvisError::VisionError(
                    format!("Buffer too small: expected {}, got {}", 
                        expected_size, buffer.as_slice().len())
                ));
            }
        }
        
        Ok(Self {
            width,
            height,
            channels,
            data: Vec::new(), // No data copy
            format,
            metadata: ImageMetadata::default(),
            zero_copy_buffer: Some(buffer),
        })
    }
    
    /// Get data as slice (handles both owned and zero-copy)
    pub fn as_slice(&self) -> &[u8] {
        if let Some(ref buffer) = self.zero_copy_buffer {
            unsafe { buffer.as_slice() }
        } else {
            &self.data
        }
    }
    
    /// Get mutable data (forces owned copy if zero-copy)
    pub fn as_mut_slice(&mut self) -> &mut [u8] {
        if self.zero_copy_buffer.is_some() {
            // Convert zero-copy to owned
            let data = self.as_slice().to_vec();
            self.data = data;
            self.zero_copy_buffer = None;
        }
        &mut self.data
    }
    
    /// Create from raw data with validation
    pub fn from_raw(width: u32, height: u32, data: Vec<u8>, format: ImageFormat) -> Result<Self> {
        let expected_size = width * height * format.channels() as u32;
        if data.len() != expected_size as usize {
            return Err(JarvisError::VisionError(
                format!("Invalid data size: expected {}, got {}", expected_size, data.len())
            ));
        }
        
        Ok(Self {
            width,
            height,
            channels: format.channels(),
            data,
            format,
            metadata: ImageMetadata::default(),
            zero_copy_buffer: None,
        })
    }
    
    /// Create from raw data with metadata
    pub fn from_raw_with_metadata(width: u32, height: u32, data: Vec<u8>, 
                                  format: ImageFormat, metadata: ImageMetadata) -> Result<Self> {
        let expected_size = width * height * format.channels() as u32;
        if data.len() != expected_size as usize {
            return Err(JarvisError::VisionError(
                format!("Invalid data size: expected {}, got {}", expected_size, data.len())
            ));
        }
        
        Ok(Self {
            width,
            height,
            channels: format.channels(),
            data,
            format,
            metadata,
            zero_copy_buffer: None,
        })
    }
    
    /// Enhanced conversion to DynamicImage with full format support
    pub fn to_dynamic_image(&self) -> Result<DynamicImage> {
        let data = self.as_slice();
        
        match self.format {
            ImageFormat::Gray8 => {
                let img = ImageBuffer::<Luma<u8>, _>::from_raw(
                    self.width, self.height, data.to_vec()
                ).ok_or_else(|| JarvisError::VisionError("Failed to create gray image".to_string()))?;
                Ok(DynamicImage::ImageLuma8(img))
            }
            ImageFormat::Rgb8 => {
                let img = ImageBuffer::<Rgb<u8>, _>::from_raw(
                    self.width, self.height, data.to_vec()
                ).ok_or_else(|| JarvisError::VisionError("Failed to create RGB image".to_string()))?;
                Ok(DynamicImage::ImageRgb8(img))
            }
            ImageFormat::Rgba8 => {
                let img = ImageBuffer::<Rgba<u8>, _>::from_raw(
                    self.width, self.height, data.to_vec()
                ).ok_or_else(|| JarvisError::VisionError("Failed to create RGBA image".to_string()))?;
                Ok(DynamicImage::ImageRgba8(img))
            }
            _ => {
                // For unsupported formats, convert to a supported one
                let converted = self.convert_format(ImageFormat::Rgba8)?;
                converted.to_dynamic_image()
            }
        }
    }
    
    /// Convert between formats
    pub fn convert_format(&self, target_format: ImageFormat) -> Result<Self> {
        if self.format == target_format {
            return Ok(self.clone());
        }
        
        // Use the image processor for conversion
        let processor = ImageProcessor::new();
        processor.convert_format(self, target_format)
    }
    
    /// Get pixel at coordinates with bounds checking
    pub fn get_pixel(&self, x: u32, y: u32) -> Result<&[u8]> {
        if x >= self.width || y >= self.height {
            return Err(JarvisError::VisionError(
                format!("Pixel coordinates ({}, {}) out of bounds ({}x{})", 
                    x, y, self.width, self.height)
            ));
        }
        
        let offset = ((y * self.width + x) * self.channels as u32) as usize;
        let data = self.as_slice();
        Ok(&data[offset..offset + self.channels as usize])
    }
    
    /// Set pixel at coordinates with validation
    pub fn set_pixel(&mut self, x: u32, y: u32, pixel: &[u8]) -> Result<()> {
        if x >= self.width || y >= self.height {
            return Err(JarvisError::VisionError(
                format!("Pixel coordinates ({}, {}) out of bounds ({}x{})", 
                    x, y, self.width, self.height)
            ));
        }
        
        if pixel.len() != self.channels as usize {
            return Err(JarvisError::VisionError(
                format!("Invalid pixel data size: expected {}, got {}", 
                    self.channels, pixel.len())
            ));
        }
        
        let channels = self.channels as usize;
        let offset = ((y * self.width + x) * self.channels as u32) as usize;
        let data = self.as_mut_slice();
        data[offset..offset + channels].copy_from_slice(pixel);
        Ok(())
    }
    
    /// Get pixel value at specific channel
    pub fn get_pixel_channel(&self, x: u32, y: u32, channel: u8) -> Result<u8> {
        if channel >= self.channels {
            return Err(JarvisError::VisionError(
                format!("Channel {} out of range (0-{})", channel, self.channels - 1)
            ));
        }
        
        let pixel = self.get_pixel(x, y)?;
        Ok(pixel[channel as usize])
    }
    
    /// Set pixel value at specific channel  
    pub fn set_pixel_channel(&mut self, x: u32, y: u32, channel: u8, value: u8) -> Result<()> {
        if channel >= self.channels {
            return Err(JarvisError::VisionError(
                format!("Channel {} out of range (0-{})", channel, self.channels - 1)
            ));
        }
        
        let offset = ((y * self.width + x) * self.channels as u32 + channel as u32) as usize;
        let data = self.as_mut_slice();
        data[offset] = value;
        Ok(())
    }
    
    /// Clone with new metadata
    pub fn clone_with_metadata(&self, metadata: ImageMetadata) -> Self {
        let mut cloned = self.clone();
        cloned.metadata = metadata;
        cloned
    }
    
    /// Get image info as string
    pub fn info(&self) -> String {
        format!("{}x{} {:?} ({} channels, {} bytes)",
            self.width, self.height, self.format, self.channels,
            self.as_slice().len())
    }
}

impl ImageFormat {
    /// Get number of channels for format
    pub fn channels(&self) -> u8 {
        match self {
            ImageFormat::Gray8 | ImageFormat::Gray16 | ImageFormat::GrayF32 => 1,
            ImageFormat::GrayA8 => 2,
            ImageFormat::Rgb8 | ImageFormat::Bgr8 | ImageFormat::Rgb16 | 
            ImageFormat::RgbF32 | ImageFormat::YCbCr | ImageFormat::Lab | 
            ImageFormat::Hsv => 3,
            ImageFormat::Rgba8 | ImageFormat::Bgra8 | ImageFormat::Rgba16 | 
            ImageFormat::RgbaF32 => 4,
        }
    }
    
    /// Get bytes per channel
    pub fn bytes_per_channel(&self) -> u8 {
        match self {
            ImageFormat::Gray8 | ImageFormat::GrayA8 | ImageFormat::Rgb8 | 
            ImageFormat::Rgba8 | ImageFormat::Bgr8 | ImageFormat::Bgra8 => 1,
            ImageFormat::Gray16 | ImageFormat::Rgb16 | ImageFormat::Rgba16 => 2,
            ImageFormat::GrayF32 | ImageFormat::RgbF32 | ImageFormat::RgbaF32 => 4,
            ImageFormat::YCbCr | ImageFormat::Lab | ImageFormat::Hsv => 1,
        }
    }
    
    /// Total bytes per pixel
    pub fn bytes_per_pixel(&self) -> u8 {
        self.channels() * self.bytes_per_channel()
    }
    
    /// Check if format has alpha channel
    pub fn has_alpha(&self) -> bool {
        matches!(self, 
            ImageFormat::GrayA8 | ImageFormat::Rgba8 | 
            ImageFormat::Bgra8 | ImageFormat::Rgba16 | ImageFormat::RgbaF32
        )
    }
    
    /// Check if format is grayscale
    pub fn is_grayscale(&self) -> bool {
        matches!(self,
            ImageFormat::Gray8 | ImageFormat::GrayA8 | 
            ImageFormat::Gray16 | ImageFormat::GrayF32
        )
    }
    
    /// Check if format is BGR order
    pub fn is_bgr(&self) -> bool {
        matches!(self, ImageFormat::Bgr8 | ImageFormat::Bgra8)
    }
    
    /// Get compatible formats for conversion
    pub fn compatible_formats(&self) -> Vec<ImageFormat> {
        match self {
            ImageFormat::Gray8 => vec![
                ImageFormat::GrayA8, ImageFormat::Rgb8, ImageFormat::Rgba8,
                ImageFormat::Gray16, ImageFormat::GrayF32
            ],
            ImageFormat::Rgb8 => vec![
                ImageFormat::Rgba8, ImageFormat::Bgr8, ImageFormat::Bgra8,
                ImageFormat::Gray8, ImageFormat::Rgb16, ImageFormat::RgbF32,
                ImageFormat::Lab, ImageFormat::Hsv
            ],
            ImageFormat::Rgba8 => vec![
                ImageFormat::Rgb8, ImageFormat::Bgra8, ImageFormat::Rgba16,
                ImageFormat::RgbaF32
            ],
            _ => vec![], // Add more as needed
        }
    }
}

/// Enhanced vision processing context with dynamic configuration
pub struct VisionContext {
    pub capture: Arc<RwLock<ScreenCapture>>,
    pub processor: Arc<ImageProcessor>,
    pub compressor: Arc<RwLock<ImageCompressor>>,
    config: Arc<RwLock<VisionGlobalConfig>>,
    memory_manager: Arc<MemoryManager>,
    stats: Arc<RwLock<VisionStats>>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct VisionStats {
    pub total_captures: u64,
    pub total_processed: u64,
    pub total_compressed: u64,
    pub total_bytes_processed: u64,
    pub average_fps: f32,
    pub peak_memory_mb: f32,
}

impl VisionContext {
    /// Create new vision context with default configuration
    pub fn new() -> Result<Self> {
        let config = VisionGlobalConfig::from_env();
        Self::with_config(config)
    }
    
    /// Create with custom configuration
    pub fn with_config(config: VisionGlobalConfig) -> Result<Self> {
        let config = Arc::new(RwLock::new(config));
        let memory_manager = MemoryManager::global();
        
        Ok(Self {
            capture: Arc::new(RwLock::new(ScreenCapture::new(CaptureConfig::default())?)),
            processor: Arc::new(ImageProcessor::new()),
            compressor: Arc::new(RwLock::new(ImageCompressor::new())),
            config: config.clone(),
            memory_manager,
            stats: Arc::new(RwLock::new(VisionStats::default())),
        })
    }
    
    /// Update global configuration
    pub fn update_config(&self, key: &str, value: &str) -> Result<()> {
        self.config.write().update(key, value)?;
        VISION_CONFIG.write().update(key, value)?;
        Ok(())
    }
    
    /// Capture and process pipeline
    pub async fn capture_and_process(&self) -> Result<ImageData> {
        // Capture
        let mut capture = self.capture.write();
        let captured = capture.capture_preprocessed()?;
        self.stats.write().total_captures += 1;
        drop(capture);
        
        // Process based on configuration
        let config = self.config.read();
        let processed = if config.enable_hardware_acceleration {
            self.processor.auto_process(&captured)?
        } else {
            captured
        };
        self.stats.write().total_processed += 1;
        
        Ok(processed)
    }
    
    /// Full pipeline: capture, process, compress
    pub async fn full_pipeline(&self, compression: CompressionFormat) -> Result<CompressedImage> {
        let image = self.capture_and_process().await?;
        
        let mut compressor = self.compressor.write();
        let compressed = compressor.compress(&image, Some(compression))?;
        self.stats.write().total_compressed += 1;
        self.stats.write().total_bytes_processed += image.as_slice().len() as u64;
        
        Ok(compressed)
    }
    
    /// Create processing pipeline
    pub fn create_pipeline(&self) -> ProcessingPipeline {
        ProcessingPipeline::new(self.processor.clone())
    }
    
    /// Get current statistics
    pub fn get_stats(&self) -> VisionStats {
        self.stats.read().clone()
    }
    
    /// Reset statistics
    pub fn reset_stats(&self) {
        *self.stats.write() = VisionStats::default();
    }
}

/// Global configuration management
pub fn update_vision_config(key: &str, value: &str) -> Result<()> {
    VISION_CONFIG.write().update(key, value)
}

pub fn get_vision_config() -> VisionGlobalConfig {
    VISION_CONFIG.read().clone()
}

/// Initialize vision module
pub fn initialize() -> Result<()> {
    // Set up global configuration
    let config = VisionGlobalConfig::from_env();
    
    tracing::info!("Vision module initialized with config: {:?}", config);
    
    if config.enable_profiling {
        tracing::info!("Profiling enabled for vision operations");
    }
    
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_image_data() {
        let mut img = ImageData::new(100, 100, 3, ImageFormat::Rgb8);
        assert_eq!(img.data.len(), 30000);
        
        // Test pixel operations
        img.set_pixel(50, 50, &[255, 128, 0]).unwrap();
        let pixel = img.get_pixel(50, 50).unwrap();
        assert_eq!(pixel, &[255, 128, 0]);
    }
}