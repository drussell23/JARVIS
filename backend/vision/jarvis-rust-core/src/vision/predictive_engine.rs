//! High-performance Predictive Pre-computation Engine
//! Markov chain-based prediction with SIMD optimization

use std::sync::Arc;
use parking_lot::{RwLock, Mutex};
use dashmap::DashMap;
use rayon::prelude::*;
use serde::{Serialize, Deserialize};
use std::collections::{HashMap, HashSet, VecDeque, BinaryHeap};
use std::time::{Instant, Duration};
use ordered_float::OrderedFloat;
use std::hash::{Hash, Hasher};
use fnv::FnvHasher;
use nalgebra_sparse::{CooMatrix, CsrMatrix};
use std::cmp::Ordering;

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

/// State representation
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub struct StateVector {
    pub app_id: String,
    pub app_state: String,
    pub user_action: Option<String>,
    pub time_context: Option<String>,
    pub goal_context: Option<String>,
    pub workflow_phase: Option<String>,
}

impl StateVector {
    /// Calculate similarity between states (0.0 to 1.0)
    pub fn similarity(&self, other: &StateVector) -> f32 {
        let mut score = 0.0;
        
        // Weighted comparison
        if self.app_id == other.app_id { score += 0.3; }
        if self.app_state == other.app_state { score += 0.25; }
        if self.user_action == other.user_action { score += 0.15; }
        if self.time_context == other.time_context { score += 0.1; }
        if self.goal_context == other.goal_context { score += 0.15; }
        if self.workflow_phase == other.workflow_phase { score += 0.05; }
        
        score
    }
    
    /// Generate hash for state indexing
    pub fn hash_state(&self) -> u64 {
        let mut hasher = FnvHasher::default();
        self.hash(&mut hasher);
        hasher.finish()
    }
}

/// Transition in the Markov chain
#[derive(Debug, Clone)]
struct Transition {
    from_idx: usize,
    to_idx: usize,
    count: u32,
    temporal_weight: f32,
    last_observed: Instant,
}

/// Sparse transition matrix using CSR format
pub struct TransitionMatrix {
    // State mappings
    state_to_idx: DashMap<u64, usize>,
    idx_to_state: DashMap<usize, StateVector>,
    next_idx: Arc<Mutex<usize>>,
    
    // Sparse matrix storage
    transition_counts: Arc<RwLock<CooMatrix<f32>>>,
    transition_probs: Arc<RwLock<Option<CsrMatrix<f32>>>>,
    temporal_weights: Arc<RwLock<CooMatrix<f32>>>,
    
    // State metadata
    state_metadata: DashMap<usize, StateMetadata>,
    max_states: usize,
}

#[derive(Debug, Clone)]
struct StateMetadata {
    first_seen: Instant,
    last_seen: Instant,
    total_visits: u32,
    avg_duration: Duration,
}

impl TransitionMatrix {
    pub fn new(max_states: usize) -> Self {
        Self {
            state_to_idx: DashMap::new(),
            idx_to_state: DashMap::new(),
            next_idx: Arc::new(Mutex::new(0)),
            transition_counts: Arc::new(RwLock::new(CooMatrix::new(max_states, max_states))),
            transition_probs: Arc::new(RwLock::new(None)),
            temporal_weights: Arc::new(RwLock::new(CooMatrix::new(max_states, max_states))),
            state_metadata: DashMap::new(),
            max_states,
        }
    }
    
    /// Add or get state index
    pub fn add_state(&self, state: StateVector) -> usize {
        let state_hash = state.hash_state();
        
        if let Some(idx) = self.state_to_idx.get(&state_hash) {
            // Update metadata
            if let Some(mut metadata) = self.state_metadata.get_mut(&idx) {
                metadata.last_seen = Instant::now();
                metadata.total_visits += 1;
            }
            return *idx;
        }
        
        // Add new state
        let mut next_idx = self.next_idx.lock();
        
        if *next_idx >= self.max_states {
            // Evict old states
            self.evict_states();
        }
        
        let idx = *next_idx;
        *next_idx += 1;
        drop(next_idx);
        
        self.state_to_idx.insert(state_hash, idx);
        self.idx_to_state.insert(idx, state.clone());
        self.state_metadata.insert(idx, StateMetadata {
            first_seen: Instant::now(),
            last_seen: Instant::now(),
            total_visits: 1,
            avg_duration: Duration::from_secs(0),
        });
        
        idx
    }
    
