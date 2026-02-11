//! Thread-safe Python bindings using refactored components
//!
//! This module provides Python bindings that work with the new message-passing
//! architecture, eliminating thread-safety compilation errors.

#![cfg(feature = "python-bindings")]

use crate::bridge::ObjCBridge;
use crate::memory::advanced_pool::{AdvancedBufferPool, TrackedBuffer};
use crate::vision::capture::{ScreenCapture, CaptureConfig, CaptureQuality};
use crate::vision::metal_accelerator::MetalAccelerator;
use crate::vision::{ImageData, ImageFormat, ImageProcessor, IntegrationPipeline};
use crate::memory::MemoryManager;
use crate::runtime::{RuntimeConfig, RuntimeManager};

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use parking_lot::Mutex;
use std::sync::Arc;
use std::collections::HashMap;
use numpy::{PyArray1, PyArray2, PyArray3, PyReadonlyArray2, PyReadonlyArrayDyn};

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
// COMPATIBILITY WRAPPERS REQUIRED BY PYTHON CALL SITES
// ============================================================================

fn numpy_to_image(image: PyReadonlyArrayDyn<u8>) -> PyResult<ImageData> {
    let shape = image.shape();
    let (height, width, channels) = match shape.len() {
        2 => (shape[0] as u32, shape[1] as u32, 1u8),
        3 => (shape[0] as u32, shape[1] as u32, shape[2] as u8),
        _ => {
            return Err(PyValueError::new_err(
                "Expected image with shape [H, W] or [H, W, C]",
            ))
        }
    };

    let format = match channels {
        1 => ImageFormat::Gray8,
        2 => ImageFormat::GrayA8,
        3 => ImageFormat::Rgb8,
        4 => ImageFormat::Rgba8,
        _ => {
            return Err(PyValueError::new_err(format!(
                "Unsupported channel count: {}",
                channels
            )))
        }
    };

    let raw = image
        .as_slice()?
        .to_vec();

    ImageData::from_raw(width, height, raw, format)
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

fn image_to_numpy<'py>(py: Python<'py>, image: &ImageData) -> PyResult<Py<PyArray3<u8>>> {
    let shape = [
        image.height as usize,
        image.width as usize,
        image.channels as usize,
    ];
    let array = unsafe { PyArray3::new(py, shape, false) };
    unsafe {
        array.as_slice_mut()?.copy_from_slice(image.as_slice());
    }
    Ok(array.to_owned())
}

fn image_to_grayscale(image: &ImageData) -> Vec<u8> {
    let data = image.as_slice();
    match image.channels {
        1 => data.to_vec(),
        2 => data.chunks_exact(2).map(|px| px[0]).collect(),
        3 | 4 => {
            let c = image.channels as usize;
            data.chunks_exact(c)
                .map(|px| {
                    let r = px[0] as f32;
                    let g = px[1] as f32;
                    let b = px[2] as f32;
                    (0.299 * r + 0.587 * g + 0.114 * b)
                        .round()
                        .clamp(0.0, 255.0) as u8
                })
                .collect()
        }
        _ => Vec::new(),
    }
}

fn edge_density_from_gray(gray: &[u8], width: usize, height: usize) -> f64 {
    if width < 2 || height < 2 || gray.is_empty() {
        return 0.0;
    }

    let mut edges = 0usize;
    let mut samples = 0usize;
    let threshold = 30i16;

    for y in 0..(height - 1) {
        for x in 0..(width - 1) {
            let idx = y * width + x;
            let right = idx + 1;
            let down = idx + width;
            let gx = (gray[right] as i16 - gray[idx] as i16).abs();
            let gy = (gray[down] as i16 - gray[idx] as i16).abs();
            if gx + gy > threshold {
                edges += 1;
            }
            samples += 1;
        }
    }

    if samples == 0 {
        0.0
    } else {
        edges as f64 / samples as f64
    }
}

#[pyclass(name = "RustImageProcessor", module = "jarvis_rust_core")]
pub struct PyRustImageProcessor {
    processor: Arc<ImageProcessor>,
}

