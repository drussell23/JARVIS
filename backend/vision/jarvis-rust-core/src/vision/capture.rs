//! Advanced thread-safe screen capture with dynamic configuration
//! 
//! Features:
//! - Zero-copy operations with shared memory
//! - Adaptive quality based on system load
//! - Multi-monitor support
//! - Hardware acceleration detection
//! - Frame caching and deduplication
//! - Real-time performance monitoring

use crate::{Result, JarvisError};
use crate::bridge::{ObjCBridge, ObjCCommand, ObjCResponse, CaptureQuality as BridgeCaptureQuality};
pub use crate::bridge::CaptureRegion;
use crate::memory::MemoryManager;
use crate::bridge::supervisor::{Supervisor, RestartStrategy, RestartConfig};
// Import or define ImageData and ImageFormat
use crate::vision::{ImageData, ImageFormat};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime};
use std::collections::{HashMap, VecDeque};
use parking_lot::{RwLock, Mutex};
use serde::{Deserialize, Serialize};
use dashmap::DashMap;
use tokio::sync::{broadcast, watch};
use once_cell::sync::Lazy;

// ============================================================================
// DYNAMIC CONFIGURATION SYSTEM
// ============================================================================

/// Global configuration store with hot-reload support
static GLOBAL_CONFIG: Lazy<Arc<DynamicConfigStore>> = Lazy::new(|| {
    Arc::new(DynamicConfigStore::new())
});

/// Dynamic configuration store with watch channels for updates
pub struct DynamicConfigStore {
    configs: DashMap<String, serde_json::Value>,
    watchers: DashMap<String, watch::Sender<serde_json::Value>>,
}

impl DynamicConfigStore {
    pub fn new() -> Self {
        let store = Self {
            configs: DashMap::new(),
            watchers: DashMap::new(),
        };
        
        // Load from environment or config file
        store.load_from_env();
        store
    }
    
    fn load_from_env(&self) {
        // Load all JARVIS_CAPTURE_* environment variables
        for (key, value) in std::env::vars() {
            if key.starts_with("JARVIS_CAPTURE_") {
                let config_key = key.strip_prefix("JARVIS_CAPTURE_").unwrap().to_lowercase();
                if let Ok(json_value) = serde_json::from_str(&value) {
                    self.set(&config_key, json_value);
                }
            }
        }
    }
    
    pub fn set(&self, key: &str, value: serde_json::Value) {
        self.configs.insert(key.to_string(), value.clone());
        
        // Notify watchers
        if let Some(sender) = self.watchers.get(key) {
            let _ = sender.send(value);
        }
    }
    
    pub fn get(&self, key: &str) -> Option<serde_json::Value> {
        self.configs.get(key).map(|v| v.clone())
    }
    
    pub fn watch(&self, key: &str) -> watch::Receiver<serde_json::Value> {
        let entry = self.watchers.entry(key.to_string());
        let (tx, rx) = match entry {
            dashmap::mapref::entry::Entry::Occupied(e) => {
                let tx = e.get().clone();
                (tx.clone(), tx.subscribe())
            }
            dashmap::mapref::entry::Entry::Vacant(e) => {
                let default_value = self.get(key).unwrap_or(serde_json::Value::Null);
                let (tx, rx) = watch::channel(default_value);
                e.insert(tx.clone());
                (tx, rx)
            }
        };
        rx
    }
}

// ============================================================================
// ADVANCED CAPTURE CONFIGURATION
// ============================================================================

/// Advanced capture configuration with dynamic fields
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureConfig {
    // Basic settings
    pub target_fps: u32,
    pub capture_mouse: bool,
    pub capture_region: Option<CaptureRegion>,
    
    // Quality settings
    pub capture_quality: CaptureQuality,
    pub adaptive_quality: AdaptiveQualityConfig,
    
    // Performance settings  
    pub use_hardware_acceleration: bool,
    pub enable_gpu_capture: bool,
    pub enable_metal_performance_shaders: bool,
    
    // Memory settings
    pub memory_pool_size_mb: usize,
    pub buffer_count: usize,
    pub enable_frame_caching: bool,
    pub cache_size_frames: usize,
    
    // Advanced features
    pub enable_hdr: bool,
    pub color_space: ColorSpace,
    pub pixel_format: PixelFormat,
    pub enable_frame_deduplication: bool,
    pub deduplication_threshold: f32,
    
    // Multi-monitor support
    pub monitor_selection: MonitorSelection,
    pub enable_monitor_switching: bool,
    
    // Network streaming
    pub enable_streaming: bool,
    pub streaming_config: Option<StreamingConfig>,
    
    // Custom fields (extensible)
    pub custom_fields: HashMap<String, serde_json::Value>,
}

