//! Enhanced dynamic image compression with multiple algorithms and auto-selection
//! No hardcoded values - fully configurable compression pipeline

use super::{ImageData, ImageFormat};
use crate::{Result, JarvisError};
use std::collections::HashMap;
use std::sync::Arc;
use parking_lot::RwLock;
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};

// Import compression libraries
use lz4;
use zstd;
use brotli::{CompressorWriter, CompressorReader};
use snap;

/// Global compression configuration
static COMPRESSION_CONFIG: Lazy<RwLock<CompressionConfig>> = 
    Lazy::new(|| RwLock::new(CompressionConfig::from_env()));

/// Dynamic compression configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompressionConfig {
    pub default_format: CompressionFormat,
    pub lz4_acceleration: i32,
    pub zstd_level: i32,
    pub brotli_quality: u32,
    pub brotli_window_size: u32,
    pub auto_select_threshold_kb: usize,
    pub enable_parallel: bool,
    pub max_threads: usize,
    pub adaptive_compression: bool,
    pub target_ratio: f32,
    pub max_compression_time_ms: u64,
}

impl CompressionConfig {
    /// Load from environment variables
    pub fn from_env() -> Self {
        Self {
            default_format: std::env::var("RUST_COMPRESSION_FORMAT")
                .unwrap_or_else(|_| "auto".to_string())
                .parse()
                .unwrap_or(CompressionFormat::Auto),
            lz4_acceleration: std::env::var("RUST_LZ4_ACCELERATION")
                .unwrap_or_else(|_| "1".to_string())
                .parse()
                .unwrap_or(1),
            zstd_level: std::env::var("RUST_ZSTD_LEVEL")
                .unwrap_or_else(|_| "3".to_string())
                .parse()
                .unwrap_or(3),
            brotli_quality: std::env::var("RUST_BROTLI_QUALITY")
                .unwrap_or_else(|_| "6".to_string())
                .parse()
                .unwrap_or(6),
            brotli_window_size: std::env::var("RUST_BROTLI_WINDOW")
                .unwrap_or_else(|_| "22".to_string())
                .parse()
                .unwrap_or(22),
            auto_select_threshold_kb: std::env::var("RUST_AUTO_THRESHOLD_KB")
                .unwrap_or_else(|_| "1024".to_string())
                .parse()
                .unwrap_or(1024),
            enable_parallel: std::env::var("RUST_PARALLEL_COMPRESSION")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            max_threads: std::env::var("RUST_COMPRESSION_THREADS")
                .unwrap_or_else(|_| "4".to_string())
                .parse()
                .unwrap_or(4),
            adaptive_compression: std::env::var("RUST_ADAPTIVE_COMPRESSION")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            target_ratio: std::env::var("RUST_TARGET_RATIO")
                .unwrap_or_else(|_| "0.5".to_string())
                .parse()
                .unwrap_or(0.5),
            max_compression_time_ms: std::env::var("RUST_MAX_COMPRESSION_MS")
                .unwrap_or_else(|_| "100".to_string())
                .parse()
                .unwrap_or(100),
        }
    }

    /// Update configuration dynamically
    pub fn update(&mut self, key: &str, value: &str) -> Result<()> {
        match key {
            "default_format" => self.default_format = value.parse()
                .map_err(|e: String| JarvisError::InvalidOperation(e))?,
            "lz4_acceleration" => self.lz4_acceleration = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid LZ4 acceleration".to_string()))?,
            "zstd_level" => self.zstd_level = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid ZSTD level".to_string()))?,
            "brotli_quality" => self.brotli_quality = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid Brotli quality".to_string()))?,
            "enable_parallel" => self.enable_parallel = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid boolean".to_string()))?,
            _ => return Err(JarvisError::InvalidOperation(format!("Unknown config key: {}", key))),
        }
        Ok(())
    }
}

/// Compression format with dynamic selection
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum CompressionFormat {
    None,
    Lz4,
    Zstd(i32),
    Brotli(u32),
    Snappy,
    Auto, // Automatically select best format
}