unsafe impl Send for PyRustImageProcessor {}
unsafe impl Sync for PyRustImageProcessor {}

impl PyRustImageProcessor {
    fn apply_operation(
        &self,
        image: &ImageData,
        operation: &str,
        params: Option<&PyDict>,
    ) -> PyResult<ImageData> {
        match operation {
            "auto_process" => self
                .processor
                .auto_process(image)
                .map_err(|e| PyRuntimeError::new_err(e.to_string())),
            "resize" => {
                let width = params
                    .and_then(|p| p.get_item("width").ok().flatten())
                    .and_then(|v| v.extract::<u32>().ok())
                    .unwrap_or(image.width);
                let height = params
                    .and_then(|p| p.get_item("height").ok().flatten())
                    .and_then(|v| v.extract::<u32>().ok())
                    .unwrap_or(image.height);
                self.processor
                    .resize(image, width, height)
                    .map_err(|e| PyRuntimeError::new_err(e.to_string()))
            }
            "convolve" => {
                let kernel_name = params
                    .and_then(|p| p.get_item("kernel").ok().flatten())
                    .and_then(|v| v.extract::<String>().ok())
                    .unwrap_or_else(|| "gaussian_3x3".to_string());
                self.processor
                    .convolve(image, &kernel_name, None)
                    .map_err(|e| PyRuntimeError::new_err(e.to_string()))
            }
            "denoise" => {
                let strength = params
                    .and_then(|p| p.get_item("strength").ok().flatten())
                    .and_then(|v| v.extract::<f32>().ok())
                    .unwrap_or(0.5);
                self.processor
                    .update_config("denoise_strength", &strength.to_string())
                    .map_err(|e| PyValueError::new_err(e.to_string()))?;
                self.processor
                    .auto_process(image)
                    .map_err(|e| PyRuntimeError::new_err(e.to_string()))
            }
            "sharpen" => {
                let amount = params
                    .and_then(|p| p.get_item("amount").ok().flatten())
                    .and_then(|v| v.extract::<f32>().ok())
                    .unwrap_or(1.0);
                self.processor
                    .update_config("sharpen_amount", &amount.to_string())
                    .map_err(|e| PyValueError::new_err(e.to_string()))?;
                self.processor
                    .auto_process(image)
                    .map_err(|e| PyRuntimeError::new_err(e.to_string()))
            }
            other => Err(PyValueError::new_err(format!(
                "Unsupported operation: {}",
                other
            ))),
        }
    }
}

#[pymethods]
impl PyRustImageProcessor {
    #[new]
    #[pyo3(signature = (config=None))]
    fn new(config: Option<&PyDict>) -> PyResult<Self> {
        let processor = ImageProcessor::new();

        if let Some(cfg) = config {
            for (key, value) in cfg.iter() {
                let key_str = key.extract::<String>()?;
                let value_str = value.str()?.to_str()?.to_string();
                processor
                    .update_config(&key_str, &value_str)
                    .map_err(|e| PyValueError::new_err(e.to_string()))?;
            }
        }

        Ok(Self {
            processor: Arc::new(processor),
        })
    }

    #[pyo3(signature = (image, operation="auto_process", params=None))]
    fn process_numpy_image(
        &self,
        py: Python<'_>,
        image: PyReadonlyArrayDyn<u8>,
        operation: &str,
        params: Option<&PyDict>,
    ) -> PyResult<Py<PyArray3<u8>>> {
        let input = numpy_to_image(image)?;
        let output = self.apply_operation(&input, operation, params)?;
        image_to_numpy(py, &output)
    }

    #[pyo3(signature = (images, operation="auto_process", params=None))]
    fn process_batch_zero_copy(
        &self,
        py: Python<'_>,
        images: Vec<PyReadonlyArrayDyn<u8>>,
        operation: &str,
        params: Option<&PyDict>,
    ) -> PyResult<Vec<Py<PyArray3<u8>>>> {
        images
            .into_iter()
            .map(|image| {
                let input = numpy_to_image(image)?;
                let output = self.apply_operation(&input, operation, params)?;
                image_to_numpy(py, &output)
            })
            .collect()
    }
}

