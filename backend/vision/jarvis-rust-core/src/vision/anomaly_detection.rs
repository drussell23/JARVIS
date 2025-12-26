//! High-performance anomaly detection for visual observations
//! Implements statistical and pattern-based anomaly detection

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, RwLock};
use serde::{Serialize, Deserialize};
use ndarray::{Array1, Array2};
use chrono::{DateTime, Utc, Duration};

/// Anomaly types
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AnomalyType {
    // Visual
    UnexpectedPopup,
    ErrorDialog,
    UnusualLayout,
    MissingElements,
    PerformanceVisual,
    
    // Behavioral
    RepeatedFailedAttempts,
    UnusualNavigation,
    StuckState,
    CircularPattern,
    TimeAnomaly,
    
    // System
    ResourceWarning,
    NetworkIssue,
    PermissionProblem,
    CrashIndicator,
    DataInconsistency,
}

/// Anomaly severity levels
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Severity {
    Low = 1,
    Medium = 2,
    High = 3,
    Critical = 4,
}

/// Statistical baseline for normal behavior
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Baseline {
    pub means: HashMap<String, f64>,
    pub stds: HashMap<String, f64>,
    pub sample_count: usize,
    pub last_updated: DateTime<Utc>,
    pub confidence: f64,
}

impl Baseline {
    pub fn new() -> Self {
        Self {
            means: HashMap::new(),
            stds: HashMap::new(),
            sample_count: 0,
            last_updated: Utc::now(),
            confidence: 0.0,
        }
    }
    
    /// Update baseline with new observations
    pub fn update(&mut self, features: &HashMap<String, f64>) {
        self.sample_count += 1;
        let n = self.sample_count as f64;
        
        for (key, &value) in features {
            let old_mean = self.means.get(key).copied().unwrap_or(0.0);
            let old_std = self.stds.get(key).copied().unwrap_or(0.0);
            
            // Update mean incrementally
            let new_mean = old_mean + (value - old_mean) / n;
            self.means.insert(key.clone(), new_mean);
            
            // Update standard deviation incrementally
            if n > 1.0 {
                let old_variance = old_std * old_std;
                let new_variance = ((n - 2.0) * old_variance + (value - old_mean) * (value - new_mean)) / (n - 1.0);
                self.stds.insert(key.clone(), new_variance.sqrt());
            }
        }
        
        self.last_updated = Utc::now();
        self.confidence = (self.sample_count as f64 / 100.0).min(1.0);
    }
    
    /// Calculate deviation score for features
    pub fn calculate_deviation(&self, features: &HashMap<String, f64>) -> HashMap<String, f64> {
        let mut deviations = HashMap::new();
        
        for (key, &value) in features {
            if let (Some(&mean), Some(&std)) = (self.means.get(key), self.stds.get(key)) {
                let z_score = if std > 0.0 {
                    (value - mean).abs() / std
                } else {
                    0.0
                };
                deviations.insert(key.clone(), z_score);
            }
        }
        
        deviations
    }
}

/// Detected anomaly
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DetectedAnomaly {
    pub id: String,
    pub anomaly_type: AnomalyType,
    pub severity: Severity,
    pub timestamp: DateTime<Utc>,
    pub description: String,
    pub confidence: f64,
    pub features: HashMap<String, f64>,
}

/// Pattern detector for behavioral anomalies
pub struct PatternDetector {
    sequence_buffer: VecDeque<String>,
    pattern_counts: HashMap<Vec<String>, usize>,
    max_sequence_length: usize,
}

impl PatternDetector {
    pub fn new(max_sequence_length: usize) -> Self {
        Self {
            sequence_buffer: VecDeque::with_capacity(max_sequence_length * 2),
            pattern_counts: HashMap::new(),
            max_sequence_length,
        }
    }
    
    /// Add action to sequence
    pub fn add_action(&mut self, action: String) {
        self.sequence_buffer.push_back(action);
        
        // Keep buffer size manageable
        if self.sequence_buffer.len() > self.max_sequence_length * 2 {
            self.sequence_buffer.pop_front();
        }
        
        // Update pattern counts
        self.update_pattern_counts();
    }
    
