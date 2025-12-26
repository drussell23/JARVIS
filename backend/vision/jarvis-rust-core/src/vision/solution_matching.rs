//! High-performance solution matching and similarity search
//! 
//! This module provides fast solution matching using SIMD operations
//! and efficient data structures for the Solution Memory Bank.

use std::collections::{HashMap, BTreeMap};
use std::sync::{Arc, RwLock};
use serde::{Serialize, Deserialize};
use ordered_float::OrderedFloat;
use rayon::prelude::*;

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

#[cfg(target_arch = "aarch64")]
use std::arch::aarch64::*;

/// Problem signature for matching
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProblemSignature {
    pub signature_id: String,
    pub feature_vector: Vec<f32>,
    pub error_keywords: Vec<String>,
    pub symptom_keywords: Vec<String>,
    pub context_hash: u64,
}

/// Solution entry for matching
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolutionEntry {
    pub solution_id: String,
    pub problem_vector: Vec<f32>,
    pub effectiveness: f32,
    pub usage_count: u32,
    pub success_rate: f32,
}

/// High-performance solution matcher
pub struct SolutionMatcher {
    solutions: Arc<RwLock<HashMap<String, SolutionEntry>>>,
    keyword_index: Arc<RwLock<HashMap<String, Vec<String>>>>, // keyword -> solution_ids
    vector_dimension: usize,
}

impl SolutionMatcher {
    pub fn new(vector_dimension: usize) -> Self {
        Self {
            solutions: Arc::new(RwLock::new(HashMap::new())),
            keyword_index: Arc::new(RwLock::new(HashMap::new())),
            vector_dimension,
        }
    }
    
    /// Add a solution to the matcher
    pub fn add_solution(&self, solution: SolutionEntry, keywords: Vec<String>) {
        let solution_id = solution.solution_id.clone();
        
        // Add to main storage
        {
            let mut solutions = self.solutions.write().unwrap();
            solutions.insert(solution_id.clone(), solution);
        }
        
        // Update keyword index
        {
            let mut index = self.keyword_index.write().unwrap();
            for keyword in keywords {
                index.entry(keyword)
                    .or_insert_with(Vec::new)
                    .push(solution_id.clone());
            }
        }
    }
    
    /// Find solutions matching keywords
    pub fn find_by_keywords(&self, keywords: &[String], min_match: usize) -> Vec<String> {
        let index = self.keyword_index.read().unwrap();
        let mut solution_counts: HashMap<String, usize> = HashMap::new();
        
        for keyword in keywords {
            if let Some(solution_ids) = index.get(keyword) {
                for solution_id in solution_ids {
                    *solution_counts.entry(solution_id.clone()).or_insert(0) += 1;
                }
            }
        }
        
        // Filter by minimum match count
        solution_counts.into_iter()
            .filter(|(_, count)| *count >= min_match)
            .map(|(id, _)| id)
            .collect()
    }
    
    /// Calculate similarity between two vectors using SIMD
    #[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
    pub fn calculate_similarity(&self, vec1: &[f32], vec2: &[f32]) -> f32 {
        if vec1.len() != vec2.len() || vec1.is_empty() {
            return 0.0;
        }
        
        #[cfg(target_arch = "x86_64")]
        unsafe {
            if is_x86_feature_detected!("avx2") {
                return self.similarity_avx2(vec1, vec2);
            }
        }
        
        // Fallback to standard implementation
        self.similarity_standard(vec1, vec2)
    }
    
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    pub fn calculate_similarity(&self, vec1: &[f32], vec2: &[f32]) -> f32 {
        self.similarity_standard(vec1, vec2)
    }
    
    /// Standard similarity calculation (cosine similarity)
    fn similarity_standard(&self, vec1: &[f32], vec2: &[f32]) -> f32 {
        let dot_product: f32 = vec1.iter()
            .zip(vec2.iter())
            .map(|(a, b)| a * b)
            .sum();
        
        let norm1: f32 = vec1.iter().map(|x| x * x).sum::<f32>().sqrt();
        let norm2: f32 = vec2.iter().map(|x| x * x).sum::<f32>().sqrt();
        
        if norm1 == 0.0 || norm2 == 0.0 {
            0.0
        } else {
            dot_product / (norm1 * norm2)
        }
    }
    
