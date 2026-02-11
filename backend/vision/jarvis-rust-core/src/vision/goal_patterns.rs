//! Goal Pattern Matching for Goal Inference System
//! High-performance pattern recognition for user goals

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

/// Goal pattern types
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum GoalPatternType {
    ActionSequence,
    ApplicationCombination,
    ContentPattern,
    TemporalPattern,
    ErrorRecovery,
}

/// Evidence type for pattern matching
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternEvidence {
    pub evidence_type: String,
    pub data: HashMap<String, serde_json::Value>,
    pub timestamp: i64,
    pub weight: f32,
}

/// Goal pattern definition
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GoalPattern {
    pub pattern_id: String,
    pub pattern_type: GoalPatternType,
    pub required_evidence: Vec<String>,
    pub optional_evidence: Vec<String>,
    pub negative_evidence: Vec<String>,
    pub min_confidence: f32,
    pub learned_weights: HashMap<String, f32>,
}

/// Pattern match result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternMatch {
    pub pattern_id: String,
    pub confidence: f32,
    pub matched_evidence: Vec<String>,
    pub missing_evidence: Vec<String>,
}

/// High-performance pattern matcher
pub struct GoalPatternMatcher {
    patterns: Arc<RwLock<HashMap<String, GoalPattern>>>,
    evidence_cache: Arc<RwLock<Vec<PatternEvidence>>>,
    match_cache: Arc<RwLock<HashMap<String, PatternMatch>>>,
}

impl GoalPatternMatcher {
    pub fn new() -> Self {
        Self {
            patterns: Arc::new(RwLock::new(HashMap::new())),
            evidence_cache: Arc::new(RwLock::new(Vec::with_capacity(100))),
            match_cache: Arc::new(RwLock::new(HashMap::new())),
        }
    }
    
    /// Add a new pattern
    pub fn add_pattern(&self, pattern: GoalPattern) {
        let mut patterns = self.patterns.write();
        patterns.insert(pattern.pattern_id.clone(), pattern);
        
        // Clear match cache when patterns change
        self.match_cache.write().clear();
    }
    
    /// Add evidence for matching
    pub fn add_evidence(&self, evidence: PatternEvidence) {
        let mut cache = self.evidence_cache.write();
        cache.push(evidence);
        
        // Keep only recent evidence (last 100)
        if cache.len() > 100 {
            cache.drain(0..20);
        }
        
        // Clear match cache when evidence changes
        self.match_cache.write().clear();
    }
    
    /// Match patterns against current evidence
    pub fn match_patterns(&self) -> Vec<PatternMatch> {
        // Check cache first
        let cache = self.match_cache.read();
        if !cache.is_empty() {
            return cache.values().cloned().collect();
        }
        drop(cache);
        
        // Perform matching
        let patterns = self.patterns.read();
        let evidence = self.evidence_cache.read();
        let mut matches = Vec::new();
        
        // Extract evidence types
        let evidence_types: HashSet<String> = evidence
            .iter()
            .map(|e| e.evidence_type.clone())
            .collect();
        
        for (pattern_id, pattern) in patterns.iter() {
            if let Some(pattern_match) = self.match_single_pattern(pattern, &evidence_types, &evidence) {
                matches.push(pattern_match);
            }
        }
        
        // Sort by confidence
        matches.sort_by(|a, b| b.confidence.partial_cmp(&a.confidence).unwrap());
        
        // Cache results
        let mut cache = self.match_cache.write();
        for m in &matches {
            cache.insert(m.pattern_id.clone(), m.clone());
        }
        
        matches
    }
    
    /// Match a single pattern
    fn match_single_pattern(
        &self,
        pattern: &GoalPattern,
        evidence_types: &HashSet<String>,
        evidence: &[PatternEvidence],
    ) -> Option<PatternMatch> {
        // Check required evidence
        let mut missing_evidence = Vec::new();
        for req in &pattern.required_evidence {
            if !evidence_types.contains(req) {
                missing_evidence.push(req.clone());
            }
        }
        
        // If missing required evidence, no match
        if !missing_evidence.is_empty() {
            return None;
        }
        
        // Calculate confidence
        let mut confidence = 0.5; // Base confidence
        let mut matched_evidence = pattern.required_evidence.clone();
        
        // Add score for optional evidence
        let mut optional_matches = 0;
        for opt in &pattern.optional_evidence {
            if evidence_types.contains(opt) {
                optional_matches += 1;
                matched_evidence.push(opt.clone());
            }
        }
        
        if !pattern.optional_evidence.is_empty() {
            let optional_ratio = optional_matches as f32 / pattern.optional_evidence.len() as f32;
            confidence += 0.3 * optional_ratio;
        }
        
        // Check negative evidence
        let mut negative_matches = 0;
        for neg in &pattern.negative_evidence {
            if evidence_types.contains(neg) {
                negative_matches += 1;
            }
        }
        
        if negative_matches > 0 && !pattern.negative_evidence.is_empty() {
            let negative_penalty = 0.2 * (negative_matches as f32 / pattern.negative_evidence.len() as f32);
            confidence -= negative_penalty;
        }
        
        // Apply learned weights
        confidence = self.apply_learned_weights(pattern, evidence, confidence);
        
        // Check minimum confidence
        if confidence < pattern.min_confidence {
            return None;
        }
        
        Some(PatternMatch {
            pattern_id: pattern.pattern_id.clone(),
            confidence,
            matched_evidence,
            missing_evidence,
        })
    }
    
