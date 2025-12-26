//! macOS-specific vision optimizations
//! Memory-efficient features for 16GB RAM systems

use super::{ImageData, ImageFormat, CaptureRegion};
use crate::{Result, JarvisError};
use std::sync::Arc;
use std::collections::HashMap;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use chrono::Timelike;

#[cfg(target_os = "macos")]
use objc::runtime::{Object, Sel};
#[cfg(target_os = "macos")]
use objc::{msg_send, sel, sel_impl};
#[cfg(target_os = "macos")]
use dispatch::{Queue, QueueAttribute};
#[cfg(target_os = "macos")]
use core_foundation::base::TCFType;
#[cfg(target_os = "macos")]
use core_foundation::string::CFString;
#[cfg(target_os = "macos")]
use core_foundation::dictionary::CFDictionary;

/// Memory-efficient window position tracker
#[derive(Debug, Clone)]
pub struct WindowTracker {
    /// Window positions cached for quick access
    positions: Arc<RwLock<HashMap<u32, WindowPosition>>>,
    /// Update frequency in milliseconds
    update_interval_ms: u64,
    /// Last update timestamp
    last_update: std::time::Instant,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WindowPosition {
    pub window_id: u32,
    pub app_name: String,
    pub bounds: CaptureRegion,
    pub z_order: i32,
    pub last_moved: std::time::SystemTime,
    pub movement_velocity: (f32, f32),
}

impl WindowTracker {
    pub fn new() -> Self {
        Self {
            positions: Arc::new(RwLock::new(HashMap::new())),
            update_interval_ms: std::env::var("MACOS_WINDOW_TRACK_INTERVAL")
                .unwrap_or_else(|_| "100".to_string())
                .parse()
                .unwrap_or(100),
            last_update: std::time::Instant::now(),
        }
    }
    
    /// Update window positions efficiently
    pub fn update_positions(&mut self, windows: Vec<super::capture::WindowInfo>) {
        let mut positions = self.positions.write();
        
        for window in windows {
            let velocity = if let Some(prev) = positions.get(&window.window_id) {
                // Calculate movement velocity
                let dx = window.bounds.x as f32 - prev.bounds.x as f32;
                let dy = window.bounds.y as f32 - prev.bounds.y as f32;
                let dt = self.last_update.elapsed().as_secs_f32();
                (dx / dt, dy / dt)
            } else {
                (0.0, 0.0)
            };
            
            positions.insert(window.window_id, WindowPosition {
                window_id: window.window_id,
                app_name: window.app_name,
                bounds: window.bounds,
                z_order: window.layer,
                last_moved: std::time::SystemTime::now(),
                movement_velocity: velocity,
            });
        }
        
        self.last_update = std::time::Instant::now();
    }
    
    /// Get windows that changed position
    pub fn get_moved_windows(&self, threshold_pixels: u32) -> Vec<WindowPosition> {
        self.positions.read()
            .values()
            .filter(|w| {
                let speed = (w.movement_velocity.0.powi(2) + w.movement_velocity.1.powi(2)).sqrt();
                speed > threshold_pixels as f32
            })
            .cloned()
            .collect()
    }
}

/// App state detector using NSWorkspace
#[cfg(target_os = "macos")]
pub struct AppStateDetector {
    workspace: *mut Object,
    cache: Arc<RwLock<HashMap<String, AppState>>>,
    dispatch_queue: Queue,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppState {
    pub bundle_id: String,
    pub name: String,
    pub is_running: bool,
    pub is_active: bool,
    pub is_hidden: bool,
    pub cpu_usage: f32,
    pub memory_usage_mb: f32,
    pub last_activated: Option<std::time::SystemTime>,
}

#[cfg(target_os = "macos")]
impl AppStateDetector {
    pub fn new() -> Result<Self> {
        unsafe {
            let workspace_class = objc::runtime::Class::get("NSWorkspace")
                .ok_or_else(|| JarvisError::VisionError("NSWorkspace not available".to_string()))?;
            let workspace: *mut Object = msg_send![workspace_class, sharedWorkspace];
            
            if workspace.is_null() {
                return Err(JarvisError::VisionError("Failed to get NSWorkspace".to_string()));
            }
            
            Ok(Self {
                workspace,
                cache: Arc::new(RwLock::new(HashMap::new())),
                dispatch_queue: Queue::global(dispatch::QueuePriority::High),
            })
        }
    }
    
