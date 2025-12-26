//! Objective-C Bridge - Thread-safe message passing architecture
//! 
//! This module implements the architectural solution for bridging Rust's async runtime
//! with macOS Objective-C APIs, solving thread-safety compilation errors.

use crate::{Result, JarvisError};
use std::sync::Arc;
use std::time::{Duration, Instant};
use parking_lot::RwLock;
use flume::{bounded, Sender, Receiver};
use serde::{Deserialize, Serialize};
use std::thread;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

// ============================================================================
// COMMAND & RESPONSE TYPES
// ============================================================================

/// Commands sent from Rust to Objective-C actor
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ObjCCommand {
    /// Capture screen with specified quality
    CaptureScreen { 
        quality: CaptureQuality,
        region: Option<CaptureRegion>,
    },
    
    /// Get list of running applications
    GetRunningApps,
    
    /// Get window list with caching
    GetWindowList { 
        use_cache: bool 
    },
    
    /// Monitor system notifications
    MonitorNotification { 
        name: String 
    },
    
    /// Stop monitoring a notification
    StopMonitoring { 
        name: String 
    },
    
    /// Execute Vision framework OCR
    DetectText { 
        buffer_id: u64,
        region: CaptureRegion,
    },
    
    /// Use Metal for GPU acceleration
    ProcessWithMetal {
        buffer_id: u64,
        shader_name: String,
    },
    
    /// Graceful shutdown
    Shutdown,
}

/// Responses from Objective-C actor to Rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ObjCResponse {
    /// Frame captured and stored in shared memory
    FrameCaptured {
        buffer_id: u64,
        timestamp: std::time::SystemTime,
        width: u32,
        height: u32,
        bytes_per_row: usize,
    },
    
    /// List of running applications
    RunningApps(Vec<AppState>),
    
    /// Window list
    WindowList(Vec<WindowInfo>),
    
    /// Notification received
    NotificationReceived(NotificationEvent),
    
    /// Text detection results
    TextDetected(Vec<TextDetection>),
    
    /// Metal processing complete
    MetalProcessingComplete {
        buffer_id: u64,
        elapsed_ms: f32,
    },
    
    /// Error occurred
    Error(String),
    
    /// Acknowledgment
    Ok,
}

// ============================================================================
// DATA STRUCTURES
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureQuality {
    pub scale_factor: f32,
    pub compression_level: u8,
}

impl CaptureQuality {
    pub fn low() -> Self {
        Self { scale_factor: 0.5, compression_level: 9 }
    }
    
    pub fn medium() -> Self {
        Self { scale_factor: 0.75, compression_level: 6 }
    }
    
    pub fn high() -> Self {
        Self { scale_factor: 1.0, compression_level: 3 }
    }
    
    pub fn ultra() -> Self {
        Self { scale_factor: 1.0, compression_level: 0 }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default)]
