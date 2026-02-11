//! Refactored Metal GPU acceleration using thread-safe message passing
//! 
//! This module replaces direct Metal object ownership with message passing
//! to solve thread-safety issues.

#[cfg(target_os = "macos")]
use crate::{Result, JarvisError};
use crate::bridge::{ObjCBridge, ObjCCommand, ObjCResponse};
use std::sync::Arc;
use parking_lot::RwLock;
use ndarray::{Array3, ArrayView3};
use std::collections::HashMap;
use std::time::Instant;

// ============================================================================
// REFACTORED METAL ACCELERATOR
// ============================================================================

/// Thread-safe Metal compute pipeline using message passing
#[cfg(target_os = "macos")]
pub struct MetalAccelerator {
    /// Bridge to Objective-C actor (which owns Metal objects)
    bridge: Arc<ObjCBridge>,
    
    /// Performance statistics
    performance_stats: Arc<RwLock<PerformanceStats>>,
    
    /// Shader configuration
    shader_config: Arc<RwLock<HashMap<String, ShaderConfig>>>,
}

// All fields are Arc-wrapped, so Clone is cheap and correct.
#[cfg(target_os = "macos")]
impl Clone for MetalAccelerator {
    fn clone(&self) -> Self {
        Self {
            bridge: Arc::clone(&self.bridge),
            performance_stats: Arc::clone(&self.performance_stats),
            shader_config: Arc::clone(&self.shader_config),
        }
    }
}

// Mark as thread-safe (no raw Metal pointers!)
#[cfg(target_os = "macos")]
unsafe impl Send for MetalAccelerator {}
#[cfg(target_os = "macos")]
unsafe impl Sync for MetalAccelerator {}

#[derive(Debug, Clone)]
pub struct ShaderConfig {
    pub name: String,
    pub thread_group_size: (u32, u32, u32),
    pub parameters: HashMap<String, f32>,
}

#[derive(Debug, Default, Clone)]
pub struct PerformanceStats {
    pub total_frames_processed: u64,
    pub total_compute_time_ms: f64,
    pub average_frame_time_ms: f64,
    pub gpu_memory_used_mb: f64,
}

#[cfg(target_os = "macos")]
impl MetalAccelerator {
    /// Create new thread-safe Metal accelerator
    pub fn new(bridge: Arc<ObjCBridge>) -> Result<Self> {
        let mut shader_config = HashMap::new();
        
        // Configure default shaders
        let shader_names = vec![
            "frame_difference",
            "motion_detection",
            "edge_detection",
            "color_analysis",
            "feature_extraction",
        ];
        
        for name in shader_names {
            shader_config.insert(name.to_string(), ShaderConfig {
                name: name.to_string(),
                thread_group_size: (32, 32, 1),
                parameters: HashMap::new(),
            });
        }
        
        Ok(Self {
            bridge,
            performance_stats: Arc::new(RwLock::new(PerformanceStats::default())),
            shader_config: Arc::new(RwLock::new(shader_config)),
        })
    }
    
    /// Process frame with Metal shader (thread-safe)
    pub async fn process_frame(
        &self,
        input_data: &[u8],
        shader_name: &str,
        width: u32,
        height: u32,
    ) -> Result<Vec<u8>> {
        let start = Instant::now();
        
        // Allocate shared buffer and copy input data
        let buffer_size = input_data.len();
        let buffer_id = self.bridge.allocate_buffer(buffer_size)?;
        
        // Copy data to shared buffer (this would be done through shared memory)
        if let Some(mut buffer) = self.bridge.get_buffer(buffer_id) {
            let buffer_mut = Arc::get_mut(&mut buffer)
                .ok_or_else(|| JarvisError::VisionError("Cannot get mutable buffer".to_string()))?;
            buffer_mut.as_mut_slice().copy_from_slice(input_data);
        }
        
        // Send Metal processing command
        let command = ObjCCommand::ProcessWithMetal {
            buffer_id,
            shader_name: shader_name.to_string(),
        };
        
        let response = self.bridge.call(command).await?;
        
        match response {
            ObjCResponse::MetalProcessingComplete { buffer_id: result_id, elapsed_ms } => {
                // Get processed data from shared buffer
                let buffer = self.bridge.get_buffer(result_id)
                    .ok_or_else(|| JarvisError::VisionError("Result buffer not found".to_string()))?;
                
                let result = buffer.as_slice().to_vec();
                
                // Update stats
                let mut stats = self.performance_stats.write();
                stats.total_frames_processed += 1;
                stats.total_compute_time_ms += elapsed_ms as f64;
                stats.average_frame_time_ms = 
                    stats.total_compute_time_ms / stats.total_frames_processed as f64;
                
                Ok(result)
            }
            ObjCResponse::Error(msg) => {
                Err(JarvisError::VisionError(format!("Metal processing failed: {}", msg)))
            }
            _ => {
                Err(JarvisError::VisionError("Unexpected response type".to_string()))
            }
        }
    }
    
