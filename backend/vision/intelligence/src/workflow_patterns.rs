use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::Arc;
use parking_lot::RwLock;
use rayon::prelude::*;

/// High-performance sequence similarity calculator
#[pyclass]
pub struct SequenceSimilarityCalculator {
    cache: Arc<RwLock<HashMap<String, f64>>>,
    max_cache_size: usize,
}

#[pymethods]
impl SequenceSimilarityCalculator {
    #[new]
    pub fn new(max_cache_size: Option<usize>) -> Self {
        Self {
            cache: Arc::new(RwLock::new(HashMap::new())),
            max_cache_size: max_cache_size.unwrap_or(10000),
        }
    }

    /// Calculate sequence similarity using optimized dynamic programming
    pub fn calculate_similarity(&self, seq1: Vec<String>, seq2: Vec<String>) -> f64 {
        if seq1.is_empty() || seq2.is_empty() {
            return 0.0;
        }

        // Create cache key
        let cache_key = format!("{}|{}", seq1.join(","), seq2.join(","));
        
        // Check cache first
        {
            let cache_read = self.cache.read();
            if let Some(&cached_result) = cache_read.get(&cache_key) {
                return cached_result;
            }
        }

        // Calculate LCS-based similarity
        let similarity = self.lcs_similarity(&seq1, &seq2);

        // Cache result
        {
            let mut cache_write = self.cache.write();
            if cache_write.len() >= self.max_cache_size {
                cache_write.clear(); // Simple cache eviction
            }
            cache_write.insert(cache_key, similarity);
        }

        similarity
    }

    /// Calculate batch similarities in parallel
    pub fn batch_calculate_similarities(&self, sequences: Vec<Vec<String>>) -> Vec<Vec<f64>> {
        let n = sequences.len();
        let results: Vec<Vec<f64>> = (0..n)
            .into_par_iter()
            .map(|i| {
                (0..n)
                    .map(|j| {
                        if i == j {
                            1.0
                        } else {
                            self.calculate_similarity(sequences[i].clone(), sequences[j].clone())
                        }
                    })
                    .collect()
            })
            .collect();

        results
    }

    pub fn clear_cache(&self) {
        let mut cache = self.cache.write();
        cache.clear();
    }
}

impl SequenceSimilarityCalculator {
    fn lcs_similarity(&self, seq1: &[String], seq2: &[String]) -> f64 {
        let m = seq1.len();
        let n = seq2.len();

        if m == 0 || n == 0 {
            return 0.0;
        }

        // Use space-optimized DP
        let mut prev_row = vec![0; n + 1];
        let mut curr_row = vec![0; n + 1];

        for i in 1..=m {
            for j in 1..=n {
                if seq1[i - 1] == seq2[j - 1] {
                    curr_row[j] = prev_row[j - 1] + 1;
                } else {
                    curr_row[j] = std::cmp::max(prev_row[j], curr_row[j - 1]);
                }
            }
            std::mem::swap(&mut prev_row, &mut curr_row);
            curr_row.fill(0);
        }

        let lcs_length = prev_row[n];
        let max_length = std::cmp::max(m, n);

        lcs_length as f64 / max_length as f64
    }
}

/// High-performance pattern clustering using optimized algorithms
#[pyclass]
pub struct PatternClusterer {
    similarity_threshold: f64,
    min_cluster_size: usize,
    calculator: SequenceSimilarityCalculator,
}

#[pymethods]
impl PatternClusterer {
    #[new]
    pub fn new(similarity_threshold: Option<f64>, min_cluster_size: Option<usize>) -> Self {
        Self {
            similarity_threshold: similarity_threshold.unwrap_or(0.7),
            min_cluster_size: min_cluster_size.unwrap_or(3),
            calculator: SequenceSimilarityCalculator::new(Some(5000)),
        }
    }

