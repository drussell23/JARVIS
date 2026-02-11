//! High-performance pattern mining for Workflow Pattern Engine
//! Implements efficient sequence mining algorithms with zero hardcoding

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;
use parking_lot::RwLock;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use ordered_float::OrderedFloat;

/// Represents a mined sequence pattern
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MinedPattern {
    pub pattern_id: String,
    pub sequence: Vec<String>,
    pub frequency: u32,
    pub support: f32,
    pub confidence: f32,
    pub variations: Vec<Vec<String>>,
    pub metadata: HashMap<String, serde_json::Value>,
}

/// Frequent pattern node for FP-Growth algorithm
#[derive(Debug)]
struct FPNode {
    item: String,
    count: u32,
    children: HashMap<String, Box<FPNode>>,
    parent: Option<*const FPNode>,
}

// FPNode is safe to Send/Sync because:
// 1. The parent pointer is only used for tree traversal within synchronized contexts
// 2. All access to the tree is protected by RwLock
unsafe impl Send for FPNode {}
unsafe impl Sync for FPNode {}

impl FPNode {
    fn new(item: String) -> Self {
        Self {
            item,
            count: 0,
            children: HashMap::new(),
            parent: None,
        }
    }
}

/// High-performance sequence pattern miner
pub struct PatternMiner {
    min_support: f32,
    max_pattern_length: usize,
    patterns: Arc<RwLock<HashMap<String, MinedPattern>>>,
    fp_tree: Arc<RwLock<FPNode>>,
}

impl PatternMiner {
    pub fn new(min_support: f32, max_pattern_length: usize) -> Self {
        Self {
            min_support,
            max_pattern_length,
            patterns: Arc::new(RwLock::new(HashMap::new())),
            fp_tree: Arc::new(RwLock::new(FPNode::new(String::from("root")))),
        }
    }
    
    /// Mine frequent patterns from sequences using FP-Growth
    pub fn mine_patterns(&self, sequences: Vec<Vec<String>>) -> Vec<MinedPattern> {
        if sequences.is_empty() {
            return vec![];
        }
        
        let total_sequences = sequences.len() as f32;
        let min_count = (self.min_support * total_sequences).ceil() as u32;
        
        // Step 1: Count item frequencies
        let item_counts = self.count_item_frequencies(&sequences);
        
        // Step 2: Filter by minimum support
        let frequent_items: Vec<_> = item_counts
            .iter()
            .filter(|(_, &count)| count >= min_count)
            .map(|(item, count)| (item.clone(), *count))
            .collect();
        
        // Step 3: Build FP-Tree
        self.build_fp_tree(&sequences, &frequent_items);
        
        // Step 4: Mine patterns from FP-Tree
        let patterns = self.mine_fp_tree(&frequent_items, min_count);
        
        // Step 5: Find pattern variations
        let patterns_with_variations = self.find_pattern_variations(patterns, &sequences);
        
        // Store patterns
        {
            let mut stored_patterns = self.patterns.write();
            for pattern in &patterns_with_variations {
                stored_patterns.insert(pattern.pattern_id.clone(), pattern.clone());
            }
        }
        
        patterns_with_variations
    }
    
    /// Count item frequencies in sequences
    fn count_item_frequencies(&self, sequences: &[Vec<String>]) -> HashMap<String, u32> {
        sequences
            .par_iter()
            .fold(HashMap::new, |mut acc, seq| {
                for item in seq.iter() {
                    *acc.entry(item.clone()).or_insert(0) += 1;
                }
                acc
            })
            .reduce(HashMap::new, |mut acc, map| {
                for (item, count) in map {
                    *acc.entry(item).or_insert(0) += count;
                }
                acc
            })
    }
    
    /// Build FP-Tree from sequences
    fn build_fp_tree(&self, sequences: &[Vec<String>], frequent_items: &[(String, u32)]) {
        // Create item order map for sorting
        let item_order: HashMap<_, _> = frequent_items
            .iter()
            .enumerate()
            .map(|(idx, (item, _))| (item.clone(), idx))
            .collect();
        
        let mut tree = self.fp_tree.write();
        
        for sequence in sequences {
            // Filter and sort sequence by frequency
            let mut filtered_seq: Vec<_> = sequence
                .iter()
                .filter(|item| item_order.contains_key(*item))
                .cloned()
                .collect();
            
            filtered_seq.sort_by_key(|item| item_order.get(item).unwrap());
            
            // Insert into FP-Tree
            self.insert_sequence(&mut tree, &filtered_seq);
        }
    }
    
    /// Insert sequence into FP-Tree
    fn insert_sequence(&self, node: &mut FPNode, sequence: &[String]) {
        if sequence.is_empty() {
            return;
        }
        
        let first = &sequence[0];
        let rest = &sequence[1..];

        // Get pointer to parent before mutable borrow
        let parent_ptr: *const FPNode = node;

        let child = node.children.entry(first.clone()).or_insert_with(|| {
            let mut child_node = Box::new(FPNode::new(first.clone()));
            child_node.parent = Some(parent_ptr);
            child_node
        });

        child.count += 1;

        if !rest.is_empty() {
            self.insert_sequence(child, rest);
        }
    }
    
