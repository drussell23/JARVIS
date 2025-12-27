use pyo3::prelude::*;
use pyo3::types::PyDict;
use serde::{Deserialize, Serialize};
use ndarray::Array2;
use std::collections::HashMap;
use ahash::AHashMap;
use rayon::prelude::*;
use fuzzy_matcher::skim::SkimMatcherV2;
use fuzzy_matcher::FuzzyMatcher;

#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VisualPattern {
    #[pyo3(get)]
    pub pattern_id: String,
    pub feature_matrix: Array2<f32>,
    pub color_histogram: Vec<f32>,
    pub edge_features: Vec<f32>,
    pub texture_descriptors: Vec<f32>,
    #[pyo3(get)]
    pub confidence_score: f32,
    #[pyo3(get)]
    pub observation_count: u32,
    pub metadata: HashMap<String, String>,
}

#[pymethods]
impl VisualPattern {
    #[new]
    fn new(pattern_id: String) -> Self {
        Self {
            pattern_id,
            feature_matrix: Array2::zeros((0, 0)),
            color_histogram: Vec::new(),
            edge_features: Vec::new(),
            texture_descriptors: Vec::new(),
            confidence_score: 0.0,
            observation_count: 0,
            metadata: HashMap::new(),
        }
    }

    fn update_from_observation(&mut self, features: &PyDict) -> PyResult<()> {
        // Extract features from Python dict
        if let Ok(Some(color_hist)) = features.get_item("color_histogram") {
            if let Ok(hist_vec) = color_hist.extract::<Vec<f32>>() {
                self.update_color_histogram(&hist_vec);
            }
        }

        if let Ok(Some(edges)) = features.get_item("edge_features") {
            if let Ok(edge_vec) = edges.extract::<Vec<f32>>() {
                self.update_edge_features(&edge_vec);
            }
        }

        self.observation_count += 1;
        self.update_confidence();
        Ok(())
    }

    fn similarity_score(&self, other: &VisualPattern) -> f32 {
        let mut score = 0.0;
        let mut weight_sum = 0.0;

        // Color histogram similarity (using histogram intersection)
        if !self.color_histogram.is_empty() && !other.color_histogram.is_empty() {
            let color_sim = histogram_intersection(&self.color_histogram, &other.color_histogram);
            score += color_sim * 0.3;
            weight_sum += 0.3;
        }

        // Edge feature similarity
        if !self.edge_features.is_empty() && !other.edge_features.is_empty() {
            let edge_sim = cosine_similarity(&self.edge_features, &other.edge_features);
            score += edge_sim * 0.4;
            weight_sum += 0.4;
        }

        // Texture similarity
        if !self.texture_descriptors.is_empty() && !other.texture_descriptors.is_empty() {
            let texture_sim = cosine_similarity(&self.texture_descriptors, &other.texture_descriptors);
            score += texture_sim * 0.3;
            weight_sum += 0.3;
        }

        if weight_sum > 0.0 {
            score / weight_sum
        } else {
            0.0
        }
    }

    fn to_dict(&self, py: Python) -> PyResult<PyObject> {
        let dict = PyDict::new(py);
        dict.set_item("pattern_id", &self.pattern_id)?;
        dict.set_item("confidence_score", self.confidence_score)?;
        dict.set_item("observation_count", self.observation_count)?;
        dict.set_item("color_histogram_size", self.color_histogram.len())?;
        dict.set_item("edge_features_size", self.edge_features.len())?;
        Ok(dict.into())
    }
}

impl VisualPattern {
    fn update_color_histogram(&mut self, new_histogram: &[f32]) {
        if self.color_histogram.is_empty() {
            self.color_histogram = new_histogram.to_vec();
        } else {
            // Exponential moving average update
            let alpha = 0.1;
            for (i, &new_val) in new_histogram.iter().enumerate() {
                if i < self.color_histogram.len() {
                    self.color_histogram[i] = 
                        self.color_histogram[i] * (1.0 - alpha) + new_val * alpha;
                }
            }
        }
    }

    fn update_edge_features(&mut self, new_features: &[f32]) {
        if self.edge_features.is_empty() {
            self.edge_features = new_features.to_vec();
        } else {
            let alpha = 0.1;
            for (i, &new_val) in new_features.iter().enumerate() {
                if i < self.edge_features.len() {
                    self.edge_features[i] = 
                        self.edge_features[i] * (1.0 - alpha) + new_val * alpha;
                }
            }
        }
    }