    /// Update counts for all subsequences
    fn update_pattern_counts(&mut self) {
        let buffer: Vec<String> = self.sequence_buffer.iter().cloned().collect();
        
        // Extract patterns of different lengths
        for length in 2..=self.max_sequence_length.min(buffer.len()) {
            for i in 0..=(buffer.len() - length) {
                let pattern = buffer[i..i + length].to_vec();
                *self.pattern_counts.entry(pattern).or_insert(0) += 1;
            }
        }
    }
    
    /// Detect circular patterns
    pub fn detect_circular_patterns(&self, threshold: usize) -> Vec<Vec<String>> {
        let mut circular_patterns = Vec::new();
        
        for (pattern, &count) in &self.pattern_counts {
            if count >= threshold && pattern.len() >= 2 {
                // Check if pattern forms a cycle
                let first = &pattern[0];
                let last = &pattern[pattern.len() - 1];
                
                // Simple cycle detection: pattern that returns to start
                if pattern.len() >= 3 {
                    for i in 1..pattern.len() - 1 {
                        if &pattern[i] == first {
                            // Found potential cycle
                            circular_patterns.push(pattern.clone());
                            break;
                        }
                    }
                }
            }
        }
        
        circular_patterns
    }
    
    /// Detect stuck patterns (repeated same action)
    pub fn detect_stuck_patterns(&self, min_repetitions: usize) -> Option<String> {
        if self.sequence_buffer.len() < min_repetitions {
            return None;
        }
        
        let recent: Vec<&String> = self.sequence_buffer.iter()
            .rev()
            .take(min_repetitions)
            .collect();
        
        // Check if all recent actions are the same
        if recent.len() == min_repetitions && recent.windows(2).all(|w| w[0] == w[1]) {
            return Some(recent[0].clone());
        }
        
        None
    }
}

/// High-performance anomaly detector
pub struct AnomalyDetector {
    baselines: Arc<RwLock<HashMap<String, Baseline>>>,
    pattern_detector: Arc<RwLock<PatternDetector>>,
    anomaly_history: Arc<RwLock<VecDeque<DetectedAnomaly>>>,
    detection_thresholds: HashMap<Severity, f64>,
}

impl AnomalyDetector {
    pub fn new() -> Self {
        let mut thresholds = HashMap::new();
        thresholds.insert(Severity::Low, 2.0);
        thresholds.insert(Severity::Medium, 3.0);
        thresholds.insert(Severity::High, 4.0);
        thresholds.insert(Severity::Critical, 5.0);
        
        Self {
            baselines: Arc::new(RwLock::new(HashMap::new())),
            pattern_detector: Arc::new(RwLock::new(PatternDetector::new(10))),
            anomaly_history: Arc::new(RwLock::new(VecDeque::with_capacity(1000))),
            detection_thresholds: thresholds,
        }
    }
    
    /// Establish baseline for a category
    pub fn establish_baseline(&self, category: &str, features_list: Vec<HashMap<String, f64>>) -> Result<(), String> {
        if features_list.is_empty() {
            return Err("No features provided for baseline".to_string());
        }
        
        let mut baseline = Baseline::new();
        
        // Calculate means and stds
        let feature_keys: Vec<String> = features_list[0].keys().cloned().collect();
        
        for key in &feature_keys {
            let values: Vec<f64> = features_list.iter()
                .filter_map(|f| f.get(key).copied())
                .collect();
            
            if !values.is_empty() {
                let mean = values.iter().sum::<f64>() / values.len() as f64;
                let variance = values.iter()
                    .map(|&v| (v - mean).powi(2))
                    .sum::<f64>() / values.len() as f64;
                let std = variance.sqrt();
                
                baseline.means.insert(key.clone(), mean);
                baseline.stds.insert(key.clone(), std);
            }
        }
        
        baseline.sample_count = features_list.len();
        baseline.confidence = (features_list.len() as f64 / 100.0).min(1.0);
        
        // Store baseline
        self.baselines.write().unwrap().insert(category.to_string(), baseline);
        
        Ok(())
    }
    