    /// Compute frame difference using Metal (thread-safe)
    pub async fn frame_difference<'a>(
        &self,
        frame1: ArrayView3<'a, u8>,
        frame2: ArrayView3<'a, u8>,
    ) -> Result<Array3<f32>> {
        let (height, width, channels) = frame1.dim();
        
        // Flatten frames for GPU processing
        let frame1_flat: Vec<u8> = frame1.iter().cloned().collect();
        let frame2_flat: Vec<u8> = frame2.iter().cloned().collect();
        
        // Concatenate frames for single buffer transfer
        let mut combined = frame1_flat;
        combined.extend(frame2_flat);
        
        // Process with Metal
        let result = self.process_frame(
            &combined,
            "frame_difference",
            width as u32,
            height as u32,
        ).await?;
        
        // Convert result back to ndarray
        let result_f32: Vec<f32> = result.chunks(4)
            .map(|chunk| f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]))
            .collect();
        
        Array3::from_shape_vec((height, width, channels), result_f32)
            .map_err(|e| JarvisError::VisionError(format!("Shape error: {}", e)))
    }
    
    /// Detect motion using Metal (thread-safe)
    pub async fn detect_motion<'a>(
        &self,
        frame: ArrayView3<'a, u8>,
        threshold: f32,
    ) -> Result<MotionMask> {
        let (height, width, _) = frame.dim();
        
        // Update shader parameters
        {
            let mut config = self.shader_config.write();
            if let Some(shader) = config.get_mut("motion_detection") {
                shader.parameters.insert("threshold".to_string(), threshold);
            }
        }
        
        // Process frame
        let frame_data: Vec<u8> = frame.iter().cloned().collect();
        let result = self.process_frame(
            &frame_data,
            "motion_detection",
            width as u32,
            height as u32,
        ).await?;
        
        Ok(MotionMask {
            width: width as u32,
            height: height as u32,
            mask: result,
            motion_pixels: 0, // Would be calculated by shader
        })
    }
    
    /// Edge detection using Metal (thread-safe)
    pub async fn detect_edges<'a>(
        &self,
        frame: ArrayView3<'a, u8>,
        low_threshold: f32,
        high_threshold: f32,
    ) -> Result<EdgeMap> {
        let (height, width, _) = frame.dim();
        
        // Update shader parameters
        {
            let mut config = self.shader_config.write();
            if let Some(shader) = config.get_mut("edge_detection") {
                shader.parameters.insert("low_threshold".to_string(), low_threshold);
                shader.parameters.insert("high_threshold".to_string(), high_threshold);
            }
        }
        
        // Process frame
        let frame_data: Vec<u8> = frame.iter().cloned().collect();
        let result = self.process_frame(
            &frame_data,
            "edge_detection",
            width as u32,
            height as u32,
        ).await?;
        
        Ok(EdgeMap {
            width: width as u32,
            height: height as u32,
            edges: result,
            edge_count: 0, // Would be calculated by shader
        })
    }
    
    /// Get performance statistics
    pub fn stats(&self) -> PerformanceStats {
        (*self.performance_stats.read()).clone()
    }
    
    /// Reset performance statistics
    pub fn reset_stats(&self) {
        let mut stats = self.performance_stats.write();
        *stats = PerformanceStats::default();
    }
}

// ============================================================================
// RESULT TYPES
// ============================================================================

#[derive(Debug, Clone)]
pub struct MotionMask {
    pub width: u32,
    pub height: u32,
    pub mask: Vec<u8>,
    pub motion_pixels: u32,
}

#[derive(Debug, Clone)]
pub struct EdgeMap {
    pub width: u32,
    pub height: u32,
    pub edges: Vec<u8>,
    pub edge_count: u32,
}

#[derive(Debug, Clone)]
pub struct ColorHistogram {
    pub red: Vec<f32>,
    pub green: Vec<f32>,
    pub blue: Vec<f32>,
    pub luminance: Vec<f32>,
}

// ============================================================================
// NON-MACOS FALLBACK
// ============================================================================

#[cfg(not(target_os = "macos"))]
pub struct MetalAccelerator;

#[cfg(not(target_os = "macos"))]
impl MetalAccelerator {
    pub fn new(_bridge: Arc<ObjCBridge>) -> Result<Self> {
        Err(JarvisError::VisionError("Metal acceleration only available on macOS".to_string()))
    }
}

// ============================================================================
// TESTS
// ============================================================================

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use ndarray::Array3;
    
    #[tokio::test]
    async fn test_thread_safe_metal() {
        let bridge = Arc::new(ObjCBridge::new(3).unwrap());
        let accelerator = MetalAccelerator::new(bridge).unwrap();
        
        // Create test frame
        let frame = Array3::<u8>::zeros((100, 100, 4));
        
        // Spawn multiple tasks to test thread safety
        let handles: Vec<_> = (0..5)
            .map(|_| {
                let accel_clone = accelerator.clone();
                let frame_clone = frame.clone();
                tokio::spawn(async move {
                    accel_clone.detect_motion(frame_clone.view(), 0.5).await
                })
            })
            .collect();
        
        // All should complete without panic
        for handle in handles {
            assert!(handle.await.is_ok());
        }
    }
    
    #[tokio::test]
    async fn test_performance_stats() {
        let bridge = Arc::new(ObjCBridge::new(3).unwrap());
        let accelerator = MetalAccelerator::new(bridge).unwrap();
        
        let stats = accelerator.stats();
        assert_eq!(stats.total_frames_processed, 0);
        
        // Process a frame (would update stats)
        let data = vec![0u8; 1024];
        let _ = accelerator.process_frame(&data, "test_shader", 32, 32).await;
        
        let stats = accelerator.stats();
        assert!(stats.total_frames_processed > 0);
    }
}