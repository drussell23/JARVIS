//! Thread-safe Python bindings using refactored components
//!
//! This module provides Python bindings that work with the new message-passing
//! architecture, eliminating thread-safety compilation errors.

#![cfg(feature = "python-bindings")]

use crate::{Result, JarvisError};
use crate::bridge::{ObjCBridge, ObjCCommand};
use crate::vision::capture::{ScreenCapture, CaptureConfig, CaptureQuality};
use crate::vision::metal_accelerator::MetalAccelerator;
use crate::memory::MemoryManager;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyBytes};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use std::sync::Arc;
use parking_lot::RwLock;
use std::collections::HashMap;
use numpy::{PyArray1, PyArray2, PyArray3};
use ndarray::{Array1, Array2, Array3};

// ============================================================================
// THREAD-SAFE SCREEN CAPTURE FOR PYTHON
// ============================================================================

/// Thread-safe screen capture for Python
#[pyclass(name = "ScreenCapture", module = "jarvis_rust_core")]
pub struct PyScreenCapture {
    inner: Arc<ScreenCapture>,
    runtime: Arc<tokio::runtime::Runtime>,
}

// These are safe because ScreenCapture no longer contains raw pointers
unsafe impl Send for PyScreenCapture {}
unsafe impl Sync for PyScreenCapture {}

#[pymethods]
impl PyScreenCapture {
    #[new]
    #[pyo3(signature = (config=None))]
    fn new(config: Option<&PyDict>) -> PyResult<Self> {
        let mut cap_config = CaptureConfig::default();
        
        // Apply Python config if provided
        if let Some(cfg) = config {
            if let Some(fps) = cfg.get_item("target_fps")? {
                if let Ok(fps_val) = fps.extract::<u32>() {
                    cap_config.target_fps = fps_val;
                }
            }
            if let Some(quality) = cfg.get_item("quality")? {
                if let Ok(q) = quality.extract::<String>() {
                    cap_config.capture_quality = match q.as_str() {
                        "low" => CaptureQuality::Low,
                        "medium" => CaptureQuality::Medium,
                        "high" => CaptureQuality::High,
                        "ultra" => CaptureQuality::Ultra,
                        _ => CaptureQuality::High,
                    };
                }
            }
        }
        
        let capture = ScreenCapture::new(cap_config)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        let runtime = tokio::runtime::Runtime::new()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;
        
        Ok(Self {
            inner: Arc::new(capture),
            runtime: Arc::new(runtime),
        })
    }
    
    /// Capture screen to numpy array
    fn capture_to_numpy(&self, py: Python) -> PyResult<Py<PyArray3<u8>>> {
        let capture = self.inner.clone();
        
        // Run async capture in runtime
        let image_data = self.runtime.block_on(async move {
            capture.capture_async().await
        }).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        // Convert to numpy
        let shape = [
            image_data.height as usize,
            image_data.width as usize,
            image_data.channels as usize,
        ];
        let array = unsafe { PyArray3::new(py, shape, false) };
        unsafe {
            array.as_slice_mut()?.copy_from_slice(image_data.as_slice());
        }
        Ok(array.to_owned())
    }
    
    /// Get window list
    fn get_window_list(&self, use_cache: Option<bool>) -> PyResult<Vec<HashMap<String, PyObject>>> {
        let capture = self.inner.clone();
        let use_cache = use_cache.unwrap_or(true);
        
        let windows = self.runtime.block_on(async move {
            capture.get_window_list(use_cache).await
        }).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        Python::with_gil(|py| {
            windows.into_iter().map(|w| {
                let mut map = HashMap::new();
                map.insert("window_id".to_string(), w.window_id.to_object(py));
                map.insert("app_name".to_string(), w.app_name.to_object(py));
                map.insert("title".to_string(), w.title.to_object(py));
                map.insert("layer".to_string(), w.layer.to_object(py));
                map.insert("alpha".to_string(), w.alpha.to_object(py));
                Ok(map)
            }).collect()
        })
    }
    
    /// Get running applications
    fn get_running_apps(&self) -> PyResult<Vec<HashMap<String, PyObject>>> {
        let capture = self.inner.clone();
        
        let apps = self.runtime.block_on(async move {
            capture.get_running_apps().await
        }).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        Python::with_gil(|py| {
            apps.into_iter().map(|a| {
                let mut map = HashMap::new();
                map.insert("bundle_id".to_string(), a.bundle_id.to_object(py));
                map.insert("name".to_string(), a.name.to_object(py));
                map.insert("pid".to_string(), a.pid.to_object(py));
                map.insert("is_active".to_string(), a.is_active.to_object(py));
                map.insert("is_hidden".to_string(), a.is_hidden.to_object(py));
                Ok(map)
            }).collect()
        })
    }
    
    /// Get capture statistics
    fn get_stats(&self) -> PyResult<HashMap<String, PyObject>> {
        let stats = self.inner.stats();
        
        Python::with_gil(|py| {
            let mut map = HashMap::new();
            map.insert("frame_count".to_string(), stats.frame_count.to_object(py));
            map.insert("actual_fps".to_string(), stats.actual_fps.to_object(py));
            map.insert("avg_capture_time_ms".to_string(), stats.avg_capture_time_ms.to_object(py));
            Ok(map)
        })
    }
    
    /// Get bridge metrics
    fn get_bridge_metrics(&self) -> PyResult<String> {
        let metrics = self.inner.bridge_metrics();
        serde_json::to_string(&metrics)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to serialize metrics: {}", e)))
    }
    