    /// Mine patterns from FP-Tree
    fn mine_fp_tree(&self, frequent_items: &[(String, u32)], min_count: u32) -> Vec<MinedPattern> {
        let mut all_patterns = Vec::new();
        
        // Mine patterns for each item
        for (item, _) in frequent_items {
            let conditional_patterns = self.get_conditional_patterns(item);
            let patterns = self.mine_conditional_patterns(conditional_patterns, item, min_count);
            all_patterns.extend(patterns);
        }
        
        // Add single items as patterns
        for (item, count) in frequent_items {
            if *count >= min_count {
                all_patterns.push(MinedPattern {
                    pattern_id: format!("pattern_{}", item),
                    sequence: vec![item.clone()],
                    frequency: *count,
                    support: *count as f32 / frequent_items.len() as f32,
                    confidence: 1.0,
                    variations: vec![],
                    metadata: HashMap::new(),
                });
            }
        }
        
        all_patterns
    }
    
    /// Get conditional patterns for an item
    fn get_conditional_patterns(&self, item: &str) -> Vec<(Vec<String>, u32)> {
        let tree = self.fp_tree.read();
        let mut patterns = Vec::new();
        
        // Find all occurrences of the item in the tree
        self.collect_conditional_patterns(&tree, item, &mut patterns, Vec::new());
        
        patterns
    }
    
    /// Recursively collect conditional patterns
    fn collect_conditional_patterns(
        &self,
        node: &FPNode,
        target_item: &str,
        patterns: &mut Vec<(Vec<String>, u32)>,
        mut current_path: Vec<String>,
    ) {
        if node.item == target_item && node.count > 0 {
            if !current_path.is_empty() {
                patterns.push((current_path.clone(), node.count));
            }
        }
        
        if node.item != "root" {
            current_path.push(node.item.clone());
        }
        
        for child in node.children.values() {
            self.collect_conditional_patterns(child, target_item, patterns, current_path.clone());
        }
    }
    
    /// Mine conditional patterns
    fn mine_conditional_patterns(
        &self,
        conditional_patterns: Vec<(Vec<String>, u32)>,
        base_item: &str,
        min_count: u32,
    ) -> Vec<MinedPattern> {
        let mut patterns = Vec::new();
        
        // Group by pattern
        let mut pattern_counts: HashMap<Vec<String>, u32> = HashMap::new();
        for (pattern, count) in conditional_patterns {
            *pattern_counts.entry(pattern).or_insert(0) += count;
        }

        // Get length before consuming the map
        let total_patterns = pattern_counts.len().max(1);

        // Create patterns that meet minimum support
        for (mut sequence, count) in pattern_counts {
            if count >= min_count && sequence.len() <= self.max_pattern_length {
                sequence.push(base_item.to_string());

                let pattern_id = format!("pattern_{}", sequence.join("_"));
                patterns.push(MinedPattern {
                    pattern_id,
                    sequence,
                    frequency: count,
                    support: count as f32 / total_patterns as f32,
                    confidence: 0.8, // Will be updated later
                    variations: vec![],
                    metadata: HashMap::new(),
                });
            }
        }
        
        patterns
    }
    
    /// Find variations of patterns in sequences
    fn find_pattern_variations(
        &self,
        mut patterns: Vec<MinedPattern>,
        sequences: &[Vec<String>],
    ) -> Vec<MinedPattern> {
        patterns.par_iter_mut().for_each(|pattern| {
            let variations = self.find_similar_sequences(&pattern.sequence, sequences);
            pattern.variations = variations;
            
            // Update confidence based on variations
            let avg_similarity = if pattern.variations.is_empty() {
                1.0
            } else {
                pattern.variations.len() as f32 / sequences.len() as f32
            };
            pattern.confidence = (pattern.support * avg_similarity).min(1.0);
        });
        
        patterns
    }
    
    /// Find sequences similar to a pattern
    fn find_similar_sequences(
        &self,
        pattern: &[String],
        sequences: &[Vec<String>],
    ) -> Vec<Vec<String>> {
        sequences
            .par_iter()
            .filter_map(|seq| {
                if self.is_subsequence(pattern, seq) {
                    Some(seq.clone())
                } else if self.calculate_sequence_similarity(pattern, seq) > 0.7 {
                    Some(seq.clone())
                } else {
                    None
                }
            })
            .collect()
    }
    
    /// Check if pattern is subsequence of sequence
    fn is_subsequence(&self, pattern: &[String], sequence: &[String]) -> bool {
        let mut pattern_idx = 0;
        
        for item in sequence {
            if pattern_idx < pattern.len() && item == &pattern[pattern_idx] {
                pattern_idx += 1;
            }
        }
        
        pattern_idx == pattern.len()
    }
    
    /// Calculate sequence similarity
    fn calculate_sequence_similarity(&self, seq1: &[String], seq2: &[String]) -> f32 {
        if seq1.is_empty() || seq2.is_empty() {
            return 0.0;
        }
        
        let lcs_len = self.longest_common_subsequence(seq1, seq2);
        let max_len = seq1.len().max(seq2.len()) as f32;
        
        lcs_len as f32 / max_len
    }
    