impl std::str::FromStr for CompressionFormat {
    type Err = String;
    
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "none" => Ok(CompressionFormat::None),
            "lz4" => Ok(CompressionFormat::Lz4),
            "zstd" => Ok(CompressionFormat::Zstd(3)), // Default level
            "brotli" => Ok(CompressionFormat::Brotli(6)), // Default quality
            "snappy" => Ok(CompressionFormat::Snappy),
            "auto" => Ok(CompressionFormat::Auto),
            _ => {
                // Try to parse format with level, e.g., "zstd:5" or "brotli:9"
                if let Some((format, level)) = s.split_once(':') {
                    match format {
                        "zstd" => {
                            let level = level.parse().map_err(|_| "Invalid ZSTD level")?;
                            Ok(CompressionFormat::Zstd(level))
                        }
                        "brotli" => {
                            let quality = level.parse().map_err(|_| "Invalid Brotli quality")?;
                            Ok(CompressionFormat::Brotli(quality))
                        }
                        _ => Err(format!("Unknown compression format: {}", s)),
                    }
                } else {
                    Err(format!("Unknown compression format: {}", s))
                }
            }
        }
    }
}

/// Enhanced image compressor with adaptive algorithms
pub struct ImageCompressor {
    config: Arc<RwLock<CompressionConfig>>,
    work_buffers: Vec<Vec<u8>>,
    stats: CompressionStats,
    algorithm_performance: HashMap<String, AlgorithmPerformance>,
}

#[derive(Debug, Clone, Default)]
pub struct CompressionStats {
    pub total_compressions: u64,
    pub total_decompressions: u64,
    pub total_bytes_processed: u64,
    pub total_bytes_compressed: u64,
    pub avg_compression_time_ms: f64,
    pub avg_decompression_time_ms: f64,
}

#[derive(Debug, Clone, Default)]
pub struct AlgorithmPerformance {
    pub uses: u64,
    pub avg_ratio: f32,
    pub avg_time_ms: f64,
    pub success_rate: f32,
}

impl ImageCompressor {
    pub fn new() -> Self {
        let config = Arc::new(RwLock::new(CompressionConfig::from_env()));
        let num_buffers = config.read().max_threads;
        
        Self {
            config: config.clone(),
            work_buffers: (0..num_buffers)
                .map(|_| Vec::with_capacity(1024 * 1024))
                .collect(),
            stats: CompressionStats::default(),
            algorithm_performance: HashMap::new(),
        }
    }
    
    /// Update global configuration
    pub fn update_config(&self, key: &str, value: &str) -> Result<()> {
        self.config.write().update(key, value)?;
        COMPRESSION_CONFIG.write().update(key, value)?;
        Ok(())
    }
    
    /// Compress image with specified or auto-selected format
    pub fn compress(&mut self, image: &ImageData, format: Option<CompressionFormat>) -> Result<CompressedImage> {
        let start_time = std::time::Instant::now();
        let config = self.config.read().clone();
        
        // Determine compression format
        let format = format.unwrap_or(config.default_format);
        let format = match format {
            CompressionFormat::Auto => self.auto_select_format(image, &config)?,
            _ => format,
        };
        
        // Get a work buffer (use local buffer to avoid borrow conflicts)
        let mut work_buffer = Vec::with_capacity(image.data.len());

        // Compress based on format
        let compressed_data = match format {
            CompressionFormat::None => image.data.clone(),
            CompressionFormat::Lz4 => self.compress_lz4(&image.data, &mut work_buffer, config.lz4_acceleration)?,
            CompressionFormat::Zstd(level) => self.compress_zstd(&image.data, level)?,
            CompressionFormat::Brotli(quality) => self.compress_brotli(&image.data, quality, config.brotli_window_size)?,
            CompressionFormat::Snappy => self.compress_snappy(&image.data)?,
            _ => unreachable!(),
        };
        
        // Update statistics
        let elapsed = start_time.elapsed();
        self.update_stats(
            &format_name(&format),
            image.data.len(),
            compressed_data.len(),
            elapsed.as_millis() as f64,
        );
        
        Ok(CompressedImage {
            width: image.width,
            height: image.height,
            channels: image.channels,
            format: image.format,
            compression: format,
            compressed_data,
            original_size: image.data.len(),
            compression_time_ms: elapsed.as_millis() as f32,
            metadata: self.create_metadata(&format),
        })
    }
    
