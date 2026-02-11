//! High-performance intervention decision engine
//! Real-time user state analysis and intervention timing optimization

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, RwLock};
use serde::{Serialize, Deserialize};
use chrono::{DateTime, Utc, Duration};

/// User psychological/productivity states
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum UserState {
    Focused,
    Frustrated,
    Productive,
    Struggling,
    Stressed,
    Idle,
    Learning,
    Confused,
}

/// Intervention types
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum InterventionType {
    SilentMonitoring,
    SubtleIndication,
    SuggestionOffer,
    DirectAssistance,
    AutonomousAction,
}

/// Signal for user state detection
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserSignal {
    pub signal_type: SignalType,
    pub value: f64,
    pub confidence: f64,
    pub timestamp: DateTime<Utc>,
    pub metadata: HashMap<String, f64>,
}

/// Types of user behavior signals
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SignalType {
    MouseMovement,
    TypingPattern,
    NavigationFlow,
    ErrorRate,
    TaskCompletion,
    ApplicationSwitch,
    IdleTime,
    DocumentationView,
    BackspaceRate,
    ScrollPattern,
}

/// Real-time user state detector
pub struct UserStateDetector {
    signal_buffer: Arc<RwLock<VecDeque<UserSignal>>>,
    state_models: HashMap<UserState, StateModel>,
    current_state: Arc<RwLock<(UserState, f64)>>, // (state, confidence)
    max_buffer_size: usize,
}

/// Model for detecting specific user states
struct StateModel {
    weights: HashMap<String, f64>,
    threshold: f64,
    signal_patterns: Vec<SignalPattern>,
}

/// Pattern of signals indicating a state
struct SignalPattern {
    required_signals: Vec<SignalType>,
    time_window: Duration,
    min_confidence: f64,
}

impl UserStateDetector {
    pub fn new(max_buffer_size: usize) -> Self {
        let mut detector = Self {
            signal_buffer: Arc::new(RwLock::new(VecDeque::with_capacity(max_buffer_size))),
            state_models: HashMap::new(),
            current_state: Arc::new(RwLock::new((UserState::Idle, 0.5))),
            max_buffer_size,
        };
        
        // Initialize state models
        detector.initialize_state_models();
        detector
    }
    
    fn initialize_state_models(&mut self) {
        // Focused state model
        self.state_models.insert(UserState::Focused, StateModel {
            weights: vec![
                ("typing_consistency".to_string(), 0.8),
                ("low_error_rate".to_string(), 0.6),
                ("steady_mouse".to_string(), 0.4),
                ("minimal_switches".to_string(), 0.7),
            ].into_iter().collect(),
            threshold: 0.7,
            signal_patterns: vec![
                SignalPattern {
                    required_signals: vec![SignalType::TypingPattern, SignalType::MouseMovement],
                    time_window: Duration::seconds(60),
                    min_confidence: 0.7,
                }
            ],
        });
        
        // Frustrated state model
        self.state_models.insert(UserState::Frustrated, StateModel {
            weights: vec![
                ("high_error_rate".to_string(), 0.9),
                ("rapid_backspace".to_string(), 0.8),
                ("erratic_mouse".to_string(), 0.6),
                ("repeated_actions".to_string(), 0.7),
            ].into_iter().collect(),
            threshold: 0.6,
            signal_patterns: vec![
                SignalPattern {
                    required_signals: vec![SignalType::ErrorRate, SignalType::BackspaceRate],
                    time_window: Duration::seconds(30),
                    min_confidence: 0.6,
                }
            ],
        });
        
        // Add more state models...
    }
    
    /// Add new signal to processing
    pub fn add_signal(&self, signal: UserSignal) {
        let mut buffer = self.signal_buffer.write().unwrap();
        buffer.push_back(signal);
        
        // Maintain buffer size
        while buffer.len() > self.max_buffer_size {
            buffer.pop_front();
        }
    }
    
    /// Detect current user state
    pub fn detect_state(&self) -> (UserState, f64) {
        let buffer = self.signal_buffer.read().unwrap();
        if buffer.is_empty() {
            return *self.current_state.read().unwrap();
        }
        
        let recent_signals: Vec<UserSignal> = buffer.iter()
            .rev()
            .take(100)
            .cloned()
            .collect();
        
        // Calculate features
        let features = self.extract_state_features(&recent_signals);
        
        // Score each state
        let mut state_scores: Vec<(UserState, f64)> = Vec::new();
        
        for (state, model) in &self.state_models {
            let score = self.score_state(&features, model, &recent_signals);
            state_scores.push((*state, score));
        }
        
        // Select highest scoring state
        state_scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        let best_state = state_scores[0];
        
        // Update current state
        *self.current_state.write().unwrap() = best_state;
        
        best_state
    }
    
