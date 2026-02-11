//! High-performance Semantic Cache with LSH
//! Optimized for low-latency similarity search and cache operations

use std::sync::Arc;
use parking_lot::RwLock;
use dashmap::DashMap;
use rayon::prelude::*;
use serde::{Serialize, Deserialize};
use std::collections::{HashMap, HashSet};
use std::time::{Instant, Duration};
use ordered_float::OrderedFloat;
use std::hash::{Hash, Hasher};
use fnv::FnvHasher;

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

/// Cache entry with metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CacheEntry {
    pub key: String,
    pub value: Vec<u8>,
    pub embedding: Option<Vec<f32>>,
    pub context: Option<HashMap<String, String>>,
    #[serde(skip, default = "Instant::now")]
    pub timestamp: Instant,
    #[serde(skip, default = "Instant::now")]
    pub last_access: Instant,
    pub access_count: u32,
    pub ttl_seconds: u64,
    pub size_bytes: usize,
}

impl CacheEntry {
    pub fn is_expired(&self) -> bool {
        self.timestamp.elapsed() > Duration::from_secs(self.ttl_seconds)
    }

    pub fn access(&mut self) {
        self.last_access = Instant::now();
        self.access_count += 1;
    }

    pub fn calculate_value_score(&self) -> f32 {
        let age_factor = 1.0 / (1.0 + self.timestamp.elapsed().as_secs() as f32 / 3600.0);
        let access_factor = (self.access_count as f32 / 10.0).min(1.0);
        let recency_factor = 1.0 / (1.0 + self.last_access.elapsed().as_secs() as f32 / 600.0);
        let size_factor = 1.0 / (1.0 + self.size_bytes as f32 / 1_048_576.0);

        age_factor * 0.2 + access_factor * 0.4 + recency_factor * 0.3 + size_factor * 0.1
    }
}

/// LSH bucket for similarity search
#[derive(Debug, Clone)]
struct LSHBucket {
    entries: Vec<String>,
}

/// High-performance LSH implementation
pub struct LSHIndex {
    dim: usize,
    num_tables: usize,
    hash_size: usize,
    tables: Vec<DashMap<u64, LSHBucket>>,
    projections: Vec<Vec<Vec<f32>>>,
}

impl LSHIndex {
    pub fn new(dim: usize, num_tables: usize, hash_size: usize) -> Self {
        let mut projections = Vec::with_capacity(num_tables);
        
        // Generate random projections
        use rand::{thread_rng, Rng};
        let mut rng = thread_rng();
        
        for _ in 0..num_tables {
            let mut table_projections = Vec::with_capacity(hash_size);
            for _ in 0..hash_size {
                let projection: Vec<f32> = (0..dim)
                    .map(|_| rng.gen_range(-1.0..1.0))
                    .collect();
                table_projections.push(projection);
            }
            projections.push(table_projections);
        }
        
        let tables = (0..num_tables)
            .map(|_| DashMap::new())
            .collect();
        
        Self {
            dim,
            num_tables,
            hash_size,
            tables,
            projections,
        }
    }

    /// Compute hash using SIMD operations when available
    #[cfg(target_arch = "x86_64")]
    unsafe fn compute_hash_simd(&self, vector: &[f32], table_idx: usize) -> u64 {
        if !is_x86_feature_detected!("avx2") {
            return self.compute_hash_scalar(vector, table_idx);
        }

        let mut hash = 0u64;
        let projections = &self.projections[table_idx];
        
        for (bit_idx, projection) in projections.iter().enumerate() {
            let mut sum = _mm256_setzero_ps();
            
            // Process 8 floats at a time, bounded by both vector and projection length
            let simd_len = vector.len().min(projection.len()) / 8;
            let mut i = 0;

            while i < simd_len {
                let v = _mm256_loadu_ps(vector[i * 8..].as_ptr());
                let p = _mm256_loadu_ps(projection[i * 8..].as_ptr());
                let prod = _mm256_mul_ps(v, p);
                sum = _mm256_add_ps(sum, prod);
                i += 1;
            }

            // Sum the vector elements
            let sum_array = std::mem::transmute::<__m256, [f32; 8]>(sum);
            let mut total: f32 = sum_array.iter().sum();

            // Process remainder (elements beyond SIMD-processed range)
            let scalar_start = simd_len * 8;
            let scalar_end = vector.len().min(projection.len());
            for j in scalar_start..scalar_end {
                total += vector[j] * projection[j];
            }
            
            // Set bit if positive
            if total > 0.0 {
                hash |= 1u64 << bit_idx;
            }
        }
        
        hash
    }

