//! Model optimization utilities for quantization and pruning

use super::{QuantizedTensor, QuantizationType, Quantize};
use crate::{Result, JarvisError};
use ndarray::{Array1, Array2, ArrayView2};
use std::collections::HashMap;

/// Model optimization configuration
#[derive(Debug, Clone)]
pub struct OptimizationConfig {
    pub quantization_type: QuantizationType,
    pub pruning_threshold: f32,
    pub calibration_samples: usize,
    pub dynamic_quantization: bool,
    pub optimize_for_hardware: bool,
}

impl Default for OptimizationConfig {
    fn default() -> Self {
        Self {
            quantization_type: QuantizationType::Int8,
            pruning_threshold: 0.5,
            calibration_samples: 100,
            dynamic_quantization: true,
            optimize_for_hardware: true,
        }
    }
}

/// Layer statistics for optimization
#[derive(Debug)]
struct LayerStats {
    min_val: f32,
    max_val: f32,
    mean: f32,
    std_dev: f32,
    sparsity: f32,
}

/// Model optimizer
pub struct ModelOptimizer {
    config: OptimizationConfig,
    layer_stats: HashMap<String, LayerStats>,
}

impl ModelOptimizer {
    pub fn new(config: OptimizationConfig) -> Self {
        Self {
            config,
            layer_stats: HashMap::new(),
        }
    }
    
    /// Analyze layer for optimization
    pub fn analyze_layer(&mut self, name: &str, weights: ArrayView2<f32>) -> Result<()> {
        let flat_weights = weights.as_slice().unwrap();
        
        let (min_val, max_val) = flat_weights.iter()
            .fold((f32::INFINITY, f32::NEG_INFINITY), |(min, max), &val| {
                (min.min(val), max.max(val))
            });
        
        let mean = flat_weights.iter().sum::<f32>() / flat_weights.len() as f32;
        
        let variance = flat_weights.iter()
            .map(|&val| (val - mean).powi(2))
            .sum::<f32>() / flat_weights.len() as f32;
        
        let std_dev = variance.sqrt();
        
        let zero_count = flat_weights.iter().filter(|&&val| val.abs() < 1e-6).count();
        let sparsity = zero_count as f32 / flat_weights.len() as f32;
        
        self.layer_stats.insert(name.to_string(), LayerStats {
            min_val,
            max_val,
            mean,
            std_dev,
            sparsity,
        });
        
        Ok(())
    }
    
    /// Select optimal quantization type for layer
    pub fn select_quantization_type(&self, layer_name: &str) -> QuantizationType {
        if !self.config.dynamic_quantization {
            return self.config.quantization_type;
        }
        
        if let Some(stats) = self.layer_stats.get(layer_name) {
            // High dynamic range -> FP16
            let dynamic_range = (stats.max_val - stats.min_val) / stats.std_dev;
            if dynamic_range > 100.0 {
                return QuantizationType::Float16;
            }
            
            // High sparsity -> INT4 (more aggressive)
            if stats.sparsity > 0.7 {
                return QuantizationType::Int4;
            }
            
            // Small range -> INT4
            if stats.max_val - stats.min_val < 1.0 {
                return QuantizationType::Int4;
            }
        }
        
        // Default to INT8
        QuantizationType::Int8
    }
    
    /// Prune weights based on magnitude
    pub fn prune_weights(&self, weights: &mut Array2<f32>) -> Result<f32> {
        let threshold = self.compute_pruning_threshold(weights);
        let total_weights = weights.len();
        let mut pruned_count = 0;
        
        weights.mapv_inplace(|val| {
            if val.abs() < threshold {
                pruned_count += 1;
                0.0
            } else {
                val
            }
        });
        
        let pruning_ratio = pruned_count as f32 / total_weights as f32;
        Ok(pruning_ratio)
    }
    
    /// Compute pruning threshold
    fn compute_pruning_threshold(&self, weights: &Array2<f32>) -> f32 {
        let mut magnitudes: Vec<f32> = weights.iter()
            .map(|&val| val.abs())
            .collect();
        
        magnitudes.sort_by(|a, b| a.total_cmp(b));
        
        let cutoff_idx = (magnitudes.len() as f32 * self.config.pruning_threshold) as usize;
        magnitudes[cutoff_idx.min(magnitudes.len() - 1)]
    }
    
    /// Optimize model for specific hardware
    pub fn optimize_for_hardware(&self, layer_name: &str) -> HardwareOptimizationHints {
        let hints = HardwareOptimizationHints::default();
        
        if self.config.optimize_for_hardware {
            // M1-specific optimizations
            if cfg!(target_arch = "aarch64") {
                return HardwareOptimizationHints {
                    preferred_tile_size: 16,  // NEON register width
                    use_amx: true,  // Apple AMX units
                    preferred_data_layout: DataLayout::NHWC,  // Better for ARM
                    ..hints
                };
            }
        }
        
        hints
    }
}

/// Hardware optimization hints
#[derive(Debug, Clone)]
pub struct HardwareOptimizationHints {
    pub preferred_tile_size: usize,
    pub use_amx: bool,
    pub preferred_data_layout: DataLayout,
    pub cache_blocking_size: usize,
}

impl Default for HardwareOptimizationHints {
    fn default() -> Self {
        Self {
            preferred_tile_size: 8,
            use_amx: false,
            preferred_data_layout: DataLayout::NCHW,
            cache_blocking_size: 256,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub enum DataLayout {
    NCHW,  // Batch, Channel, Height, Width
    NHWC,  // Batch, Height, Width, Channel
}

/// Calibration dataset for quantization
pub struct CalibrationDataset {
    samples: Vec<Array2<f32>>,
}

impl CalibrationDataset {
    pub fn new() -> Self {
        Self {
            samples: Vec::new(),
        }
    }
    
    pub fn add_sample(&mut self, sample: Array2<f32>) {
        self.samples.push(sample);
    }
    
    /// Compute optimal quantization parameters
    pub fn compute_quantization_params(&self) -> (f32, i32) {
        if self.samples.is_empty() {
            return (1.0, 0);
        }
        
        let mut all_values = Vec::new();
        for sample in &self.samples {
            all_values.extend(sample.iter().copied());
        }
        
        let min_val = all_values.iter().fold(f32::INFINITY, |a, &b| a.min(b));
        let max_val = all_values.iter().fold(f32::NEG_INFINITY, |a, &b| a.max(b));
        
        // Symmetric quantization for INT8
        let scale = max_val.max(-min_val) / 127.0;
        let zero_point = 0;
        
        (scale, zero_point)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ndarray::arr2;
    
    #[test]
    fn test_weight_pruning() {
        let config = OptimizationConfig {
            pruning_threshold: 0.5,
            ..Default::default()
        };
        
        let optimizer = ModelOptimizer::new(config);
        let mut weights = arr2(&[
            [0.1, 0.001, 0.5],
            [0.0001, 0.8, 0.002]
        ]);
        
        let pruning_ratio = optimizer.prune_weights(&mut weights).unwrap();
        assert!(pruning_ratio > 0.0);
        
        // Check that small values were pruned
        assert_eq!(weights[[1, 0]], 0.0);
    }
    
    #[test]
    fn test_layer_analysis() {
        let config = OptimizationConfig::default();
        let mut optimizer = ModelOptimizer::new(config);
        
        let weights = arr2(&[
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0]
        ]);
        
        optimizer.analyze_layer("test_layer", weights.view()).unwrap();
        
        let stats = optimizer.layer_stats.get("test_layer").unwrap();
        assert_eq!(stats.min_val, 1.0);
        assert_eq!(stats.max_val, 6.0);
    }
}