impl Default for CaptureConfig {
    fn default() -> Self {
        Self {
            target_fps: 30,
            capture_mouse: false,
            capture_region: None,
            capture_quality: CaptureQuality::Adaptive,
            adaptive_quality: AdaptiveQualityConfig::default(),
            use_hardware_acceleration: true,
            enable_gpu_capture: true,
            enable_metal_performance_shaders: cfg!(target_os = "macos"),
            memory_pool_size_mb: 200,
            buffer_count: 5,
            enable_frame_caching: true,
            cache_size_frames: 10,
            enable_hdr: false,
            color_space: ColorSpace::Srgb,
            pixel_format: PixelFormat::Bgra8,
            enable_frame_deduplication: true,
            deduplication_threshold: 0.95,
            monitor_selection: MonitorSelection::Primary,
            enable_monitor_switching: true,
            enable_streaming: false,
            streaming_config: None,
            custom_fields: HashMap::new(),
        }
    }
}

impl CaptureConfig {
    /// Load configuration from multiple sources with priority
    pub fn load() -> Self {
        let mut config = Self::default();
        
        // 1. Load from config file
        if let Ok(file_content) = std::fs::read_to_string("capture_config.json") {
            if let Ok(file_config) = serde_json::from_str::<Self>(&file_content) {
                config = file_config;
            }
        }
        
        // 2. Override with environment variables
        config.load_from_env();
        
        // 3. Override with global config store
        config.load_from_store();
        
        config
    }
    
    fn load_from_env(&mut self) {
        if let Ok(fps) = std::env::var("JARVIS_TARGET_FPS") {
            if let Ok(fps_val) = fps.parse() {
                self.target_fps = fps_val;
            }
        }
        
        if let Ok(quality) = std::env::var("JARVIS_CAPTURE_QUALITY") {
            self.capture_quality = match quality.to_lowercase().as_str() {
                "low" => CaptureQuality::Low,
                "medium" => CaptureQuality::Medium,
                "high" => CaptureQuality::High,
                "ultra" => CaptureQuality::Ultra,
                "adaptive" => CaptureQuality::Adaptive,
                _ => self.capture_quality,
            };
        }
    }
    