    /// Extract features for state detection
    fn extract_state_features(&self, signals: &[UserSignal]) -> HashMap<String, f64> {
        let mut features = HashMap::new();
        
        // Signal type statistics
        let mut type_counts: HashMap<SignalType, usize> = HashMap::new();
        let mut type_values: HashMap<SignalType, Vec<f64>> = HashMap::new();
        
        for signal in signals {
            *type_counts.entry(signal.signal_type).or_insert(0) += 1;
            type_values.entry(signal.signal_type)
                .or_insert_with(Vec::new)
                .push(signal.value);
        }
        
        // Calculate features for each signal type
        for (signal_type, values) in &type_values {
            if !values.is_empty() {
                let mean = values.iter().sum::<f64>() / values.len() as f64;
                let variance = self.calculate_variance(values);
                let trend = self.calculate_trend(values);
                
                features.insert(format!("{:?}_mean", signal_type), mean);
                features.insert(format!("{:?}_variance", signal_type), variance);
                features.insert(format!("{:?}_trend", signal_type), trend);
            }
        }
        
        // Behavioral features
        features.insert("typing_consistency".to_string(), 
                       self.calculate_typing_consistency(signals));
        features.insert("low_error_rate".to_string(), 
                       1.0 - self.calculate_error_rate(signals));
        features.insert("steady_mouse".to_string(), 
                       self.calculate_mouse_steadiness(signals));
        features.insert("minimal_switches".to_string(), 
                       1.0 - self.calculate_switch_frequency(signals));
        features.insert("high_error_rate".to_string(), 
                       self.calculate_error_rate(signals));
        features.insert("rapid_backspace".to_string(), 
                       self.calculate_backspace_rate(signals));
        features.insert("erratic_mouse".to_string(), 
                       1.0 - self.calculate_mouse_steadiness(signals));
        features.insert("repeated_actions".to_string(), 
                       self.calculate_repetition_score(signals));
        
        features
    }
    
    /// Score a state based on features and model
    fn score_state(&self, features: &HashMap<String, f64>, 
                   model: &StateModel, signals: &[UserSignal]) -> f64 {
        let mut score = 0.0;
        let mut weight_sum = 0.0;
        
        // Weight-based scoring
        for (feature_name, &weight) in &model.weights {
            if let Some(&value) = features.get(feature_name) {
                score += weight * value;
                weight_sum += weight.abs();
            }
        }
        
        if weight_sum > 0.0 {
            score /= weight_sum;
        }
        
        // Check signal patterns
        let pattern_match = self.check_signal_patterns(signals, &model.signal_patterns);
        score = score * 0.7 + pattern_match * 0.3;
        
        // Apply threshold
        if score >= model.threshold {
            score
        } else {
            score * 0.5 // Penalize below-threshold scores
        }
    }
    
    /// Check if signals match required patterns
    fn check_signal_patterns(&self, signals: &[UserSignal], 
                           patterns: &[SignalPattern]) -> f64 {
        if patterns.is_empty() {
            return 1.0;
        }
        
        let mut best_match: f64 = 0.0;

        for pattern in patterns {
            let recent_window = Utc::now() - pattern.time_window;
            let window_signals: Vec<&UserSignal> = signals.iter()
                .filter(|s| s.timestamp > recent_window)
                .collect();
            
            // Check if all required signals are present
            let mut found_signals = 0;
            for required in &pattern.required_signals {
                if window_signals.iter().any(|s| s.signal_type == *required) {
                    found_signals += 1;
                }
            }
            
            let match_score = found_signals as f64 / pattern.required_signals.len() as f64;
            best_match = best_match.max(match_score);
        }
        
        best_match
    }
    
    // Feature calculation methods
    
    fn calculate_variance(&self, values: &[f64]) -> f64 {
        if values.len() < 2 {
            return 0.0;
        }
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        values.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / values.len() as f64
    }
    
    fn calculate_trend(&self, values: &[f64]) -> f64 {
        if values.len() < 2 {
            return 0.0;
        }
        
        // Simple linear trend
        let n = values.len() as f64;
        let x_mean = (n - 1.0) / 2.0;
        let y_mean = values.iter().sum::<f64>() / n;
        
        let mut num = 0.0;
        let mut den = 0.0;
        
        for (i, &y) in values.iter().enumerate() {
            let x = i as f64;
            num += (x - x_mean) * (y - y_mean);
            den += (x - x_mean).powi(2);
        }
        
        if den > 0.0 {
            num / den
        } else {
            0.0
        }
    }
    
    fn calculate_typing_consistency(&self, signals: &[UserSignal]) -> f64 {
        let typing_signals: Vec<f64> = signals.iter()
            .filter(|s| s.signal_type == SignalType::TypingPattern)
            .map(|s| s.value)
            .collect();
        
        if typing_signals.len() < 2 {
            return 0.5;
        }
        
        // Lower variance means more consistency
        let variance = self.calculate_variance(&typing_signals);
        1.0 / (1.0 + variance)
    }
    
