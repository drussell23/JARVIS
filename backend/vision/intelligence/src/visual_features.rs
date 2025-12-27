use pyo3::prelude::*;
use pyo3::types::PyDict;
use image::{GrayImage, RgbImage};
use imageproc::edges::canny;
use std::collections::HashMap;
use rayon::prelude::*;

#[pyclass]
pub struct ColorHistogram {
    bins: usize,
    normalized: bool,
}

#[pymethods]
impl ColorHistogram {
    #[new]
    fn new(bins: Option<usize>) -> Self {
        Self {
            bins: bins.unwrap_or(256),
            normalized: true,
        }
    }

    fn compute(&self, image_data: Vec<u8>, width: u32, height: u32) -> PyResult<Vec<f32>> {
        let image = RgbImage::from_raw(width, height, image_data)
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("Invalid image data"))?;

        let mut r_hist = vec![0u32; self.bins];
        let mut g_hist = vec![0u32; self.bins];
        let mut b_hist = vec![0u32; self.bins];

        let scale = 256 / self.bins;

        for pixel in image.pixels() {
            r_hist[(pixel[0] as usize) / scale] += 1;
            g_hist[(pixel[1] as usize) / scale] += 1;
            b_hist[(pixel[2] as usize) / scale] += 1;
        }

        let total_pixels = (width * height) as f32;
        let mut histogram = Vec::with_capacity(self.bins * 3);

        if self.normalized {
            histogram.extend(r_hist.iter().map(|&c| c as f32 / total_pixels));
            histogram.extend(g_hist.iter().map(|&c| c as f32 / total_pixels));
            histogram.extend(b_hist.iter().map(|&c| c as f32 / total_pixels));
        } else {
            histogram.extend(r_hist.iter().map(|&c| c as f32));
            histogram.extend(g_hist.iter().map(|&c| c as f32));
            histogram.extend(b_hist.iter().map(|&c| c as f32));
        }

        Ok(histogram)
    }
}

#[pyclass]
pub struct StructuralFeatures {
    edge_threshold_low: f32,
    edge_threshold_high: f32,
}

#[pymethods]
impl StructuralFeatures {
    #[new]
    fn new() -> Self {
        Self {
            edge_threshold_low: 50.0,
            edge_threshold_high: 100.0,
        }
    }

    fn compute_edge_density(&self, image_data: Vec<u8>, width: u32, height: u32) -> PyResult<f32> {
        let image = GrayImage::from_raw(width, height, image_data)
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("Invalid image data"))?;

        let edges = canny(&image, self.edge_threshold_low, self.edge_threshold_high);
        
        let edge_count = edges.pixels().filter(|&p| p.0[0] > 0).count();
        let total_pixels = (width * height) as f32;

        Ok(edge_count as f32 / total_pixels)
    }

    fn compute_corner_features(&self, image_data: Vec<u8>, width: u32, height: u32) -> PyResult<Vec<f32>> {
        // Simplified corner detection using gradients
        let image = GrayImage::from_raw(width, height, image_data)
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("Invalid image data"))?;

        let mut corner_strength = vec![0.0f32; 9]; // 3x3 grid
        let grid_w = width / 3;
        let grid_h = height / 3;

        for (x, y, _pixel) in image.enumerate_pixels() {
            let grid_x = (x / grid_w).min(2) as usize;
            let grid_y = (y / grid_h).min(2) as usize;
            let idx = grid_y * 3 + grid_x;
            
            // Simple gradient-based corner metric
            if x > 0 && x < width - 1 && y > 0 && y < height - 1 {
                let dx = image.get_pixel(x + 1, y).0[0] as f32 - 
                        image.get_pixel(x - 1, y).0[0] as f32;
                let dy = image.get_pixel(x, y + 1).0[0] as f32 - 
                        image.get_pixel(x, y - 1).0[0] as f32;
                
                corner_strength[idx] += (dx * dx + dy * dy).sqrt();
            }
        }

        // Normalize
        let max_strength = corner_strength.iter().cloned().fold(0.0f32, f32::max);
        if max_strength > 0.0 {
            for s in &mut corner_strength {
                *s /= max_strength;
            }
        }

        Ok(corner_strength)
    }
}

#[pyclass]
pub struct FeatureExtractor {
    color_histogram: ColorHistogram,
    structural_features: StructuralFeatures,
    parallel: bool,
}

#[pymethods]
impl FeatureExtractor {
    #[new]
    fn new() -> Self {
        Self {
            color_histogram: ColorHistogram::new(Some(64)),
            structural_features: StructuralFeatures::new(),
            parallel: true,
        }
    }