    /// Decompress image
    pub fn decompress(&mut self, compressed: &CompressedImage) -> Result<ImageData> {
        let start_time = std::time::Instant::now();
        
        let decompressed_data = match compressed.compression {
            CompressionFormat::None => compressed.compressed_data.clone(),
            CompressionFormat::Lz4 => self.decompress_lz4(
                &compressed.compressed_data,
                compressed.original_size,
            )?,
            CompressionFormat::Zstd(_) => self.decompress_zstd(&compressed.compressed_data)?,
            CompressionFormat::Brotli(_) => self.decompress_brotli(&compressed.compressed_data)?,
            CompressionFormat::Snappy => self.decompress_snappy(&compressed.compressed_data)?,
            _ => unreachable!(),
        };
        
        let elapsed = start_time.elapsed();
        self.stats.total_decompressions += 1;
        self.stats.avg_decompression_time_ms = 
            (self.stats.avg_decompression_time_ms * (self.stats.total_decompressions - 1) as f64 
             + elapsed.as_millis() as f64) / self.stats.total_decompressions as f64;
        
        ImageData::from_raw(
            compressed.width,
            compressed.height,
            decompressed_data,
            compressed.format,
        )
    }
    
    /// Auto-select best compression format based on image characteristics
    fn auto_select_format(&self, image: &ImageData, config: &CompressionConfig) -> Result<CompressionFormat> {
        let size_kb = image.data.len() / 1024;
        
        // Analyze image characteristics
        let entropy = self.estimate_entropy(&image.data);
        let is_high_frequency = self.has_high_frequency_content(image);
        
        // Select based on heuristics and past performance
        if !config.adaptive_compression {
            // Simple heuristic
            if size_kb < 100 {
                Ok(CompressionFormat::Lz4) // Fast for small images
            } else if entropy < 0.5 {
                Ok(CompressionFormat::Zstd(5)) // Good for low entropy
            } else {
                Ok(CompressionFormat::Zstd(3)) // Balanced
            }
        } else {
            // Use adaptive selection based on past performance
            self.adaptive_select_format(image, config)
        }
    }
    
    /// Adaptive format selection based on historical performance
    fn adaptive_select_format(&self, image: &ImageData, config: &CompressionConfig) -> Result<CompressionFormat> {
        // If we have performance data, use it
        if !self.algorithm_performance.is_empty() {
            let mut best_score = 0.0;
            let mut best_format = CompressionFormat::Lz4;
            
            for (name, perf) in &self.algorithm_performance {
                // Score based on compression ratio and time
                let time_factor = 1.0 - (perf.avg_time_ms / config.max_compression_time_ms as f64).min(1.0);
                let ratio_factor = perf.avg_ratio / config.target_ratio;
                let score = time_factor * 0.4 + (ratio_factor * 0.6) as f64;
                
                if score > best_score {
                    best_score = score;
                    best_format = match name.as_str() {
                        "lz4" => CompressionFormat::Lz4,
                        "zstd" => CompressionFormat::Zstd(config.zstd_level),
                        "brotli" => CompressionFormat::Brotli(config.brotli_quality),
                        "snappy" => CompressionFormat::Snappy,
                        _ => CompressionFormat::Lz4,
                    };
                }
            }
            
            Ok(best_format)
        } else {
            // Fallback to simple heuristic
            Ok(CompressionFormat::Lz4)
        }
    }
    
    /// Estimate data entropy for compression selection
    fn estimate_entropy(&self, data: &[u8]) -> f32 {
        if data.is_empty() {
            return 0.0;
        }
        
        // Sample the data for performance
        let sample_size = data.len().min(1024);
        let mut histogram = [0u32; 256];
        
        for &byte in &data[..sample_size] {
            histogram[byte as usize] += 1;
        }
        
        let mut entropy = 0.0;
        let len = sample_size as f32;
        
        for &count in &histogram {
            if count > 0 {
                let p = count as f32 / len;
                entropy -= p * p.log2();
            }
        }
        
        entropy / 8.0 // Normalize to 0-1
    }
    