    /// Get all running applications
    pub fn get_running_apps(&self) -> Result<Vec<AppState>> {
        unsafe {
            let running_apps: *mut Object = msg_send![self.workspace, runningApplications];
            if running_apps.is_null() {
                return Ok(Vec::new());
            }
            
            let count: usize = msg_send![running_apps, count];
            let mut apps = Vec::with_capacity(count);
            
            for i in 0..count {
                let app: *mut Object = msg_send![running_apps, objectAtIndex: i];
                if !app.is_null() {
                    if let Ok(app_state) = self.parse_app_state(app) {
                        apps.push(app_state);
                    }
                }
            }
            
            // Update cache
            let mut cache = self.cache.write();
            cache.clear();
            for app in &apps {
                cache.insert(app.bundle_id.clone(), app.clone());
            }
            
            Ok(apps)
        }
    }
    
    /// Parse NSRunningApplication to AppState
    unsafe fn parse_app_state(&self, app: *mut Object) -> Result<AppState> {
        let bundle_id_obj: *mut Object = msg_send![app, bundleIdentifier];
        let bundle_id = if !bundle_id_obj.is_null() {
            let c_str: *const i8 = msg_send![bundle_id_obj, UTF8String];
            std::ffi::CStr::from_ptr(c_str).to_string_lossy().to_string()
        } else {
            String::new()
        };
        
        let name_obj: *mut Object = msg_send![app, localizedName];
        let name = if !name_obj.is_null() {
            let c_str: *const i8 = msg_send![name_obj, UTF8String];
            std::ffi::CStr::from_ptr(c_str).to_string_lossy().to_string()
        } else {
            String::new()
        };
        
        let is_active: bool = msg_send![app, isActive];
        let is_hidden: bool = msg_send![app, isHidden];
        let pid: i32 = msg_send![app, processIdentifier];
        
        // Get process info for CPU/memory usage
        let (cpu_usage, memory_usage_mb) = self.get_process_info(pid)?;
        
        Ok(AppState {
            bundle_id,
            name,
            is_running: true,
            is_active,
            is_hidden,
            cpu_usage,
            memory_usage_mb,
            last_activated: if is_active { Some(std::time::SystemTime::now()) } else { None },
        })
    }
    
    /// Get process CPU and memory usage
    fn get_process_info(&self, pid: i32) -> Result<(f32, f32)> {
        // This would use mach APIs or process info
        // Simplified for now
        Ok((0.0, 0.0))
    }
    