    fn extract_all_features(&self, image_data: Vec<u8>, width: u32, height: u32, py: Python) -> PyResult<PyObject> {
        let dict = PyDict::new(py);

        // Extract color features
        let color_hist = self.color_histogram.compute(image_data.clone(), width, height)?;
        dict.set_item("color_histogram", color_hist)?;

        // Convert to grayscale for structural features
        let gray_data = rgb_to_grayscale(&image_data, width, height);

        // Extract edge features
        let edge_density = self.structural_features.compute_edge_density(gray_data.clone(), width, height)?;
        dict.set_item("edge_density", edge_density)?;

        // Extract corner features
        let corner_features = self.structural_features.compute_corner_features(gray_data, width, height)?;
        dict.set_item("corner_features", corner_features)?;

        // Compute additional statistics
        let stats = compute_image_statistics(&image_data, width, height);
        dict.set_item("statistics", stats)?;

        Ok(dict.into())
    }

    fn extract_region_features(&self, 
                              image_data: Vec<u8>, 
                              width: u32, 
                              height: u32,
                              regions: Vec<(u32, u32, u32, u32)>, // (x, y, w, h)
                              py: Python) -> PyResult<PyObject> {
        let results = PyDict::new(py);

        if self.parallel {
            let region_features: Vec<_> = regions.par_iter()
                .map(|&(x, y, w, h)| {
                    extract_region(&image_data, width, height, x, y, w, h)
                        .and_then(|region_data| {
                            self.color_histogram.compute(region_data, w, h)
                        })
                })
                .collect();

            for (i, features) in region_features.into_iter().enumerate() {
                if let Ok(feat) = features {
                    results.set_item(format!("region_{}", i), feat)?;
                }
            }
        } else {
            for (i, &(x, y, w, h)) in regions.iter().enumerate() {
                if let Ok(region_data) = extract_region(&image_data, width, height, x, y, w, h) {
                    if let Ok(features) = self.color_histogram.compute(region_data, w, h) {
                        results.set_item(format!("region_{}", i), features)?;
                    }
                }
            }
        }

        Ok(results.into())
    }

    fn set_parallel(&mut self, parallel: bool) {
        self.parallel = parallel;
    }
}

// Helper functions
fn rgb_to_grayscale(rgb_data: &[u8], width: u32, height: u32) -> Vec<u8> {
    let mut gray_data = Vec::with_capacity((width * height) as usize);
    
    for i in (0..rgb_data.len()).step_by(3) {
        let r = rgb_data[i] as f32;
        let g = rgb_data[i + 1] as f32;
        let b = rgb_data[i + 2] as f32;
        let gray = (0.299 * r + 0.587 * g + 0.114 * b) as u8;
        gray_data.push(gray);
    }
    
    gray_data
}

fn extract_region(image_data: &[u8], img_width: u32, img_height: u32, 
                 x: u32, y: u32, width: u32, height: u32) -> PyResult<Vec<u8>> {
    if x + width > img_width || y + height > img_height {
        return Err(pyo3::exceptions::PyValueError::new_err("Region out of bounds"));
    }

    let mut region_data = Vec::with_capacity((width * height * 3) as usize);
    
    for row in y..(y + height) {
        let start = ((row * img_width + x) * 3) as usize;
        let end = start + (width * 3) as usize;
        region_data.extend_from_slice(&image_data[start..end]);
    }
    
    Ok(region_data)
}

fn compute_image_statistics(image_data: &[u8], width: u32, height: u32) -> HashMap<String, f32> {
    let mut stats = HashMap::new();
    
    let pixels = (width * height) as f32;
    let mut r_sum = 0.0;
    let mut g_sum = 0.0;
    let mut b_sum = 0.0;
    
    for i in (0..image_data.len()).step_by(3) {
        r_sum += image_data[i] as f32;
        g_sum += image_data[i + 1] as f32;
        b_sum += image_data[i + 2] as f32;
    }
    
    stats.insert("mean_r".to_string(), r_sum / pixels);
    stats.insert("mean_g".to_string(), g_sum / pixels);
    stats.insert("mean_b".to_string(), b_sum / pixels);
    
    // Compute brightness
    let brightness = (r_sum + g_sum + b_sum) / (pixels * 3.0);
    stats.insert("brightness".to_string(), brightness);
    
    // Compute contrast (simplified)
    let mut min_val: f32 = 255.0;
    let mut max_val: f32 = 0.0;
    
    for chunk in image_data.chunks(3) {
        let gray = 0.299 * chunk[0] as f32 + 0.587 * chunk[1] as f32 + 0.114 * chunk[2] as f32;
        min_val = min_val.min(gray);
        max_val = max_val.max(gray);
    }
    
    stats.insert("contrast".to_string(), (max_val - min_val) / 255.0);
    
    stats
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_color_histogram() {
        let hist = ColorHistogram::new(Some(4));
        let image_data = vec![255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]; // RGBW pixels
        let result = hist.compute(image_data, 2, 2).unwrap();
        
        assert_eq!(result.len(), 12); // 4 bins * 3 channels
    }

    #[test]
    fn test_feature_extractor() {
        let extractor = FeatureExtractor::new();
        let image_data = vec![128; 300]; // 10x10 gray image
        
        Python::with_gil(|py| {
            let features = extractor.extract_all_features(image_data, 10, 10, py).unwrap();
            assert!(!features.is_none());
        });
    }
}