#[pyclass(name = "RustRuntimeManager", module = "jarvis_rust_core")]
pub struct PyRustRuntimeManager {
    runtime: Arc<RuntimeManager>,
}

unsafe impl Send for PyRustRuntimeManager {}
unsafe impl Sync for PyRustRuntimeManager {}

#[pymethods]
impl PyRustRuntimeManager {
    #[new]
    #[pyo3(signature = (worker_threads=None, enable_cpu_affinity=true))]
    fn new(worker_threads: Option<usize>, enable_cpu_affinity: bool) -> PyResult<Self> {
        let config = RuntimeConfig {
            worker_threads: worker_threads.unwrap_or_else(num_cpus::get),
            enable_cpu_affinity,
            ..Default::default()
        };

        let runtime = RuntimeManager::new(config)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

        Ok(Self {
            runtime: Arc::new(runtime),
        })
    }

    fn run_cpu_task(&self, py: Python<'_>, func: PyObject) -> PyResult<PyObject> {
        let handle = self.runtime.spawn_cpu("python-cpu-task", move || {
            Python::with_gil(|gil| func.call0(gil))
        });

        py.allow_threads(|| {
            let wait_runtime = tokio::runtime::Runtime::new()
                .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

            let result = wait_runtime
                .block_on(async { handle.await })
                .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

            result
        })
    }

    fn stats(&self, py: Python<'_>) -> PyResult<PyObject> {
        let stats = self.runtime.stats();
        let dict = PyDict::new(py);
        dict.set_item("active_tasks", stats.active_tasks)?;
        dict.set_item("total_spawned", stats.total_spawned)?;
        dict.set_item("total_completed", stats.total_completed)?;
        dict.set_item("active_workers", stats.active_workers)?;
        dict.set_item("queue_depth", stats.queue_depth)?;
        Ok(dict.to_object(py))
    }
}

#[pyclass(name = "RustTrackedBuffer", module = "jarvis_rust_core")]
pub struct PyRustTrackedBuffer {
    buffer: Arc<Mutex<Option<TrackedBuffer>>>,
}

unsafe impl Send for PyRustTrackedBuffer {}
unsafe impl Sync for PyRustTrackedBuffer {}

#[pymethods]
impl PyRustTrackedBuffer {
    fn as_numpy(&self, py: Python<'_>) -> PyResult<Py<PyArray1<u8>>> {
        let guard = self.buffer.lock();
        let tracked = guard
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("Buffer already released"))?;
        Ok(PyArray1::from_slice(py, tracked.as_slice()).to_owned())
    }

    fn id(&self) -> PyResult<u64> {
        let guard = self.buffer.lock();
        let tracked = guard
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("Buffer already released"))?;
        Ok(tracked.id())
    }

    fn len(&self) -> PyResult<usize> {
        let guard = self.buffer.lock();
        let tracked = guard
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("Buffer already released"))?;
        Ok(tracked.len())
    }

    fn release(&self) {
        let mut guard = self.buffer.lock();
        *guard = None;
    }
}

#[pyclass(name = "RustAdvancedMemoryPool", module = "jarvis_rust_core")]
pub struct PyRustAdvancedMemoryPool {
    pool: Arc<AdvancedBufferPool>,
    leaks: Arc<Mutex<Vec<String>>>,
}

unsafe impl Send for PyRustAdvancedMemoryPool {}
unsafe impl Sync for PyRustAdvancedMemoryPool {}