    /// Apply learned weights to confidence
    fn apply_learned_weights(
        &self,
        pattern: &GoalPattern,
        evidence: &[PatternEvidence],
        base_confidence: f32,
    ) -> f32 {
        if pattern.learned_weights.is_empty() {
            return base_confidence;
        }
        
        let mut weighted_sum = 0.0;
        let mut total_weight = 0.0;
        
        for e in evidence {
            if let Some(weight) = pattern.learned_weights.get(&e.evidence_type) {
                weighted_sum += weight * e.weight;
                total_weight += weight;
            }
        }
        
        if total_weight > 0.0 {
            let weight_factor = weighted_sum / total_weight;
            base_confidence * (0.8 + 0.4 * weight_factor) // Scale between 0.8x and 1.2x
        } else {
            base_confidence
        }
    }
    
    /// Update learned weights based on feedback
    pub fn update_weights(&self, pattern_id: &str, success: bool) {
        let mut patterns = self.patterns.write();
        if let Some(pattern) = patterns.get_mut(pattern_id) {
            let evidence = self.evidence_cache.read();
            
            // Simple weight update based on success/failure
            let adjustment = if success { 0.1 } else { -0.05 };
            
            for e in evidence.iter() {
                let current_weight = pattern.learned_weights.get(&e.evidence_type).copied().unwrap_or(1.0);
                let new_weight = (current_weight + adjustment).clamp(0.1, 2.0);
                pattern.learned_weights.insert(e.evidence_type.clone(), new_weight);
            }
        }
        
        // Clear match cache after weight update
        self.match_cache.write().clear();
    }
    
    /// Get pattern statistics
    pub fn get_stats(&self) -> HashMap<String, serde_json::Value> {
        use serde_json::json;
        
        let patterns = self.patterns.read();
        let evidence = self.evidence_cache.read();
        let matches = self.match_cache.read();
        
        let mut stats = HashMap::new();
        stats.insert("pattern_count".to_string(), json!(patterns.len()));
        stats.insert("evidence_count".to_string(), json!(evidence.len()));
        stats.insert("cached_matches".to_string(), json!(matches.len()));
        
        // Pattern type distribution
        let mut type_counts = HashMap::new();
        for pattern in patterns.values() {
            *type_counts.entry(format!("{:?}", pattern.pattern_type)).or_insert(0) += 1;
        }
        stats.insert("pattern_types".to_string(), json!(type_counts));
        
        stats
    }
}

/// Python FFI bindings
#[cfg(feature = "python-bindings")]
pub mod python_bindings {
    use super::*;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;
    
    #[pyclass]
    struct PyGoalPatternMatcher {
        inner: GoalPatternMatcher,
    }
    
    #[pymethods]
    impl PyGoalPatternMatcher {
        #[new]
        fn new() -> Self {
            Self {
                inner: GoalPatternMatcher::new(),
            }
        }
        
        fn add_pattern(&self, pattern_json: &str) -> PyResult<()> {
            let pattern: GoalPattern = serde_json::from_str(pattern_json)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
            self.inner.add_pattern(pattern);
            Ok(())
        }
        
        fn add_evidence(&self, evidence_json: &str) -> PyResult<()> {
            let evidence: PatternEvidence = serde_json::from_str(evidence_json)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
            self.inner.add_evidence(evidence);
            Ok(())
        }
        
        fn match_patterns(&self) -> PyResult<String> {
            let matches = self.inner.match_patterns();
            serde_json::to_string(&matches)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
        }
        
        fn update_weights(&self, pattern_id: &str, success: bool) -> PyResult<()> {
            self.inner.update_weights(pattern_id, success);
            Ok(())
        }
        
        fn get_stats(&self, py: Python) -> PyResult<PyObject> {
            let stats = self.inner.get_stats();
            let dict = PyDict::new(py);
            
            for (key, value) in stats {
                let val_str = value.to_string();
                dict.set_item(key, val_str)?;
            }
            
            Ok(dict.into())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    
    #[test]
    fn test_pattern_matching() {
        let matcher = GoalPatternMatcher::new();
        
        // Add a pattern
        let pattern = GoalPattern {
            pattern_id: "debug_pattern".to_string(),
            pattern_type: GoalPatternType::ActionSequence,
            required_evidence: vec!["error".to_string(), "action".to_string()],
            optional_evidence: vec!["application".to_string()],
            negative_evidence: vec!["communication".to_string()],
            min_confidence: 0.7,
            learned_weights: HashMap::new(),
        };
        
        matcher.add_pattern(pattern);
        
        // Add evidence
        matcher.add_evidence(PatternEvidence {
            evidence_type: "error".to_string(),
            data: [(String::from("type"), json!("syntax_error"))].into_iter().collect(),
            timestamp: 1234567890,
            weight: 1.0,
        });
        
        matcher.add_evidence(PatternEvidence {
            evidence_type: "action".to_string(),
            data: [(String::from("type"), json!("debugging"))].into_iter().collect(),
            timestamp: 1234567891,
            weight: 0.9,
        });
        
        // Match patterns
        let matches = matcher.match_patterns();
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].pattern_id, "debug_pattern");
        assert!(matches[0].confidence >= 0.7);
    }
}