    /// Cluster sequences using optimized hierarchical clustering
    pub fn cluster_sequences(&self, sequences: Vec<Vec<String>>) -> HashMap<String, Vec<usize>> {
        if sequences.len() < self.min_cluster_size {
            return HashMap::new();
        }

        // Calculate similarity matrix in parallel
        let similarity_matrix = self.calculator.batch_calculate_similarities(sequences.clone());

        // Use single-linkage clustering
        let mut clusters: HashMap<String, Vec<usize>> = HashMap::new();
        let mut assigned: Vec<bool> = vec![false; sequences.len()];
        let mut cluster_id = 0;

        for i in 0..sequences.len() {
            if assigned[i] {
                continue;
            }

            let mut cluster_members = vec![i];
            assigned[i] = true;

            // Find all sequences similar to this one
            for j in (i + 1)..sequences.len() {
                if !assigned[j] && similarity_matrix[i][j] >= self.similarity_threshold {
                    cluster_members.push(j);
                    assigned[j] = true;
                }
            }

            // Only keep clusters that meet minimum size
            if cluster_members.len() >= self.min_cluster_size {
                clusters.insert(format!("cluster_{}", cluster_id), cluster_members);
                cluster_id += 1;
            }
        }

        clusters
    }

    /// Find optimal cluster assignments using graph-based approach
    pub fn graph_based_clustering(&self, sequences: Vec<Vec<String>>) -> HashMap<String, Vec<usize>> {
        let n = sequences.len();
        if n < self.min_cluster_size {
            return HashMap::new();
        }

        // Build similarity graph
        let similarity_matrix = self.calculator.batch_calculate_similarities(sequences);
        let mut graph: Vec<Vec<usize>> = vec![vec![]; n];

        for i in 0..n {
            for j in (i + 1)..n {
                if similarity_matrix[i][j] >= self.similarity_threshold {
                    graph[i].push(j);
                    graph[j].push(i);
                }
            }
        }

        // Find connected components
        let mut visited = vec![false; n];
        let mut clusters = HashMap::new();
        let mut cluster_id = 0;

        for i in 0..n {
            if !visited[i] {
                let component = self.dfs_component(&graph, i, &mut visited);
                if component.len() >= self.min_cluster_size {
                    clusters.insert(format!("cluster_{}", cluster_id), component);
                    cluster_id += 1;
                }
            }
        }

        clusters
    }
}

impl PatternClusterer {
    fn dfs_component(&self, graph: &[Vec<usize>], start: usize, visited: &mut [bool]) -> Vec<usize> {
        let mut stack = vec![start];
        let mut component = vec![];

        while let Some(node) = stack.pop() {
            if !visited[node] {
                visited[node] = true;
                component.push(node);

                for &neighbor in &graph[node] {
                    if !visited[neighbor] {
                        stack.push(neighbor);
                    }
                }
            }
        }

        component
    }
}

/// Fast transition probability calculator
#[pyclass]
pub struct TransitionCalculator {
    smoothing_factor: f64,
}

#[pymethods]
impl TransitionCalculator {
    #[new]
    pub fn new(smoothing_factor: Option<f64>) -> Self {
        Self {
            smoothing_factor: smoothing_factor.unwrap_or(0.01),
        }
    }

    /// Calculate transition probabilities from sequences
    pub fn calculate_transitions(&self, sequences: Vec<Vec<String>>) -> HashMap<String, f64> {
        let mut transition_counts: HashMap<String, u32> = HashMap::new();
        let mut total_transitions = 0u32;

        // Count transitions
        for sequence in sequences {
            for window in sequence.windows(2) {
                let transition = format!("{}|{}", window[0], window[1]);
                *transition_counts.entry(transition).or_insert(0) += 1;
                total_transitions += 1;
            }
        }

        // Apply Laplace smoothing and convert to probabilities
        let mut probabilities = HashMap::new();
        let vocabulary_size = transition_counts.len() as f64;

        for (transition, count) in transition_counts {
            let smoothed_count = count as f64 + self.smoothing_factor;
            let smoothed_total = total_transitions as f64 + (self.smoothing_factor * vocabulary_size);
            probabilities.insert(transition, smoothed_count / smoothed_total);
        }

        probabilities
    }