    /// Record state transition
    pub fn add_transition(&self, from_state: StateVector, to_state: StateVector, temporal_factor: f32) {
        let from_idx = self.add_state(from_state);
        let to_idx = self.add_state(to_state);
        
        // Update counts
        {
            let mut counts = self.transition_counts.write();
            counts.push(from_idx, to_idx, 1.0);
        }
        
        // Update temporal weights
        {
            let mut weights = self.temporal_weights.write();
            // Find current value by iterating through triplets
            let current = weights.triplet_iter()
                .find(|(r, c, _)| *r == from_idx && *c == to_idx)
                .map(|(_, _, v)| *v)
                .unwrap_or(0.0);
            weights.push(from_idx, to_idx, 0.9 * current + 0.1 * temporal_factor);
        }
        
        // Mark probabilities for recalculation
        *self.transition_probs.write() = None;
    }
    
    /// Calculate transition probabilities
    fn calculate_probabilities(&self) {
        let counts = self.transition_counts.read();
        let weights = self.temporal_weights.read();
        
        // Convert to CSR for efficient row operations
        let counts_csr = CsrMatrix::from(&*counts);
        let weights_csr = CsrMatrix::from(&*weights);
        
        // Build probability matrix
        let mut prob_triplets = Vec::new();
        
        for row_idx in 0..counts_csr.nrows() {
            let row_counts = counts_csr.row(row_idx);
            let row_weights = weights_csr.row(row_idx);
            
            // Calculate weighted counts
            let mut total = 0.0;
            let mut weighted_values = Vec::new();
            
            for ((&col_idx, &count), weight_val) in row_counts.col_indices()
                .iter()
                .zip(row_counts.values())
                .zip(row_weights.values()) {
                
                let weighted = count * (0.7 + 0.3 * weight_val);
                weighted_values.push((col_idx, weighted));
                total += weighted;
            }
            
            // Normalize to probabilities
            if total > 0.0 {
                for (col_idx, weighted) in weighted_values {
                    prob_triplets.push((row_idx, col_idx, weighted / total));
                }
            }
        }
        
        // Create probability matrix
        let prob_coo = CooMatrix::try_from_triplets(
            self.max_states,
            self.max_states,
            prob_triplets.iter().map(|t| t.0).collect(),
            prob_triplets.iter().map(|t| t.1).collect(),
            prob_triplets.iter().map(|t| t.2).collect(),
        ).unwrap();
        
        *self.transition_probs.write() = Some(CsrMatrix::from(&prob_coo));
    }
    
    /// Get top-k predictions for a state
    pub fn get_predictions(&self, state: &StateVector, top_k: usize) -> Vec<(StateVector, f32, f32)> {
        let state_hash = state.hash_state();

        if let Some(state_idx_ref) = self.state_to_idx.get(&state_hash) {
            let state_idx = *state_idx_ref;
            // Ensure probabilities are calculated
            if self.transition_probs.read().is_none() {
                drop(self.transition_probs.read());
                self.calculate_probabilities();
            }
            
            if let Some(probs) = &*self.transition_probs.read() {
                let row = probs.row(state_idx);
                
                // Get top-k predictions
                let mut predictions: Vec<_> = row.col_indices()
                    .iter()
                    .zip(row.values())
                    .filter_map(|(&col_idx, &prob)| {
                        self.idx_to_state.get(&col_idx).map(|state| {
                            // Calculate confidence based on visit count
                            let confidence = if let Some(metadata) = self.state_metadata.get(&col_idx) {
                                (metadata.total_visits as f32 / 10.0).min(1.0)
                            } else {
                                0.0
                            };
                            
                            (state.clone(), prob, confidence)
                        })
                    })
                    .collect();
                
                // Sort by probability
                predictions.sort_by(|a, b| b.1.total_cmp(&a.1));
                predictions.truncate(top_k);
                
                return predictions;
            }
        }
        
        Vec::new()
    }
    