    /// AVX2 optimized similarity calculation
    #[cfg(target_arch = "x86_64")]
    unsafe fn similarity_avx2(&self, vec1: &[f32], vec2: &[f32]) -> f32 {
        use std::arch::x86_64::*;
        
        let len = vec1.len();
        let simd_len = len / 8 * 8;
        
        let mut dot_sum = _mm256_setzero_ps();
        let mut norm1_sum = _mm256_setzero_ps();
        let mut norm2_sum = _mm256_setzero_ps();
        
        for i in (0..simd_len).step_by(8) {
            let v1 = _mm256_loadu_ps(vec1.as_ptr().add(i));
            let v2 = _mm256_loadu_ps(vec2.as_ptr().add(i));
            
            // dot product
            dot_sum = _mm256_fmadd_ps(v1, v2, dot_sum);
            
            // norms
            norm1_sum = _mm256_fmadd_ps(v1, v1, norm1_sum);
            norm2_sum = _mm256_fmadd_ps(v2, v2, norm2_sum);
        }
        
        // Sum the SIMD vectors
        let dot_array: [f32; 8] = std::mem::transmute(dot_sum);
        let norm1_array: [f32; 8] = std::mem::transmute(norm1_sum);
        let norm2_array: [f32; 8] = std::mem::transmute(norm2_sum);
        
        let mut dot_product = dot_array.iter().sum::<f32>();
        let mut norm1_sq = norm1_array.iter().sum::<f32>();
        let mut norm2_sq = norm2_array.iter().sum::<f32>();
        
        // Handle remaining elements
        for i in simd_len..len {
            dot_product += vec1[i] * vec2[i];
            norm1_sq += vec1[i] * vec1[i];
            norm2_sq += vec2[i] * vec2[i];
        }
        
        let norm1 = norm1_sq.sqrt();
        let norm2 = norm2_sq.sqrt();
        
        if norm1 == 0.0 || norm2 == 0.0 {
            0.0
        } else {
            dot_product / (norm1 * norm2)
        }
    }
    
    /// Find most similar solutions using parallel search
    pub fn find_similar_solutions(
        &self, 
        problem: &ProblemSignature, 
        top_k: usize,
        min_similarity: f32
    ) -> Vec<(String, f32)> {
        let solutions = self.solutions.read().unwrap();
        
        // Parallel similarity calculation
        let mut similarities: Vec<(String, f32)> = solutions
            .par_iter()
            .map(|(id, solution)| {
                let similarity = self.calculate_similarity(
                    &problem.feature_vector,
                    &solution.problem_vector
                );
                (id.clone(), similarity)
            })
            .filter(|(_, sim)| *sim >= min_similarity)
            .collect();
        
        // Sort by similarity (descending)
        similarities.par_sort_unstable_by_key(|(_, sim)| OrderedFloat(-*sim));
        
        // Take top k
        similarities.truncate(top_k);
        similarities
    }
    
    /// Calculate combined score with effectiveness
    pub fn calculate_combined_score(
        &self,
        similarity: f32,
        effectiveness: f32,
        usage_count: u32,
        success_rate: f32
    ) -> f32 {
        // Weighted combination
        let usage_factor = (usage_count as f32).log2().max(1.0) / 10.0;
        let usage_boost = 1.0 + usage_factor.min(0.2); // Max 20% boost
        
        (similarity * 0.4 + effectiveness * 0.4 + success_rate * 0.2) * usage_boost
    }
}

/// Efficient similarity index using LSH (Locality Sensitive Hashing)
pub struct SimilarityIndex {
    dimension: usize,
    hash_tables: Vec<HashMap<u64, Vec<String>>>,
    num_tables: usize,
    solutions: Arc<RwLock<HashMap<String, Vec<f32>>>>,
}

impl SimilarityIndex {
    pub fn new(dimension: usize, num_tables: usize) -> Self {
        let mut hash_tables = Vec::with_capacity(num_tables);
        for _ in 0..num_tables {
            hash_tables.push(HashMap::new());
        }
        
        Self {
            dimension,
            hash_tables,
            num_tables,
            solutions: Arc::new(RwLock::new(HashMap::new())),
        }
    }
    
    /// Generate LSH hash for a vector
    fn lsh_hash(&self, vector: &[f32], table_idx: usize) -> u64 {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        
        let mut hasher = DefaultHasher::new();
        
        // Simple random projection LSH
        for (i, &val) in vector.iter().enumerate() {
            let projected = val * ((i + table_idx * 1000) as f32).sin();
            let bit = if projected > 0.0 { 1u8 } else { 0u8 };
            bit.hash(&mut hasher);
        }
        
        hasher.finish()
    }
    
    /// Add vector to index
    pub fn add_vector(&mut self, id: String, vector: Vec<f32>) {
        // Store vector
        {
            let mut solutions = self.solutions.write().unwrap();
            solutions.insert(id.clone(), vector.clone());
        }
        
        // Add to hash tables
        for i in 0..self.num_tables {
            let hash = self.lsh_hash(&vector, i);
            self.hash_tables[i]
                .entry(hash)
                .or_insert_with(Vec::new)
                .push(id.clone());
        }
    }
    