    fn update_confidence(&mut self) {
        // Confidence increases with observations, capped at 0.99
        self.confidence_score = 1.0 - (1.0 / (self.observation_count as f32 + 1.0));
        self.confidence_score = self.confidence_score.min(0.99);
    }
}

#[pyclass]
pub struct PatternMatcher {
    patterns: AHashMap<String, VisualPattern>,
    fuzzy_matcher: SkimMatcherV2,
    similarity_threshold: f32,
}

#[pymethods]
impl PatternMatcher {
    #[new]
    fn new() -> Self {
        Self {
            patterns: AHashMap::new(),
            fuzzy_matcher: SkimMatcherV2::default(),
            similarity_threshold: 0.7,
        }
    }

    fn add_pattern(&mut self, pattern: VisualPattern) {
        self.patterns.insert(pattern.pattern_id.clone(), pattern);
    }

    fn match_pattern(&self, query_pattern: &VisualPattern) -> Option<(String, f32)> {
        let matches: Vec<(String, f32)> = self.patterns
            .par_iter()
            .map(|(id, pattern)| {
                let score = pattern.similarity_score(query_pattern);
                (id.clone(), score)
            })
            .filter(|(_, score)| *score > self.similarity_threshold)
            .collect();

        matches.into_iter()
            .max_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap())
    }

    fn fuzzy_match_by_id(&self, query: &str) -> Vec<(String, i64)> {
        let mut results: Vec<(String, i64)> = self.patterns
            .keys()
            .filter_map(|id| {
                self.fuzzy_matcher.fuzzy_match(id, query)
                    .map(|score| (id.clone(), score))
            })
            .collect();

        results.sort_by(|(_, a), (_, b)| b.cmp(a));
        results.truncate(5);
        results
    }

    fn get_pattern(&self, pattern_id: &str) -> Option<VisualPattern> {
        self.patterns.get(pattern_id).cloned()
    }

    fn remove_pattern(&mut self, pattern_id: &str) -> bool {
        self.patterns.remove(pattern_id).is_some()
    }

    fn pattern_count(&self) -> usize {
        self.patterns.len()
    }

    fn set_similarity_threshold(&mut self, threshold: f32) {
        self.similarity_threshold = threshold.max(0.0).min(1.0);
    }

    fn batch_match(&self, query_patterns: Vec<VisualPattern>) -> PyResult<Vec<Option<(String, f32)>>> {
        Ok(query_patterns
            .par_iter()
            .map(|pattern| self.match_pattern(pattern))
            .collect())
    }

    fn get_all_patterns(&self, py: Python) -> PyResult<PyObject> {
        let dict = PyDict::new(py);
        for (id, pattern) in &self.patterns {
            dict.set_item(id, pattern.to_dict(py)?)?;
        }
        Ok(dict.into())
    }
}

// Utility functions
fn histogram_intersection(hist1: &[f32], hist2: &[f32]) -> f32 {
    hist1.iter()
        .zip(hist2.iter())
        .map(|(a, b)| a.min(*b))
        .sum()
}

fn cosine_similarity(vec1: &[f32], vec2: &[f32]) -> f32 {
    let dot_product: f32 = vec1.iter().zip(vec2.iter()).map(|(a, b)| a * b).sum();
    let magnitude1: f32 = vec1.iter().map(|x| x * x).sum::<f32>().sqrt();
    let magnitude2: f32 = vec2.iter().map(|x| x * x).sum::<f32>().sqrt();
    
    if magnitude1 * magnitude2 > 0.0 {
        dot_product / (magnitude1 * magnitude2)
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pattern_similarity() {
        let mut pattern1 = VisualPattern::new("test1".to_string());
        pattern1.color_histogram = vec![0.1, 0.2, 0.3, 0.4];
        pattern1.edge_features = vec![0.5, 0.6, 0.7, 0.8];

        let mut pattern2 = VisualPattern::new("test2".to_string());
        pattern2.color_histogram = vec![0.1, 0.2, 0.3, 0.4];
        pattern2.edge_features = vec![0.5, 0.6, 0.7, 0.8];

        let similarity = pattern1.similarity_score(&pattern2);
        assert!((similarity - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_pattern_matcher() {
        let mut matcher = PatternMatcher::new();
        let pattern = VisualPattern::new("test_pattern".to_string());
        matcher.add_pattern(pattern.clone());

        let result = matcher.match_pattern(&pattern);
        assert!(result.is_some());
        assert_eq!(result.unwrap().0, "test_pattern");
    }
}