    /// Detect anomalies in features
    pub fn detect_anomalies(&self, category: &str, features: HashMap<String, f64>) -> Vec<DetectedAnomaly> {
        let mut anomalies = Vec::new();
        
        // Statistical anomaly detection
        if let Some(baseline) = self.baselines.read().unwrap().get(category) {
            let deviations = baseline.calculate_deviation(&features);
            
            // Check each deviation against thresholds
            for (feature, deviation) in deviations {
                for (severity, &threshold) in &self.detection_thresholds {
                    if deviation >= threshold {
                        let anomaly = DetectedAnomaly {
                            id: format!("anomaly_{}", Utc::now().timestamp_nanos_opt().unwrap_or(0)),
                            anomaly_type: self.infer_anomaly_type(&feature, category),
                            severity: *severity,
                            timestamp: Utc::now(),
                            description: format!("{} deviates significantly (z-score: {:.2})", feature, deviation),
                            confidence: baseline.confidence * (deviation / 10.0).min(1.0),
                            features: features.clone(),
                        };
                        
                        anomalies.push(anomaly.clone());
                        
                        // Store in history
                        self.anomaly_history.write().unwrap().push_back(anomaly);
                        
                        break; // Only report highest severity
                    }
                }
            }
        }
        
        anomalies
    }
    
    /// Process behavioral sequence
    pub fn process_sequence(&self, action: String) -> Vec<DetectedAnomaly> {
        let mut anomalies = Vec::new();
        
        // Add to pattern detector
        self.pattern_detector.write().unwrap().add_action(action.clone());
        
        let detector = self.pattern_detector.read().unwrap();
        
        // Check for stuck patterns
        if let Some(stuck_action) = detector.detect_stuck_patterns(5) {
            anomalies.push(DetectedAnomaly {
                id: format!("stuck_{}", Utc::now().timestamp_nanos_opt().unwrap_or(0)),
                anomaly_type: AnomalyType::StuckState,
                severity: Severity::Medium,
                timestamp: Utc::now(),
                description: format!("Stuck in repeated action: {}", stuck_action),
                confidence: 0.9,
                features: HashMap::new(),
            });
        }
        
        // Check for circular patterns
        let circular = detector.detect_circular_patterns(3);
        if !circular.is_empty() {
            anomalies.push(DetectedAnomaly {
                id: format!("circular_{}", Utc::now().timestamp_nanos_opt().unwrap_or(0)),
                anomaly_type: AnomalyType::CircularPattern,
                severity: Severity::Low,
                timestamp: Utc::now(),
                description: format!("Circular pattern detected: {:?}", circular[0]),
                confidence: 0.8,
                features: HashMap::new(),
            });
        }
        
        anomalies
    }
    
    /// Fast anomaly check for real-time processing
    pub fn quick_check(&self, category: &str, key_features: &[(&str, f64)]) -> Option<(AnomalyType, Severity)> {
        // Quick threshold checks for common anomalies
        for (feature, value) in key_features {
            match *feature {
                "cpu_usage" if *value > 90.0 => {
                    return Some((AnomalyType::ResourceWarning, Severity::High));
                }
                "memory_usage" if *value > 85.0 => {
                    return Some((AnomalyType::ResourceWarning, Severity::High));
                }
                "error_count" if *value > 0.0 => {
                    return Some((AnomalyType::ErrorDialog, Severity::Medium));
                }
                "latency_ms" if *value > 1000.0 => {
                    return Some((AnomalyType::NetworkIssue, Severity::Medium));
                }
                _ => {}
            }
        }
        
        None
    }
    
    /// Infer anomaly type from feature name
    fn infer_anomaly_type(&self, feature: &str, category: &str) -> AnomalyType {
        match (category, feature) {
            ("visual", "element_count") | ("visual", "layout_complexity") => AnomalyType::UnusualLayout,
            ("visual", "modal_present") | ("visual", "popup_count") => AnomalyType::UnexpectedPopup,
            ("visual", "error_keywords") => AnomalyType::ErrorDialog,
            ("behavioral", "repetition_score") => AnomalyType::CircularPattern,
            ("behavioral", "idle_ratio") => AnomalyType::StuckState,
            ("behavioral", "navigation_complexity") => AnomalyType::UnusualNavigation,
            ("system", "cpu_usage") | ("system", "memory_usage") => AnomalyType::ResourceWarning,
            ("system", "network_latency") => AnomalyType::NetworkIssue,
            ("system", "crash_count") => AnomalyType::CrashIndicator,
            _ => AnomalyType::DataInconsistency,
        }
    }
    
    /// Get recent anomalies
    pub fn get_recent_anomalies(&self, count: usize) -> Vec<DetectedAnomaly> {
        let history = self.anomaly_history.read().unwrap();
        history.iter().rev().take(count).cloned().collect()
    }
    