    fn calculate_error_rate(&self, signals: &[UserSignal]) -> f64 {
        let error_count = signals.iter()
            .filter(|s| s.signal_type == SignalType::ErrorRate)
            .map(|s| s.value)
            .sum::<f64>();
        
        error_count / signals.len().max(1) as f64
    }
    
    fn calculate_mouse_steadiness(&self, signals: &[UserSignal]) -> f64 {
        let mouse_signals: Vec<f64> = signals.iter()
            .filter(|s| s.signal_type == SignalType::MouseMovement)
            .map(|s| s.value)
            .collect();
        
        if mouse_signals.is_empty() {
            return 0.5;
        }
        
        // Calculate smoothness
        let mut smoothness = 0.0;
        for window in mouse_signals.windows(2) {
            let diff = (window[1] - window[0]).abs();
            smoothness += 1.0 / (1.0 + diff);
        }
        
        smoothness / mouse_signals.len().max(1) as f64
    }
    
    fn calculate_switch_frequency(&self, signals: &[UserSignal]) -> f64 {
        let switch_count = signals.iter()
            .filter(|s| s.signal_type == SignalType::ApplicationSwitch)
            .count();
        
        switch_count as f64 / signals.len().max(1) as f64
    }
    
    fn calculate_backspace_rate(&self, signals: &[UserSignal]) -> f64 {
        signals.iter()
            .filter(|s| s.signal_type == SignalType::BackspaceRate)
            .map(|s| s.value)
            .max_by(|a, b| a.partial_cmp(b).unwrap())
            .unwrap_or(0.0)
    }
    
    fn calculate_repetition_score(&self, signals: &[UserSignal]) -> f64 {
        // Look for repeated patterns in navigation
        let nav_signals: Vec<&UserSignal> = signals.iter()
            .filter(|s| s.signal_type == SignalType::NavigationFlow)
            .collect();
        
        if nav_signals.len() < 3 {
            return 0.0;
        }
        
        // Simple repetition detection
        let mut repetitions = 0;
        for i in 1..nav_signals.len() {
            if (nav_signals[i].value - nav_signals[i-1].value).abs() < 0.1 {
                repetitions += 1;
            }
        }
        
        repetitions as f64 / nav_signals.len() as f64
    }
}

/// Timing optimizer for interventions
pub struct TimingOptimizer {
    activity_patterns: Arc<RwLock<Vec<ActivityPattern>>>,
    break_detector: BreakDetector,
    load_estimator: CognitiveLoadEstimator,
}

#[derive(Clone)]
struct ActivityPattern {
    start_time: DateTime<Utc>,
    end_time: DateTime<Utc>,
    activity_type: String,
    completion_status: bool,
}

struct BreakDetector {
    idle_threshold: Duration,
    context_switch_weight: f64,
}

struct CognitiveLoadEstimator {
    signal_weights: HashMap<SignalType, f64>,
}

impl TimingOptimizer {
    pub fn new() -> Self {
        Self {
            activity_patterns: Arc::new(RwLock::new(Vec::new())),
            break_detector: BreakDetector {
                idle_threshold: Duration::seconds(30),
                context_switch_weight: 0.7,
            },
            load_estimator: CognitiveLoadEstimator {
                signal_weights: vec![
                    (SignalType::ErrorRate, 2.0),
                    (SignalType::BackspaceRate, 1.5),
                    (SignalType::TypingPattern, 1.0),
                    (SignalType::MouseMovement, 0.5),
                ].into_iter().collect(),
            },
        }
    }
    
    /// Calculate optimal intervention timing score
    pub fn calculate_timing_score(&self, signals: &[UserSignal]) -> TimingScore {
        TimingScore {
            natural_break: self.detect_natural_break(signals),
            task_boundary: self.detect_task_boundary(signals),
            cognitive_load: self.estimate_cognitive_load(signals),
            request_likelihood: self.predict_request_likelihood(signals),
            overall_score: 0.0, // Will be calculated
        }
    }
    
    fn detect_natural_break(&self, signals: &[UserSignal]) -> f64 {
        // Check for idle time
        let recent_idle = signals.iter()
            .rev()
            .take(10)
            .filter(|s| s.signal_type == SignalType::IdleTime)
            .map(|s| s.value)
            .max_by(|a, b| a.partial_cmp(b).unwrap())
            .unwrap_or(0.0);
        
        // Check for context switches
        let switch_count = signals.iter()
            .rev()
            .take(20)
            .filter(|s| s.signal_type == SignalType::ApplicationSwitch)
            .count();
        
        let idle_score = (recent_idle / 30.0).min(1.0);
        let switch_score = (switch_count as f64 / 5.0).min(1.0) * self.break_detector.context_switch_weight;
        
        (idle_score + switch_score) / 2.0
    }
    
