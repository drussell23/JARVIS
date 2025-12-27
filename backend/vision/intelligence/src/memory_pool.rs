use std::sync::{Arc, Mutex};
use std::collections::VecDeque;
use bumpalo::Bump;
use mimalloc::MiMalloc;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

use pyo3::prelude::*;
use pyo3::types::PyList;

/// Global memory pool for vision processing
static mut GLOBAL_POOL: Option<Arc<Mutex<MemoryPool>>> = None;

pub struct MemoryPool {
    image_buffers: VecDeque<Vec<u8>>,
    feature_buffers: VecDeque<Vec<f32>>,
    bump_allocator: Bump,
    max_buffers: usize,
}

impl MemoryPool {
    fn new(max_buffers: usize) -> Self {
        Self {
            image_buffers: VecDeque::with_capacity(max_buffers),
            feature_buffers: VecDeque::with_capacity(max_buffers),
            bump_allocator: Bump::new(),
            max_buffers,
        }
    }

    fn get_image_buffer(&mut self, size: usize) -> Vec<u8> {
        self.image_buffers.pop_front()
            .filter(|buf| buf.capacity() >= size)
            .unwrap_or_else(|| Vec::with_capacity(size))
    }

    fn return_image_buffer(&mut self, mut buffer: Vec<u8>) {
        if self.image_buffers.len() < self.max_buffers {
            buffer.clear();
            self.image_buffers.push_back(buffer);
        }
    }

    fn get_feature_buffer(&mut self, size: usize) -> Vec<f32> {
        self.feature_buffers.pop_front()
            .filter(|buf| buf.capacity() >= size)
            .unwrap_or_else(|| Vec::with_capacity(size))
    }

    fn return_feature_buffer(&mut self, mut buffer: Vec<f32>) {
        if self.feature_buffers.len() < self.max_buffers {
            buffer.clear();
            self.feature_buffers.push_back(buffer);
        }
    }

    fn reset_bump_allocator(&mut self) {
        self.bump_allocator.reset();
    }

    fn stats(&self) -> PoolStats {
        PoolStats {
            image_buffers_available: self.image_buffers.len(),
            feature_buffers_available: self.feature_buffers.len(),
            bump_allocated: self.bump_allocator.allocated_bytes(),
        }
    }
}

#[derive(Debug, Clone)]
struct PoolStats {
    image_buffers_available: usize,
    feature_buffers_available: usize,
    bump_allocated: usize,
}

pub fn initialize_global_pool() {
    unsafe {
        if GLOBAL_POOL.is_none() {
            GLOBAL_POOL = Some(Arc::new(Mutex::new(MemoryPool::new(100))));
        }
    }
}

fn get_global_pool() -> Arc<Mutex<MemoryPool>> {
    unsafe {
        GLOBAL_POOL.as_ref()
            .expect("Global memory pool not initialized")
            .clone()
    }
}

#[pyclass]
pub struct VisionMemoryPool {
    pool: Arc<Mutex<MemoryPool>>,
}

#[pymethods]
impl VisionMemoryPool {
    #[new]
    fn new(_max_buffers: Option<usize>) -> Self {
        initialize_global_pool();
        Self {
            pool: get_global_pool(),
        }
    }

    fn allocate_image_buffer(&mut self, size: usize) -> PyResult<ImageBuffer> {
        let mut pool = self.pool.lock().unwrap();
        let buffer = pool.get_image_buffer(size);
        Ok(ImageBuffer {
            data: buffer,
            pool: self.pool.clone(),
        })
    }

    fn allocate_feature_buffer(&mut self, size: usize) -> PyResult<FeatureBuffer> {
        let mut pool = self.pool.lock().unwrap();
        let buffer = pool.get_feature_buffer(size);
        Ok(FeatureBuffer {
            data: buffer,
            pool: self.pool.clone(),
        })
    }