    /// Calculate longest common subsequence length
    fn longest_common_subsequence(&self, seq1: &[String], seq2: &[String]) -> usize {
        let m = seq1.len();
        let n = seq2.len();
        
        let mut dp = vec![vec![0; n + 1]; m + 1];
        
        for i in 1..=m {
            for j in 1..=n {
                if seq1[i - 1] == seq2[j - 1] {
                    dp[i][j] = dp[i - 1][j - 1] + 1;
                } else {
                    dp[i][j] = dp[i - 1][j].max(dp[i][j - 1]);
                }
            }
        }
        
        dp[m][n]
    }
    
    /// Get pattern statistics
    pub fn get_pattern_stats(&self) -> HashMap<String, serde_json::Value> {
        use serde_json::json;
        
        let patterns = self.patterns.read();
        
        let mut stats = HashMap::new();
        stats.insert("total_patterns".to_string(), json!(patterns.len()));
        
        // Pattern length distribution
        let mut length_dist: HashMap<usize, usize> = HashMap::new();
        for pattern in patterns.values() {
            *length_dist.entry(pattern.sequence.len()).or_insert(0) += 1;
        }
        stats.insert("length_distribution".to_string(), json!(length_dist));
        
        // Average confidence
        let avg_confidence = if patterns.is_empty() {
            0.0
        } else {
            patterns.values().map(|p| p.confidence).sum::<f32>() / patterns.len() as f32
        };
        stats.insert("average_confidence".to_string(), json!(avg_confidence));
        
        // Top patterns by frequency
        let mut top_patterns: Vec<_> = patterns.values().collect();
        top_patterns.sort_by_key(|p| std::cmp::Reverse(p.frequency));
        let top_5: Vec<_> = top_patterns
            .iter()
            .take(5)
            .map(|p| json!({
                "sequence": p.sequence,
                "frequency": p.frequency,
                "confidence": p.confidence
            }))
            .collect();
        stats.insert("top_patterns".to_string(), json!(top_5));
        
        stats
    }
    
    /// Predict next items given current sequence
    pub fn predict_next(&self, sequence: &[String], top_k: usize) -> Vec<(String, f32)> {
        let patterns = self.patterns.read();
        let mut predictions: HashMap<String, f32> = HashMap::new();
        
        // Find patterns that match the current sequence
        for pattern in patterns.values() {
            if pattern.sequence.len() > sequence.len() {
                // Check if sequence is prefix of pattern
                let matches = sequence.iter().zip(&pattern.sequence).all(|(a, b)| a == b);
                
                if matches {
                    let next_idx = sequence.len();
                    if next_idx < pattern.sequence.len() {
                        let next_item = &pattern.sequence[next_idx];
                        let score = pattern.confidence * pattern.support;
                        
                        predictions.entry(next_item.clone())
                            .and_modify(|e| *e = e.max(score))
                            .or_insert(score);
                    }
                }
            }
        }
        
        // Sort by score and return top K
        let mut sorted_predictions: Vec<_> = predictions.into_iter().collect();
        sorted_predictions.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        sorted_predictions.truncate(top_k);
        
        sorted_predictions
    }
}

/// Python FFI bindings
#[cfg(feature = "python-bindings")]
pub mod python_bindings {
    use super::*;
    use pyo3::prelude::*;
    
    #[pyclass]
    struct PyPatternMiner {
        inner: PatternMiner,
    }
    
    #[pymethods]
    impl PyPatternMiner {
        #[new]
        fn new(min_support: f32, max_pattern_length: usize) -> Self {
            Self {
                inner: PatternMiner::new(min_support, max_pattern_length),
            }
        }
        
        fn mine_patterns(&self, sequences: Vec<Vec<String>>) -> PyResult<String> {
            let patterns = self.inner.mine_patterns(sequences);
            serde_json::to_string(&patterns)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
        }
        
        fn predict_next(&self, sequence: Vec<String>, top_k: usize) -> Vec<(String, f32)> {
            self.inner.predict_next(&sequence, top_k)
        }
        
        fn get_stats(&self) -> PyResult<String> {
            let stats = self.inner.get_pattern_stats();
            serde_json::to_string(&stats)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_pattern_mining() {
        let miner = PatternMiner::new(0.2, 5);
        
        let sequences = vec![
            vec!["open".to_string(), "edit".to_string(), "save".to_string()],
            vec!["open".to_string(), "edit".to_string(), "test".to_string(), "save".to_string()],
            vec!["open".to_string(), "search".to_string(), "edit".to_string(), "save".to_string()],
            vec!["new".to_string(), "edit".to_string(), "save".to_string()],
        ];
        
        let patterns = miner.mine_patterns(sequences);
        
        assert!(!patterns.is_empty());
        
        // Test prediction
        let predictions = miner.predict_next(&["open".to_string(), "edit".to_string()], 3);
        assert!(!predictions.is_empty());
    }
}