    #[cfg(not(target_arch = "x86_64"))]
    fn compute_hash_simd(&self, vector: &[f32], table_idx: usize) -> u64 {
        self.compute_hash_scalar(vector, table_idx)
    }

    fn compute_hash_scalar(&self, vector: &[f32], table_idx: usize) -> u64 {
        let mut hash = 0u64;
        let projections = &self.projections[table_idx];
        
        for (bit_idx, projection) in projections.iter().enumerate() {
            let dot_product: f32 = vector.iter()
                .zip(projection.iter())
                .map(|(a, b)| a * b)
                .sum();
            
            if dot_product > 0.0 {
                hash |= 1u64 << bit_idx;
            }
        }
        
        hash
    }

    pub fn add(&self, key: String, vector: &[f32]) {
        for i in 0..self.num_tables {
            #[cfg(target_arch = "x86_64")]
            let hash = unsafe { self.compute_hash_simd(vector, i) };
            #[cfg(not(target_arch = "x86_64"))]
            let hash = self.compute_hash_simd(vector, i);
            
            self.tables[i]
                .entry(hash)
                .or_insert_with(|| LSHBucket { entries: Vec::new() })
                .entries
                .push(key.clone());
        }
    }

    pub fn query(&self, vector: &[f32], max_candidates: usize) -> Vec<String> {
        let mut candidates = HashSet::new();
        
        // Query each table in parallel
        let table_results: Vec<_> = (0..self.num_tables)
            .into_par_iter()
            .map(|i| {
                #[cfg(target_arch = "x86_64")]
                let hash = unsafe { self.compute_hash_simd(vector, i) };
                #[cfg(not(target_arch = "x86_64"))]
                let hash = self.compute_hash_simd(vector, i);
                
                let mut table_candidates = Vec::new();
                
                // Check exact bucket
                if let Some(bucket) = self.tables[i].get(&hash) {
                    table_candidates.extend_from_slice(&bucket.entries);
                }
                
                // Check nearby buckets (1-bit flips)
                for bit in 0..self.hash_size.min(16) {
                    let nearby_hash = hash ^ (1u64 << bit);
                    if let Some(bucket) = self.tables[i].get(&nearby_hash) {
                        table_candidates.extend_from_slice(&bucket.entries);
                    }
                }
                
                table_candidates
            })
            .collect();
        
        // Merge results
        for table_candidates in table_results {
            candidates.extend(table_candidates);
            if candidates.len() >= max_candidates {
                break;
            }
        }
        
        candidates.into_iter().take(max_candidates).collect()
    }

    pub fn remove(&self, key: &str, vector: &[f32]) {
        for i in 0..self.num_tables {
            #[cfg(target_arch = "x86_64")]
            let hash = unsafe { self.compute_hash_simd(vector, i) };
            #[cfg(not(target_arch = "x86_64"))]
            let hash = self.compute_hash_simd(vector, i);
            
            if let Some(mut bucket) = self.tables[i].get_mut(&hash) {
                bucket.entries.retain(|k| k != key);
            }
        }
    }
}

/// Similarity computation with SIMD
pub struct SimilarityComputer;