#[pymethods]
impl PyRustAdvancedMemoryPool {
    #[new]
    fn new() -> Self {
        Self {
            pool: Arc::new(AdvancedBufferPool::new()),
            leaks: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn allocate(&self, size: usize) -> PyResult<PyRustTrackedBuffer> {
        let tracked = self
            .pool
            .allocate(size)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

        Ok(PyRustTrackedBuffer {
            buffer: Arc::new(Mutex::new(Some(tracked))),
        })
    }

    fn stats(&self, py: Python<'_>) -> PyResult<PyObject> {
        let stats = self.pool.stats();
        let dict = PyDict::new(py);
        dict.set_item("total_active", stats.total_active)?;
        dict.set_item("total_allocated_bytes", stats.total_allocated_bytes)?;
        dict.set_item("memory_pressure", format!("{:?}", stats.pressure))?;

        let size_classes = PyDict::new(py);
        for class in stats.size_classes {
            let class_dict = PyDict::new(py);
            class_dict.set_item("available", class.available)?;
            class_dict.set_item("capacity", class.capacity)?;
            class_dict.set_item("high_water_mark", class.high_water_mark)?;
            size_classes.set_item(class.size.to_string(), class_dict)?;
        }
        dict.set_item("size_classes", size_classes)?;
        Ok(dict.to_object(py))
    }

    fn check_leaks(&self) -> Vec<String> {
        self.leaks.lock().clone()
    }
}

#[pyclass(name = "IntegrationPipeline", module = "jarvis_rust_core.vision")]
pub struct PyIntegrationPipeline {
    inner: Arc<IntegrationPipeline>,
}

unsafe impl Send for PyIntegrationPipeline {}
unsafe impl Sync for PyIntegrationPipeline {}

#[pymethods]
impl PyIntegrationPipeline {
    #[new]
    #[pyo3(signature = (total_budget_mb=1200.0))]
    fn new(total_budget_mb: f64) -> Self {
        Self {
            inner: Arc::new(IntegrationPipeline::new(total_budget_mb)),
        }
    }

    fn process_batch(&self, py: Python<'_>, frames: Vec<&PyBytes>) -> PyResult<Vec<PyObject>> {
        let frame_batch: Vec<Vec<u8>> = frames.into_iter().map(|frame| frame.as_bytes().to_vec()).collect();
        let results = self.inner.process_batch(frame_batch);

        results
            .into_iter()
            .map(|result| {
                let dict = PyDict::new(py);
                dict.set_item("success", result.success)?;
                dict.set_item("total_time_ms", result.total_time.as_secs_f64() * 1000.0)?;
                dict.set_item("features", result.features)?;

                let stage_times = PyDict::new(py);
                for (name, duration) in result.stage_times {
                    stage_times.set_item(name, duration.as_secs_f64() * 1000.0)?;
                }
                dict.set_item("stage_times_ms", stage_times)?;
                Ok(dict.to_object(py))
            })
            .collect()
    }

    fn get_memory_status(&self, py: Python<'_>) -> PyResult<PyObject> {
        let status = self.inner.get_memory_status();
        let dict = PyDict::new(py);
        dict.set_item("total_budget_mb", status.total_budget_mb)?;
        dict.set_item("total_allocated_mb", status.total_allocated_mb)?;
        dict.set_item("total_used_mb", status.total_used_mb)?;
        dict.set_item("mode", format!("{:?}", status.mode))?;

        let components = PyDict::new(py);
        for (name, component) in status.components {
            let component_dict = PyDict::new(py);
            component_dict.set_item("allocated_mb", component.allocated_mb)?;
            component_dict.set_item("used_mb", component.used_mb)?;
            component_dict.set_item("utilization", component.utilization)?;
            component_dict.set_item("priority", component.priority)?;
            components.set_item(name, component_dict)?;
        }
        dict.set_item("components", components)?;
        Ok(dict.to_object(py))
    }
}

#[pyfunction]
#[pyo3(signature = (images, config=None, operation="auto_process", params=None))]
fn process_image_batch(
    py: Python<'_>,
    images: Vec<PyReadonlyArrayDyn<u8>>,
    config: Option<&PyDict>,
    operation: &str,
    params: Option<&PyDict>,
) -> PyResult<Vec<Py<PyArray3<u8>>>> {
    let processor = PyRustImageProcessor::new(config)?;
    processor.process_batch_zero_copy(py, images, operation, params)
}

#[pyfunction]
fn quantize_model_weights(weights: PyReadonlyArray2<f32>) -> PyResult<Vec<i8>> {
    let values = weights.as_slice()?;
    if values.is_empty() {
        return Ok(Vec::new());
    }

    let max_abs = values
        .iter()
        .fold(0.0_f32, |acc, value| acc.max(value.abs()));

    if max_abs == 0.0 {
        return Ok(vec![0_i8; values.len()]);
    }

    let scale = max_abs / 127.0;
    Ok(values
        .iter()
        .map(|value| ((*value / scale).round().clamp(-128.0, 127.0)) as i8)
        .collect())
}

#[pyfunction]
#[pyo3(signature = (image, num_colors=5))]
fn extract_dominant_colors(
    image: PyReadonlyArrayDyn<u8>,
    num_colors: usize,
) -> PyResult<Vec<(u8, u8, u8)>> {
    let data = numpy_to_image(image)?;
    let pixels = data.as_slice();
    if pixels.is_empty() || num_colors == 0 {
        return Ok(Vec::new());
    }

    let channels = data.channels as usize;
    let pixel_count = (data.width as usize) * (data.height as usize);
    let sample_stride = (pixel_count / 200_000).max(1);
    let mut frequencies: HashMap<(u8, u8, u8), usize> = HashMap::new();

    for pixel_index in (0..pixel_count).step_by(sample_stride) {
        let offset = pixel_index * channels;
        if offset + channels > pixels.len() {
            break;
        }

        let color = match channels {
            1 => {
                let v = pixels[offset];
                (v, v, v)
            }
            2 => {
                let v = pixels[offset];
                (v, v, v)
            }
            3 | 4 => (pixels[offset], pixels[offset + 1], pixels[offset + 2]),
            _ => continue,
        };
        *frequencies.entry(color).or_insert(0) += 1;
    }

    let mut ranked: Vec<((u8, u8, u8), usize)> = frequencies.into_iter().collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1));
    ranked.truncate(num_colors);