    /// Check if image has high frequency content
    fn has_high_frequency_content(&self, image: &ImageData) -> bool {
        // Simple heuristic: check variance in pixel values
        if image.data.len() < 100 {
            return false;
        }
        
        let sample_size = 100.min(image.data.len());
        let mut sum = 0u64;
        let mut sum_sq = 0u64;
        
        for i in 0..sample_size {
            let val = image.data[i] as u64;
            sum += val;
            sum_sq += val * val;
        }
        
        let mean = sum as f64 / sample_size as f64;
        let variance = (sum_sq as f64 / sample_size as f64) - (mean * mean);
        
        variance > 1000.0 // High variance indicates high frequency content
    }
    
    // Compression implementations
    
    fn compress_lz4(&self, data: &[u8], work_buffer: &mut Vec<u8>, acceleration: i32) -> Result<Vec<u8>> {
        let max_size = lz4::block::compress_bound(data.len())?;
        work_buffer.resize(max_size, 0);
        
        let compressed_size = lz4::block::compress_to_buffer(
            data,
            None,
            false,
            work_buffer,
        )?;
        
        Ok(work_buffer[..compressed_size].to_vec())
    }
    
    fn decompress_lz4(&self, compressed: &[u8], original_size: usize) -> Result<Vec<u8>> {
        let mut decompressed = vec![0u8; original_size];
        
        let size = lz4::block::decompress_to_buffer(compressed, None, &mut decompressed)?;
        
        if size != original_size {
            return Err(JarvisError::VisionError("LZ4 decompression size mismatch".to_string()));
        }
        
        Ok(decompressed)
    }
    
    fn compress_zstd(&self, data: &[u8], level: i32) -> Result<Vec<u8>> {
        zstd::encode_all(std::io::Cursor::new(data), level)
            .map_err(|e| JarvisError::VisionError(format!("ZSTD compression failed: {}", e)))
    }
    
    fn decompress_zstd(&self, compressed: &[u8]) -> Result<Vec<u8>> {
        zstd::decode_all(std::io::Cursor::new(compressed))
            .map_err(|e| JarvisError::VisionError(format!("ZSTD decompression failed: {}", e)))
    }
    
    fn compress_brotli(&self, data: &[u8], quality: u32, window_size: u32) -> Result<Vec<u8>> {
        let mut compressed = Vec::new();
        let mut compressor = CompressorWriter::new(
            &mut compressed,
            4096, // Buffer size
            quality,
            window_size,
        );
        
        std::io::Write::write_all(&mut compressor, data)
            .map_err(|e| JarvisError::VisionError(format!("Brotli compression failed: {}", e)))?;
        
        drop(compressor); // Ensure flush
        Ok(compressed)
    }
    
    fn decompress_brotli(&self, compressed: &[u8]) -> Result<Vec<u8>> {
        let mut decompressed = Vec::new();
        let mut decoder = brotli::Decompressor::new(std::io::Cursor::new(compressed), 4096);
        
        std::io::Read::read_to_end(&mut decoder, &mut decompressed)
            .map_err(|e| JarvisError::VisionError(format!("Brotli decompression failed: {}", e)))?;
        
        Ok(decompressed)
    }
    
    fn compress_snappy(&self, data: &[u8]) -> Result<Vec<u8>> {
        let mut encoder = snap::raw::Encoder::new();
        encoder.compress_vec(data)
            .map_err(|e| JarvisError::VisionError(format!("Snappy compression failed: {}", e)))
    }
    
    fn decompress_snappy(&self, compressed: &[u8]) -> Result<Vec<u8>> {
        let mut decoder = snap::raw::Decoder::new();
        decoder.decompress_vec(compressed)
            .map_err(|e| JarvisError::VisionError(format!("Snappy decompression failed: {}", e)))
    }
    
