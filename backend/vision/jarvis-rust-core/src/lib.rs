//! JARVIS Rust Core - High-performance vision and ML operations
//!
//! This crate provides optimized implementations of:
//! - Quantized ML inference (INT4/INT8/FP16)
//! - Memory-efficient buffer management
//! - Vision processing with hardware acceleration
//! - Zero-copy Python interop
//! - Advanced async runtime with work stealing
//! - Automatic memory leak detection

// Note: portable SIMD requires nightly Rust
// #![feature(portable_simd)]

// Allow dead code for library APIs that may not be used yet
#![allow(dead_code)]
// Allow unused variables in placeholder implementations
#![allow(unused_variables)]
// Allow unused imports during development
#![allow(unused_imports)]
// Allow non-local impl definitions (PyO3 macro warning)
#![allow(non_local_definitions)]
// Allow non-upper-case globals for CGConstants compatibility
#![allow(non_upper_case_globals)]
// Allow private types in public interfaces (intentional for internal APIs)
#![allow(private_interfaces)]

use std::sync::Once;
use anyhow;

// Common types will be available to all modules

pub mod quantized_ml;
pub mod memory;
pub mod vision;
pub mod bridge;
pub mod runtime;

use runtime::{RuntimeConfig, initialize_runtime};
use memory::advanced_pool::AdvancedBufferPool;

/// Main error type for JARVIS operations
#[derive(thiserror::Error, Debug)]
pub enum JarvisError {
    #[error("Memory allocation failed: {0}")]
    MemoryError(String),
    
    #[error("ML inference error: {0}")]
    InferenceError(String),
    
    #[error("Vision processing error: {0}")]
    VisionError(String),
    
    #[error("Python bridge error: {0}")]
    BridgeError(String),
    
    #[error("Invalid operation: {0}")]
    InvalidOperation(String),
    
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl From<std::io::Error> for JarvisError {
    fn from(err: std::io::Error) -> Self {
        JarvisError::Other(err.into())
    }
}

impl From<ndarray::ShapeError> for JarvisError {
    fn from(err: ndarray::ShapeError) -> Self {
        JarvisError::Other(anyhow::anyhow!("Shape error: {}", err))
    }
}

pub type Result<T> = std::result::Result<T, JarvisError>;

// Global initialization
static INIT: Once = Once::new();

/// Initialize the JARVIS Rust core with advanced features
pub fn initialize() {
    INIT.call_once(|| {
        // Initialize tracing/logging
        tracing_subscriber::fmt()
            .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
            .with_thread_ids(true)
            .with_thread_names(true)
            .init();
        
        // Initialize mimalloc as global allocator
        #[cfg(feature = "mimalloc")]
        {
            use mimalloc::MiMalloc;
            #[global_allocator]
            static GLOBAL: MiMalloc = MiMalloc;
        }
        
        // Initialize advanced runtime
        let runtime_config = RuntimeConfig {
            worker_threads: num_cpus::get(),
            enable_cpu_affinity: true,
            enable_work_stealing: true,
            ..Default::default()
        };
        
        if let Err(e) = initialize_runtime(runtime_config) {
            tracing::error!("Failed to initialize runtime: {}", e);
        }
        
        // Initialize thread pool for Rayon with custom configuration
        rayon::ThreadPoolBuilder::new()
            .num_threads(num_cpus::get())
            .thread_name(|index| format!("jarvis-compute-{}", index))
            .stack_size(4 * 1024 * 1024) // 4MB stack for compute threads
            .build_global()
            .expect("Failed to initialize compute thread pool");
        
        // Initialize global memory pool
        let _ = AdvancedBufferPool::new();
        
        tracing::info!("JARVIS Rust Core initialized");
        tracing::info!("CPU cores: {} (physical: {})", 
            num_cpus::get(), 
            num_cpus::get_physical()
        );
        tracing::info!("SIMD support: {}", cfg!(feature = "simd"));
        tracing::info!("Memory allocator: {}", 
            if cfg!(feature = "mimalloc") { "mimalloc" } else { "system" }
        );
    });
}


/// Performance metrics
#[derive(Debug, Clone, Default)]
pub struct Metrics {
    pub inference_time_ms: f64,
    pub memory_usage_mb: f64,
    pub cpu_usage_percent: f64,
    pub operations_per_second: f64,
}

/// Python module initialization
#[cfg(feature = "python-bindings")]
use pyo3::prelude::*;

#[cfg(feature = "python-bindings")]
#[pymodule]
fn jarvis_rust_core(_py: Python<'_>, m: &PyModule) -> PyResult<()> {
    initialize();
    
    // Add submodules
    bridge::register_python_module(m)?;
    
    Ok(())
}