    /// Update configuration
    fn update_config(&self, key: &str, value: &str) -> PyResult<()> {
        self.inner.update_config(|config| {
            match key {
                "target_fps" => {
                    if let Ok(fps) = value.parse::<u32>() {
                        config.target_fps = fps;
                    }
                }
                "capture_mouse" => {
                    if let Ok(mouse) = value.parse::<bool>() {
                        config.capture_mouse = mouse;
                    }
                }
                _ => {}
            }
        }).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }
}

// ============================================================================
// THREAD-SAFE METAL ACCELERATOR FOR PYTHON
// ============================================================================

#[cfg(target_os = "macos")]
#[pyclass(name = "MetalAccelerator", module = "jarvis_rust_core")]
pub struct PyMetalAccelerator {
    inner: Arc<MetalAccelerator>,
    runtime: Arc<tokio::runtime::Runtime>,
}

#[cfg(target_os = "macos")]
unsafe impl Send for PyMetalAccelerator {}
#[cfg(target_os = "macos")]
unsafe impl Sync for PyMetalAccelerator {}

#[cfg(target_os = "macos")]
#[pymethods]
impl PyMetalAccelerator {
    #[new]
    fn new() -> PyResult<Self> {
        let bridge = Arc::new(ObjCBridge::new(3)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?);
        
        let accelerator = MetalAccelerator::new(bridge)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        let runtime = tokio::runtime::Runtime::new()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;
        
        Ok(Self {
            inner: Arc::new(accelerator),
            runtime: Arc::new(runtime),
        })
    }
    
    /// Process frame with Metal shader
    fn process_frame(
        &self,
        py: Python,
        data: &PyArray3<u8>,
        shader_name: &str,
    ) -> PyResult<Py<PyArray3<u8>>> {
        let input_slice = unsafe { data.as_slice()? };
        let shape = data.shape();
        let (height, width, channels) = (shape[0] as u32, shape[1] as u32, shape[2]);
        
        let accel = self.inner.clone();
        let shader = shader_name.to_string();
        let input_vec = input_slice.to_vec();
        
        let result = self.runtime.block_on(async move {
            accel.process_frame(&input_vec, &shader, width, height).await
        }).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        // Convert back to numpy
        let output_shape = [height as usize, width as usize, channels];
        let array = unsafe { PyArray3::new(py, output_shape, false) };
        unsafe {
            array.as_slice_mut()?.copy_from_slice(&result);
        }
        Ok(array.to_owned())
    }
    
    /// Compute frame difference
    fn frame_difference(
        &self,
        py: Python,
        frame1: &PyArray3<u8>,
        frame2: &PyArray3<u8>,
    ) -> PyResult<Py<PyArray3<f32>>> {
        let shape1 = frame1.shape();
        let shape2 = frame2.shape();
        
        if shape1 != shape2 {
            return Err(PyValueError::new_err("Frame shapes must match"));
        }
        
        let arr1 = unsafe { frame1.as_array() };
        let arr2 = unsafe { frame2.as_array() };
        
        let accel = self.inner.clone();
        
        let result = self.runtime.block_on(async move {
            accel.frame_difference(arr1, arr2).await
        }).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        
        // Convert to PyArray
        Ok(PyArray3::from_owned_array(py, result).to_owned())
    }
    
    /// Get performance statistics
    fn get_stats(&self) -> PyResult<HashMap<String, PyObject>> {
        let stats = self.inner.stats();
        
        Python::with_gil(|py| {
            let mut map = HashMap::new();
            map.insert("total_frames".to_string(), stats.total_frames_processed.to_object(py));
            map.insert("total_time_ms".to_string(), stats.total_compute_time_ms.to_object(py));
            map.insert("avg_frame_time_ms".to_string(), stats.average_frame_time_ms.to_object(py));
            Ok(map)
        })
    }
}

// ============================================================================
// THREAD-SAFE MEMORY MANAGER FOR PYTHON
// ============================================================================

#[pyclass(name = "MemoryManager", module = "jarvis_rust_core")]
pub struct PyMemoryManager {
    inner: Arc<MemoryManager>,
}

unsafe impl Send for PyMemoryManager {}
unsafe impl Sync for PyMemoryManager {}

#[pymethods]
impl PyMemoryManager {
    #[new]
    fn new() -> PyResult<Self> {
        Ok(Self {
            inner: MemoryManager::global(),
        })
    }
    
    /// Get memory statistics
    fn get_stats(&self) -> PyResult<HashMap<String, PyObject>> {
        let stats = self.inner.stats();
        
        Python::with_gil(|py| {
            let mut map = HashMap::new();
            map.insert("allocated_mb".to_string(), (stats.allocated_bytes / 1_048_576).to_object(py));
            map.insert("deallocated_mb".to_string(), (stats.deallocated_bytes / 1_048_576).to_object(py));
            map.insert("peak_usage_mb".to_string(), (stats.peak_usage_bytes / 1_048_576).to_object(py));
            map.insert("allocation_count".to_string(), stats.allocation_count.to_object(py));
            map.insert("deallocation_count".to_string(), stats.deallocation_count.to_object(py));
            Ok(map)
        })
    }
}

// ============================================================================
// MODULE REGISTRATION
// ============================================================================

/// Register refactored Python module with thread-safe components
pub fn register_python_module(m: &PyModule) -> PyResult<()> {
    m.add_class::<PyScreenCapture>()?;
    m.add_class::<PyMemoryManager>()?;
    
    #[cfg(target_os = "macos")]
    m.add_class::<PyMetalAccelerator>()?;
    
    // Add version info
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("__thread_safe__", true)?;
    
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::Python;
    
    #[test]
    fn test_python_binding_compilation() {
        // This test just ensures the code compiles without Send/Sync errors
        Python::with_gil(|py| {
            let module = PyModule::new(py, "test").unwrap();
            register_python_module(module).unwrap();
        });
    }
}