pub struct CaptureRegion {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

impl CaptureRegion {
    pub fn area(&self) -> u32 {
        self.width * self.height
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppState {
    pub bundle_id: String,
    pub name: String,
    pub pid: i32,
    pub is_active: bool,
    pub is_hidden: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WindowInfo {
    pub window_id: u32,
    pub app_name: String,
    pub title: String,
    pub bounds: CaptureRegion,
    pub layer: i32,
    pub alpha: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationEvent {
    pub name: String,
    pub user_info: std::collections::HashMap<String, String>,
    pub timestamp: std::time::SystemTime,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextDetection {
    pub text: String,
    pub confidence: f32,
    pub bounds: CaptureRegion,
}

// ============================================================================
// BRIDGE METRICS
// ============================================================================

#[derive(Debug, Default)]
pub struct BridgeMetrics {
    pub commands_sent: AtomicUsize,
    pub responses_received: AtomicUsize,
    pub errors: AtomicUsize,
    pub frames_dropped: AtomicUsize,
    pub avg_latency_us: AtomicUsize,
}

impl BridgeMetrics {
    pub fn record_command(&self) {
        self.commands_sent.fetch_add(1, Ordering::Relaxed);
    }
    
    pub fn record_response(&self) {
        self.responses_received.fetch_add(1, Ordering::Relaxed);
    }
    
    pub fn record_error(&self) {
        self.errors.fetch_add(1, Ordering::Relaxed);
    }
    
    pub fn record_frame_drop(&self) {
        self.frames_dropped.fetch_add(1, Ordering::Relaxed);
    }
    
    pub fn update_latency(&self, latency_us: usize) {
        // Simple moving average (could be improved with proper EMA)
        let current = self.avg_latency_us.load(Ordering::Relaxed);
        let new = (current * 9 + latency_us) / 10;
        self.avg_latency_us.store(new, Ordering::Relaxed);
    }
}

// ============================================================================
// OBJECTIVE-C BRIDGE
// ============================================================================

/// Thread-safe bridge between Rust and Objective-C
pub struct ObjCBridge {
    /// Channel for sending commands to ObjC actor
    command_tx: Sender<(ObjCCommand, Instant)>,
    
    /// Channel for receiving responses from ObjC actor
    response_rx: Receiver<ObjCResponse>,
    
    /// Shared memory manager
    shared_memory: Arc<SharedMemoryManager>,
    
    /// Bridge metrics
    metrics: Arc<BridgeMetrics>,
    
    /// Actor thread handle
    actor_handle: Option<thread::JoinHandle<()>>,
    
    /// Shutdown flag
    shutdown: Arc<AtomicBool>,
}

impl ObjCBridge {
    /// Create new bridge with bounded channels
    pub fn new(channel_capacity: usize) -> Result<Self> {
        let (command_tx, command_rx) = bounded::<(ObjCCommand, Instant)>(channel_capacity);
        let (response_tx, response_rx) = bounded::<ObjCResponse>(channel_capacity);
        
        let shared_memory = Arc::new(SharedMemoryManager::new()?);
        let metrics = Arc::new(BridgeMetrics::default());
        let shutdown = Arc::new(AtomicBool::new(false));
        
        // Spawn ObjC actor thread
        let actor_handle = {
            let shared_memory = shared_memory.clone();
            let metrics = metrics.clone();
            let shutdown = shutdown.clone();
            
            thread::Builder::new()
                .name("objc-actor".to_string())
                .spawn(move || {
                    ObjCActor::run(command_rx, response_tx, shared_memory, metrics, shutdown);
                })
                .map_err(|e| JarvisError::BridgeError(format!("Failed to spawn actor: {}", e)))?
        };
        
        Ok(Self {
            command_tx,
            response_rx,
            shared_memory,
            metrics,
            actor_handle: Some(actor_handle),
            shutdown,
        })
    }
    
    /// Send command to ObjC actor (with backpressure)
    pub fn send_command(&self, command: ObjCCommand) -> Result<()> {
        let start = Instant::now();
        
        self.metrics.record_command();
        
        // Try to send with timeout to prevent infinite blocking
        match self.command_tx.send_timeout((command, start), Duration::from_secs(5)) {
            Ok(()) => Ok(()),
            Err(flume::SendTimeoutError::Timeout(_)) => {
                self.metrics.record_error();
                Err(JarvisError::BridgeError("Command channel full (backpressure)".to_string()))
            }
            Err(flume::SendTimeoutError::Disconnected(_)) => {
                self.metrics.record_error();
                Err(JarvisError::BridgeError("Actor disconnected".to_string()))
            }
        }
    }
    
    /// Receive response from ObjC actor
    pub fn receive_response(&self, timeout: Duration) -> Result<ObjCResponse> {
        match self.response_rx.recv_timeout(timeout) {
            Ok(response) => {
                self.metrics.record_response();
                
                if let ObjCResponse::Error(ref msg) = response {
                    self.metrics.record_error();
                }
                
                Ok(response)
            }
            Err(flume::RecvTimeoutError::Timeout) => {
                Err(JarvisError::BridgeError("Response timeout".to_string()))
            }
            Err(flume::RecvTimeoutError::Disconnected) => {
                Err(JarvisError::BridgeError("Actor disconnected".to_string()))
            }
        }
    }
    
    /// Send command and wait for response (convenience method)
    pub async fn call(&self, command: ObjCCommand) -> Result<ObjCResponse> {
        self.send_command(command)?;
        self.receive_response(Duration::from_secs(10))
    }
    
    /// Get shared memory buffer for zero-copy access
    pub fn get_buffer(&self, buffer_id: u64) -> Option<Arc<SharedBuffer>> {
        self.shared_memory.get_buffer(buffer_id)
    }
    
    /// Allocate new shared buffer
    pub fn allocate_buffer(&self, size: usize) -> Result<u64> {
        self.shared_memory.allocate_buffer(size)
    }
    
    /// Get bridge metrics
    pub fn metrics(&self) -> &BridgeMetrics {
        &self.metrics
    }
    
    /// Graceful shutdown
    pub fn shutdown(&mut self) -> Result<()> {
        self.shutdown.store(true, Ordering::SeqCst);
        self.send_command(ObjCCommand::Shutdown)?;
        
        if let Some(handle) = self.actor_handle.take() {
            handle.join()
                .map_err(|_| JarvisError::BridgeError("Actor thread panicked".to_string()))?;
        }
        
        Ok(())
    }
}

impl Drop for ObjCBridge {
    fn drop(&mut self) {
        if !self.shutdown.load(Ordering::SeqCst) {
            let _ = self.shutdown();
        }
    }
}

// ============================================================================
// SHARED MEMORY MANAGER
// ============================================================================

use memmap2::{MmapMut, MmapOptions};
use std::collections::HashMap;
use std::fs::OpenOptions;

/// Manages shared memory buffers for zero-copy transfer
pub struct SharedMemoryManager {
    buffers: Arc<RwLock<HashMap<u64, Arc<SharedBuffer>>>>,
    next_buffer_id: AtomicUsize,
}

impl SharedMemoryManager {
    pub fn new() -> Result<Self> {
        Ok(Self {
            buffers: Arc::new(RwLock::new(HashMap::new())),
            next_buffer_id: AtomicUsize::new(1),
        })
    }
    
    /// Allocate new shared buffer
    pub fn allocate_buffer(&self, size: usize) -> Result<u64> {
        let buffer_id = self.next_buffer_id.fetch_add(1, Ordering::SeqCst) as u64;
        let buffer = SharedBuffer::new(buffer_id, size)?;
        
        self.buffers.write().insert(buffer_id, Arc::new(buffer));
        
        Ok(buffer_id)
    }
    
    /// Get buffer by ID
    pub fn get_buffer(&self, buffer_id: u64) -> Option<Arc<SharedBuffer>> {
        self.buffers.read().get(&buffer_id).cloned()
    }
    
    /// Release buffer
    pub fn release_buffer(&self, buffer_id: u64) {
        self.buffers.write().remove(&buffer_id);
    }
}

/// Thread-safe wrapper for shared memory buffer
pub struct SharedBuffer {
    pub id: u64,
    pub size: usize,
    inner: Arc<RwLock<SharedBufferInner>>,
}

struct SharedBufferInner {
    mmap: MmapMut,
}

impl SharedBuffer {
    fn new(id: u64, size: usize) -> Result<Self> {
        // Create anonymous memory mapping
        let mmap = MmapOptions::new()
            .len(size)
            .map_anon()
            .map_err(|e| JarvisError::MemoryError(format!("Failed to create mmap: {}", e)))?;
        
        Ok(Self {
            id,
            size,
            inner: Arc::new(RwLock::new(SharedBufferInner { mmap })),
        })
    }
    
    /// Get mutable slice to buffer data
    pub fn as_mut_slice(&self) -> parking_lot::MappedRwLockWriteGuard<'_, [u8]> {
        parking_lot::RwLockWriteGuard::map(self.inner.write(), |inner| &mut inner.mmap[..])
    }

    /// Get immutable slice to buffer data
    pub fn as_slice(&self) -> parking_lot::MappedRwLockReadGuard<'_, [u8]> {
        parking_lot::RwLockReadGuard::map(self.inner.read(), |inner| &inner.mmap[..])
    }
}

// ============================================================================
// OBJECTIVE-C ACTOR IMPLEMENTATION
// ============================================================================

// Import the actual implementation
#[path = "objc_actor_impl.rs"]
mod objc_actor_impl;
use objc_actor_impl::ObjCActor;

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_bridge_creation() {
        let bridge = ObjCBridge::new(3);
        assert!(bridge.is_ok());
    }
    
    #[test]
    fn test_send_receive() {
        let mut bridge = ObjCBridge::new(3).unwrap();
        
        bridge.send_command(ObjCCommand::GetRunningApps).unwrap();
        let response = bridge.receive_response(Duration::from_secs(1)).unwrap();
        
        match response {
            ObjCResponse::RunningApps(_) => {}
            _ => panic!("Unexpected response"),
        }
    }
    
    #[test]
    fn test_backpressure() {
        let bridge = ObjCBridge::new(2).unwrap();
        
        // Fill the channel
        for _ in 0..2 {
            bridge.send_command(ObjCCommand::GetRunningApps).unwrap();
        }
        
        // This should timeout due to backpressure
        let result = bridge.send_command(ObjCCommand::GetRunningApps);
        assert!(result.is_err());
    }
}