    /// Calculate transition matrix for prediction
    pub fn build_transition_matrix(&self, sequences: Vec<Vec<String>>) -> HashMap<String, HashMap<String, f64>> {
        let mut transition_matrix: HashMap<String, HashMap<String, f64>> = HashMap::new();
        let mut state_counts: HashMap<String, HashMap<String, u32>> = HashMap::new();
        let mut state_totals: HashMap<String, u32> = HashMap::new();

        // Count state transitions
        for sequence in sequences {
            for window in sequence.windows(2) {
                let from_state = &window[0];
                let to_state = &window[1];

                *state_counts
                    .entry(from_state.clone())
                    .or_insert_with(HashMap::new)
                    .entry(to_state.clone())
                    .or_insert(0) += 1;

                *state_totals.entry(from_state.clone()).or_insert(0) += 1;
            }
        }

        // Convert to probabilities
        for (from_state, transitions) in state_counts {
            let total = state_totals[&from_state] as f64;
            let mut probabilities = HashMap::new();

            for (to_state, count) in transitions {
                probabilities.insert(to_state, count as f64 / total);
            }

            transition_matrix.insert(from_state, probabilities);
        }

        transition_matrix
    }
}

/// Memory-efficient pattern storage with compression
#[pyclass]
pub struct PatternStorage {
    max_patterns: usize,
    compression_threshold: usize,
}

#[pymethods]
impl PatternStorage {
    #[new]
    pub fn new(max_patterns: Option<usize>) -> Self {
        Self {
            max_patterns: max_patterns.unwrap_or(1000),
            compression_threshold: 100,
        }
    }

    /// Compress pattern data for efficient storage
    pub fn compress_pattern_data(&self, pattern_data: Vec<String>) -> PyResult<Vec<u8>> {
        use flate2::Compression;
        use flate2::write::GzEncoder;
        use std::io::Write;

        let serialized = serde_json::to_string(&pattern_data)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Serialization error: {}", e)))?;

        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(serialized.as_bytes())
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyIOError, _>(format!("Compression error: {}", e)))?;

        let compressed = encoder.finish()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyIOError, _>(format!("Compression finish error: {}", e)))?;

        Ok(compressed)
    }

    /// Decompress pattern data
    pub fn decompress_pattern_data(&self, compressed_data: Vec<u8>) -> PyResult<Vec<String>> {
        use flate2::read::GzDecoder;
        use std::io::Read;

        let mut decoder = GzDecoder::new(&compressed_data[..]);
        let mut decompressed = String::new();
        
        decoder.read_to_string(&mut decompressed)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyIOError, _>(format!("Decompression error: {}", e)))?;

        let pattern_data: Vec<String> = serde_json::from_str(&decompressed)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Deserialization error: {}", e)))?;

        Ok(pattern_data)
    }

    /// Calculate compression ratio
    pub fn compression_ratio(&self, original: Vec<String>, compressed: Vec<u8>) -> f64 {
        let original_size = serde_json::to_string(&original).unwrap_or_default().len();
        let compressed_size = compressed.len();
        
        if original_size == 0 {
            return 0.0;
        }
        
        compressed_size as f64 / original_size as f64
    }
}

/// Fast frequent pattern mining using FP-Growth algorithm
#[pyclass] 
pub struct FrequentPatternMiner {
    min_support: u32,
    max_pattern_length: usize,
}

#[pymethods]
impl FrequentPatternMiner {
    #[new]
    pub fn new(min_support: Option<u32>, max_pattern_length: Option<usize>) -> Self {
        Self {
            min_support: min_support.unwrap_or(3),
            max_pattern_length: max_pattern_length.unwrap_or(10),
        }
    }