impl SimilarityComputer {
    /// Compute cosine similarity using SIMD
    #[cfg(target_arch = "x86_64")]
    pub unsafe fn cosine_similarity_simd(a: &[f32], b: &[f32]) -> f32 {
        if !is_x86_feature_detected!("avx2") {
            return Self::cosine_similarity_scalar(a, b);
        }

        let mut dot = _mm256_setzero_ps();
        let mut norm_a = _mm256_setzero_ps();
        let mut norm_b = _mm256_setzero_ps();
        
        let chunks = a.chunks_exact(8).zip(b.chunks_exact(8));
        let remainder_a = a.chunks_exact(8).remainder();
        let remainder_b = b.chunks_exact(8).remainder();
        
        for (chunk_a, chunk_b) in chunks {
            let va = _mm256_loadu_ps(chunk_a.as_ptr());
            let vb = _mm256_loadu_ps(chunk_b.as_ptr());
            
            dot = _mm256_fmadd_ps(va, vb, dot);
            norm_a = _mm256_fmadd_ps(va, va, norm_a);
            norm_b = _mm256_fmadd_ps(vb, vb, norm_b);
        }
        
        // Sum the vectors
        let dot_array = std::mem::transmute::<__m256, [f32; 8]>(dot);
        let norm_a_array = std::mem::transmute::<__m256, [f32; 8]>(norm_a);
        let norm_b_array = std::mem::transmute::<__m256, [f32; 8]>(norm_b);
        
        let mut dot_sum: f32 = dot_array.iter().sum();
        let mut norm_a_sum: f32 = norm_a_array.iter().sum();
        let mut norm_b_sum: f32 = norm_b_array.iter().sum();
        
        // Process remainder
        for (i, (&a_val, &b_val)) in remainder_a.iter().zip(remainder_b.iter()).enumerate() {
            dot_sum += a_val * b_val;
            norm_a_sum += a_val * a_val;
            norm_b_sum += b_val * b_val;
        }
        
        if norm_a_sum > 0.0 && norm_b_sum > 0.0 {
            dot_sum / (norm_a_sum.sqrt() * norm_b_sum.sqrt())
        } else {
            0.0
        }
    }

    #[cfg(not(target_arch = "x86_64"))]
    pub fn cosine_similarity_simd(a: &[f32], b: &[f32]) -> f32 {
        Self::cosine_similarity_scalar(a, b)
    }

    pub fn cosine_similarity_scalar(a: &[f32], b: &[f32]) -> f32 {
        let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
        let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
        let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
        
        if norm_a > 0.0 && norm_b > 0.0 {
            dot / (norm_a * norm_b)
        } else {
            0.0
        }
    }
}

/// Multi-level cache with LSH
pub struct SemanticCacheLSH {
    l1_exact: DashMap<String, CacheEntry>,
    l2_semantic: Arc<RwLock<HashMap<String, CacheEntry>>>,
    l2_lsh: Arc<LSHIndex>,
    l3_contextual: DashMap<String, CacheEntry>,
    l4_predictive: DashMap<String, CacheEntry>,
    
    // Statistics
    stats: Arc<RwLock<CacheStats>>,
    
    // Configuration
    similarity_threshold: f32,
    embedding_dim: usize,
}

#[derive(Debug, Default, Clone)]
struct CacheStats {
    l1_hits: u64,
    l1_misses: u64,
    l2_hits: u64,
    l2_misses: u64,
    l3_hits: u64,
    l3_misses: u64,
    l4_hits: u64,
    l4_misses: u64,
}

impl SemanticCacheLSH {
    pub fn new(embedding_dim: usize) -> Self {
        Self {
            l1_exact: DashMap::new(),
            l2_semantic: Arc::new(RwLock::new(HashMap::new())),
            l2_lsh: Arc::new(LSHIndex::new(embedding_dim, 12, 10)),
            l3_contextual: DashMap::new(),
            l4_predictive: DashMap::new(),
            stats: Arc::new(RwLock::new(CacheStats::default())),
            similarity_threshold: 0.85,
            embedding_dim,
        }
    }