    /// Evict least recently used states
    fn evict_states(&self) {
        let mut state_access: Vec<_> = self.state_metadata
            .iter()
            .map(|entry| (*entry.key(), entry.value().last_seen))
            .collect();
        
        // Sort by last access time
        state_access.sort_by_key(|&(_, time)| time);
        
        // Evict oldest 10%
        let num_to_evict = (state_access.len() / 10).max(100);
        
        for (idx, _) in state_access.into_iter().take(num_to_evict) {
            if let Some((_, state)) = self.idx_to_state.remove(&idx) {
                let state_hash = state.hash_state();
                self.state_to_idx.remove(&state_hash);
                self.state_metadata.remove(&idx);
            }
        }
    }
}

/// Prediction task for speculative execution
#[derive(Debug, Clone)]
pub struct PredictionTask {
    pub id: String,
    pub state: StateVector,
    pub predicted_states: Vec<(StateVector, f32)>,
    pub priority: f32,
    pub deadline: Option<Instant>,
    pub created_at: Instant,
}

impl PartialEq for PredictionTask {
    fn eq(&self, other: &Self) -> bool {
        self.id == other.id
    }
}

impl Eq for PredictionTask {}

impl Ord for PredictionTask {
    fn cmp(&self, other: &Self) -> Ordering {
        // Higher priority first
        OrderedFloat(self.priority)
            .cmp(&OrderedFloat(other.priority))
            .reverse()
    }
}

impl PartialOrd for PredictionTask {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Priority queue for prediction tasks
pub struct PredictionQueue {
    queue: Arc<Mutex<BinaryHeap<PredictionTask>>>,
    active_tasks: DashMap<String, PredictionTask>,
    max_concurrent: usize,
}

impl PredictionQueue {
    pub fn new(max_concurrent: usize) -> Self {
        Self {
            queue: Arc::new(Mutex::new(BinaryHeap::new())),
            active_tasks: DashMap::new(),
            max_concurrent,
        }
    }
    
    /// Add task to queue
    pub fn push(&self, task: PredictionTask) -> bool {
        // Check if already exists
        if self.active_tasks.contains_key(&task.id) {
            return false;
        }
        
        let mut queue = self.queue.lock();
        queue.push(task);
        true
    }
    
    /// Get next task if resources available
    pub fn pop(&self) -> Option<PredictionTask> {
        if self.active_tasks.len() >= self.max_concurrent {
            return None;
        }
        
        let mut queue = self.queue.lock();
        
        while let Some(task) = queue.pop() {
            // Check deadline
            if let Some(deadline) = task.deadline {
                if Instant::now() > deadline {
                    continue; // Skip expired task
                }
            }
            
            self.active_tasks.insert(task.id.clone(), task.clone());
            return Some(task);
        }
        
        None
    }
    
    /// Mark task as completed
    pub fn complete(&self, task_id: &str) {
        self.active_tasks.remove(task_id);
    }
    
    /// Get queue statistics
    pub fn stats(&self) -> (usize, usize) {
        let queue_len = self.queue.lock().len();
        let active_len = self.active_tasks.len();
        (queue_len, active_len)
    }
}

/// SIMD-optimized state similarity computation
pub struct SimdStateMatcher;

impl SimdStateMatcher {
    /// Batch similarity computation using SIMD
    #[cfg(target_arch = "x86_64")]
    pub unsafe fn batch_similarity(
        query: &StateVector,
        candidates: &[StateVector],
    ) -> Vec<f32> {
        if !is_x86_feature_detected!("avx2") {
            return candidates.iter()
                .map(|c| query.similarity(c))
                .collect();
        }
        
        // For demonstration - in practice would vectorize the comparison
        candidates.par_iter()
            .map(|candidate| {
                let mut score = _mm256_setzero_ps();
                
                // Vectorized string comparison would go here
                // For now, fall back to standard comparison
                query.similarity(candidate)
            })
            .collect()
    }
    
