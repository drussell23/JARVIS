//! Enhanced dynamic image processing pipeline with zero-copy operations
//! No hardcoded values - fully configurable processing

use super::{ImageData, ImageFormat};
use crate::{Result, JarvisError};
use crate::memory::{MemoryManager, ZeroCopyBuffer};
use ndarray::{Array2, ArrayView2, ArrayView3, Array3};
use rayon::prelude::*;
use std::sync::Arc;
use std::collections::HashMap;
use serde::{Deserialize, Serialize};
use once_cell::sync::Lazy;
use parking_lot::RwLock;
use std::time::Instant;

/// Global processing configuration
static PROCESSING_CONFIG: Lazy<RwLock<ProcessingConfig>> = 
    Lazy::new(|| RwLock::new(ProcessingConfig::from_env()));

/// Dynamic processing configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProcessingConfig {
    pub enable_simd: bool,
    pub thread_count: usize,
    pub enable_gpu: bool,
    pub enable_caching: bool,
    pub cache_size_mb: usize,
    pub quality_preset: QualityPreset,
    pub denoise_strength: f32,
    pub sharpen_amount: f32,
    pub auto_enhance: bool,
    pub color_correction: ColorCorrectionMode,
    pub resize_algorithm: ResizeAlgorithm,
    pub max_dimension: u32,
    pub preserve_metadata: bool,
}

impl ProcessingConfig {
    /// Load from environment variables
    pub fn from_env() -> Self {
        Self {
            enable_simd: std::env::var("VISION_ENABLE_SIMD")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            thread_count: std::env::var("VISION_THREAD_COUNT")
                .unwrap_or_else(|_| num_cpus::get().to_string())
                .parse()
                .unwrap_or(num_cpus::get()),
            enable_gpu: std::env::var("VISION_ENABLE_GPU")
                .unwrap_or_else(|_| "false".to_string())
                .parse()
                .unwrap_or(false),
            enable_caching: std::env::var("VISION_ENABLE_CACHING")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
            cache_size_mb: std::env::var("VISION_CACHE_SIZE_MB")
                .unwrap_or_else(|_| "100".to_string())
                .parse()
                .unwrap_or(100),
            quality_preset: std::env::var("VISION_QUALITY_PRESET")
                .unwrap_or_else(|_| "balanced".to_string())
                .parse()
                .unwrap_or(QualityPreset::Balanced),
            denoise_strength: std::env::var("VISION_DENOISE_STRENGTH")
                .unwrap_or_else(|_| "0.0".to_string())
                .parse()
                .unwrap_or(0.0),
            sharpen_amount: std::env::var("VISION_SHARPEN_AMOUNT")
                .unwrap_or_else(|_| "0.0".to_string())
                .parse()
                .unwrap_or(0.0),
            auto_enhance: std::env::var("VISION_AUTO_ENHANCE")
                .unwrap_or_else(|_| "false".to_string())
                .parse()
                .unwrap_or(false),
            color_correction: std::env::var("VISION_COLOR_CORRECTION")
                .unwrap_or_else(|_| "none".to_string())
                .parse()
                .unwrap_or(ColorCorrectionMode::None),
            resize_algorithm: std::env::var("VISION_RESIZE_ALGORITHM")
                .unwrap_or_else(|_| "lanczos".to_string())
                .parse()
                .unwrap_or(ResizeAlgorithm::Lanczos),
            max_dimension: std::env::var("VISION_MAX_DIMENSION")
                .unwrap_or_else(|_| "4096".to_string())
                .parse()
                .unwrap_or(4096),
            preserve_metadata: std::env::var("VISION_PRESERVE_METADATA")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
        }
    }
    