    /// Get anomaly statistics
    pub fn get_statistics(&self) -> HashMap<String, usize> {
        let history = self.anomaly_history.read().unwrap();
        let mut stats = HashMap::new();
        
        stats.insert("total_anomalies".to_string(), history.len());
        
        // Count by type
        let mut type_counts: HashMap<AnomalyType, usize> = HashMap::new();
        for anomaly in history.iter() {
            *type_counts.entry(anomaly.anomaly_type).or_insert(0) += 1;
        }
        
        // Convert to string keys for serialization
        for (anomaly_type, count) in type_counts {
            stats.insert(format!("{:?}", anomaly_type), count);
        }
        
        stats
    }
}

/// Python bindings
#[cfg(feature = "python")]
pub mod python {
    use super::*;
    use pyo3::prelude::*;
    use pyo3::types::{PyDict, PyList};
    use std::collections::HashMap;
    
    #[pyclass]
    pub struct PyAnomalyDetector {
        detector: AnomalyDetector,
    }
    
    #[pymethods]
    impl PyAnomalyDetector {
        #[new]
        pub fn new() -> Self {
            Self {
                detector: AnomalyDetector::new(),
            }
        }
        
        /// Establish baseline from feature dictionaries
        pub fn establish_baseline(&self, category: &str, features_list: &PyList) -> PyResult<()> {
            let mut rust_features = Vec::new();
            
            for item in features_list {
                let dict: &PyDict = item.downcast()?;
                let mut features = HashMap::new();
                
                for (key, value) in dict {
                    let key_str: String = key.extract()?;
                    let val: f64 = value.extract()?;
                    features.insert(key_str, val);
                }
                
                rust_features.push(features);
            }
            
            self.detector.establish_baseline(category, rust_features)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e))
        }
        
        /// Detect anomalies in features
        pub fn detect_anomalies(&self, category: &str, features: &PyDict) -> PyResult<Vec<PyObject>> {
            let mut rust_features = HashMap::new();
            
            for (key, value) in features {
                let key_str: String = key.extract()?;
                let val: f64 = value.extract()?;
                rust_features.insert(key_str, val);
            }
            
            let anomalies = self.detector.detect_anomalies(category, rust_features);
            
            // Convert to Python objects
            Python::with_gil(|py| {
                anomalies.into_iter()
                    .map(|a| {
                        let dict = PyDict::new(py);
                        dict.set_item("id", a.id)?;
                        dict.set_item("type", format!("{:?}", a.anomaly_type))?;
                        dict.set_item("severity", a.severity as u8)?;
                        dict.set_item("description", a.description)?;
                        dict.set_item("confidence", a.confidence)?;
                        Ok(dict.into())
                    })
                    .collect()
            })
        }
        
        /// Process action sequence
        pub fn process_sequence(&self, action: String) -> PyResult<Vec<PyObject>> {
            let anomalies = self.detector.process_sequence(action);
            
            Python::with_gil(|py| {
                anomalies.into_iter()
                    .map(|a| {
                        let dict = PyDict::new(py);
                        dict.set_item("id", a.id)?;
                        dict.set_item("type", format!("{:?}", a.anomaly_type))?;
                        dict.set_item("severity", a.severity as u8)?;
                        dict.set_item("description", a.description)?;
                        Ok(dict.into())
                    })
                    .collect()
            })
        }
        
        /// Quick anomaly check
        pub fn quick_check(&self, category: &str, features: &PyList) -> PyResult<Option<(String, u8)>> {
            let mut key_features = Vec::new();
            
            for item in features {
                let tuple: (&str, f64) = item.extract()?;
                key_features.push(tuple);
            }
            
            Ok(self.detector.quick_check(category, &key_features)
                .map(|(t, s)| (format!("{:?}", t), s as u8)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_baseline_update() {
        let mut baseline = Baseline::new();
        
        let mut features = HashMap::new();
        features.insert("cpu".to_string(), 50.0);
        features.insert("memory".to_string(), 60.0);
        
        baseline.update(&features);
        
        assert_eq!(baseline.sample_count, 1);
        assert_eq!(baseline.means.get("cpu"), Some(&50.0));
    }
    
    #[test]
    fn test_pattern_detection() {
        let mut detector = PatternDetector::new(5);
        
        // Add stuck pattern
        for _ in 0..6 {
            detector.add_action("click_button".to_string());
        }
        
        assert!(detector.detect_stuck_patterns(5).is_some());
    }
}