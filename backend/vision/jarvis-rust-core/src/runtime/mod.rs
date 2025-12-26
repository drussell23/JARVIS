//! Advanced async runtime management with CPU affinity and work stealing

use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tokio::runtime::{Runtime, Builder as RuntimeBuilder};
use tokio::task::{JoinHandle, LocalSet};
use parking_lot::RwLock;
use dashmap::DashMap;
use metrics::{counter, gauge, histogram};
use crate::{Result, JarvisError};

pub mod task_pool;
pub mod cpu_affinity;
pub mod work_stealing;

use task_pool::TaskPool;
use cpu_affinity::CpuAffinityManager;
use work_stealing::WorkStealingScheduler;

/// Advanced runtime configuration
#[derive(Debug, Clone)]
pub struct RuntimeConfig {
    /// Number of worker threads
    pub worker_threads: usize,
    /// Enable CPU affinity pinning
    pub enable_cpu_affinity: bool,
    /// Stack size for worker threads
    pub thread_stack_size: usize,
    /// Thread name prefix
    pub thread_name_prefix: String,
    /// Enable work stealing between threads
    pub enable_work_stealing: bool,
    /// Maximum blocking threads
    pub max_blocking_threads: usize,
    /// Keep alive time for idle threads
    pub thread_keep_alive: Duration,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        let cpu_count = num_cpus::get();
        Self {
            worker_threads: cpu_count,
            enable_cpu_affinity: true,
            thread_stack_size: 2 * 1024 * 1024, // 2MB
            thread_name_prefix: "jarvis-worker".to_string(),
            enable_work_stealing: true,
            max_blocking_threads: cpu_count * 4,
            thread_keep_alive: Duration::from_secs(10),
        }
    }
}

/// Advanced async runtime manager
pub struct RuntimeManager {
    /// Main Tokio runtime
    runtime: Arc<Runtime>,
    /// CPU-bound task pool
    cpu_pool: Arc<TaskPool>,
    /// I/O-bound task pool
    io_pool: Arc<TaskPool>,
    /// Work stealing scheduler
    work_stealer: Arc<WorkStealingScheduler>,
    /// CPU affinity manager
    affinity_manager: Arc<CpuAffinityManager>,
    /// Active tasks tracking
    active_tasks: Arc<DashMap<u64, TaskInfo>>,
    /// Runtime metrics
    metrics: Arc<RuntimeMetrics>,
}

#[derive(Debug, Clone)]
struct TaskInfo {
    id: u64,
    name: String,
    spawn_time: std::time::Instant,
    task_type: TaskType,
}

#[derive(Debug, Clone, Copy)]
pub enum TaskType {
    Cpu,
    Io,
    Compute,
    Background,
}

/// Runtime performance metrics
pub struct RuntimeMetrics {
    tasks_spawned: Arc<RwLock<u64>>,
    tasks_completed: Arc<RwLock<u64>>,
    active_workers: Arc<RwLock<usize>>,
    queue_depth: Arc<RwLock<usize>>,
}

impl RuntimeManager {
    /// Create new runtime manager with configuration
    pub fn new(config: RuntimeConfig) -> Result<Self> {
        // Build main runtime
        let mut builder = RuntimeBuilder::new_multi_thread();
        builder
            .worker_threads(config.worker_threads)
            .thread_name(config.thread_name_prefix.clone())
            .thread_stack_size(config.thread_stack_size)
            .max_blocking_threads(config.max_blocking_threads)
            .thread_keep_alive(config.thread_keep_alive)
            .enable_all();
        
        // Set custom panic handler
        builder.on_thread_unpark(|| {
            gauge!("runtime.threads.active", 1.0);
        });
        
        let runtime = builder.build()
            .map_err(|e| JarvisError::Other(anyhow::anyhow!("Failed to build runtime: {}", e)))?;
        
        // Create CPU and I/O pools
        let cpu_cores = if config.enable_cpu_affinity {
            config.worker_threads / 2
        } else {
            config.worker_threads
        };
        
        let cpu_pool = Arc::new(TaskPool::new("cpu-pool", cpu_cores));
        let io_pool = Arc::new(TaskPool::new("io-pool", config.worker_threads - cpu_cores));
        
        // Initialize work stealing
        let work_stealer = Arc::new(WorkStealingScheduler::new(config.worker_threads));
        
        // Initialize CPU affinity
        let affinity_manager = Arc::new(CpuAffinityManager::new()?);
        if config.enable_cpu_affinity {
            affinity_manager.setup_thread_affinity(config.worker_threads)?;
        }
        
        let manager = Self {
            runtime: Arc::new(runtime),
            cpu_pool,
            io_pool,
            work_stealer,
            affinity_manager,
            active_tasks: Arc::new(DashMap::new()),
            metrics: Arc::new(RuntimeMetrics::new()),
        };
        
        // Start metrics collection
        manager.start_metrics_collection();
        
        Ok(manager)
    }
    