    /// Update performance statistics
    fn update_stats(&mut self, algorithm: &str, original_size: usize, compressed_size: usize, time_ms: f64) {
        self.stats.total_compressions += 1;
        self.stats.total_bytes_processed += original_size as u64;
        self.stats.total_bytes_compressed += compressed_size as u64;
        self.stats.avg_compression_time_ms = 
            (self.stats.avg_compression_time_ms * (self.stats.total_compressions - 1) as f64 + time_ms) 
            / self.stats.total_compressions as f64;
        
        // Update algorithm-specific stats
        let perf = self.algorithm_performance.entry(algorithm.to_string())
            .or_insert_with(AlgorithmPerformance::default);
        
        perf.uses += 1;
        let ratio = compressed_size as f32 / original_size as f32;
        perf.avg_ratio = (perf.avg_ratio * (perf.uses - 1) as f32 + ratio) / perf.uses as f32;
        perf.avg_time_ms = (perf.avg_time_ms * (perf.uses - 1) as f64 + time_ms) / perf.uses as f64;
        perf.success_rate = 1.0; // Update on errors
    }
    
    /// Create metadata for compressed image
    fn create_metadata(&self, format: &CompressionFormat) -> HashMap<String, String> {
        let mut metadata = HashMap::new();
        metadata.insert("algorithm".to_string(), format_name(format));
        metadata.insert("timestamp".to_string(), chrono::Utc::now().to_rfc3339());
        metadata.insert("version".to_string(), "2.0".to_string());
        
        if let Some(perf) = self.algorithm_performance.get(&format_name(format)) {
            metadata.insert("avg_ratio".to_string(), perf.avg_ratio.to_string());
            metadata.insert("avg_time_ms".to_string(), perf.avg_time_ms.to_string());
        }
        
        metadata
    }
    
    /// Get compression statistics
    pub fn get_stats(&self) -> CompressionStats {
        self.stats.clone()
    }
    
    /// Get per-algorithm performance data
    pub fn get_algorithm_performance(&self) -> HashMap<String, AlgorithmPerformance> {
        self.algorithm_performance.clone()
    }
    
    /// Parallel compression for large images
    pub fn compress_parallel(&mut self, image: &ImageData, format: CompressionFormat) -> Result<CompressedImage> {
        let (enable_parallel, max_threads) = {
            let config = self.config.read();
            (config.enable_parallel, config.max_threads)
        };

        if !enable_parallel || image.data.len() < 1024 * 1024 {
            // Fall back to single-threaded for small images
            return self.compress(image, Some(format));
        }

        // Split image into chunks for parallel processing
        let chunk_size = image.data.len() / max_threads;
        let chunks: Vec<&[u8]> = image.data.chunks(chunk_size).collect();
        
        use rayon::prelude::*;
        
        let compressed_chunks: Result<Vec<Vec<u8>>> = chunks
            .par_iter()
            .map(|chunk| {
                // Each thread compresses its chunk
                match format {
                    CompressionFormat::Lz4 => {
                        let mut buffer = vec![0u8; lz4::block::compress_bound(chunk.len())?];
                        let size = lz4::block::compress_to_buffer(chunk, None, false, &mut buffer)?;
                        Ok(buffer[..size].to_vec())
                    }
                    CompressionFormat::Zstd(level) => {
                        zstd::encode_all(std::io::Cursor::new(chunk), level)
                            .map_err(|e| JarvisError::VisionError(e.to_string()))
                    }
                    _ => Err(JarvisError::VisionError("Parallel compression not supported for this format".to_string())),
                }
            })
            .collect();
        
        let compressed_chunks = compressed_chunks?;
        
        // Combine compressed chunks
        let total_size: usize = compressed_chunks.iter().map(|c| c.len()).sum();
        let mut combined = Vec::with_capacity(total_size + chunks.len() * 4);
        
        // Add metadata about chunks
        combined.extend_from_slice(&(chunks.len() as u32).to_le_bytes());
        
        for chunk in compressed_chunks {
            combined.extend_from_slice(&(chunk.len() as u32).to_le_bytes());
            combined.extend_from_slice(&chunk);
        }
        
        Ok(CompressedImage {
            width: image.width,
            height: image.height,
            channels: image.channels,
            format: image.format,
            compression: format,
            compressed_data: combined,
            original_size: image.data.len(),
            compression_time_ms: 0.0, // TODO: measure
            metadata: self.create_metadata(&format),
        })
    }
}