    Ok(ranked.into_iter().map(|(color, _)| color).collect())
}

#[pyfunction]
fn calculate_edge_density(image: PyReadonlyArrayDyn<u8>) -> PyResult<f64> {
    let image = numpy_to_image(image)?;
    let gray = image_to_grayscale(&image);
    Ok(edge_density_from_gray(
        &gray,
        image.width as usize,
        image.height as usize,
    ))
}

#[pyfunction]
fn analyze_texture(image: PyReadonlyArrayDyn<u8>) -> PyResult<HashMap<String, f64>> {
    let image = numpy_to_image(image)?;
    let gray = image_to_grayscale(&image);
    if gray.is_empty() {
        return Ok(HashMap::new());
    }

    let len = gray.len() as f64;
    let mean = gray.iter().map(|v| *v as f64).sum::<f64>() / len;
    let variance = gray
        .iter()
        .map(|v| {
            let d = *v as f64 - mean;
            d * d
        })
        .sum::<f64>()
        / len;
    let std_dev = variance.sqrt();

    let mut histogram = [0usize; 256];
    for value in &gray {
        histogram[*value as usize] += 1;
    }
    let entropy = histogram
        .iter()
        .filter(|count| **count > 0)
        .map(|count| {
            let p = *count as f64 / len;
            -p * p.log2()
        })
        .sum::<f64>();

    let edge_density = edge_density_from_gray(&gray, image.width as usize, image.height as usize);

    let mut result = HashMap::new();
    result.insert("mean".to_string(), mean);
    result.insert("std_dev".to_string(), std_dev);
    result.insert("entropy".to_string(), entropy);
    result.insert("edge_density".to_string(), edge_density);
    Ok(result)
}

