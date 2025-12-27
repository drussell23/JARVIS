use pyo3::prelude::*;
use pyo3::types::PyDict;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use chrono::Utc;

#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StateSignature {
    #[pyo3(get)]
    pub signature_id: String,
    #[pyo3(get)]
    pub state_type: String,
    #[pyo3(get)]
    pub confidence: f32,
    pub visual_features: HashMap<String, Vec<f32>>,
    pub temporal_features: Vec<f32>,
    pub transition_probabilities: HashMap<String, f32>,
    #[pyo3(get)]
    pub last_observed: String,
    #[pyo3(get)]
    pub observation_count: u32,
}

#[pymethods]
impl StateSignature {
    #[new]
    fn new(signature_id: String, state_type: String) -> Self {
        Self {
            signature_id,
            state_type,
            confidence: 0.0,
            visual_features: HashMap::new(),
            temporal_features: Vec::new(),
            transition_probabilities: HashMap::new(),
            last_observed: Utc::now().to_rfc3339(),
            observation_count: 0,
        }
    }

    fn update(&mut self, _py: Python<'_>, features: &PyDict) -> PyResult<()> {
        // Convert PyDict to HashMap<String, Vec<f32>>
        let mut features_map = HashMap::new();
        for (key, value) in features.iter() {
            let key_str: String = key.extract()?;
            let new_values: Vec<f32> = value.extract()?;
            features_map.insert(key_str, new_values);
        }

        // Call internal update method
        self.update_internal(&features_map);
        Ok(())
    }

    fn similarity(&self, other: &StateSignature) -> f32 {
        let mut total_similarity = 0.0;
        let mut feature_count = 0;

        // Compare visual features
        for (key, features) in &self.visual_features {
            if let Some(other_features) = other.visual_features.get(key) {
                let similarity = cosine_similarity(features, other_features);
                total_similarity += similarity;
                feature_count += 1;
            }
        }

        if feature_count > 0 {
            total_similarity / feature_count as f32
        } else {
            0.0
        }
    }
}

impl StateSignature {
    // Internal method for Rust code to update features
    fn update_internal(&mut self, features: &HashMap<String, Vec<f32>>) {
        // Update visual features with exponential moving average
        for (key, new_values) in features {
            self.visual_features
                .entry(key.clone())
                .and_modify(|existing| {
                    for (i, new_val) in new_values.iter().enumerate() {
                        if i < existing.len() {
                            existing[i] = existing[i] * 0.9 + new_val * 0.1;
                        }
                    }
                })
                .or_insert_with(|| new_values.clone());
        }

        self.observation_count += 1;
        self.confidence = (self.confidence * 0.95) + (0.05 * self.observation_count as f32).min(1.0);
        self.last_observed = Utc::now().to_rfc3339();
    }
}

#[pyclass]
pub struct StateDetector {
    state_signatures: HashMap<String, StateSignature>,
    detection_threshold: f32,
    learning_rate: f32,
    max_states: usize,
}

#[pymethods]
impl StateDetector {
    #[new]
    fn new() -> Self {
        Self {
            state_signatures: HashMap::new(),
            detection_threshold: 0.75,
            learning_rate: 0.1,
            max_states: 1000,
        }
    }

    fn detect(&self, features: HashMap<String, Vec<f32>>) -> Option<(String, f32)> {
        let mut best_match: Option<(String, f32)> = None;
        let mut highest_similarity = 0.0;

        // Create temporary signature for comparison
        let temp_signature = StateSignature {
            signature_id: "temp".to_string(),
            state_type: "unknown".to_string(),
            confidence: 1.0,
            visual_features: features,
            temporal_features: Vec::new(),
            transition_probabilities: HashMap::new(),
            last_observed: Utc::now().to_rfc3339(),
            observation_count: 1,
        };

        for (state_id, signature) in &self.state_signatures {
            let similarity = signature.similarity(&temp_signature);
            if similarity > highest_similarity && similarity > self.detection_threshold {
                highest_similarity = similarity;
                best_match = Some((state_id.clone(), similarity));
            }
        }

        best_match
    }

    fn learn(&mut self, state_id: String, features: HashMap<String, Vec<f32>>, state_type: Option<String>) {
        if let Some(signature) = self.state_signatures.get_mut(&state_id) {
            // Update existing state
            signature.update_internal(&features);
        } else if self.state_signatures.len() < self.max_states {
            // Create new state
            let mut new_signature = StateSignature::new(
                state_id.clone(),
                state_type.unwrap_or_else(|| "custom".to_string())
            );
            new_signature.update_internal(&features);
            self.state_signatures.insert(state_id, new_signature);
        } else {
            // Replace least confident state if at capacity
            if let Some((weakest_id, _)) = self.state_signatures
                .iter()
                .min_by(|(_, a), (_, b)| a.confidence.partial_cmp(&b.confidence).unwrap()) {
                let weakest_id = weakest_id.clone();
                self.state_signatures.remove(&weakest_id);

                let mut new_signature = StateSignature::new(
                    state_id.clone(),
                    state_type.unwrap_or_else(|| "custom".to_string())
                );
                new_signature.update_internal(&features);
                self.state_signatures.insert(state_id, new_signature);
            }
        }
    }

    fn get_state(&self, state_id: &str) -> Option<StateSignature> {
        self.state_signatures.get(state_id).cloned()
    }

    fn state_count(&self) -> usize {
        self.state_signatures.len()
    }

    fn set_detection_threshold(&mut self, threshold: f32) {
        self.detection_threshold = threshold.clamp(0.0, 1.0);
    }

    fn get_all_states(&self) -> Vec<StateSignature> {
        self.state_signatures.values().cloned().collect()
    }

    fn prune_old_states(&mut self, days: u32) {
        let cutoff = Utc::now() - chrono::Duration::days(days as i64);
        let cutoff_str = cutoff.to_rfc3339();

        self.state_signatures.retain(|_, signature| {
            signature.last_observed > cutoff_str
        });
    }
}

fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }

    let dot_product: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let magnitude_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let magnitude_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();

    if magnitude_a * magnitude_b > 0.0 {
        dot_product / (magnitude_a * magnitude_b)
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_state_detection() {
        let mut detector = StateDetector::new();
        
        let mut features = HashMap::new();
        features.insert("color".to_string(), vec![0.1, 0.2, 0.3]);
        features.insert("edge".to_string(), vec![0.4, 0.5, 0.6]);
        
        // Learn a state
        detector.learn("state1".to_string(), features.clone(), Some("active".to_string()));
        
        // Detect the same state
        let result = detector.detect(features);
        assert!(result.is_some());
        
        let (detected_id, confidence) = result.unwrap();
        assert_eq!(detected_id, "state1");
        assert!(confidence > 0.9);
    }

    #[test]
    fn test_similarity() {
        let mut sig1 = StateSignature::new("sig1".to_string(), "type1".to_string());
        let mut sig2 = StateSignature::new("sig2".to_string(), "type1".to_string());
        
        let mut features = HashMap::new();
        features.insert("test".to_string(), vec![1.0, 0.0, 0.0]);

        sig1.update_internal(&features);
        sig2.update_internal(&features);
        
        let similarity = sig1.similarity(&sig2);
        assert!((similarity - 1.0).abs() < 0.01);
    }
}