    fn reset_bump_allocator(&mut self) {
        let mut pool = self.pool.lock().unwrap();
        pool.reset_bump_allocator();
    }

    fn get_stats(&self) -> PyResult<String> {
        let pool = self.pool.lock().unwrap();
        let stats = pool.stats();
        Ok(format!(
            "Image buffers: {}, Feature buffers: {}, Bump allocated: {} bytes",
            stats.image_buffers_available,
            stats.feature_buffers_available,
            stats.bump_allocated
        ))
    }
}

#[pyclass]
pub struct ImageBuffer {
    data: Vec<u8>,
    pool: Arc<Mutex<MemoryPool>>,
}

#[pymethods]
impl ImageBuffer {
    fn resize(&mut self, new_size: usize) {
        self.data.resize(new_size, 0);
    }

    fn capacity(&self) -> usize {
        self.data.capacity()
    }

    fn len(&self) -> usize {
        self.data.len()
    }

    fn as_bytes<'py>(&self, py: Python<'py>) -> &'py PyList {
        PyList::new(py, &self.data)
    }

    fn fill(&mut self, value: u8) {
        self.data.fill(value);
    }

    fn __len__(&self) -> usize {
        self.data.len()
    }

    fn __getitem__(&self, idx: usize) -> PyResult<u8> {
        self.data.get(idx)
            .copied()
            .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("Index out of bounds"))
    }

    fn __setitem__(&mut self, idx: usize, value: u8) -> PyResult<()> {
        self.data.get_mut(idx)
            .map(|v| *v = value)
            .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("Index out of bounds"))
    }
}

impl Drop for ImageBuffer {
    fn drop(&mut self) {
        let buffer = std::mem::take(&mut self.data);
        if let Ok(mut pool) = self.pool.lock() {
            pool.return_image_buffer(buffer);
        }
    }
}

#[pyclass]
pub struct FeatureBuffer {
    data: Vec<f32>,
    pool: Arc<Mutex<MemoryPool>>,
}

#[pymethods]
impl FeatureBuffer {
    fn resize(&mut self, new_size: usize) {
        self.data.resize(new_size, 0.0);
    }

    fn capacity(&self) -> usize {
        self.data.capacity()
    }

    fn len(&self) -> usize {
        self.data.len()
    }

    fn as_slice<'py>(&self, py: Python<'py>) -> &'py PyList {
        PyList::new(py, &self.data)
    }

    fn fill(&mut self, value: f32) {
        self.data.fill(value);
    }

    fn __len__(&self) -> usize {
        self.data.len()
    }

    fn __getitem__(&self, idx: usize) -> PyResult<f32> {
        self.data.get(idx)
            .copied()
            .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("Index out of bounds"))
    }

    fn __setitem__(&mut self, idx: usize, value: f32) -> PyResult<()> {
        self.data.get_mut(idx)
            .map(|v| *v = value)
            .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("Index out of bounds"))
    }

    fn to_list(&self) -> Vec<f32> {
        self.data.clone()
    }
}

impl Drop for FeatureBuffer {
    fn drop(&mut self) {
        let buffer = std::mem::take(&mut self.data);
        if let Ok(mut pool) = self.pool.lock() {
            pool.return_feature_buffer(buffer);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_memory_pool() {
        initialize_global_pool();
        let pool = get_global_pool();
        
        let mut pool_guard = pool.lock().unwrap();
        
        // Test image buffer
        let img_buf = pool_guard.get_image_buffer(1024);
        assert!(img_buf.capacity() >= 1024);
        pool_guard.return_image_buffer(img_buf);
        
        // Test feature buffer
        let feat_buf = pool_guard.get_feature_buffer(256);
        assert!(feat_buf.capacity() >= 256);
        pool_guard.return_feature_buffer(feat_buf);
        
        // Check stats
        let stats = pool_guard.stats();
        assert_eq!(stats.image_buffers_available, 1);
        assert_eq!(stats.feature_buffers_available, 1);
    }
}