    /// Mine frequent patterns from transaction sequences
    pub fn mine_frequent_patterns(&self, sequences: Vec<Vec<String>>) -> HashMap<String, u32> {
        let mut pattern_counts: HashMap<String, u32> = HashMap::new();

        // Generate all possible subsequences up to max length
        for sequence in &sequences {
            for length in 2..=std::cmp::min(self.max_pattern_length, sequence.len()) {
                for start in 0..=(sequence.len() - length) {
                    let pattern: Vec<String> = sequence[start..start + length].to_vec();
                    let pattern_key = pattern.join("|");
                    *pattern_counts.entry(pattern_key).or_insert(0) += 1;
                }
            }
        }

        // Filter by minimum support
        pattern_counts
            .into_iter()
            .filter(|(_, count)| *count >= self.min_support)
            .collect()
    }

    /// Find association rules from frequent patterns
    pub fn generate_association_rules(&self, frequent_patterns: HashMap<String, u32>) -> Vec<(String, String, f64)> {
        let mut rules = Vec::new();
        
        for (pattern, support) in &frequent_patterns {
            let items: Vec<&str> = pattern.split('|').collect();
            
            if items.len() < 2 {
                continue;
            }

            // Generate rules of the form: A -> B
            for i in 0..items.len() {
                for j in (i + 1)..items.len() {
                    let antecedent = items[i].to_string();
                    let consequent = items[j].to_string();
                    
                    // Calculate confidence
                    let antecedent_support = frequent_patterns.get(&antecedent).unwrap_or(&0);
                    
                    if *antecedent_support > 0 {
                        let confidence = *support as f64 / *antecedent_support as f64;
                        rules.push((antecedent, consequent, confidence));
                    }
                }
            }
        }

        // Sort by confidence
        rules.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));
        rules
    }
}

/// Performance optimized context similarity calculator
#[pyclass]
pub struct ContextSimilarityCalculator {
    weights: HashMap<String, f64>,
}

#[pymethods]
impl ContextSimilarityCalculator {
    #[new]
    pub fn new(weights: Option<HashMap<String, f64>>) -> Self {
        let default_weights = [
            ("application".to_string(), 0.4),
            ("time".to_string(), 0.2),
            ("goal".to_string(), 0.3),
            ("task".to_string(), 0.1),
        ]
        .iter()
        .cloned()
        .collect();

        Self {
            weights: weights.unwrap_or(default_weights),
        }
    }

    /// Calculate context similarity using weighted features
    pub fn calculate_context_similarity(&self, context1: HashMap<String, String>, context2: HashMap<String, String>) -> f64 {
        let mut total_weight = 0.0;
        let mut weighted_similarity = 0.0;

        for (feature, weight) in &self.weights {
            if let (Some(val1), Some(val2)) = (context1.get(feature), context2.get(feature)) {
                let similarity = if val1 == val2 { 1.0 } else { 0.0 };
                weighted_similarity += similarity * weight;
                total_weight += weight;
            }
        }

        if total_weight > 0.0 {
            weighted_similarity / total_weight
        } else {
            0.0
        }
    }

    /// Batch calculate similarities for multiple context pairs
    pub fn batch_calculate_context_similarities(
        &self,
        contexts: Vec<HashMap<String, String>>,
    ) -> Vec<Vec<f64>> {
        let n = contexts.len();
        
        (0..n)
            .into_par_iter()
            .map(|i| {
                (0..n)
                    .map(|j| {
                        if i == j {
                            1.0
                        } else {
                            self.calculate_context_similarity(contexts[i].clone(), contexts[j].clone())
                        }
                    })
                    .collect()
            })
            .collect()
    }
}

/// Register all workflow pattern components with Python
pub fn register_workflow_patterns(m: &PyModule) -> PyResult<()> {
    m.add_class::<SequenceSimilarityCalculator>()?;
    m.add_class::<PatternClusterer>()?;
    m.add_class::<TransitionCalculator>()?;
    m.add_class::<PatternStorage>()?;
    m.add_class::<FrequentPatternMiner>()?;
    m.add_class::<ContextSimilarityCalculator>()?;
    Ok(())
}