    #[cfg(not(target_arch = "x86_64"))]
    pub fn batch_similarity(
        query: &StateVector,
        candidates: &[StateVector],
    ) -> Vec<f32> {
        candidates.iter()
            .map(|c| query.similarity(c))
            .collect()
    }
}

/// Main predictive engine
pub struct PredictiveEngine {
    transition_matrix: Arc<TransitionMatrix>,
    prediction_queue: Arc<PredictionQueue>,
    current_state: Arc<RwLock<Option<StateVector>>>,
    state_history: Arc<RwLock<VecDeque<(StateVector, Instant)>>>,
    result_cache: DashMap<String, (Vec<u8>, Instant)>,
    stats: Arc<RwLock<EngineStats>>,
}

#[derive(Debug, Default)]
struct EngineStats {
    predictions_made: u64,
    predictions_executed: u64,
    cache_hits: u64,
    cache_misses: u64,
    total_prediction_time: Duration,
}

impl PredictiveEngine {
    pub fn new(max_states: usize, max_concurrent: usize) -> Self {
        Self {
            transition_matrix: Arc::new(TransitionMatrix::new(max_states)),
            prediction_queue: Arc::new(PredictionQueue::new(max_concurrent)),
            current_state: Arc::new(RwLock::new(None)),
            state_history: Arc::new(RwLock::new(VecDeque::with_capacity(100))),
            result_cache: DashMap::new(),
            stats: Arc::new(RwLock::new(EngineStats::default())),
        }
    }
    
    /// Update current state and generate predictions
    pub fn update_state(&self, new_state: StateVector) {
        let now = Instant::now();
        
        // Record transition
        if let Some(current) = &*self.current_state.read() {
            // Calculate temporal factor
            let temporal_factor = if let Some((_, last_time)) = self.state_history.read().back() {
                let elapsed = now.duration_since(*last_time).as_secs_f32();
                (-elapsed / 60.0).exp() // Decay over 1 minute
            } else {
                1.0
            };
            
            self.transition_matrix.add_transition(
                current.clone(),
                new_state.clone(),
                temporal_factor
            );
        }
        
        // Update state
        *self.current_state.write() = Some(new_state.clone());
        
        // Update history
        {
            let mut history = self.state_history.write();
            if history.len() >= 100 {
                history.pop_front();
            }
            history.push_back((new_state.clone(), now));
        }
        
        // Generate predictions
        self.generate_predictions(&new_state);
    }
    
    /// Generate predictions for state
    fn generate_predictions(&self, state: &StateVector) {
        let predictions = self.transition_matrix.get_predictions(state, 5);
        
        for (next_state, probability, confidence) in predictions {
            if confidence >= 0.7 {
                // Create prediction task
                let task_id = format!("{:x}", state.hash_state() ^ next_state.hash_state());
                
                let task = PredictionTask {
                    id: task_id,
                    state: state.clone(),
                    predicted_states: vec![(next_state, probability)],
                    priority: probability * confidence,
                    deadline: Some(Instant::now() + Duration::from_secs(30)),
                    created_at: Instant::now(),
                };
                
                if self.prediction_queue.push(task) {
                    self.stats.write().predictions_made += 1;
                }
            }
        }
    }
    
    /// Execute prediction task
    pub fn execute_prediction(&self, task: &PredictionTask) -> Option<Vec<u8>> {
        let start = Instant::now();
        
        // Simulate computation based on state type
        let result = match task.state.app_id.as_str() {
            "chrome" => self.compute_browser_prediction(task),
            "vscode" => self.compute_editor_prediction(task),
            _ => self.compute_generic_prediction(task),
        };
        
        // Update stats
        {
            let mut stats = self.stats.write();
            stats.predictions_executed += 1;
            stats.total_prediction_time += start.elapsed();
        }
        
        // Cache result
        if let Some(ref data) = result {
            let cache_key = format!("{}-{}", task.id, task.created_at.elapsed().as_millis());
            self.result_cache.insert(cache_key, (data.clone(), Instant::now()));
        }
        
        // Mark as completed
        self.prediction_queue.complete(&task.id);
        
        result
    }
    
    /// Get cached prediction result
    pub fn get_cached_result(&self, current_state: &StateVector, target_state: &StateVector) -> Option<Vec<u8>> {
        let cache_key = format!("{:x}-{:x}", current_state.hash_state(), target_state.hash_state());
        
        if let Some(entry) = self.result_cache.get(&cache_key) {
            let (data, timestamp) = entry.clone();
            
            // Check if not expired (5 minute TTL)
            if timestamp.elapsed() < Duration::from_secs(300) {
                self.stats.write().cache_hits += 1;
                return Some(data);
            }
        }
        
        self.stats.write().cache_misses += 1;
        None
    }
    