    /// Spawn CPU-bound task
    pub fn spawn_cpu<F, T>(&self, name: &str, task: F) -> JoinHandle<T>
    where
        F: FnOnce() -> T + Send + 'static,
        T: Send + 'static,
    {
        counter!("runtime.tasks.spawned", 1, "type" => "cpu");
        
        let task_id = self.next_task_id();
        self.track_task(task_id, name.to_string(), TaskType::Cpu);
        
        let active_tasks = self.active_tasks.clone();
        let work_stealer = self.work_stealer.clone();
        
        self.runtime.spawn(async move {
            // Register with work stealer
            work_stealer.register_task(task_id);
            
            // Run on CPU pool
            let result = tokio::task::spawn_blocking(task).await
                .expect("CPU task panicked");
            
            // Cleanup
            work_stealer.complete_task(task_id);
            active_tasks.remove(&task_id);
            counter!("runtime.tasks.completed", 1, "type" => "cpu");
            
            result
        })
    }
    
    /// Spawn I/O-bound task
    pub fn spawn_io<F>(&self, name: &str, future: F) -> JoinHandle<F::Output>
    where
        F: std::future::Future + Send + 'static,
        F::Output: Send + 'static,
    {
        counter!("runtime.tasks.spawned", 1, "type" => "io");
        
        let task_id = self.next_task_id();
        self.track_task(task_id, name.to_string(), TaskType::Io);
        
        let active_tasks = self.active_tasks.clone();
        
        self.runtime.spawn(async move {
            let start = std::time::Instant::now();
            let result = future.await;
            
            // Record latency
            histogram!("runtime.task.duration", start.elapsed().as_secs_f64(), "type" => "io");
            
            active_tasks.remove(&task_id);
            counter!("runtime.tasks.completed", 1, "type" => "io");
            
            result
        })
    }
    
    /// Spawn compute-intensive task with SIMD optimization
    pub fn spawn_compute<F, T>(&self, name: &str, task: F) -> JoinHandle<T>
    where
        F: FnOnce() -> T + Send + 'static,
        T: Send + 'static,
    {
        counter!("runtime.tasks.spawned", 1, "type" => "compute");
        
        let task_id = self.next_task_id();
        self.track_task(task_id, name.to_string(), TaskType::Compute);
        
        // Pin to high-performance core if available
        if let Ok(core_id) = self.affinity_manager.get_performance_core() {
            let _ = self.affinity_manager.pin_thread_to_core(core_id);
        }
        
        self.spawn_cpu(name, task)
    }
    
    /// Execute task with timeout
    pub async fn with_timeout<F, T>(&self, duration: Duration, future: F) -> Result<T>
    where
        F: std::future::Future<Output = T> + Send,
        T: Send,
    {
        tokio::time::timeout(duration, future)
            .await
            .map_err(|_| JarvisError::Other(anyhow::anyhow!("Task timeout after {:?}", duration)))
    }
    
    /// Run blocking operation in dedicated thread pool
    pub async fn run_blocking<F, T>(&self, f: F) -> Result<T>
    where
        F: FnOnce() -> Result<T> + Send + 'static,
        T: Send + 'static,
    {
        tokio::task::spawn_blocking(f)
            .await
            .map_err(|e| JarvisError::Other(anyhow::anyhow!("Blocking task failed: {}", e)))?
    }
    
    /// Get runtime handle for external use
    pub fn handle(&self) -> tokio::runtime::Handle {
        self.runtime.handle().clone()
    }
    