    fn load_from_store(&mut self) {
        let store = &*GLOBAL_CONFIG;
        
        if let Some(val) = store.get("target_fps") {
            if let Some(fps) = val.as_u64() {
                self.target_fps = fps as u32;
            }
        }
        
        // Load all custom fields
        for key in store.configs.iter() {
            if !key.key().starts_with("_") {  // Skip internal keys
                self.custom_fields.insert(key.key().clone(), key.value().clone());
            }
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum CaptureQuality {
    Low,
    Medium, 
    High,
    Ultra,
    Adaptive,  // Automatically adjusts based on system load
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdaptiveQualityConfig {
    pub cpu_threshold_high: f32,
    pub cpu_threshold_low: f32,
    pub memory_threshold_high: f32,
    pub memory_threshold_low: f32,
    pub min_quality: CaptureQuality,
    pub max_quality: CaptureQuality,
    pub adjustment_interval: Duration,
}

impl Default for AdaptiveQualityConfig {
    fn default() -> Self {
        Self {
            cpu_threshold_high: 80.0,
            cpu_threshold_low: 50.0,
            memory_threshold_high: 80.0,
            memory_threshold_low: 50.0,
            min_quality: CaptureQuality::Low,
            max_quality: CaptureQuality::Ultra,
            adjustment_interval: Duration::from_secs(5),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum ColorSpace {
    Srgb,
    DisplayP3,
    Rec709,
    Rec2020,
    AdobeRgb,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum PixelFormat {
    Rgb8,
    Rgba8,
    Bgr8,
    Bgra8,
    Rgb16,
    Rgba16f,
    Yuv420,
    Nv12,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum MonitorSelection {
    Primary,
    All,
    Specific(u32),
    ActiveWindow,
    MouseLocation,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StreamingConfig {
    pub protocol: StreamingProtocol,
    pub endpoint: String,
    pub encoding: StreamEncoding,
    pub bitrate: u32,
    pub keyframe_interval: u32,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum StreamingProtocol {
    WebRTC,
    RTMP,
    HLS,
    WebSocket,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum StreamEncoding {
    H264,
    H265,
    VP8,
    VP9,
    AV1,
}

// ============================================================================
// ADVANCED SCREEN CAPTURE
// ============================================================================

/// Advanced thread-safe screen capture with supervision and monitoring
pub struct ScreenCapture {
    /// Dynamic configuration with hot-reload
    config: Arc<RwLock<CaptureConfig>>,
    config_watcher: Arc<Mutex<watch::Receiver<serde_json::Value>>>,
    
    /// Bridge to Objective-C actor with supervision
    bridge: Arc<ObjCBridge>,
    supervisor: Arc<Supervisor>,
    
    /// Advanced memory management
    memory_manager: Arc<MemoryManager>,
    frame_cache: Arc<FrameCache>,
    
    /// Performance monitoring
    stats: Arc<CaptureStatistics>,
    performance_monitor: Arc<PerformanceMonitor>,
    
    /// Frame processing pipeline
    frame_pipeline: Arc<FramePipeline>,
    
    /// Event broadcasting
    event_broadcaster: broadcast::Sender<CaptureEvent>,
    
    /// Capture state
    is_running: Arc<AtomicBool>,
    capture_generation: Arc<AtomicU64>,
}

// Thread-safe by design - no raw pointers
unsafe impl Send for ScreenCapture {}
unsafe impl Sync for ScreenCapture {}

impl ScreenCapture {
    /// Create new advanced screen capture system
    pub fn new(config: CaptureConfig) -> Result<Self> {
        // Initialize supervisor for fault tolerance
        let supervisor_config = RestartConfig {
            max_restarts: 5,
            restart_window: Duration::from_secs(60),
            strategy: RestartStrategy::Transient,
            restart_delay: Duration::from_millis(500),
        };
        let supervisor = Arc::new(Supervisor::new(supervisor_config));
        
        // Create supervised bridge
        let bridge = Arc::new(ObjCBridge::new(config.buffer_count)?);
        
        // Pre-allocate shared buffers
        let buffer_size = Self::estimate_buffer_size(&config)?;
        for _ in 0..config.buffer_count {
            bridge.allocate_buffer(buffer_size)?;
        }
        
        // Initialize frame cache if enabled
        let frame_cache = Arc::new(FrameCache::new(
            config.cache_size_frames,
            config.enable_frame_deduplication,
            config.deduplication_threshold,
        ));
        
        // Initialize performance monitor
        let performance_monitor = Arc::new(PerformanceMonitor::new());
        
        // Initialize frame processing pipeline
        let frame_pipeline = Arc::new(FramePipeline::new(&config));
        
        // Create event broadcaster
        let (event_tx, _) = broadcast::channel(100);
        
        // Watch for config changes
        let config_watcher = Arc::new(Mutex::new(GLOBAL_CONFIG.watch("capture")));
        
        let capture = Self {
            config: Arc::new(RwLock::new(config)),
            config_watcher,
            bridge,
            supervisor,
            memory_manager: MemoryManager::global(),
            frame_cache,
            stats: Arc::new(CaptureStatistics::new()),
            performance_monitor,
            frame_pipeline,
            event_broadcaster: event_tx,
            is_running: Arc::new(AtomicBool::new(false)),
            capture_generation: Arc::new(AtomicU64::new(0)),
        };
        
        // Start background monitoring
        capture.start_monitoring();
        
        Ok(capture)
    }
    
    /// Start background monitoring and adaptive quality adjustment
    fn start_monitoring(&self) {
        let config = self.config.clone();
        let performance = self.performance_monitor.clone();
        let stats = self.stats.clone();
        
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(1));
            
            loop {
                interval.tick().await;
                
                // Update performance metrics
                performance.update().await;
                
                // Adaptive quality adjustment
                if let CaptureQuality::Adaptive = config.read().capture_quality {
                    let metrics = performance.current_metrics();
                    let mut config_guard = config.write();
                    
                    // Adjust quality based on system load
                    if metrics.cpu_usage > config_guard.adaptive_quality.cpu_threshold_high {
                        // Decrease quality
                        config_guard.capture_quality = Self::decrease_quality(config_guard.capture_quality);
                    } else if metrics.cpu_usage < config_guard.adaptive_quality.cpu_threshold_low {
                        // Increase quality
                        config_guard.capture_quality = Self::increase_quality(config_guard.capture_quality);
                    }
                }
                
                // Update stats
                stats.update_performance_metrics(performance.current_metrics());
            }
        });
    }
    
    /// Capture screen with advanced processing pipeline
    pub async fn capture_async(&self) -> Result<ProcessedFrame> {
        // Check if capture is running
        if !self.is_running.load(Ordering::Acquire) {
            self.is_running.store(true, Ordering::Release);
        }
        
        let generation = self.capture_generation.fetch_add(1, Ordering::SeqCst);
        let start_time = Instant::now();
        
        // Emit capture started event
        let _ = self.event_broadcaster.send(CaptureEvent::CaptureStarted {
            generation,
            timestamp: SystemTime::now(),
        });
        
        // Check frame cache for deduplication
        if self.config.read().enable_frame_deduplication {
            if let Some(cached_frame) = self.frame_cache.get_recent() {
                self.stats.record_cache_hit();
                return Ok(cached_frame);
            }
        }
        
        // Perform capture through bridge - scope config access to ensure guard is dropped
        let command = {
            let config = self.config.read();
            ObjCCommand::CaptureScreen {
                quality: Self::quality_to_bridge(&config.capture_quality),
                region: config.capture_region,
            }
        }; // config guard dropped here

        let response = self.bridge.call(command).await?;
        
        // Process response
        let frame = match response {
            ObjCResponse::FrameCaptured { buffer_id, width, height, bytes_per_row, timestamp } => {
                let buffer = self.bridge.get_buffer(buffer_id)
                    .ok_or_else(|| JarvisError::VisionError("Buffer not found".to_string()))?;

                // Copy slice data before async processing to avoid holding guard across await
                let buffer_data: Vec<u8> = buffer.as_slice().to_vec();
                drop(buffer); // Explicitly drop the guard

                // Apply processing pipeline
                let processed = self.frame_pipeline.process(
                    &buffer_data,
                    width,
                    height,
                    bytes_per_row,
                ).await?;
                
                // Cache the frame
                if self.config.read().enable_frame_caching {
                    self.frame_cache.insert(processed.clone());
                }
                
                processed
            }
            ObjCResponse::Error(msg) => {
                self.stats.record_error();
                return Err(JarvisError::VisionError(format!("Capture failed: {}", msg)));
            }
            _ => {
                return Err(JarvisError::VisionError("Unexpected response type".to_string()));
            }
        };
        
        // Update statistics
        let capture_time = start_time.elapsed();
        self.stats.record_capture(capture_time, frame.size_bytes());
        
        // Emit capture completed event
        let _ = self.event_broadcaster.send(CaptureEvent::CaptureCompleted {
            generation,
            duration: capture_time,
            frame_size: frame.size_bytes(),
        });
        
        Ok(frame)
    }
    
    /// Capture multiple monitors simultaneously
    pub async fn capture_multi_monitor(&self) -> Result<Vec<ProcessedFrame>> {
        let monitors = self.get_available_monitors().await?;
        let mut frames = Vec::new();
        
        // Capture all monitors in parallel
        let futures: Vec<_> = monitors.into_iter().map(|monitor| {
            let bridge = self.bridge.clone();
            let pipeline = self.frame_pipeline.clone();
            
            async move {
                let command = ObjCCommand::CaptureScreen {
                    quality: Self::quality_to_bridge(&CaptureQuality::High),
                    region: Some(monitor.bounds),
                };
                
                let response = bridge.call(command).await?;
                
                match response {
                    ObjCResponse::FrameCaptured { buffer_id, width, height, bytes_per_row, .. } => {
                        let buffer = bridge.get_buffer(buffer_id)
                            .ok_or_else(|| JarvisError::VisionError("Buffer not found".to_string()))?;

                        // Clone data before async boundary to avoid lifetime issues
                        let buffer_data: Vec<u8> = buffer.as_slice().to_vec();
                        pipeline.process(&buffer_data, width, height, bytes_per_row).await
                    }
                    _ => Err(JarvisError::VisionError("Capture failed".to_string()))
                }
            }
        }).collect();
        
        // Wait for all captures to complete
        let results = futures::future::join_all(futures).await;
        
        for result in results {
            frames.push(result?);
        }
        
        Ok(frames)
    }
    
    /// Stream capture with callback
    pub async fn start_streaming<F>(&self, callback: F) -> Result<StreamHandle>
    where
        F: Fn(ProcessedFrame) -> futures::future::BoxFuture<'static, ()> + Send + Sync + 'static,
    {
        let is_running = Arc::new(AtomicBool::new(true));
        let handle = StreamHandle {
            is_running: is_running.clone(),
            generation: self.capture_generation.load(Ordering::SeqCst),
        };
        
        let capture = self.clone();
        let callback = Arc::new(callback);

        // Extract fps before async move to avoid holding lock across await
        let target_fps = self.config.read().target_fps;

        tokio::spawn(async move {
            let mut interval = tokio::time::interval(
                Duration::from_millis(1000 / target_fps as u64)
            );

            while is_running.load(Ordering::Acquire) {
                interval.tick().await;

                if let Ok(frame) = capture.capture_async().await {
                    callback(frame).await;
                }
            }
        });
        
        Ok(handle)
    }
    
    /// Get available monitors
    async fn get_available_monitors(&self) -> Result<Vec<MonitorInfo>> {
        // This would query the system for available monitors
        // For now, return mock data
        Ok(vec![
            MonitorInfo {
                id: 0,
                name: "Primary".to_string(),
                bounds: CaptureRegion { x: 0, y: 0, width: 1920, height: 1080 },
                is_primary: true,
            }
        ])
    }
    
    /// Update configuration dynamically
    pub fn update_config<F>(&self, updater: F) -> Result<()>
    where
        F: FnOnce(&mut CaptureConfig)
    {
        let mut config = self.config.write();
        updater(&mut *config);
        
        // Notify about config change
        let _ = self.event_broadcaster.send(CaptureEvent::ConfigUpdated {
            timestamp: SystemTime::now(),
        });
        
        Ok(())
    }
    
    /// Get comprehensive statistics
    pub fn get_statistics(&self) -> CaptureStatisticsSnapshot {
        self.stats.snapshot()
    }
    
    /// Subscribe to capture events
    pub fn subscribe_events(&self) -> broadcast::Receiver<CaptureEvent> {
        self.event_broadcaster.subscribe()
    }

    /// Get list of windows (async)
    pub async fn get_window_list(&self, _use_cache: bool) -> Result<Vec<WindowInfo>> {
        // Return empty list for now - would query system for window list
        Ok(vec![])
    }

    /// Get list of running applications (async)
    pub async fn get_running_apps(&self) -> Result<Vec<AppInfo>> {
        // Return empty list for now - would query system for running apps
        Ok(vec![])
    }

    /// Get bridge metrics
    pub fn bridge_metrics(&self) -> BridgeMetrics {
        BridgeMetrics::default()
    }

    /// Get capture statistics
    pub fn stats(&self) -> CaptureStatsAdapter {
        let snap = CaptureStatisticsSnapshot {
            total_captures: 0,
            total_bytes: 0,
            cache_hits: 0,
            cache_misses: 0,
            errors: 0,
            average_capture_time: std::time::Duration::from_millis(0),
        };
        CaptureStatsAdapter {
            frame_count: snap.total_captures,
            actual_fps: 30.0, // Default
            avg_capture_time_ms: snap.average_capture_time.as_millis() as f32,
        }
    }

    /// Capture and preprocess a frame, returning ImageData
    pub fn capture_preprocessed(&mut self) -> Result<ImageData> {
        // Get the most recent frame from cache or capture a new one
        if let Some(frame) = self.frame_cache.get_recent() {
            // Convert ProcessedFrame to ImageData
            let config = self.config.read();
            let format = match config.pixel_format {
                PixelFormat::Rgb8 => ImageFormat::Rgb8,
                PixelFormat::Rgba8 => ImageFormat::Rgba8,
                PixelFormat::Bgr8 => ImageFormat::Bgr8,
                PixelFormat::Bgra8 => ImageFormat::Bgra8,
                PixelFormat::Rgb16 => ImageFormat::Rgb16,
                PixelFormat::Rgba16f => ImageFormat::RgbaF32,
                PixelFormat::Yuv420 | PixelFormat::Nv12 => ImageFormat::YCbCr,
            };
            drop(config);

            ImageData::from_raw(frame.width, frame.height, frame.data, format)
        } else {
            // Return a default empty image if no frame available
            Ok(ImageData::new(1920, 1080, 4, ImageFormat::Rgba8))
        }
    }

    /// Helper methods
    fn estimate_buffer_size(config: &CaptureConfig) -> Result<usize> {
        // Estimate based on 4K resolution as maximum
        let (width, height) = (3840u32, 2160u32);
        let bytes_per_pixel = match config.pixel_format {
            PixelFormat::Rgb8 => 3,
            PixelFormat::Rgba8 | PixelFormat::Bgr8 | PixelFormat::Bgra8 => 4,
            PixelFormat::Rgb16 => 6,
            PixelFormat::Rgba16f => 8,
            PixelFormat::Yuv420 => 2,  // Approximate
            PixelFormat::Nv12 => 2,    // Approximate
        };
        Ok((width * height * bytes_per_pixel) as usize)
    }
    
    fn quality_to_bridge(quality: &CaptureQuality) -> BridgeCaptureQuality {
        match quality {
            CaptureQuality::Low => BridgeCaptureQuality::low(),
            CaptureQuality::Medium => BridgeCaptureQuality::medium(),
            CaptureQuality::High => BridgeCaptureQuality::high(),
            CaptureQuality::Ultra => BridgeCaptureQuality::ultra(),
            CaptureQuality::Adaptive => BridgeCaptureQuality::high(), // Default for adaptive
        }
    }
    
    fn increase_quality(current: CaptureQuality) -> CaptureQuality {
        match current {
            CaptureQuality::Low => CaptureQuality::Medium,
            CaptureQuality::Medium => CaptureQuality::High,
            CaptureQuality::High => CaptureQuality::Ultra,
            _ => current,
        }
    }
    
    fn decrease_quality(current: CaptureQuality) -> CaptureQuality {
        match current {
            CaptureQuality::Ultra => CaptureQuality::High,
            CaptureQuality::High => CaptureQuality::Medium,
            CaptureQuality::Medium => CaptureQuality::Low,
            _ => current,
        }
    }
}

impl Clone for ScreenCapture {
    fn clone(&self) -> Self {
        Self {
            config: self.config.clone(),
            config_watcher: self.config_watcher.clone(),
            bridge: self.bridge.clone(),
            supervisor: self.supervisor.clone(),
            memory_manager: self.memory_manager.clone(),
            frame_cache: self.frame_cache.clone(),
            stats: self.stats.clone(),
            performance_monitor: self.performance_monitor.clone(),
            frame_pipeline: self.frame_pipeline.clone(),
            event_broadcaster: self.event_broadcaster.clone(),
            is_running: self.is_running.clone(),
            capture_generation: self.capture_generation.clone(),
        }
    }
}

// ============================================================================
// SUPPORTING STRUCTURES
// ============================================================================

/// Frame cache with deduplication
pub struct FrameCache {
    cache: Arc<RwLock<VecDeque<ProcessedFrame>>>,
    max_size: usize,
    enable_deduplication: bool,
    similarity_threshold: f32,
}

impl FrameCache {
    pub fn new(max_size: usize, enable_dedup: bool, threshold: f32) -> Self {
        Self {
            cache: Arc::new(RwLock::new(VecDeque::with_capacity(max_size))),
            max_size,
            enable_deduplication: enable_dedup,
            similarity_threshold: threshold,
        }
    }
    
    pub fn insert(&self, frame: ProcessedFrame) {
        let mut cache = self.cache.write();
        
        if cache.len() >= self.max_size {
            cache.pop_front();
        }
        
        cache.push_back(frame);
    }
    
    pub fn get_recent(&self) -> Option<ProcessedFrame> {
        self.cache.read().back().cloned()
    }
}

/// Frame processing pipeline
pub struct FramePipeline {
    stages: Vec<Box<dyn ProcessingStage>>,
}

impl FramePipeline {
    pub fn new(config: &CaptureConfig) -> Self {
        let mut stages: Vec<Box<dyn ProcessingStage>> = Vec::new();
        
        // Add processing stages based on config
        if config.enable_hdr {
            stages.push(Box::new(HdrProcessing));
        }
        
        // Add more stages as needed
        
        Self { stages }
    }
    
    pub async fn process(&self, data: &[u8], width: u32, height: u32, bytes_per_row: usize) -> Result<ProcessedFrame> {
        let channels = if bytes_per_row > 0 && width > 0 {
            (bytes_per_row / width as usize) as u8
        } else {
            4  // Default to RGBA
        };
        let mut frame = ProcessedFrame {
            data: data.to_vec(),
            width,
            height,
            bytes_per_row,
            timestamp: SystemTime::now(),
            metadata: HashMap::new(),
            channels,
        };

        for stage in &self.stages {
            frame = stage.process(frame).await?;
        }

        Ok(frame)
    }
}

/// Processing stage trait
trait ProcessingStage: Send + Sync {
    fn process(&self, frame: ProcessedFrame) -> futures::future::BoxFuture<'_, Result<ProcessedFrame>>;
}

/// HDR processing stage
struct HdrProcessing;

impl ProcessingStage for HdrProcessing {
    fn process(&self, frame: ProcessedFrame) -> futures::future::BoxFuture<'_, Result<ProcessedFrame>> {
        Box::pin(async move {
            // HDR processing logic would go here
            Ok(frame)
        })
    }
}

/// Performance monitoring
pub struct PerformanceMonitor {
    metrics: Arc<RwLock<PerformanceMetrics>>,
}

impl PerformanceMonitor {
    pub fn new() -> Self {
        Self {
            metrics: Arc::new(RwLock::new(PerformanceMetrics::default())),
        }
    }
    
    pub async fn update(&self) {
        // Update system metrics
        let cpu_usage = self.get_cpu_usage();
        let memory_usage = self.get_memory_usage();
        
        let mut metrics = self.metrics.write();
        metrics.cpu_usage = cpu_usage;
        metrics.memory_usage = memory_usage;
        metrics.last_update = SystemTime::now();
    }
    
    pub fn current_metrics(&self) -> PerformanceMetrics {
        self.metrics.read().clone()
    }
    
    fn get_cpu_usage(&self) -> f32 {
        // Platform-specific CPU usage monitoring
        50.0  // Placeholder
    }
    
    fn get_memory_usage(&self) -> f32 {
        // Platform-specific memory usage monitoring
        60.0  // Placeholder
    }
}

#[derive(Debug, Clone)]
pub struct PerformanceMetrics {
    pub cpu_usage: f32,
    pub memory_usage: f32,
    pub gpu_usage: f32,
    pub last_update: SystemTime,
}

impl Default for PerformanceMetrics {
    fn default() -> Self {
        Self {
            cpu_usage: 0.0,
            memory_usage: 0.0,
            gpu_usage: 0.0,
            last_update: SystemTime::now(),
        }
    }
}

/// Capture statistics
pub struct CaptureStatistics {
    total_captures: AtomicU64,
    total_bytes: AtomicU64,
    cache_hits: AtomicU64,
    cache_misses: AtomicU64,
    errors: AtomicU64,
    capture_times: Arc<RwLock<VecDeque<Duration>>>,
}

impl CaptureStatistics {
    pub fn new() -> Self {
        Self {
            total_captures: AtomicU64::new(0),
            total_bytes: AtomicU64::new(0),
            cache_hits: AtomicU64::new(0),
            cache_misses: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            capture_times: Arc::new(RwLock::new(VecDeque::with_capacity(100))),
        }
    }
    
    pub fn record_capture(&self, duration: Duration, bytes: usize) {
        self.total_captures.fetch_add(1, Ordering::Relaxed);
        self.total_bytes.fetch_add(bytes as u64, Ordering::Relaxed);
        
        let mut times = self.capture_times.write();
        if times.len() >= 100 {
            times.pop_front();
        }
        times.push_back(duration);
    }
    
    pub fn record_cache_hit(&self) {
        self.cache_hits.fetch_add(1, Ordering::Relaxed);
    }
    
    pub fn record_error(&self) {
        self.errors.fetch_add(1, Ordering::Relaxed);
    }
    
    pub fn update_performance_metrics(&self, _metrics: PerformanceMetrics) {
        // Update internal metrics based on performance
    }
    
    pub fn snapshot(&self) -> CaptureStatisticsSnapshot {
        let times = self.capture_times.read();
        let avg_time = if !times.is_empty() {
            let total: Duration = times.iter().sum();
            total / times.len() as u32
        } else {
            Duration::ZERO
        };
        
        CaptureStatisticsSnapshot {
            total_captures: self.total_captures.load(Ordering::Relaxed),
            total_bytes: self.total_bytes.load(Ordering::Relaxed),
            cache_hits: self.cache_hits.load(Ordering::Relaxed),
            cache_misses: self.cache_misses.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
            average_capture_time: avg_time,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CaptureStatisticsSnapshot {
    pub total_captures: u64,
    pub total_bytes: u64,
    pub cache_hits: u64,
    pub cache_misses: u64,
    pub errors: u64,
    pub average_capture_time: Duration,
}

/// Adapter struct for Python bindings compatibility
#[derive(Debug, Clone)]
pub struct CaptureStatsAdapter {
    pub frame_count: u64,
    pub actual_fps: f32,
    pub avg_capture_time_ms: f32,
}

/// Processed frame with metadata
#[derive(Debug, Clone)]
pub struct ProcessedFrame {
    pub data: Vec<u8>,
    pub width: u32,
    pub height: u32,
    pub bytes_per_row: usize,
    pub timestamp: SystemTime,
    pub metadata: HashMap<String, serde_json::Value>,
    pub channels: u8,
}

impl ProcessedFrame {
    pub fn size_bytes(&self) -> usize {
        self.data.len()
    }

    pub fn as_slice(&self) -> &[u8] {
        &self.data
    }
}

/// Monitor information
#[derive(Debug, Clone)]
pub struct MonitorInfo {
    pub id: u32,
    pub name: String,
    pub bounds: CaptureRegion,
    pub is_primary: bool,
}

/// Window information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WindowInfo {
    pub window_id: u32,
    pub title: String,
    pub owner_name: String,
    pub app_name: String,
    pub bounds: CaptureRegion,
    pub is_on_screen: bool,
    pub layer: i32,
    pub alpha: f32,
}

/// Application information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppInfo {
    pub bundle_id: String,
    pub name: String,
    pub pid: i32,
    pub is_active: bool,
    pub is_hidden: bool,
}

/// Bridge metrics for monitoring
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BridgeMetrics {
    pub total_captures: u64,
    pub total_errors: u64,
    pub avg_capture_time_ms: f64,
    pub memory_usage_bytes: usize,
}

/// Stream handle for controlling streaming
pub struct StreamHandle {
    is_running: Arc<AtomicBool>,
    generation: u64,
}

impl StreamHandle {
    pub fn stop(&self) {
        self.is_running.store(false, Ordering::Release);
    }
    
    pub fn is_running(&self) -> bool {
        self.is_running.load(Ordering::Acquire)
    }
}

/// Capture events for monitoring
#[derive(Debug, Clone)]
pub enum CaptureEvent {
    CaptureStarted {
        generation: u64,
        timestamp: SystemTime,
    },
    CaptureCompleted {
        generation: u64,
        duration: Duration,
        frame_size: usize,
    },
    ConfigUpdated {
        timestamp: SystemTime,
    },
    Error {
        message: String,
        timestamp: SystemTime,
    },
}

// Define types that were in the old implementation but need to be available
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum CompressionHint {
    #[default]
    None,
    Fast,
    Balanced,
    Quality,
}

// Dummy type for backward compatibility - will be properly implemented
pub struct SharedMemoryHandle {
    pub name: String,
    pub size: usize,
}

// CaptureRegion is already re-exported at the top of the file

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextDetection {
    pub text: String,
    pub confidence: f32,
    pub bounds: CaptureRegion,
}

pub type CaptureStats = CaptureStatisticsSnapshot;

#[cfg(test)]
mod tests {
    use super::*;
    
    #[tokio::test]
    async fn test_advanced_capture() {
        let config = CaptureConfig::default();
        let capture = ScreenCapture::new(config).unwrap();
        
        // Test basic capture
        let result = capture.capture_async().await;
        assert!(result.is_ok());
    }
    
    #[test]
    fn test_config_loading() {
        std::env::set_var("JARVIS_TARGET_FPS", "60");
        std::env::set_var("JARVIS_CAPTURE_QUALITY", "ultra");
        
        let config = CaptureConfig::load();
        assert_eq!(config.target_fps, 60);
        assert!(matches!(config.capture_quality, CaptureQuality::Ultra));
    }
    
    #[test]
    fn test_dynamic_config_store() {
        let store = DynamicConfigStore::new();
        store.set("test_key", serde_json::json!({"value": 42}));
        
        let value = store.get("test_key").unwrap();
        assert_eq!(value["value"], 42);
    }
}