    /// Search for similar vectors
    pub fn search(&self, query: &[f32], max_results: usize) -> Vec<(String, f32)> {
        let mut candidates = std::collections::HashSet::new();
        
        // Get candidates from all hash tables
        for i in 0..self.num_tables {
            let hash = self.lsh_hash(query, i);
            if let Some(bucket) = self.hash_tables[i].get(&hash) {
                for id in bucket {
                    candidates.insert(id.clone());
                }
            }
        }
        
        // Calculate actual similarities for candidates
        let solutions = self.solutions.read().unwrap();
        let matcher = SolutionMatcher::new(self.dimension);
        
        let mut results: Vec<(String, f32)> = candidates
            .into_par_iter()
            .filter_map(|id| {
                solutions.get(&id).map(|vec| {
                    let similarity = matcher.calculate_similarity(query, vec);
                    (id, similarity)
                })
            })
            .collect();
        
        // Sort and return top results
        results.par_sort_unstable_by_key(|(_, sim)| OrderedFloat(-*sim));
        results.truncate(max_results);
        results
    }
}

/// Solution pattern analyzer for identifying common patterns
pub struct PatternAnalyzer {
    min_support: f32,
    patterns: Arc<RwLock<HashMap<String, PatternInfo>>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternInfo {
    pub pattern_id: String,
    pub action_sequence: Vec<String>,
    pub occurrence_count: u32,
    pub success_rate: f32,
    pub avg_execution_time: f32,
}

impl PatternAnalyzer {
    pub fn new(min_support: f32) -> Self {
        Self {
            min_support,
            patterns: Arc::new(RwLock::new(HashMap::new())),
        }
    }
    
    /// Analyze solutions to find common patterns
    pub fn analyze_patterns(&self, solutions: &[SolutionEntry]) -> Vec<PatternInfo> {
        // Extract action sequences and analyze patterns
        // This is a simplified implementation - real one would use FP-Growth or similar
        
        let mut pattern_counts: HashMap<Vec<String>, (u32, f32, f32)> = HashMap::new();
        
        // Count occurrences
        for solution in solutions {
            // Assuming we have action sequences in metadata
            // This would need to be extended with actual solution data
            let sequence = vec!["action1".to_string(), "action2".to_string()]; // Placeholder
            
            let entry = pattern_counts.entry(sequence).or_insert((0, 0.0, 0.0));
            entry.0 += 1;
            entry.1 += solution.success_rate;
            entry.2 += 30.0; // Placeholder execution time
        }
        
        // Filter by minimum support
        let total = solutions.len() as f32;
        let patterns: Vec<PatternInfo> = pattern_counts
            .into_iter()
            .filter(|(_, (count, _, _))| (*count as f32 / total) >= self.min_support)
            .map(|(sequence, (count, total_success, total_time))| {
                PatternInfo {
                    pattern_id: format!("pattern_{}", uuid::Uuid::new_v4()),
                    action_sequence: sequence,
                    occurrence_count: count,
                    success_rate: total_success / count as f32,
                    avg_execution_time: total_time / count as f32,
                }
            })
            .collect();
        
        // Store patterns
        {
            let mut stored_patterns = self.patterns.write().unwrap();
            for pattern in &patterns {
                stored_patterns.insert(pattern.pattern_id.clone(), pattern.clone());
            }
        }
        
        patterns
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_solution_matcher() {
        let matcher = SolutionMatcher::new(128);
        
        let solution = SolutionEntry {
            solution_id: "test1".to_string(),
            problem_vector: vec![0.1; 128],
            effectiveness: 0.9,
            usage_count: 10,
            success_rate: 0.95,
        };
        
        matcher.add_solution(solution, vec!["error".to_string(), "crash".to_string()]);
        
        let results = matcher.find_by_keywords(&["error".to_string()], 1);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0], "test1");
    }
    
    #[test]
    fn test_similarity_calculation() {
        let matcher = SolutionMatcher::new(4);
        
        let vec1 = vec![1.0, 0.0, 1.0, 0.0];
        let vec2 = vec![1.0, 0.0, 1.0, 0.0];
        let vec3 = vec![0.0, 1.0, 0.0, 1.0];
        
        let sim1 = matcher.calculate_similarity(&vec1, &vec2);
        let sim2 = matcher.calculate_similarity(&vec1, &vec3);
        
        assert!((sim1 - 1.0).abs() < 0.001); // Should be 1.0 (identical)
        assert!((sim2 - 0.0).abs() < 0.001); // Should be 0.0 (orthogonal)
    }
    
    #[test]
    fn test_similarity_index() {
        let index = SimilarityIndex::new(64, 5);
        
        let vec1 = vec![0.5; 64];
        let vec2 = vec![0.6; 64];
        
        index.add_vector("sol1".to_string(), vec1);
        index.add_vector("sol2".to_string(), vec2);
        
        let query = vec![0.55; 64];
        let results = index.search(&query, 2);
        
        assert!(!results.is_empty());
    }
}

// Re-export for Python bindings
pub use self::{
    SolutionMatcher as PySolutionMatcher,
    SimilarityIndex as PySimilarityIndex,
};