    fn detect_task_boundary(&self, signals: &[UserSignal]) -> f64 {
        let recent_completions = signals.iter()
            .rev()
            .take(10)
            .filter(|s| s.signal_type == SignalType::TaskCompletion)
            .count();
        
        (recent_completions as f64 / 3.0).min(1.0)
    }
    
    fn estimate_cognitive_load(&self, signals: &[UserSignal]) -> f64 {
        let mut load = 0.0;
        let mut weight_sum = 0.0;
        
        for signal in signals.iter().rev().take(50) {
            if let Some(&weight) = self.load_estimator.signal_weights.get(&signal.signal_type) {
                load += signal.value * weight;
                weight_sum += weight;
            }
        }
        
        if weight_sum > 0.0 {
            (load / weight_sum).min(1.0)
        } else {
            0.5
        }
    }
    
    fn predict_request_likelihood(&self, signals: &[UserSignal]) -> f64 {
        // Look for help-seeking patterns
        let help_signals = signals.iter()
            .rev()
            .take(30)
            .filter(|s| s.signal_type == SignalType::DocumentationView)
            .count();
        
        let error_rate = signals.iter()
            .rev()
            .take(20)
            .filter(|s| s.signal_type == SignalType::ErrorRate)
            .map(|s| s.value)
            .max_by(|a, b| a.partial_cmp(b).unwrap())
            .unwrap_or(0.0);
        
        let help_score = (help_signals as f64 / 5.0).min(1.0);
        let error_score = error_rate;
        
        (help_score + error_score) / 2.0
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimingScore {
    pub natural_break: f64,
    pub task_boundary: f64,
    pub cognitive_load: f64,
    pub request_likelihood: f64,
    pub overall_score: f64,
}

impl TimingScore {
    pub fn calculate_overall(&mut self) {
        // Weight the different timing factors
        self.overall_score = 
            self.natural_break * 0.3 +
            self.task_boundary * 0.3 +
            (1.0 - self.cognitive_load) * 0.2 +
            self.request_likelihood * 0.2;
    }
}

/// Python bindings
#[cfg(feature = "python-bindings")]
pub mod python {
    use super::*;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;
    
    #[pyclass]
    pub struct PyUserStateDetector {
        detector: UserStateDetector,
    }
    
    #[pymethods]
    impl PyUserStateDetector {
        #[new]
        pub fn new(max_buffer_size: usize) -> Self {
            Self {
                detector: UserStateDetector::new(max_buffer_size),
            }
        }
        
        pub fn add_signal(&self, signal_type: &str, value: f64, confidence: f64) {
            let signal = UserSignal {
                signal_type: match signal_type {
                    "mouse_movement" => SignalType::MouseMovement,
                    "typing_pattern" => SignalType::TypingPattern,
                    "error_rate" => SignalType::ErrorRate,
                    _ => SignalType::NavigationFlow,
                },
                value,
                confidence,
                timestamp: Utc::now(),
                metadata: HashMap::new(),
            };
            
            self.detector.add_signal(signal);
        }
        
        pub fn detect_state(&self) -> (String, f64) {
            let (state, confidence) = self.detector.detect_state();
            let state_str = match state {
                UserState::Focused => "focused",
                UserState::Frustrated => "frustrated",
                UserState::Productive => "productive",
                UserState::Struggling => "struggling",
                UserState::Stressed => "stressed",
                UserState::Idle => "idle",
                UserState::Learning => "learning",
                UserState::Confused => "confused",
            };
            (state_str.to_string(), confidence)
        }
    }
    
    #[pyclass]
    pub struct PyTimingOptimizer {
        optimizer: TimingOptimizer,
    }
    
    #[pymethods]
    impl PyTimingOptimizer {
        #[new]
        pub fn new() -> Self {
            Self {
                optimizer: TimingOptimizer::new(),
            }
        }
        
        pub fn calculate_timing_score(&self, py: Python) -> PyResult<PyObject> {
            // Would need to accept signals and convert
            let score = TimingScore {
                natural_break: 0.5,
                task_boundary: 0.3,
                cognitive_load: 0.6,
                request_likelihood: 0.4,
                overall_score: 0.45,
            };
            
            let dict = PyDict::new(py);
            dict.set_item("natural_break", score.natural_break)?;
            dict.set_item("task_boundary", score.task_boundary)?;
            dict.set_item("cognitive_load", score.cognitive_load)?;
            dict.set_item("request_likelihood", score.request_likelihood)?;
            dict.set_item("overall_score", score.overall_score)?;
            
            Ok(dict.into())
        }
    }
}