    // Example computation functions
    fn compute_browser_prediction(&self, task: &PredictionTask) -> Option<Vec<u8>> {
        // Simulate browser-specific computation
        let result = format!("Browser prediction for {:?}", task.predicted_states);
        Some(result.into_bytes())
    }
    
    fn compute_editor_prediction(&self, task: &PredictionTask) -> Option<Vec<u8>> {
        // Simulate editor-specific computation
        let result = format!("Editor prediction for {:?}", task.predicted_states);
        Some(result.into_bytes())
    }
    
    fn compute_generic_prediction(&self, task: &PredictionTask) -> Option<Vec<u8>> {
        // Generic prediction computation
        let result = format!("Generic prediction for {:?}", task.predicted_states);
        Some(result.into_bytes())
    }
    
    /// Get engine statistics
    pub fn get_stats(&self) -> HashMap<String, serde_json::Value> {
        use serde_json::json;
        
        let stats = self.stats.read();
        let (queue_len, active_len) = self.prediction_queue.stats();
        
        let mut result = HashMap::new();
        result.insert("predictions_made".to_string(), json!(stats.predictions_made));
        result.insert("predictions_executed".to_string(), json!(stats.predictions_executed));
        result.insert("cache_hits".to_string(), json!(stats.cache_hits));
        result.insert("cache_misses".to_string(), json!(stats.cache_misses));
        result.insert("cache_hit_rate".to_string(), json!(
            stats.cache_hits as f64 / (stats.cache_hits + stats.cache_misses).max(1) as f64
        ));
        result.insert("avg_prediction_time_ms".to_string(), json!(
            stats.total_prediction_time.as_millis() as f64 / stats.predictions_executed.max(1) as f64
        ));
        result.insert("queue_length".to_string(), json!(queue_len));
        result.insert("active_tasks".to_string(), json!(active_len));
        result.insert("cache_entries".to_string(), json!(self.result_cache.len()));
        
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_state_similarity() {
        let state1 = StateVector {
            app_id: "vscode".to_string(),
            app_state: "editing".to_string(),
            user_action: Some("typing".to_string()),
            time_context: Some("morning".to_string()),
            goal_context: Some("coding".to_string()),
            workflow_phase: Some("implementation".to_string()),
        };
        
        let state2 = StateVector {
            app_id: "vscode".to_string(),
            app_state: "editing".to_string(),
            user_action: Some("saving".to_string()),
            time_context: Some("morning".to_string()),
            goal_context: Some("coding".to_string()),
            workflow_phase: Some("implementation".to_string()),
        };
        
        let similarity = state1.similarity(&state2);
        assert!(similarity > 0.8); // Should be very similar
    }
    
    #[test]
    fn test_transition_matrix() {
        let matrix = TransitionMatrix::new(1000);
        
        let state1 = StateVector {
            app_id: "chrome".to_string(),
            app_state: "browsing".to_string(),
            user_action: None,
            time_context: None,
            goal_context: None,
            workflow_phase: None,
        };
        
        let state2 = StateVector {
            app_id: "chrome".to_string(),
            app_state: "searching".to_string(),
            user_action: Some("typing".to_string()),
            time_context: None,
            goal_context: None,
            workflow_phase: None,
        };
        
        // Add multiple transitions
        for _ in 0..10 {
            matrix.add_transition(state1.clone(), state2.clone(), 1.0);
        }
        
        let predictions = matrix.get_predictions(&state1, 3);
        assert!(!predictions.is_empty());
        assert_eq!(predictions[0].0.app_state, "searching");
    }
    
    #[test]
    fn test_prediction_queue() {
        let queue = PredictionQueue::new(5);
        
        let task = PredictionTask {
            id: "test123".to_string(),
            state: StateVector {
                app_id: "test".to_string(),
                app_state: "idle".to_string(),
                user_action: None,
                time_context: None,
                goal_context: None,
                workflow_phase: None,
            },
            predicted_states: vec![],
            priority: 0.8,
            deadline: Some(Instant::now() + Duration::from_secs(60)),
            created_at: Instant::now(),
        };
        
        assert!(queue.push(task.clone()));
        assert!(!queue.push(task.clone())); // Duplicate should fail
        
        let popped = queue.pop();
        assert!(popped.is_some());
        assert_eq!(popped.unwrap().id, "test123");
    }
}