    /// Get from L1 exact cache
    pub fn get_l1(&self, key: &str) -> Option<CacheEntry> {
        if let Some(mut entry) = self.l1_exact.get_mut(key) {
            if !entry.is_expired() {
                entry.access();
                self.stats.write().l1_hits += 1;
                return Some(entry.clone());
            } else {
                drop(entry);
                self.l1_exact.remove(key);
            }
        }
        
        self.stats.write().l1_misses += 1;
        None
    }

    /// Get from L2 semantic cache with LSH
    pub fn get_l2(&self, key: &str, embedding: &[f32]) -> Option<(CacheEntry, f32)> {
        // Get candidates from LSH
        let candidates = self.l2_lsh.query(embedding, 20);
        
        if candidates.is_empty() {
            self.stats.write().l2_misses += 1;
            return None;
        }
        
        // Find best match
        let l2_cache = self.l2_semantic.read();
        let mut best_match = None;
        let mut best_similarity = 0.0;
        
        for candidate_key in candidates {
            if let Some(entry) = l2_cache.get(&candidate_key) {
                if entry.is_expired() {
                    continue;
                }
                
                if let Some(ref entry_embedding) = entry.embedding {
                    #[cfg(target_arch = "x86_64")]
                    let similarity = unsafe {
                        SimilarityComputer::cosine_similarity_simd(embedding, entry_embedding)
                    };
                    #[cfg(not(target_arch = "x86_64"))]
                    let similarity = SimilarityComputer::cosine_similarity_simd(embedding, entry_embedding);
                    
                    if similarity > best_similarity && similarity >= self.similarity_threshold {
                        best_similarity = similarity;
                        best_match = Some(entry.clone());
                    }
                }
            }
        }
        
        if let Some(mut entry) = best_match {
            entry.access();
            self.stats.write().l2_hits += 1;
            Some((entry, best_similarity))
        } else {
            self.stats.write().l2_misses += 1;
            None
        }
    }

    /// Put in L1 cache
    pub fn put_l1(&self, key: String, mut entry: CacheEntry) {
        entry.size_bytes = entry.value.len();
        self.l1_exact.insert(key, entry);
    }

    /// Put in L2 cache with LSH indexing
    pub fn put_l2(&self, key: String, mut entry: CacheEntry) {
        if let Some(ref embedding) = entry.embedding {
            // Add to LSH index
            self.l2_lsh.add(key.clone(), embedding);
            
            // Add to cache
            entry.size_bytes = entry.value.len() + embedding.len() * 4;
            self.l2_semantic.write().insert(key, entry);
        }
    }

    /// Batch similarity search
    pub fn batch_similarity_search(&self, embeddings: &[Vec<f32>], top_k: usize) -> Vec<Vec<(String, f32)>> {
        embeddings
            .par_iter()
            .map(|embedding| {
                let candidates = self.l2_lsh.query(embedding, top_k * 2);
                let l2_cache = self.l2_semantic.read();
                
                let mut results: Vec<_> = candidates
                    .into_iter()
                    .filter_map(|key| {
                        l2_cache.get(&key).and_then(|entry| {
                            entry.embedding.as_ref().map(|e| {
                                #[cfg(target_arch = "x86_64")]
                                let sim = unsafe {
                                    SimilarityComputer::cosine_similarity_simd(embedding, e)
                                };
                                #[cfg(not(target_arch = "x86_64"))]
                                let sim = SimilarityComputer::cosine_similarity_simd(embedding, e);
                                (key, sim)
                            })
                        })
                    })
                    .filter(|(_, sim)| *sim >= self.similarity_threshold)
                    .collect();
                
                results.sort_by_key(|(_, sim)| OrderedFloat(-*sim));
                results.truncate(top_k);
                results
            })
            .collect()
    }

    /// Get cache statistics
    pub fn get_stats(&self) -> CacheStats {
        (*self.stats.read()).clone()
    }