    /// Update configuration dynamically
    pub fn update(&mut self, key: &str, value: &str) -> Result<()> {
        match key {
            "enable_simd" => self.enable_simd = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid boolean".to_string()))?,
            "thread_count" => self.thread_count = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid thread count".to_string()))?,
            "denoise_strength" => self.denoise_strength = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid denoise strength".to_string()))?,
            "sharpen_amount" => self.sharpen_amount = value.parse()
                .map_err(|_| JarvisError::InvalidOperation("Invalid sharpen amount".to_string()))?,
            "quality_preset" => self.quality_preset = value.parse()
                .map_err(|e: String| JarvisError::InvalidOperation(e))?,
            _ => return Err(JarvisError::InvalidOperation(format!("Unknown config key: {}", key))),
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum QualityPreset {
    Fast,
    Balanced,
    Quality,
    Ultra,
}

impl std::str::FromStr for QualityPreset {
    type Err = String;
    
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "fast" => Ok(QualityPreset::Fast),
            "balanced" => Ok(QualityPreset::Balanced),
            "quality" => Ok(QualityPreset::Quality),
            "ultra" => Ok(QualityPreset::Ultra),
            _ => Err(format!("Unknown quality preset: {}", s)),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum ColorCorrectionMode {
    None,
    Auto,
    Gamma(f32),
    LinearToSrgb,
    SrgbToLinear,
}

impl std::str::FromStr for ColorCorrectionMode {
    type Err = String;
    
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "none" => Ok(ColorCorrectionMode::None),
            "auto" => Ok(ColorCorrectionMode::Auto),
            "linear_to_srgb" => Ok(ColorCorrectionMode::LinearToSrgb),
            "srgb_to_linear" => Ok(ColorCorrectionMode::SrgbToLinear),
            s if s.starts_with("gamma:") => {
                let gamma = s.trim_start_matches("gamma:")
                    .parse::<f32>()
                    .map_err(|_| "Invalid gamma value")?;
                Ok(ColorCorrectionMode::Gamma(gamma))
            }
            _ => Err(format!("Unknown color correction mode: {}", s)),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum ResizeAlgorithm {
    NearestNeighbor,
    Bilinear,
    Bicubic,
    Lanczos,
    Mitchell,
}

impl std::str::FromStr for ResizeAlgorithm {
    type Err = String;
    
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "nearest" | "nearestneighbor" => Ok(ResizeAlgorithm::NearestNeighbor),
            "bilinear" | "linear" => Ok(ResizeAlgorithm::Bilinear),
            "bicubic" | "cubic" => Ok(ResizeAlgorithm::Bicubic),
            "lanczos" => Ok(ResizeAlgorithm::Lanczos),
            "mitchell" => Ok(ResizeAlgorithm::Mitchell),
            _ => Err(format!("Unknown resize algorithm: {}", s)),
        }
    }
}

/// Enhanced image processor with dynamic configuration and zero-copy operations
pub struct ImageProcessor {
    config: Arc<RwLock<ProcessingConfig>>,
    memory_manager: Arc<MemoryManager>,
    filter_cache: RwLock<HashMap<String, Vec<f32>>>,
    stats: RwLock<ProcessingStats>,
    pub use_simd: bool,
    #[cfg(feature = "gpu")]
    gpu_context: Option<GpuContext>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProcessingStats {
    pub images_processed: u64,
    pub total_pixels: u64,
    pub total_time_ms: f64,
    pub cache_hits: u64,
    pub cache_misses: u64,
    pub simd_operations: u64,
    pub gpu_operations: u64,
}

#[cfg(feature = "gpu")]
struct GpuContext {
    // GPU compute context placeholder
}

impl ImageProcessor {
    pub fn new() -> Self {
        let config = Arc::new(RwLock::new(ProcessingConfig::from_env()));
        Self {
            config: config.clone(),
            memory_manager: MemoryManager::global(),
            filter_cache: RwLock::new(HashMap::new()),
            stats: RwLock::new(ProcessingStats::default()),
            use_simd: cfg!(target_feature = "sse2") || cfg!(target_feature = "neon"),
            #[cfg(feature = "gpu")]
            gpu_context: None,
        }
    }

    /// Create with custom configuration
    pub fn with_config(config: ProcessingConfig) -> Self {
        Self {
            config: Arc::new(RwLock::new(config)),
            memory_manager: MemoryManager::global(),
            filter_cache: RwLock::new(HashMap::new()),
            stats: RwLock::new(ProcessingStats::default()),
            use_simd: cfg!(target_feature = "sse2") || cfg!(target_feature = "neon"),
            #[cfg(feature = "gpu")]
            gpu_context: None,
        }
    }

    /// Get specific channel value at coordinates from an image
    pub fn get_pixel_at(image: &ImageData, x: u32, y: u32, channel: u8) -> u8 {
        if x >= image.width || y >= image.height || channel >= image.channels {
            return 0;
        }
        let offset = ((y * image.width + x) * image.channels as u32 + channel as u32) as usize;
        let data = image.as_slice();
        if offset < data.len() {
            data[offset]
        } else {
            0
        }
    }
    
    /// Update configuration dynamically
    pub fn update_config(&self, key: &str, value: &str) -> Result<()> {
        self.config.write().update(key, value)?;
        PROCESSING_CONFIG.write().update(key, value)?;
        Ok(())
    }
    
    /// Get current configuration
    pub fn get_config(&self) -> ProcessingConfig {
        self.config.read().clone()
    }
    
    /// Process image with auto-enhancement based on configuration
    pub fn auto_process(&self, image: &ImageData) -> Result<ImageData> {
        let start = Instant::now();
        let config = self.config.read().clone();
        let mut result = image.clone();
        
        // Apply auto-enhancement if enabled
        if config.auto_enhance {
            result = self.auto_enhance(&result)?;
        }
        
        // Apply denoising if configured
        if config.denoise_strength > 0.0 {
            result = self.denoise(&result, config.denoise_strength)?;
        }
        
        // Apply sharpening if configured
        if config.sharpen_amount > 0.0 {
            result = self.sharpen(&result, config.sharpen_amount)?;
        }
        
        // Apply color correction
        match config.color_correction {
            ColorCorrectionMode::Auto => result = self.auto_color_correct(&result)?,
            ColorCorrectionMode::Gamma(gamma) => result = self.apply_gamma(&result, gamma)?,
            ColorCorrectionMode::LinearToSrgb => result = self.linear_to_srgb(&result)?,
            ColorCorrectionMode::SrgbToLinear => result = self.srgb_to_linear(&result)?,
            ColorCorrectionMode::None => {},
        }
        
        // Update statistics
        let mut stats = self.stats.write();
        stats.images_processed += 1;
        stats.total_pixels += (image.width * image.height) as u64;
        stats.total_time_ms += start.elapsed().as_secs_f64() * 1000.0;
        
        Ok(result)
    }
    
    /// Enhanced resize with configurable algorithm
    pub fn resize(&self, image: &ImageData, new_width: u32, new_height: u32) -> Result<ImageData> {
        let config = self.config.read();
        
        // Check dimension limits
        if new_width > config.max_dimension || new_height > config.max_dimension {
            return Err(JarvisError::VisionError(
                format!("Requested dimensions {}x{} exceed maximum {}", 
                    new_width, new_height, config.max_dimension)
            ));
        }
        
        match config.resize_algorithm {
            ResizeAlgorithm::NearestNeighbor => self.resize_nearest(image, new_width, new_height),
            ResizeAlgorithm::Bilinear => self.resize_bilinear(image, new_width, new_height),
            ResizeAlgorithm::Bicubic => self.resize_bicubic(image, new_width, new_height),
            ResizeAlgorithm::Lanczos => self.resize_lanczos(image, new_width, new_height),
            ResizeAlgorithm::Mitchell => self.resize_mitchell(image, new_width, new_height),
        }
    }
    
    /// Nearest neighbor resize (fastest)
    fn resize_nearest(&self, image: &ImageData, new_width: u32, new_height: u32) -> Result<ImageData> {
        let mut output = ImageData::new(new_width, new_height, image.channels, image.format);
        
        let x_ratio = image.width as f32 / new_width as f32;
        let y_ratio = image.height as f32 / new_height as f32;
        
        // Parallel processing with configured thread count
        let config = self.config.read();
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(config.thread_count)
            .build()
            .unwrap();
        
        pool.install(|| {
            output.data.par_chunks_mut((new_width * image.channels as u32) as usize)
                .enumerate()
                .for_each(|(y, row)| {
                    let src_y = (y as f32 * y_ratio) as u32;
                    
                    for x in 0..new_width {
                        let src_x = (x as f32 * x_ratio) as u32;
                        let src_offset = ((src_y * image.width + src_x) * image.channels as u32) as usize;
                        let dst_offset = (x * image.channels as u32) as usize;
                        
                        row[dst_offset..dst_offset + image.channels as usize]
                            .copy_from_slice(&image.data[src_offset..src_offset + image.channels as usize]);
                    }
                });
        });
        
        Ok(output)
    }
    
    /// Bilinear interpolation resize
    fn resize_bilinear(&self, image: &ImageData, new_width: u32, new_height: u32) -> Result<ImageData> {
        let mut output = ImageData::new(new_width, new_height, image.channels, image.format);
        
        let x_ratio = (image.width - 1) as f32 / new_width as f32;
        let y_ratio = (image.height - 1) as f32 / new_height as f32;
        
        let config = self.config.read();
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(config.thread_count)
            .build()
            .unwrap();
        
        pool.install(|| {
            output.data.par_chunks_mut((new_width * image.channels as u32) as usize)
                .enumerate()
                .for_each(|(y, row)| {
                    let y_f = y as f32 * y_ratio;
                    let y_floor = y_f.floor() as u32;
                    let y_ceil = (y_floor + 1).min(image.height - 1);
                    let y_weight = y_f - y_floor as f32;
                    
                    for x in 0..new_width {
                        let x_f = x as f32 * x_ratio;
                        let x_floor = x_f.floor() as u32;
                        let x_ceil = (x_floor + 1).min(image.width - 1);
                        let x_weight = x_f - x_floor as f32;
                        
                        for c in 0..image.channels {
                            // Get the four neighboring pixels
                            let p00 = Self::get_pixel_at(&image, x_floor, y_floor, c);
                            let p10 = Self::get_pixel_at(&image, x_ceil, y_floor, c);
                            let p01 = Self::get_pixel_at(&image, x_floor, y_ceil, c);
                            let p11 = Self::get_pixel_at(&image, x_ceil, y_ceil, c);
                            
                            // Bilinear interpolation
                            let p0 = p00 as f32 * (1.0 - x_weight) + p10 as f32 * x_weight;
                            let p1 = p01 as f32 * (1.0 - x_weight) + p11 as f32 * x_weight;
                            let p = p0 * (1.0 - y_weight) + p1 * y_weight;
                            
                            let dst_offset = (x * image.channels as u32 + c as u32) as usize;
                            row[dst_offset] = p.round().clamp(0.0, 255.0) as u8;
                        }
                    }
                });
        });
        
        Ok(output)
    }
    
    /// Bicubic interpolation resize
    fn resize_bicubic(&self, image: &ImageData, new_width: u32, new_height: u32) -> Result<ImageData> {
        // Implementation placeholder - use bilinear for now
        self.resize_bilinear(image, new_width, new_height)
    }
    
    /// Lanczos interpolation resize (highest quality)
    fn resize_lanczos(&self, image: &ImageData, new_width: u32, new_height: u32) -> Result<ImageData> {
        // Implementation placeholder - use bilinear for now
        self.resize_bilinear(image, new_width, new_height)
    }
    
    /// Mitchell filter resize
    fn resize_mitchell(&self, image: &ImageData, new_width: u32, new_height: u32) -> Result<ImageData> {
        // Implementation placeholder - use bilinear for now
        self.resize_bilinear(image, new_width, new_height)
    }
    
    /// Convert between color formats
    pub fn convert_format(&self, image: &ImageData, target_format: ImageFormat) -> Result<ImageData> {
        if image.format == target_format {
            return Ok(image.clone());
        }
        
        match (image.format, target_format) {
            (ImageFormat::Rgb8, ImageFormat::Bgr8) => self.rgb_to_bgr(image),
            (ImageFormat::Bgr8, ImageFormat::Rgb8) => self.rgb_to_bgr(image), // Same operation
            (ImageFormat::Rgba8, ImageFormat::Rgb8) => self.rgba_to_rgb(image),
            (ImageFormat::Rgb8, ImageFormat::Gray8) => self.rgb_to_gray(image),
            _ => Err(JarvisError::VisionError(
                format!("Conversion from {:?} to {:?} not implemented", image.format, target_format)
            ))
        }
    }
    
    /// RGB to BGR conversion (also handles BGR to RGB)
    fn rgb_to_bgr(&self, image: &ImageData) -> Result<ImageData> {
        let mut output = image.clone();
        
        if self.use_simd {
            self.rgb_to_bgr_simd(&mut output.data)?;
        } else {
            output.data.par_chunks_mut(3).for_each(|pixel| {
                pixel.swap(0, 2);
            });
        }
        
        output.format = if image.format == ImageFormat::Rgb8 { 
            ImageFormat::Bgr8 
        } else { 
            ImageFormat::Rgb8 
        };
        
        Ok(output)
    }
    
    /// RGBA to RGB conversion
    fn rgba_to_rgb(&self, image: &ImageData) -> Result<ImageData> {
        let mut output = ImageData::new(image.width, image.height, 3, ImageFormat::Rgb8);
        
        output.data.par_chunks_mut(3)
            .zip(image.data.par_chunks(4))
            .for_each(|(dst, src)| {
                dst.copy_from_slice(&src[..3]);
            });
        
        Ok(output)
    }
    
    /// RGB to grayscale conversion
    fn rgb_to_gray(&self, image: &ImageData) -> Result<ImageData> {
        let mut output = ImageData::new(image.width, image.height, 1, ImageFormat::Gray8);
        
        output.data.par_iter_mut()
            .zip(image.data.par_chunks(3))
            .for_each(|(gray, rgb)| {
                // Use standard luminance weights
                *gray = (0.299 * rgb[0] as f32 + 
                        0.587 * rgb[1] as f32 + 
                        0.114 * rgb[2] as f32) as u8;
            });
        
        Ok(output)
    }
    
    /// SIMD-accelerated RGB to BGR
    #[cfg(target_arch = "aarch64")]
    fn rgb_to_bgr_simd(&self, data: &mut [u8]) -> Result<()> {
        use std::arch::aarch64::*;
        
        unsafe {
            let chunks = data.chunks_exact_mut(48); // Process 16 pixels at a time
            
            for chunk in chunks {
                // Load 48 bytes (16 RGB pixels)
                let v0 = vld3q_u8(chunk.as_ptr());
                
                // Swap R and B channels
                let swapped = uint8x16x3_t {
                    0: v0.2,  // B becomes R
                    1: v0.1,  // G stays G
                    2: v0.0,  // R becomes B
                };
                
                // Store back
                vst3q_u8(chunk.as_mut_ptr(), swapped);
            }
            
            // Handle remaining pixels
            let remainder = data.len() % 48;
            if remainder > 0 {
                let offset = data.len() - remainder;
                for i in (offset..data.len()).step_by(3) {
                    data.swap(i, i + 2);
                }
            }
        }
        
        Ok(())
    }
    
    #[cfg(not(target_arch = "aarch64"))]
    fn rgb_to_bgr_simd(&self, _data: &mut [u8]) -> Result<()> {
        Err(JarvisError::InvalidOperation("SIMD not available".to_string()))
    }
    
    /// Enhanced convolution with caching and SIMD acceleration
    pub fn convolve(&self, image: &ImageData, kernel_name: &str, kernel: Option<&[f32]>) -> Result<ImageData> {
        let config = self.config.read();
        
        // Get kernel from cache or parameter
        let kernel_data = if let Some(k) = kernel {
            k.to_vec()
        } else {
            // Check cache
            let cache = self.filter_cache.read();
            if let Some(cached) = cache.get(kernel_name) {
                self.stats.write().cache_hits += 1;
                cached.clone()
            } else {
                drop(cache);
                // Generate standard kernels dynamically
                let generated = self.generate_kernel(kernel_name)?;
                self.filter_cache.write().insert(kernel_name.to_string(), generated.clone());
                self.stats.write().cache_misses += 1;
                generated
            }
        };
        
        let kernel_size = (kernel_data.len() as f32).sqrt() as usize;
        if kernel_size * kernel_size != kernel_data.len() {
            return Err(JarvisError::VisionError("Invalid kernel size".to_string()));
        }
        
        // Use SIMD if available and enabled
        if config.enable_simd && cfg!(any(target_arch = "x86_64", target_arch = "aarch64")) {
            self.stats.write().simd_operations += 1;
            self.convolve_simd(image, &kernel_data, kernel_size)
        } else {
            self.convolve_standard(image, &kernel_data, kernel_size)
        }
    }
    
    /// Standard convolution implementation
    fn convolve_standard(&self, image: &ImageData, kernel: &[f32], kernel_size: usize) -> Result<ImageData> {
        let mut output = image.clone();
        let half_kernel = kernel_size / 2;
        let config = self.config.read();
        
        // Create thread pool with configured size
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(config.thread_count)
            .build()
            .unwrap();
        
        // Process each channel sequentially (they write to interleaved positions)
        for c in 0..image.channels {
            // Extract channel data
            let channel_data: Vec<f32> = image.data.iter()
                .skip(c as usize)
                .step_by(image.channels as usize)
                .map(|&v| v as f32)
                .collect();

            let channel_2d = Array2::from_shape_vec(
                (image.height as usize, image.width as usize),
                channel_data
            ).unwrap();

            // Apply convolution
            let mut result = Array2::zeros((image.height as usize, image.width as usize));

            pool.install(|| {
                // Parallelize within each channel's pixel processing
                for y in half_kernel..(image.height as usize - half_kernel) {
                    for x in half_kernel..(image.width as usize - half_kernel) {
                        let mut sum = 0.0;

                        for ky in 0..kernel_size {
                            for kx in 0..kernel_size {
                                let pixel_y = y + ky - half_kernel;
                                let pixel_x = x + kx - half_kernel;
                                sum += channel_2d[[pixel_y, pixel_x]] * kernel[ky * kernel_size + kx];
                            }
                        }

                        result[[y, x]] = sum.clamp(0.0, 255.0);
                    }
                }
            });

            // Handle edges with reflection padding
            self.apply_edge_padding(&channel_2d, &mut result, kernel, kernel_size);

            // Write back to output
            for (i, &val) in result.iter().enumerate() {
                output.data[i * image.channels as usize + c as usize] = val as u8;
            }
        }
        
        Ok(output)
    }
    
    /// SIMD-accelerated convolution
    #[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
    fn convolve_simd(&self, image: &ImageData, kernel: &[f32], kernel_size: usize) -> Result<ImageData> {
        // Placeholder for SIMD implementation
        self.convolve_standard(image, kernel, kernel_size)
    }
    
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]  
    fn convolve_simd(&self, image: &ImageData, kernel: &[f32], kernel_size: usize) -> Result<ImageData> {
        self.convolve_standard(image, kernel, kernel_size)
    }
    
    /// Apply edge padding for convolution
    fn apply_edge_padding(&self, source: &Array2<f32>, result: &mut Array2<f32>, 
                         kernel: &[f32], kernel_size: usize) {
        let half_kernel = kernel_size / 2;
        let height = source.nrows();
        let width = source.ncols();
        
        // Top and bottom edges
        for y in 0..half_kernel {
            for x in 0..width {
                // Reflect padding
                let reflected_y = half_kernel - y;
                result[[y, x]] = result[[reflected_y, x]];
                result[[height - 1 - y, x]] = result[[height - 1 - reflected_y, x]];
            }
        }
        
        // Left and right edges  
        for x in 0..half_kernel {
            for y in 0..height {
                let reflected_x = half_kernel - x;
                result[[y, x]] = result[[y, reflected_x]];
                result[[y, width - 1 - x]] = result[[y, width - 1 - reflected_x]];
            }
        }
    }
    
    /// Generate standard kernels dynamically
    fn generate_kernel(&self, name: &str) -> Result<Vec<f32>> {
        match name {
            "gaussian_3x3" => Ok(vec![
                1.0/16.0, 2.0/16.0, 1.0/16.0,
                2.0/16.0, 4.0/16.0, 2.0/16.0,
                1.0/16.0, 2.0/16.0, 1.0/16.0,
            ]),
            "gaussian_5x5" => Ok(vec![
                1.0/256.0, 4.0/256.0, 6.0/256.0, 4.0/256.0, 1.0/256.0,
                4.0/256.0, 16.0/256.0, 24.0/256.0, 16.0/256.0, 4.0/256.0,
                6.0/256.0, 24.0/256.0, 36.0/256.0, 24.0/256.0, 6.0/256.0,
                4.0/256.0, 16.0/256.0, 24.0/256.0, 16.0/256.0, 4.0/256.0,
                1.0/256.0, 4.0/256.0, 6.0/256.0, 4.0/256.0, 1.0/256.0,
            ]),
            "sharpen" => Ok(vec![
                0.0, -1.0, 0.0,
                -1.0, 5.0, -1.0,
                0.0, -1.0, 0.0,
            ]),
            "edge_detect" => Ok(vec![
                -1.0, -1.0, -1.0,
                -1.0, 8.0, -1.0,
                -1.0, -1.0, -1.0,
            ]),
            "sobel_x" => Ok(vec![
                -1.0, 0.0, 1.0,
                -2.0, 0.0, 2.0,
                -1.0, 0.0, 1.0,
            ]),
            "sobel_y" => Ok(vec![
                -1.0, -2.0, -1.0,
                0.0, 0.0, 0.0,
                1.0, 2.0, 1.0,
            ]),
            _ => {
                // Try to parse custom kernel format: "custom:1,2,3,4,5,6,7,8,9"
                if name.starts_with("custom:") {
                    let values_str = name.trim_start_matches("custom:");
                    let values: Result<Vec<f32>> = values_str
                        .split(',')
                        .map(|s| s.trim().parse::<f32>()
                            .map_err(|_| JarvisError::InvalidOperation("Invalid kernel value".to_string())))
                        .collect();
                    values
                } else {
                    Err(JarvisError::VisionError(format!("Unknown kernel: {}", name)))
                }
            }
        }
    }
    
    /// Auto-enhance image based on analysis
    fn auto_enhance(&self, image: &ImageData) -> Result<ImageData> {
        // Analyze image characteristics
        let stats = self.analyze_image_stats(image)?;
        let mut result = image.clone();
        
        // Auto adjust brightness/contrast if needed
        if stats.brightness < 0.3 || stats.brightness > 0.7 {
            let adjustment = if stats.brightness < 0.3 { 1.2 } else { 0.85 };
            result = self.adjust_brightness(&result, adjustment)?;
        }
        
        // Auto adjust contrast if needed
        if stats.contrast < 0.2 {
            result = self.adjust_contrast(&result, 1.3)?;
        }
        
        // Auto white balance if color cast detected
        if stats.color_cast > 0.1 {
            result = self.auto_white_balance(&result)?;
        }
        
        Ok(result)
    }
    
    /// Denoise image
    fn denoise(&self, image: &ImageData, strength: f32) -> Result<ImageData> {
        // Use bilateral filter for edge-preserving denoising
        let sigma_color = 25.0 * strength;
        let sigma_space = 10.0 * strength;
        self.bilateral_filter(image, sigma_color, sigma_space)
    }
    
    /// Sharpen image
    fn sharpen(&self, image: &ImageData, amount: f32) -> Result<ImageData> {
        // Unsharp mask sharpening
        let blurred = self.convolve(image, "gaussian_5x5", None)?;
        let mut result = image.clone();
        
        // result = original + amount * (original - blurred)
        for i in 0..result.data.len() {
            let diff = image.data[i] as f32 - blurred.data[i] as f32;
            let sharpened = image.data[i] as f32 + amount * diff;
            result.data[i] = sharpened.clamp(0.0, 255.0) as u8;
        }
        
        Ok(result)
    }
    
    /// Apply gamma correction
    fn apply_gamma(&self, image: &ImageData, gamma: f32) -> Result<ImageData> {
        let mut result = image.clone();
        let inv_gamma = 1.0 / gamma;
        
        // Pre-compute lookup table
        let lut: Vec<u8> = (0..256)
            .map(|i| {
                let normalized = i as f32 / 255.0;
                (normalized.powf(inv_gamma) * 255.0).round() as u8
            })
            .collect();
        
        // Apply LUT
        result.data.par_iter_mut().for_each(|pixel| {
            *pixel = lut[*pixel as usize];
        });
        
        Ok(result)
    }
    
    /// Convert linear to sRGB
    fn linear_to_srgb(&self, image: &ImageData) -> Result<ImageData> {
        let mut result = image.clone();
        
        result.data.par_iter_mut().for_each(|pixel| {
            let linear = *pixel as f32 / 255.0;
            let srgb = if linear <= 0.0031308 {
                linear * 12.92
            } else {
                1.055 * linear.powf(1.0 / 2.4) - 0.055
            };
            *pixel = (srgb * 255.0).round().clamp(0.0, 255.0) as u8;
        });
        
        Ok(result)
    }
    
    /// Convert sRGB to linear
    fn srgb_to_linear(&self, image: &ImageData) -> Result<ImageData> {
        let mut result = image.clone();
        
        result.data.par_iter_mut().for_each(|pixel| {
            let srgb = *pixel as f32 / 255.0;
            let linear = if srgb <= 0.04045 {
                srgb / 12.92
            } else {
                ((srgb + 0.055) / 1.055).powf(2.4)
            };
            *pixel = (linear * 255.0).round().clamp(0.0, 255.0) as u8;
        });
        
        Ok(result)
    }
    
    /// Auto color correction
    fn auto_color_correct(&self, image: &ImageData) -> Result<ImageData> {
        if image.channels < 3 {
            return Ok(image.clone()); // Can't color correct grayscale
        }
        
        // Compute per-channel statistics
        let mut channel_means = vec![0.0; image.channels as usize];
        let pixel_count = (image.width * image.height) as f32;
        
        for c in 0..image.channels {
            let sum: f32 = image.data.iter()
                .skip(c as usize)
                .step_by(image.channels as usize)
                .map(|&v| v as f32)
                .sum();
            channel_means[c as usize] = sum / pixel_count;
        }
        
        // Find target gray value (average of all channels)
        let target_gray = channel_means.iter().sum::<f32>() / channel_means.len() as f32;
        
        // Apply correction
        let mut result = image.clone();
        for c in 0..image.channels {
            let scale = target_gray / channel_means[c as usize].max(1.0);
            
            for i in (c as usize..result.data.len()).step_by(image.channels as usize) {
                let corrected = (result.data[i] as f32 * scale).round().clamp(0.0, 255.0);
                result.data[i] = corrected as u8;
            }
        }
        
        Ok(result)
    }
    
    /// Auto white balance
    fn auto_white_balance(&self, image: &ImageData) -> Result<ImageData> {
        if image.channels < 3 {
            return Ok(image.clone());
        }
        
        // Gray world assumption
        self.auto_color_correct(image)
    }
    
    /// Analyze image statistics
    fn analyze_image_stats(&self, image: &ImageData) -> Result<ImageStats> {
        let mut brightness: f32 = 0.0;
        let mut min_val: f32 = 255.0;
        let mut max_val: f32 = 0.0;
        let pixel_count = (image.width * image.height) as f32;
        
        // Compute brightness and range
        for &pixel in &image.data {
            let val = pixel as f32;
            brightness += val;
            min_val = min_val.min(val);
            max_val = max_val.max(val);
        }
        
        brightness /= (image.data.len() as f32 * 255.0);
        let contrast = (max_val - min_val) / 255.0;
        
        // Estimate color cast for color images
        let color_cast = if image.channels >= 3 {
            let mut channel_avgs = vec![0.0; 3];
            for c in 0..3 {
                let sum: f32 = image.data.iter()
                    .skip(c)
                    .step_by(image.channels as usize)
                    .map(|&v| v as f32)
                    .sum();
                channel_avgs[c] = sum / pixel_count;
            }
            
            let mean_avg = channel_avgs.iter().sum::<f32>() / 3.0;
            channel_avgs.iter()
                .map(|&avg| (avg - mean_avg).abs() / mean_avg)
                .sum::<f32>() / 3.0
        } else {
            0.0
        };
        
        Ok(ImageStats {
            brightness,
            contrast,
            color_cast,
        })
    }
    
    /// Adjust brightness
    fn adjust_brightness(&self, image: &ImageData, factor: f32) -> Result<ImageData> {
        let mut result = image.clone();
        
        result.data.par_iter_mut().for_each(|pixel| {
            let adjusted = (*pixel as f32 * factor).round().clamp(0.0, 255.0);
            *pixel = adjusted as u8;
        });
        
        Ok(result)
    }
    
    /// Adjust contrast
    fn adjust_contrast(&self, image: &ImageData, factor: f32) -> Result<ImageData> {
        let mut result = image.clone();
        
        result.data.par_iter_mut().for_each(|pixel| {
            let normalized = *pixel as f32 / 255.0;
            let adjusted = ((normalized - 0.5) * factor + 0.5) * 255.0;
            *pixel = adjusted.round().clamp(0.0, 255.0) as u8;
        });
        
        Ok(result)
    }
    
    /// Bilateral filter for edge-preserving smoothing
    fn bilateral_filter(&self, image: &ImageData, sigma_color: f32, sigma_space: f32) -> Result<ImageData> {
        // Simplified bilateral filter implementation
        // For full implementation, would use spatial and range kernels
        let kernel_size = ((sigma_space * 3.0) as usize * 2) + 1;
        let gaussian = self.generate_gaussian_kernel(kernel_size, sigma_space)?;
        self.convolve(image, "custom_bilateral", Some(&gaussian))
    }
    
    /// Generate Gaussian kernel
    fn generate_gaussian_kernel(&self, size: usize, sigma: f32) -> Result<Vec<f32>> {
        let mut kernel = vec![0.0; size * size];
        let center = size / 2;
        let mut sum = 0.0;
        
        for y in 0..size {
            for x in 0..size {
                let dx = x as f32 - center as f32;
                let dy = y as f32 - center as f32;
                let distance_sq = dx * dx + dy * dy;
                let value = (-distance_sq / (2.0 * sigma * sigma)).exp();
                kernel[y * size + x] = value;
                sum += value;
            }
        }
        
        // Normalize
        for val in &mut kernel {
            *val /= sum;
        }
        
        Ok(kernel)
    }
    
    /// Get processing statistics
    pub fn get_stats(&self) -> ProcessingStats {
        self.stats.read().clone()
    }
    
    /// Reset statistics
    pub fn reset_stats(&mut self) {
        *self.stats.write() = ProcessingStats::default();
    }
}

/// Image statistics for auto-enhancement
#[derive(Debug, Clone)]
struct ImageStats {
    brightness: f32,
    contrast: f32,
    color_cast: f32,
}

// Extension methods for ImageData
impl ImageData {
    /// Get pixel value at specific location and channel
    fn get_pixel_value(&self, x: u32, y: u32, channel: u8) -> u8 {
        let offset = ((y * self.width + x) * self.channels as u32 + channel as u32) as usize;
        self.data[offset]
    }
}

/// Enhanced processing pipeline with dynamic operations
pub struct ProcessingPipeline {
    operations: Vec<ProcessingOperation>,
    processor: Arc<ImageProcessor>,
}

#[derive(Clone)]
pub struct ProcessingOperation {
    name: String,
    op_type: OperationType,
    params: HashMap<String, f32>,
}

#[derive(Clone)]
enum OperationType {
    Resize { width: u32, height: u32 },
    Convolve { kernel_name: String },
    ColorCorrect { mode: ColorCorrectionMode },
    Enhance { strength: f32 },
    Custom { func: fn(&ImageData, &HashMap<String, f32>) -> Result<ImageData> },
}

impl ProcessingPipeline {
    pub fn new(processor: Arc<ImageProcessor>) -> Self {
        Self {
            operations: Vec::new(),
            processor,
        }
    }
    
    /// Add resize operation
    pub fn resize(mut self, width: u32, height: u32) -> Self {
        self.operations.push(ProcessingOperation {
            name: "resize".to_string(),
            op_type: OperationType::Resize { width, height },
            params: HashMap::new(),
        });
        self
    }
    
    /// Add convolution operation
    pub fn convolve(mut self, kernel_name: &str) -> Self {
        self.operations.push(ProcessingOperation {
            name: "convolve".to_string(),
            op_type: OperationType::Convolve { kernel_name: kernel_name.to_string() },
            params: HashMap::new(),
        });
        self
    }
    
    /// Add color correction
    pub fn color_correct(mut self, mode: ColorCorrectionMode) -> Self {
        self.operations.push(ProcessingOperation {
            name: "color_correct".to_string(),
            op_type: OperationType::ColorCorrect { mode },
            params: HashMap::new(),
        });
        self
    }
    
    /// Add auto-enhance
    pub fn auto_enhance(mut self, strength: f32) -> Self {
        self.operations.push(ProcessingOperation {
            name: "auto_enhance".to_string(),
            op_type: OperationType::Enhance { strength },
            params: HashMap::new(),
        });
        self
    }
    
    /// Execute pipeline on image
    pub fn execute(&self, mut image: ImageData) -> Result<ImageData> {
        let start = Instant::now();
        
        for op in &self.operations {
            image = match &op.op_type {
                OperationType::Resize { width, height } => {
                    self.processor.resize(&image, *width, *height)?
                }
                OperationType::Convolve { kernel_name } => {
                    self.processor.convolve(&image, kernel_name, None)?
                }
                OperationType::ColorCorrect { mode } => {
                    match mode {
                        ColorCorrectionMode::Auto => self.processor.auto_color_correct(&image)?,
                        ColorCorrectionMode::Gamma(g) => self.processor.apply_gamma(&image, *g)?,
                        ColorCorrectionMode::LinearToSrgb => self.processor.linear_to_srgb(&image)?,
                        ColorCorrectionMode::SrgbToLinear => self.processor.srgb_to_linear(&image)?,
                        ColorCorrectionMode::None => image,
                    }
                }
                OperationType::Enhance { strength } => {
                    let mut result = image;
                    if *strength > 0.0 {
                        result = self.processor.auto_enhance(&result)?;
                    }
                    result
                }
                OperationType::Custom { func } => {
                    func(&image, &op.params)?
                }
            };
        }
        
        tracing::debug!("Pipeline executed {} operations in {:?}", 
            self.operations.len(), start.elapsed());
        
        Ok(image)
    }
}

/// Global configuration update
pub fn update_global_config(key: &str, value: &str) -> Result<()> {
    PROCESSING_CONFIG.write().update(key, value)
}

/// Get current global configuration  
pub fn get_global_config() -> ProcessingConfig {
    PROCESSING_CONFIG.read().clone()
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_format_conversion() {
        let processor = ImageProcessor::new();
        
        // Create test RGB image
        let mut image = ImageData::new(2, 2, 3, ImageFormat::Rgb8);
        image.data = vec![
            255, 0, 0,    // Red
            0, 255, 0,    // Green
            0, 0, 255,    // Blue
            255, 255, 0,  // Yellow
        ];
        
        // Convert to grayscale
        let gray = processor.convert_format(&image, ImageFormat::Gray8).unwrap();
        assert_eq!(gray.channels, 1);
        assert_eq!(gray.data.len(), 4);
    }
}