/// Enhanced compressed image with metadata
#[derive(Clone)]
pub struct CompressedImage {
    pub width: u32,
    pub height: u32,
    pub channels: u8,
    pub format: ImageFormat,
    pub compression: CompressionFormat,
    pub compressed_data: Vec<u8>,
    pub original_size: usize,
    pub compression_time_ms: f32,
    pub metadata: HashMap<String, String>,
}

impl CompressedImage {
    /// Get compression ratio
    pub fn compression_ratio(&self) -> f32 {
        self.original_size as f32 / self.compressed_data.len() as f32
    }
    
    /// Get size reduction percentage
    pub fn size_reduction(&self) -> f32 {
        (1.0 - self.compressed_data.len() as f32 / self.original_size as f32) * 100.0
    }
    
    /// Get effective compression speed (MB/s)
    pub fn compression_speed_mbps(&self) -> f32 {
        if self.compression_time_ms > 0.0 {
            (self.original_size as f32 / 1024.0 / 1024.0) / (self.compression_time_ms / 1000.0)
        } else {
            0.0
        }
    }
    
    /// Export metadata as JSON
    pub fn metadata_json(&self) -> String {
        serde_json::to_string(&self.metadata).unwrap_or_default()
    }
}

/// Get human-readable format name
fn format_name(format: &CompressionFormat) -> String {
    match format {
        CompressionFormat::None => "none".to_string(),
        CompressionFormat::Lz4 => "lz4".to_string(),
        CompressionFormat::Zstd(level) => format!("zstd:{}", level),
        CompressionFormat::Brotli(quality) => format!("brotli:{}", quality),
        CompressionFormat::Snappy => "snappy".to_string(),
        CompressionFormat::Auto => "auto".to_string(),
    }
}

/// Global compression settings
pub fn update_global_config(key: &str, value: &str) -> Result<()> {
    COMPRESSION_CONFIG.write().update(key, value)
}

/// Get current global configuration
pub fn get_global_config() -> CompressionConfig {
    COMPRESSION_CONFIG.read().clone()
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_dynamic_compression() {
        let mut compressor = ImageCompressor::new();
        
        // Create test image
        let mut image = ImageData::new(100, 100, 3, ImageFormat::Rgb8);
        for i in 0..image.data.len() {
            image.data[i] = (i % 256) as u8;
        }
        
        // Test auto-selection
        let compressed = compressor.compress(&image, Some(CompressionFormat::Auto)).unwrap();
        println!("Auto-selected format: {}", format_name(&compressed.compression));
        println!("Compression ratio: {:.2}x", compressed.compression_ratio());
        println!("Size reduction: {:.1}%", compressed.size_reduction());
        println!("Speed: {:.1} MB/s", compressed.compression_speed_mbps());
        
        // Test decompression
        let decompressed = compressor.decompress(&compressed).unwrap();
        assert_eq!(decompressed.data, image.data);
    }
    
    #[test]
    fn test_config_from_env() {
        std::env::set_var("RUST_COMPRESSION_FORMAT", "zstd:7");
        std::env::set_var("RUST_PARALLEL_COMPRESSION", "true");
        
        let config = CompressionConfig::from_env();
        match config.default_format {
            CompressionFormat::Zstd(level) => assert_eq!(level, 7),
            _ => panic!("Expected ZSTD format"),
        }
        assert!(config.enable_parallel);
    }
    
    #[test]
    fn test_adaptive_compression() {
        let mut compressor = ImageCompressor::new();
        
        // Update config for adaptive mode
        compressor.update_config("adaptive_compression", "true").unwrap();
        
        // Compress multiple times to build performance data
        let mut image = ImageData::new(200, 200, 4, ImageFormat::Rgba8);
        for _ in 0..5 {
            // Vary the image content
            for i in 0..image.data.len() {
                image.data[i] = rand::random();
            }
            
            let _ = compressor.compress(&image, Some(CompressionFormat::Auto)).unwrap();
        }
        
        // Check that we have performance data
        let perf = compressor.get_algorithm_performance();
        assert!(!perf.is_empty());
        
        println!("Algorithm performance:");
        for (name, stats) in perf {
            println!("  {}: ratio={:.2}, time={:.1}ms, uses={}",
                name, stats.avg_ratio, stats.avg_time_ms, stats.uses);
        }
    }
}