    /// Clear all caches
    pub fn clear(&self) {
        self.l1_exact.clear();
        self.l2_semantic.write().clear();
        self.l3_contextual.clear();
        self.l4_predictive.clear();
        *self.stats.write() = CacheStats::default();
    }
}

/// Predictive cache entry
#[derive(Debug, Clone)]
pub struct PredictiveEntry {
    pub query: String,
    pub confidence: f32,
    pub predicted_at: Instant,
    pub source: String,
}

/// Pattern-based predictor for cache pre-warming
pub struct CachePredictor {
    access_history: Arc<RwLock<Vec<(String, Instant)>>>,
    pattern_sequences: Arc<RwLock<HashMap<String, Vec<String>>>>,
    max_history: usize,
}

impl CachePredictor {
    pub fn new() -> Self {
        Self {
            access_history: Arc::new(RwLock::new(Vec::new())),
            pattern_sequences: Arc::new(RwLock::new(HashMap::new())),
            max_history: 1000,
        }
    }

    pub fn record_access(&self, query: String) {
        let mut history = self.access_history.write();
        history.push((query.clone(), Instant::now()));
        
        // Maintain max size
        if history.len() > self.max_history {
            let drain_count = history.len() - self.max_history;
            history.drain(0..drain_count);
        }
        
        // Update patterns
        if history.len() >= 2 {
            let prev_query = history[history.len() - 2].0.clone();
            drop(history);
            
            self.pattern_sequences
                .write()
                .entry(prev_query)
                .or_insert_with(Vec::new)
                .push(query);
        }
    }

    pub fn predict_next(&self, current_query: &str, top_k: usize) -> Vec<PredictiveEntry> {
        let sequences = self.pattern_sequences.read();
        
        if let Some(next_queries) = sequences.get(current_query) {
            // Count frequencies
            let mut freq_map = HashMap::new();
            for q in next_queries {
                *freq_map.entry(q.clone()).or_insert(0) += 1;
            }
            
            // Calculate confidence
            let total = next_queries.len() as f32;
            let mut predictions: Vec<_> = freq_map
                .into_iter()
                .map(|(query, count)| PredictiveEntry {
                    query,
                    confidence: count as f32 / total,
                    predicted_at: Instant::now(),
                    source: "pattern".to_string(),
                })
                .collect();
            
            predictions.sort_by_key(|p| OrderedFloat(-p.confidence));
            predictions.truncate(top_k);
            predictions
        } else {
            Vec::new()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lsh_index() {
        let lsh = LSHIndex::new(128, 5, 8);
        
        let vec1: Vec<f32> = (0..128).map(|i| i as f32 / 128.0).collect();
        let vec2: Vec<f32> = (0..128).map(|i| (i as f32 / 128.0) * 0.9).collect();
        
        lsh.add("key1".to_string(), &vec1);
        lsh.add("key2".to_string(), &vec2);
        
        let results = lsh.query(&vec1, 10);
        assert!(results.contains(&"key1".to_string()));
    }

    #[test]
    fn test_cosine_similarity() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![0.0, 1.0, 0.0];
        let c = vec![1.0, 0.0, 0.0];
        
        #[cfg(target_arch = "x86_64")]
        unsafe {
            assert!((SimilarityComputer::cosine_similarity_simd(&a, &c) - 1.0).abs() < 0.001);
            assert!((SimilarityComputer::cosine_similarity_simd(&a, &b) - 0.0).abs() < 0.001);
        }
    }

    #[test]
    fn test_cache_predictor() {
        let predictor = CachePredictor::new();
        
        predictor.record_access("query1".to_string());
        predictor.record_access("query2".to_string());
        predictor.record_access("query1".to_string());
        predictor.record_access("query2".to_string());
        
        let predictions = predictor.predict_next("query1", 3);
        assert!(!predictions.is_empty());
        assert_eq!(predictions[0].query, "query2");
    }
}