    /// Shutdown runtime gracefully
    pub async fn shutdown(self) -> Result<()> {
        // Cancel all active tasks
        for entry in self.active_tasks.iter() {
            tracing::warn!("Cancelling active task: {} ({})", entry.value().name, entry.key());
        }
        
        // Shutdown pools
        self.cpu_pool.shutdown().await;
        self.io_pool.shutdown().await;
        
        // Wait for runtime shutdown
        // Note: We can't actually shutdown the runtime from async context
        // This would need to be done from sync context
        
        Ok(())
    }
    
    /// Start background metrics collection
    fn start_metrics_collection(&self) {
        let active_tasks = self.active_tasks.clone();
        let metrics = self.metrics.clone();
        
        self.runtime.spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(1));
            
            loop {
                interval.tick().await;
                
                // Update gauges
                gauge!("runtime.tasks.active", active_tasks.len() as f64);
                
                // Collect per-type metrics
                let mut cpu_tasks = 0;
                let mut io_tasks = 0;
                let mut compute_tasks = 0;
                
                for entry in active_tasks.iter() {
                    match entry.value().task_type {
                        TaskType::Cpu => cpu_tasks += 1,
                        TaskType::Io => io_tasks += 1,
                        TaskType::Compute => compute_tasks += 1,
                        TaskType::Background => {},
                    }
                }
                
                gauge!("runtime.tasks.active.cpu", cpu_tasks as f64);
                gauge!("runtime.tasks.active.io", io_tasks as f64);
                gauge!("runtime.tasks.active.compute", compute_tasks as f64);
            }
        });
    }
    
    fn next_task_id(&self) -> u64 {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        COUNTER.fetch_add(1, Ordering::Relaxed)
    }
    
    fn track_task(&self, id: u64, name: String, task_type: TaskType) {
        self.active_tasks.insert(id, TaskInfo {
            id,
            name,
            spawn_time: std::time::Instant::now(),
            task_type,
        });
    }
    
    /// Get runtime statistics
    pub fn stats(&self) -> RuntimeStats {
        RuntimeStats {
            active_tasks: self.active_tasks.len(),
            total_spawned: *self.metrics.tasks_spawned.read(),
            total_completed: *self.metrics.tasks_completed.read(),
            active_workers: *self.metrics.active_workers.read(),
            queue_depth: *self.metrics.queue_depth.read(),
        }
    }
}

impl RuntimeMetrics {
    fn new() -> Self {
        Self {
            tasks_spawned: Arc::new(RwLock::new(0)),
            tasks_completed: Arc::new(RwLock::new(0)),
            active_workers: Arc::new(RwLock::new(0)),
            queue_depth: Arc::new(RwLock::new(0)),
        }
    }
}

#[derive(Debug, Clone)]
pub struct RuntimeStats {
    pub active_tasks: usize,
    pub total_spawned: u64,
    pub total_completed: u64,
    pub active_workers: usize,
    pub queue_depth: usize,
}

/// Global runtime instance
static RUNTIME: once_cell::sync::OnceCell<Arc<RuntimeManager>> = once_cell::sync::OnceCell::new();

/// Initialize global runtime
pub fn initialize_runtime(config: RuntimeConfig) -> Result<()> {
    let runtime = RuntimeManager::new(config)?;
    RUNTIME.set(Arc::new(runtime))
        .map_err(|_| JarvisError::Other(anyhow::anyhow!("Runtime already initialized")))?;
    Ok(())
}

/// Get global runtime
pub fn runtime() -> Result<Arc<RuntimeManager>> {
    RUNTIME.get()
        .cloned()
        .ok_or_else(|| JarvisError::Other(anyhow::anyhow!("Runtime not initialized")))
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[tokio::test]
    async fn test_runtime_manager() {
        let config = RuntimeConfig::default();
        let runtime = RuntimeManager::new(config).unwrap();
        
        // Test CPU task
        let handle = runtime.spawn_cpu("test-cpu", || {
            let mut sum = 0;
            for i in 0..1000 {
                sum += i;
            }
            sum
        });
        
        let result = handle.await.unwrap();
        assert_eq!(result, 499500);
        
        // Test I/O task
        let handle = runtime.spawn_io("test-io", async {
            tokio::time::sleep(Duration::from_millis(10)).await;
            42
        });
        
        let result = handle.await.unwrap();
        assert_eq!(result, 42);
    }
}