#[pyfunction]
fn analyze_spatial_layout(
    py: Python<'_>,
    image: PyReadonlyArrayDyn<u8>,
) -> PyResult<HashMap<String, PyObject>> {
    let image = numpy_to_image(image)?;
    let gray = image_to_grayscale(&image);
    let width = image.width as usize;
    let height = image.height as usize;

    let x0 = width / 4;
    let x1 = (width * 3) / 4;
    let y0 = height / 4;
    let y1 = (height * 3) / 4;

    let mut center_sum = 0usize;
    let mut center_count = 0usize;
    for y in y0..y1 {
        for x in x0..x1 {
            let idx = y * width + x;
            if idx < gray.len() {
                center_sum += gray[idx] as usize;
                center_count += 1;
            }
        }
    }

    let center_brightness = if center_count == 0 {
        0.0
    } else {
        center_sum as f64 / center_count as f64
    };

    let mut result = HashMap::new();
    result.insert("width".to_string(), image.width.to_object(py));
    result.insert("height".to_string(), image.height.to_object(py));
    result.insert("channels".to_string(), image.channels.to_object(py));
    result.insert(
        "aspect_ratio".to_string(),
        (image.width as f64 / image.height.max(1) as f64).to_object(py),
    );
    result.insert("center_brightness".to_string(), center_brightness.to_object(py));

    let edge_density = edge_density_from_gray(&gray, width, height);
    result.insert("edge_density".to_string(), edge_density.to_object(py));

    Ok(result)
}

// ============================================================================
// MODULE REGISTRATION
// ============================================================================

/// Register refactored Python module with thread-safe components
pub fn register_python_module(m: &PyModule) -> PyResult<()> {
    m.add_class::<PyScreenCapture>()?;
    m.add_class::<PyMemoryManager>()?;
    m.add_class::<PyRustImageProcessor>()?;
    m.add_class::<PyRustRuntimeManager>()?;
    m.add_class::<PyRustAdvancedMemoryPool>()?;
    m.add_class::<PyRustTrackedBuffer>()?;

    // Compatibility alias used by some Python call sites.
    let memory_cls = m.getattr("MemoryManager")?;
    m.add("RustMemoryPool", memory_cls)?;

    #[cfg(target_os = "macos")]
    {
        m.add_class::<PyMetalAccelerator>()?;
        let metal_cls = m.getattr("MetalAccelerator")?;
        m.add("PyMetalAccelerator", metal_cls)?;

        let metal_submodule = PyModule::new(m.py(), "metal_accelerator")?;
        metal_submodule.add_class::<PyMetalAccelerator>()?;
        let submodule_cls = metal_submodule.getattr("MetalAccelerator")?;
        metal_submodule.add("PyMetalAccelerator", submodule_cls)?;
        m.add_submodule(metal_submodule)?;
    }

    // Register high-performance submodules expected by Python integration.
    crate::vision::bloom_filter::register_module(m)?;
    crate::vision::sliding_window_bindings::register_module(m)?;
    crate::memory::zero_copy::register_module(m)?;

    let vision_submodule = PyModule::new(m.py(), "vision")?;
    vision_submodule.add_class::<PyIntegrationPipeline>()?;
    m.add_submodule(vision_submodule)?;

    // Register free functions consumed by Python wrappers.
    m.add_function(wrap_pyfunction!(process_image_batch, m)?)?;
    m.add_function(wrap_pyfunction!(quantize_model_weights, m)?)?;
    m.add_function(wrap_pyfunction!(extract_dominant_colors, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_edge_density, m)?)?;
    m.add_function(wrap_pyfunction!(analyze_texture, m)?)?;
    m.add_function(wrap_pyfunction!(analyze_spatial_layout, m)?)?;

    // Ensure submodule imports resolve via importlib.
    let sys = m.py().import("sys")?;
    let modules = sys.getattr("modules")?;
    if let Ok(bloom) = m.getattr("bloom_filter") {
        modules.set_item("jarvis_rust_core.bloom_filter", bloom)?;
    }
    if let Ok(sliding) = m.getattr("sliding_window") {
        modules.set_item("jarvis_rust_core.sliding_window", sliding)?;
    }
    if let Ok(zero_copy) = m.getattr("zero_copy") {
        modules.set_item("jarvis_rust_core.zero_copy", zero_copy)?;
    }
    if let Ok(vision) = m.getattr("vision") {
        modules.set_item("jarvis_rust_core.vision", vision)?;
    }
    #[cfg(target_os = "macos")]
    if let Ok(metal) = m.getattr("metal_accelerator") {
        modules.set_item("jarvis_rust_core.metal_accelerator", metal)?;
    }

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