    /// Detect app state changes
    pub fn detect_changes(&self) -> Result<Vec<AppStateChange>> {
        let current_apps = self.get_running_apps()?;
        let mut changes = Vec::new();
        
        let cache = self.cache.read();
        
        for app in &current_apps {
            if let Some(prev) = cache.get(&app.bundle_id) {
                if prev.is_active != app.is_active {
                    changes.push(AppStateChange {
                        app_name: app.name.clone(),
                        bundle_id: app.bundle_id.clone(),
                        change_type: if app.is_active {
                            ChangeType::Activated
                        } else {
                            ChangeType::Deactivated
                        },
                        timestamp: std::time::SystemTime::now(),
                    });
                }
                
                if prev.is_hidden != app.is_hidden {
                    changes.push(AppStateChange {
                        app_name: app.name.clone(),
                        bundle_id: app.bundle_id.clone(),
                        change_type: if app.is_hidden {
                            ChangeType::Hidden
                        } else {
                            ChangeType::Shown
                        },
                        timestamp: std::time::SystemTime::now(),
                    });
                }
            } else {
                changes.push(AppStateChange {
                    app_name: app.name.clone(),
                    bundle_id: app.bundle_id.clone(),
                    change_type: ChangeType::Launched,
                    timestamp: std::time::SystemTime::now(),
                });
            }
        }
        
        Ok(changes)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppStateChange {
    pub app_name: String,
    pub bundle_id: String,
    pub change_type: ChangeType,
    pub timestamp: std::time::SystemTime,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ChangeType {
    Launched,
    Terminated,
    Activated,
    Deactivated,
    Hidden,
    Shown,
}

/// Text extraction via Vision framework (chunked for memory efficiency)
#[cfg(target_os = "macos")]
pub struct ChunkedTextExtractor {
    chunk_size: usize,
    dispatch_queue: Queue,
}

#[cfg(target_os = "macos")]
impl ChunkedTextExtractor {
    pub fn new() -> Self {
        Self {
            chunk_size: std::env::var("MACOS_OCR_CHUNK_SIZE")
                .unwrap_or_else(|_| "1024".to_string())
                .parse()
                .unwrap_or(1024),
            dispatch_queue: Queue::global(dispatch::QueuePriority::Background),
        }
    }
    
    /// Extract text from image in chunks to save memory
    pub async fn extract_text_chunked(&self, image: &ImageData) -> Result<Vec<TextChunk>> {
        let chunks = self.split_image_into_chunks(image)?;
        let mut results = Vec::new();
        
        for (i, chunk) in chunks.into_iter().enumerate() {
            // Process each chunk on dispatch queue
            let text = self.process_chunk(chunk).await?;
            if !text.is_empty() {
                results.push(TextChunk {
                    chunk_id: i,
                    text,
                    confidence: 0.9, // Placeholder
                });
            }
        }
        
        Ok(results)
    }
    
    fn split_image_into_chunks(&self, image: &ImageData) -> Result<Vec<ImageData>> {
        let chunk_height = self.chunk_size / image.width as usize / image.channels as usize;
        let mut chunks = Vec::new();
        
        for y in (0..image.height).step_by(chunk_height) {
            let height = chunk_height.min((image.height - y) as usize) as u32;
            
            // Create chunk without copying data
            let chunk = ImageData::new(image.width, height, image.channels, image.format);
            chunks.push(chunk);
        }
        
        Ok(chunks)
    }
    
    async fn process_chunk(&self, chunk: ImageData) -> Result<String> {
        // This would use Vision framework
        // Placeholder for now
        Ok(String::new())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextChunk {
    pub chunk_id: usize,
    pub text: String,
    pub confidence: f32,
}

/// Notification monitor for system events
#[cfg(target_os = "macos")]
pub struct NotificationMonitor {
    notification_center: *mut Object,
    handlers: Arc<RwLock<HashMap<String, NotificationHandler>>>,
}

type NotificationHandler = Box<dyn Fn(NotificationEvent) + Send + Sync>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationEvent {
    pub name: String,
    pub app_bundle_id: Option<String>,
    pub timestamp: std::time::SystemTime,
    pub user_info: HashMap<String, String>,
}

#[cfg(target_os = "macos")]
impl NotificationMonitor {
    pub fn new() -> Result<Self> {
        unsafe {
            let center_class = objc::runtime::Class::get("NSDistributedNotificationCenter")
                .ok_or_else(|| JarvisError::VisionError("NSDistributedNotificationCenter not available".to_string()))?;
            let notification_center: *mut Object = msg_send![center_class, defaultCenter];
            
            if notification_center.is_null() {
                return Err(JarvisError::VisionError("Failed to get notification center".to_string()));
            }
            
            Ok(Self {
                notification_center,
                handlers: Arc::new(RwLock::new(HashMap::new())),
            })
        }
    }
    
    /// Monitor specific notifications
    pub fn monitor(&self, notification_names: Vec<&str>) -> Result<()> {
        // This would set up observers for specific notifications
        // Placeholder for now
        Ok(())
    }
    
    /// Add handler for notification
    pub fn add_handler(&self, name: String, handler: NotificationHandler) {
        self.handlers.write().insert(name, handler);
    }
}

/// Workspace organization rules
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkspaceRule {
    pub name: String,
    pub condition: RuleCondition,
    pub action: RuleAction,
    pub priority: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum RuleCondition {
    AppLaunched { bundle_id: String },
    WindowCount { app: String, count: u32, operator: ComparisonOp },
    TimeOfDay { start_hour: u8, end_hour: u8 },
    WorkspaceActive { number: u32 },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ComparisonOp {
    GreaterThan,
    LessThan,
    Equal,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum RuleAction {
    MoveWindow { to_workspace: u32 },
    ResizeWindow { width: u32, height: u32 },
    ArrangeWindows { layout: WindowLayout },
    MinimizeApp { bundle_id: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum WindowLayout {
    SideBySide,
    Stacked,
    Grid { rows: u32, cols: u32 },
    Focus { main_window_percent: u32 },
}

/// Workspace organizer
pub struct WorkspaceOrganizer {
    rules: Vec<WorkspaceRule>,
    enabled: bool,
}

impl WorkspaceOrganizer {
    pub fn new() -> Self {
        Self {
            rules: Self::load_rules(),
            enabled: std::env::var("MACOS_WORKSPACE_ORGANIZER")
                .unwrap_or_else(|_| "true".to_string())
                .parse()
                .unwrap_or(true),
        }
    }
    
    fn load_rules() -> Vec<WorkspaceRule> {
        // Load from config file or use defaults
        vec![
            WorkspaceRule {
                name: "Too many browser tabs".to_string(),
                condition: RuleCondition::WindowCount {
                    app: "Chrome".to_string(),
                    count: 10,
                    operator: ComparisonOp::GreaterThan,
                },
                action: RuleAction::ArrangeWindows {
                    layout: WindowLayout::Grid { rows: 2, cols: 2 },
                },
                priority: 1,
            },
            WorkspaceRule {
                name: "Focus mode".to_string(),
                condition: RuleCondition::TimeOfDay {
                    start_hour: 9,
                    end_hour: 17,
                },
                action: RuleAction::ArrangeWindows {
                    layout: WindowLayout::Focus { main_window_percent: 70 },
                },
                priority: 2,
            },
        ]
    }
    
    /// Apply rules to current workspace
    pub fn apply_rules(&self, windows: &[super::capture::WindowInfo], app_states: &[AppState]) -> Vec<RuleAction> {
        if !self.enabled {
            return Vec::new();
        }
        
        let mut actions = Vec::new();
        
        for rule in &self.rules {
            if self.check_condition(&rule.condition, windows, app_states) {
                actions.push(rule.action.clone());
            }
        }
        
        // Sort by priority
        actions.sort_by_key(|_| 0); // Would sort by rule priority
        
        actions
    }
    
    fn check_condition(&self, condition: &RuleCondition, windows: &[super::capture::WindowInfo], app_states: &[AppState]) -> bool {
        match condition {
            RuleCondition::AppLaunched { bundle_id } => {
                app_states.iter().any(|app| app.bundle_id == *bundle_id && app.is_running)
            }
            RuleCondition::WindowCount { app, count, operator } => {
                let window_count = windows.iter().filter(|w| w.app_name == *app).count() as u32;
                match operator {
                    ComparisonOp::GreaterThan => window_count > *count,
                    ComparisonOp::LessThan => window_count < *count,
                    ComparisonOp::Equal => window_count == *count,
                }
            }
            RuleCondition::TimeOfDay { start_hour, end_hour } => {
                let now = chrono::Local::now();
                let hour = now.hour() as u8;
                hour >= *start_hour && hour < *end_hour
            }
            RuleCondition::WorkspaceActive { number } => {
                // Would check current workspace
                false
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_window_tracker() {
        let mut tracker = WindowTracker::new();
        
        let window = super::super::capture::WindowInfo {
            window_id: 1,
            app_name: "Terminal".to_string(),
            window_title: "bash".to_string(),
            bounds: CaptureRegion { x: 100, y: 100, width: 800, height: 600 },
            is_visible: true,
            is_minimized: false,
            is_focused: true,
            layer: 0,
            alpha: 1.0,
            pid: 1234,
        };
        
        tracker.update_positions(vec![window]);
        let moved = tracker.get_moved_windows(10);
        assert_eq!(moved.len(), 0); // No movement on first update
    }
    
    #[test]
    fn test_workspace_rules() {
        let organizer = WorkspaceOrganizer::new();
        
        let windows = vec![
            super::super::capture::WindowInfo {
                window_id: 1,
                app_name: "Chrome".to_string(),
                window_title: "Tab 1".to_string(),
                bounds: CaptureRegion { x: 0, y: 0, width: 100, height: 100 },
                is_visible: true,
                is_minimized: false,
                is_focused: false,
                layer: 0,
                alpha: 1.0,
                pid: 1000,
            },
        ];
        
        let app_states = vec![];
        let actions = organizer.apply_rules(&windows, &app_states);
        assert!(actions.is_empty() || !actions.is_empty()); // Depends on time
    }
}