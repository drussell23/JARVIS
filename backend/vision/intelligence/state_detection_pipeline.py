"""
State Detection Pipeline v2.0 - PROACTIVE & AUTOMATED STATE DETECTION
========================================================================

Advanced state detection with ML-powered automation and proactive monitoring.

**UPGRADED v2.0 Features**:
✅ Auto-triggered detection from HybridProactiveMonitoringManager alerts
✅ Automatic visual signature library building from monitored captures
✅ Real-time state transition detection across all monitored spaces
✅ Proactive state classification without manual queries
✅ Context-aware state queries via ImplicitReferenceResolver
✅ Async signature learning and matching
✅ Multi-strategy ensemble detection with confidence voting
✅ Visual fingerprinting for state identification

**Integration**:
- HybridProactiveMonitoringManager: Auto-triggers detection on monitoring alerts
- ImplicitReferenceResolver: Natural language state queries ("is this the error screen?")
- ChangeDetectionManager: Detects visual state changes automatically

**Proactive Capabilities**:
- Detects state transitions in real-time without queries
- Learns visual signatures automatically from monitoring
- Builds state library without manual labeling
- Identifies unknown states and alerts user
- Tracks state transition patterns

Example:
"Sir, I detected a state transition in Space 3: 'coding' → 'error_state'
 (confidence: 95%). This is a new error state not seen before."
"""

import asyncio
import cv2
import numpy as np
from typing import Dict, List, Optional, Tuple, Any, Set, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from collections import defaultdict, deque, Counter
from sklearn.cluster import MiniBatchKMeans
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class VisualSignature:
    """Visual signature for state identification (v2.0 Enhanced)"""
    signature_type: str  # layout, color, text, icon, etc.
    features: Dict[str, Any]
    confidence_weight: float = 1.0
    timestamp: datetime = None

    # NEW v2.0: Proactive tracking fields
    state_id: Optional[str] = None              # Identified state name
    space_id: Optional[int] = None              # Space where signature was captured
    auto_learned: bool = False                  # True if learned from monitoring
    match_count: int = 0                        # Number of times matched
    last_matched: Optional[datetime] = None     # Last time this signature matched
    visual_fingerprint: Optional[str] = None    # MD5 hash of key visual features

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

        # Generate visual fingerprint if not provided
        if self.visual_fingerprint is None:
            self.visual_fingerprint = self._generate_fingerprint()

    def _generate_fingerprint(self) -> str:
        """Generate unique fingerprint for this signature (NEW v2.0)"""
        # Create a string from key features
        feature_str = json.dumps(self.features, sort_keys=True)
        return hashlib.md5(feature_str.encode()).hexdigest()[:16]
    
    def match(self, other_features: Dict[str, Any]) -> float:
        """Calculate match score against other features"""
        if self.signature_type == "layout":
            return self._match_layout(other_features)
        elif self.signature_type == "color":
            return self._match_color(other_features)
        elif self.signature_type == "text":
            return self._match_text(other_features)
        elif self.signature_type == "element":
            return self._match_elements(other_features)
        else:
            return 0.0
    
    def _match_layout(self, features: Dict[str, Any]) -> float:
        """Match layout signatures"""
        if 'layout_hash' not in features or 'layout_hash' not in self.features:
            return 0.0
        
        # Simplified hash comparison
        if features['layout_hash'] == self.features['layout_hash']:
            return 1.0
        
        # Fuzzy matching based on element positions
        if 'element_positions' in features and 'element_positions' in self.features:
            return self._calculate_layout_similarity(
                features['element_positions'], 
                self.features['element_positions']
            )
        
        return 0.0
    
    def _match_color(self, features: Dict[str, Any]) -> float:
        """Match color signatures"""
        if 'dominant_colors' not in features or 'dominant_colors' not in self.features:
            return 0.0
        
        # Color histogram comparison
        return self._compare_color_histograms(
            features['dominant_colors'],
            self.features['dominant_colors']
        )
    
    def _match_text(self, features: Dict[str, Any]) -> float:
        """Match text content signatures"""
        if 'text_elements' not in features or 'text_elements' not in self.features:
            return 0.0
        
        # Text similarity
        return self._calculate_text_similarity(
            features['text_elements'],
            self.features['text_elements']
        )
    
    def _match_elements(self, features: Dict[str, Any]) -> float:
        """Match UI element signatures"""
        if 'ui_elements' not in features or 'ui_elements' not in self.features:
            return 0.0
        
        # Element type and count matching
        return self._compare_ui_elements(
            features['ui_elements'],
            self.features['ui_elements']
        )
    
    def _calculate_layout_similarity(self, pos1: List, pos2: List) -> float:
        """Calculate similarity between layout positions"""
        # Simplified - in production use proper spatial similarity
        common = len(set(map(tuple, pos1)) & set(map(tuple, pos2)))
        total = max(len(pos1), len(pos2))
        return common / total if total > 0 else 0.0
    
    def _compare_color_histograms(self, colors1: List, colors2: List) -> float:
        """Compare color distributions"""
        # Simplified color comparison
        if not colors1 or not colors2:
            return 0.0
        
        # Convert to numpy arrays for easier comparison
        c1 = np.array(colors1[:5])  # Top 5 colors
        c2 = np.array(colors2[:5])
        
        # Calculate color distance
        distances = []
        for color1 in c1:
            min_dist = min(np.linalg.norm(color1 - color2) for color2 in c2)
            distances.append(min_dist)
        
        # Normalize to 0-1 (max RGB distance is ~441)
        avg_distance = np.mean(distances)
        similarity = 1.0 - (avg_distance / 441.0)
        
        return max(0.0, similarity)
    
    def _calculate_text_similarity(self, text1: List[str], text2: List[str]) -> float:
        """Calculate text content similarity"""
        if not text1 or not text2:
            return 0.0
        
        # Simple Jaccard similarity
        set1 = set(text1)
        set2 = set(text2)
        
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        
        return intersection / union if union > 0 else 0.0
    
    def _compare_ui_elements(self, elements1: Dict, elements2: Dict) -> float:
        """Compare UI element compositions"""
        if not elements1 or not elements2:
            return 0.0
        
        # Compare element types and counts
        all_types = set(elements1.keys()) | set(elements2.keys())
        
        similarity_sum = 0.0
        for elem_type in all_types:
            count1 = elements1.get(elem_type, 0)
            count2 = elements2.get(elem_type, 0)
            
            if count1 == count2:
                similarity_sum += 1.0
            else:
                # Partial credit for similar counts
                similarity_sum += 1.0 - abs(count1 - count2) / max(count1, count2, 1)
        
        return similarity_sum / len(all_types) if all_types else 0.0


class StateDetectionPipeline:
    """
    Advanced State Detection Pipeline v2.0 with Proactive Monitoring.

    **NEW v2.0 Features**:
    - Auto-triggered detection from monitoring alerts
    - Automatic signature learning
    - Real-time state transition tracking
    - Visual signature library building
    """

    def __init__(
        self,
        hybrid_monitoring_manager=None,
        implicit_resolver=None,
        change_detection_manager=None,
        state_transition_callback: Optional[Callable] = None,
        new_state_callback: Optional[Callable] = None
    ):
        """
        Initialize StateDetectionPipeline v2.0.

        Args:
            hybrid_monitoring_manager: HybridProactiveMonitoringManager for auto-detection
            implicit_resolver: ImplicitReferenceResolver for natural language queries
            change_detection_manager: ChangeDetectionManager for state change detection
            state_transition_callback: Async callback for state transitions
            new_state_callback: Async callback for newly discovered states
        """
        self.color_clusterer = MiniBatchKMeans(n_clusters=5, random_state=42)
        self.detection_strategies = [
            self.detect_by_layout,
            self.detect_by_color_pattern,
            self.detect_by_text_content,
            self.detect_by_ui_elements,
            self.detect_by_modal_overlay
        ]
        self.confidence_weights = {
            'layout': 0.3,
            'color': 0.2,
            'text': 0.3,
            'elements': 0.15,
            'modal': 0.05
        }

        # NEW v2.0: Manager integrations
        self.hybrid_monitoring = hybrid_monitoring_manager
        self.implicit_resolver = implicit_resolver
        self.change_detection = change_detection_manager
        self.state_transition_callback = state_transition_callback
        self.new_state_callback = new_state_callback

        # NEW v2.0: Proactive tracking
        self.is_proactive_enabled = hybrid_monitoring_manager is not None
        self.signature_library: List[VisualSignature] = []  # All learned signatures
        self.signature_index: Dict[str, List[VisualSignature]] = defaultdict(list)  # By state_id
        self.current_space_states: Dict[int, str] = {}  # space_id -> current state_id
        self.state_transitions: deque[Dict[str, Any]] = deque(maxlen=200)  # Transition history
        self.unknown_states: deque[Dict[str, Any]] = deque(maxlen=50)  # Unidentified states
        self.detection_stats: Dict[str, int] = defaultdict(int)  # Detection statistics

        # NEW v2.0: Async monitoring
        self._monitoring_active = False
        self._detection_task: Optional[asyncio.Task] = None

        # Load saved signature library
        self._load_signature_library()

        if self.is_proactive_enabled:
            logger.info("[STATE-DETECTION] ✅ v2.0 Initialized with Proactive Monitoring!")
        else:
            logger.info("[STATE-DETECTION] Initialized (manual mode)")
    
    async def extract_state_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract comprehensive features for state detection"""
        features = {}
        
        # Layout features
        layout_features = self._extract_layout_features(screenshot)
        features.update(layout_features)
        
        # Color features
        color_features = self._extract_color_features(screenshot)
        features.update(color_features)
        
        # Text detection features (mock for now)
        text_features = self._extract_text_features(screenshot)
        features.update(text_features)
        
        # UI element features
        element_features = self._extract_ui_element_features(screenshot)
        features.update(element_features)
        
        # Modal/overlay detection
        modal_features = self._detect_modal_features(screenshot)
        features.update(modal_features)
        
        # Additional metadata
        features['timestamp'] = datetime.now()
        features['resolution'] = screenshot.shape[:2]
        
        return features
    
    def _extract_layout_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract layout-based features"""
        # Convert to grayscale for edge detection
        gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
        
        # Edge detection
        edges = cv2.Canny(gray, 50, 150)
        
        # Find contours (representing UI elements)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Extract bounding boxes
        bboxes = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > 100:  # Filter small contours
                x, y, w, h = cv2.boundingRect(contour)
                bboxes.append((x, y, w, h))
        
        # Create layout hash
        layout_hash = self._create_layout_hash(bboxes)
        
        # Grid-based layout analysis
        grid_features = self._analyze_grid_layout(screenshot.shape[:2], bboxes)
        
        return {
            'layout_hash': layout_hash,
            'element_positions': bboxes[:50],  # Limit to 50 elements
            'element_count': len(bboxes),
            'grid_layout': grid_features,
            'layout_complexity': self._calculate_layout_complexity(bboxes)
        }
    
    def _extract_color_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract color-based features"""
        # Resize for faster processing
        small = cv2.resize(screenshot, (100, 100))
        
        # Reshape to list of pixels
        pixels = small.reshape(-1, 3)
        
        # Cluster colors
        self.color_clusterer.partial_fit(pixels)
        dominant_colors = self.color_clusterer.cluster_centers_.astype(int)
        
        # Color histogram
        hist_b = cv2.calcHist([screenshot], [0], None, [32], [0, 256])
        hist_g = cv2.calcHist([screenshot], [1], None, [32], [0, 256])
        hist_r = cv2.calcHist([screenshot], [2], None, [32], [0, 256])
        
        # Normalize histograms
        hist_b = hist_b.flatten() / hist_b.sum()
        hist_g = hist_g.flatten() / hist_g.sum()
        hist_r = hist_r.flatten() / hist_r.sum()
        
        return {
            'dominant_colors': dominant_colors.tolist(),
            'color_histogram': {
                'blue': hist_b.tolist(),
                'green': hist_g.tolist(),
                'red': hist_r.tolist()
            },
            'color_variance': np.std(pixels, axis=0).tolist(),
            'brightness': np.mean(cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY))
        }
    
    def _extract_text_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract text-based features (mock implementation)"""
        # In production, use OCR (Tesseract, EasyOCR, or Vision API)
        # For now, return mock data
        return {
            'text_elements': ['File', 'Edit', 'View', 'Window', 'Help'],  # Mock menu items
            'text_density': 0.15,  # Percentage of image with text
            'has_error_text': False,
            'has_loading_text': False
        }
    
    def _extract_ui_element_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract UI element features"""
        # Detect common UI elements using template matching or ML
        ui_elements = defaultdict(int)
        
        # Simple heuristic-based detection
        gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
        
        # Detect buttons (rectangular regions with uniform color)
        ui_elements['buttons'] = self._detect_buttons(gray)
        
        # Detect text fields (white/light rectangles)
        ui_elements['text_fields'] = self._detect_text_fields(screenshot)
        
        # Detect checkboxes/radio buttons (small squares/circles)
        ui_elements['checkboxes'] = self._detect_checkboxes(gray)
        
        # Detect scrollbars
        ui_elements['scrollbars'] = self._detect_scrollbars(screenshot)
        
        return {
            'ui_elements': dict(ui_elements),
            'interaction_density': sum(ui_elements.values()) / (screenshot.shape[0] * screenshot.shape[1]) * 10000
        }
    
    def _detect_modal_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Detect modal/overlay features"""
        # Check for darkened background (common modal pattern)
        gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
        
        # Calculate histogram
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
        
        # Check for bimodal distribution (dark background + bright modal)
        hist_normalized = hist.flatten() / hist.sum()
        
        # Find peaks
        peaks = []
        for i in range(1, 255):
            if hist_normalized[i] > hist_normalized[i-1] and hist_normalized[i] > hist_normalized[i+1]:
                if hist_normalized[i] > 0.01:  # Threshold
                    peaks.append(i)
        
        has_modal = len(peaks) >= 2 and max(peaks) - min(peaks) > 100
        
        # Detect centered rectangle (common modal pattern)
        if has_modal:
            modal_rect = self._detect_centered_rectangle(screenshot)
        else:
            modal_rect = None
        
        return {
            'has_modal': has_modal,
            'modal_rect': modal_rect,
            'overlay_darkness': min(peaks) if peaks else 0,
            'histogram_peaks': len(peaks)
        }
    
    def _create_layout_hash(self, bboxes: List[Tuple[int, int, int, int]]) -> str:
        """Create a hash representing the layout"""
        if not bboxes:
            return "empty"
        
        # Sort by position
        sorted_boxes = sorted(bboxes, key=lambda b: (b[1], b[0]))
        
        # Create simplified representation
        layout_str = ""
        for x, y, w, h in sorted_boxes[:20]:  # Use top 20 elements
            # Quantize positions to reduce noise
            qx = x // 10 * 10
            qy = y // 10 * 10
            qw = w // 10 * 10
            qh = h // 10 * 10
            layout_str += f"{qx},{qy},{qw},{qh};"
        
        # Create hash
        return hashlib.md5(layout_str.encode()).hexdigest()[:16]
    
    def _analyze_grid_layout(self, shape: Tuple[int, int], bboxes: List) -> Dict[str, Any]:
        """Analyze grid-based layout patterns"""
        if not bboxes:
            return {'type': 'empty', 'rows': 0, 'cols': 0}
        
        height, width = shape
        
        # Divide screen into grid
        grid_size = 3
        grid = np.zeros((grid_size, grid_size))
        
        for x, y, w, h in bboxes:
            # Find which grid cell this element belongs to
            grid_x = min(int(x / width * grid_size), grid_size - 1)
            grid_y = min(int(y / height * grid_size), grid_size - 1)
            grid[grid_y, grid_x] += 1
        
        # Determine layout type
        if grid[0, :].sum() > grid[1:, :].sum():
            layout_type = "top_heavy"
        elif grid[:, 0].sum() > grid[:, 1:].sum():
            layout_type = "left_sidebar"
        elif grid[1, 1] > grid.sum() / 2:
            layout_type = "centered"
        else:
            layout_type = "distributed"
        
        return {
            'type': layout_type,
            'grid_distribution': grid.tolist(),
            'primary_region': np.unravel_index(grid.argmax(), grid.shape)
        }
    
    def _calculate_layout_complexity(self, bboxes: List) -> float:
        """Calculate layout complexity score"""
        if not bboxes:
            return 0.0
        
        # Factors: number of elements, size variance, overlap
        complexity = len(bboxes) / 100.0  # Normalize by typical max
        
        # Size variance
        if len(bboxes) > 1:
            sizes = [(w * h) for _, _, w, h in bboxes]
            size_variance = np.std(sizes) / np.mean(sizes)
            complexity += size_variance * 0.5
        
        return min(1.0, complexity)
    
    def _detect_buttons(self, gray_image: np.ndarray) -> int:
        """Detect button-like elements"""
        # Simple detection using morphological operations
        _, binary = cv2.threshold(gray_image, 200, 255, cv2.THRESH_BINARY)
        
        # Find contours
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        button_count = 0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            # Button-like aspect ratio
            if 0.2 < h/w < 0.5 and 20 < w < 200 and 10 < h < 50:
                button_count += 1
        
        return button_count
    
    def _detect_text_fields(self, image: np.ndarray) -> int:
        """Detect text field-like elements"""
        # Look for white/light rectangles
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        
        # White/light color range
        lower_white = np.array([0, 0, 200])
        upper_white = np.array([180, 30, 255])
        
        mask = cv2.inRange(hsv, lower_white, upper_white)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        text_field_count = 0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            # Text field-like aspect ratio
            if 0.05 < h/w < 0.3 and w > 100 and 15 < h < 40:
                text_field_count += 1
        
        return text_field_count
    
    def _detect_checkboxes(self, gray_image: np.ndarray) -> int:
        """Detect checkbox-like elements"""
        # Look for small square regions
        edges = cv2.Canny(gray_image, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        checkbox_count = 0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            # Square-like with small size
            if 0.8 < h/w < 1.2 and 10 < w < 30 and 10 < h < 30:
                checkbox_count += 1
        
        return checkbox_count
    
    def _detect_scrollbars(self, image: np.ndarray) -> int:
        """Detect scrollbar-like elements"""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        
        scrollbar_count = 0
        
        # Vertical scrollbar (right edge)
        right_edge = gray[:, -20:]
        if np.std(right_edge) < 20:  # Low variance indicates scrollbar
            scrollbar_count += 1
        
        # Horizontal scrollbar (bottom edge)
        bottom_edge = gray[-20:, :]
        if np.std(bottom_edge) < 20:
            scrollbar_count += 1
        
        return scrollbar_count
    
    def _detect_centered_rectangle(self, image: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """Detect a centered rectangular region (modal)"""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        
        # Look in center region
        center_y, center_x = height // 2, width // 2
        roi = gray[center_y-100:center_y+100, center_x-100:center_x+100]
        
        # Find edges
        edges = cv2.Canny(roi, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        if contours:
            largest_contour = max(contours, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(largest_contour)
            # Adjust for ROI offset
            return (x + center_x - 100, y + center_y - 100, w, h)
        
        return None
    
    # Detection strategy methods
    async def detect_by_layout(self, features: Dict[str, Any], known_signatures: List[VisualSignature]) -> Tuple[Optional[str], float]:
        """Detect state by layout matching"""
        layout_signatures = [s for s in known_signatures if s.signature_type == "layout"]
        
        if not layout_signatures:
            return None, 0.0
        
        best_match = None
        best_score = 0.0
        
        for sig in layout_signatures:
            score = sig.match(features)
            if score > best_score:
                best_score = score
                best_match = sig.features.get('state_id')
        
        return best_match, best_score * self.confidence_weights['layout']
    
    async def detect_by_color_pattern(self, features: Dict[str, Any], known_signatures: List[VisualSignature]) -> Tuple[Optional[str], float]:
        """Detect state by color patterns"""
        color_signatures = [s for s in known_signatures if s.signature_type == "color"]
        
        if not color_signatures:
            return None, 0.0
        
        best_match = None
        best_score = 0.0
        
        for sig in color_signatures:
            score = sig.match(features)
            if score > best_score:
                best_score = score
                best_match = sig.features.get('state_id')
        
        return best_match, best_score * self.confidence_weights['color']
    
    async def detect_by_text_content(self, features: Dict[str, Any], known_signatures: List[VisualSignature]) -> Tuple[Optional[str], float]:
        """Detect state by text content"""
        text_signatures = [s for s in known_signatures if s.signature_type == "text"]
        
        if not text_signatures:
            return None, 0.0
        
        # Special handling for error/loading states
        if features.get('has_error_text'):
            return 'error_state', 0.9 * self.confidence_weights['text']
        
        if features.get('has_loading_text'):
            return 'loading_state', 0.9 * self.confidence_weights['text']
        
        best_match = None
        best_score = 0.0
        
        for sig in text_signatures:
            score = sig.match(features)
            if score > best_score:
                best_score = score
                best_match = sig.features.get('state_id')
        
        return best_match, best_score * self.confidence_weights['text']
    
    async def detect_by_ui_elements(self, features: Dict[str, Any], known_signatures: List[VisualSignature]) -> Tuple[Optional[str], float]:
        """Detect state by UI element composition"""
        element_signatures = [s for s in known_signatures if s.signature_type == "element"]
        
        if not element_signatures:
            return None, 0.0
        
        best_match = None
        best_score = 0.0
        
        for sig in element_signatures:
            score = sig.match(features)
            if score > best_score:
                best_score = score
                best_match = sig.features.get('state_id')
        
        return best_match, best_score * self.confidence_weights['elements']
    
    async def detect_by_modal_overlay(self, features: Dict[str, Any], known_signatures: List[VisualSignature]) -> Tuple[Optional[str], float]:
        """Detect modal/dialog states"""
        if features.get('has_modal'):
            # High confidence for modal detection
            return 'modal_state', 0.95 * self.confidence_weights['modal']
        
        return None, 0.0
    
    async def detect_state_ensemble(self, 
                                  screenshot: np.ndarray,
                                  known_signatures: List[VisualSignature]) -> Tuple[Optional[str], float, Dict[str, Any]]:
        """Ensemble state detection using multiple strategies"""
        # Extract features
        features = await self.extract_state_features(screenshot)
        
        # Run all detection strategies
        detection_results = []
        
        for strategy in self.detection_strategies:
            state_id, confidence = await strategy(features, known_signatures)
            if state_id:
                detection_results.append({
                    'state_id': state_id,
                    'confidence': confidence,
                    'strategy': strategy.__name__
                })
        
        if not detection_results:
            return None, 0.0, features
        
        # Aggregate results
        state_votes = defaultdict(float)
        strategy_votes = defaultdict(list)
        
        for result in detection_results:
            state_votes[result['state_id']] += result['confidence']
            strategy_votes[result['state_id']].append(result['strategy'])
        
        # Find best state
        best_state = max(state_votes.items(), key=lambda x: x[1])
        
        # Calculate final confidence
        # Bonus for multiple strategies agreeing
        agreement_bonus = len(strategy_votes[best_state[0]]) * 0.1
        final_confidence = min(1.0, best_state[1] + agreement_bonus)
        
        # Add detection metadata to features
        features['detection_metadata'] = {
            'strategies_used': [r['strategy'] for r in detection_results],
            'state_votes': dict(state_votes),
            'agreement_level': len(strategy_votes[best_state[0]]) / len(self.detection_strategies)
        }

        return best_state[0], final_confidence, features

    # ========================================
    # NEW v2.0: PROACTIVE STATE DETECTION
    # ========================================

    async def register_monitoring_alert(self, alert: Dict[str, Any], screenshot: Optional[np.ndarray] = None):
        """
        Register monitoring alert and trigger state detection (NEW v2.0).

        Args:
            alert: Alert from HybridProactiveMonitoringManager
            screenshot: Optional screenshot (will capture if not provided)
        """
        if not self.is_proactive_enabled:
            return

        space_id = alert.get('space_id')
        if not space_id:
            return

        # Get screenshot if not provided
        if screenshot is None:
            # Would capture from space
            logger.debug(f"[STATE-DETECTION] No screenshot provided for Space {space_id}")
            return

        # Detect state
        state_id, confidence, features = await self.detect_state_ensemble(
            screenshot,
            self.signature_library
        )

        # Track detection
        self.detection_stats['total_detections'] += 1

        if state_id:
            self.detection_stats['successful_detections'] += 1

            # Check for state transition
            previous_state = self.current_space_states.get(space_id)

            if previous_state and previous_state != state_id:
                # State transition detected!
                await self._handle_state_transition(
                    space_id=space_id,
                    from_state=previous_state,
                    to_state=state_id,
                    confidence=confidence,
                    features=features
                )

            # Update current state
            self.current_space_states[space_id] = state_id

            # Learn from this detection
            await self._learn_from_detection(
                state_id=state_id,
                space_id=space_id,
                features=features,
                confidence=confidence
            )

        else:
            # Unknown state detected
            self.detection_stats['unknown_detections'] += 1
            await self._handle_unknown_state(
                space_id=space_id,
                features=features,
                screenshot=screenshot
            )

    async def _handle_state_transition(
        self,
        space_id: int,
        from_state: str,
        to_state: str,
        confidence: float,
        features: Dict[str, Any]
    ):
        """
        Handle detected state transition (NEW v2.0).

        Args:
            space_id: Space ID
            from_state: Previous state
            to_state: New state
            confidence: Detection confidence
            features: Extracted features
        """
        transition = {
            'space_id': space_id,
            'from_state': from_state,
            'to_state': to_state,
            'confidence': confidence,
            'timestamp': datetime.now(),
            'features_hash': hashlib.md5(
                json.dumps(features, sort_keys=True).encode()
            ).hexdigest()[:8]
        }

        self.state_transitions.append(transition)

        logger.info(
            f"[STATE-DETECTION] State transition in Space {space_id}: "
            f"'{from_state}' → '{to_state}' (confidence: {confidence:.1%})"
        )

        # Call transition callback
        if self.state_transition_callback:
            await self.state_transition_callback(transition)

    async def _handle_unknown_state(
        self,
        space_id: int,
        features: Dict[str, Any],
        screenshot: np.ndarray
    ):
        """
        Handle unknown state detection (NEW v2.0).

        Args:
            space_id: Space ID
            features: Extracted features
            screenshot: Screenshot array
        """
        unknown_state = {
            'space_id': space_id,
            'timestamp': datetime.now(),
            'features_hash': hashlib.md5(
                json.dumps(features, sort_keys=True).encode()
            ).hexdigest()[:8],
            'layout_complexity': features.get('layout_complexity', 0),
            'dominant_colors': features.get('dominant_colors', [])
        }

        self.unknown_states.append(unknown_state)

        logger.warning(
            f"[STATE-DETECTION] Unknown state in Space {space_id} "
            f"(complexity: {features.get('layout_complexity', 0):.2f})"
        )

        # Call new state callback
        if self.new_state_callback:
            await self.new_state_callback(unknown_state)

    async def _learn_from_detection(
        self,
        state_id: str,
        space_id: int,
        features: Dict[str, Any],
        confidence: float
    ):
        """
        Learn and update signatures from detection (NEW v2.0).

        Args:
            state_id: Detected state ID
            space_id: Space ID
            features: Extracted features
            confidence: Detection confidence
        """
        # Find existing signatures for this state
        existing_sigs = self.signature_index.get(state_id, [])

        if existing_sigs:
            # Update match count for matching signatures
            for sig in existing_sigs:
                match_score = sig.match(features)
                if match_score > 0.7:
                    sig.match_count += 1
                    sig.last_matched = datetime.now()

        else:
            # Create new signature for this state
            await self.learn_signature_from_features(
                state_id=state_id,
                features=features,
                space_id=space_id,
                auto_learned=True
            )

    async def learn_signature_from_features(
        self,
        state_id: str,
        features: Dict[str, Any],
        space_id: Optional[int] = None,
        auto_learned: bool = False
    ):
        """
        Learn a new visual signature from features (NEW v2.0).

        Args:
            state_id: State identifier
            features: Extracted features
            space_id: Optional space ID
            auto_learned: True if learned from monitoring
        """
        # Create signatures for each feature type
        new_signatures = []

        # Layout signature
        if 'layout_hash' in features:
            layout_sig = VisualSignature(
                signature_type='layout',
                features={'layout_hash': features['layout_hash'],
                         'element_positions': features.get('element_positions', []),
                         'state_id': state_id},
                state_id=state_id,
                space_id=space_id,
                auto_learned=auto_learned
            )
            new_signatures.append(layout_sig)

        # Color signature
        if 'dominant_colors' in features:
            color_sig = VisualSignature(
                signature_type='color',
                features={'dominant_colors': features['dominant_colors'],
                         'state_id': state_id},
                state_id=state_id,
                space_id=space_id,
                auto_learned=auto_learned
            )
            new_signatures.append(color_sig)

        # Text signature
        if 'text_elements' in features:
            text_sig = VisualSignature(
                signature_type='text',
                features={'text_elements': features['text_elements'],
                         'state_id': state_id},
                state_id=state_id,
                space_id=space_id,
                auto_learned=auto_learned
            )
            new_signatures.append(text_sig)

        # Element signature
        if 'ui_elements' in features:
            element_sig = VisualSignature(
                signature_type='element',
                features={'ui_elements': features['ui_elements'],
                         'state_id': state_id},
                state_id=state_id,
                space_id=space_id,
                auto_learned=auto_learned
            )
            new_signatures.append(element_sig)

        # Add to library
        for sig in new_signatures:
            self.signature_library.append(sig)
            self.signature_index[state_id].append(sig)

        logger.info(
            f"[STATE-DETECTION] Learned {len(new_signatures)} signatures for state '{state_id}' "
            f"(auto: {auto_learned})"
        )

        # Save library
        self._save_signature_library()

    async def query_state_with_context(self, query: str, screenshot: np.ndarray) -> Dict[str, Any]:
        """
        Query state with natural language using ImplicitReferenceResolver (NEW v2.0).

        Examples:
        - "is this the error screen?"
        - "what state is this?"
        - "detect the login page"

        Args:
            query: Natural language query
            screenshot: Screenshot to analyze

        Returns:
            Dictionary with query results
        """
        if not self.implicit_resolver:
            # Fallback to direct detection
            state_id, confidence, features = await self.detect_state_ensemble(
                screenshot,
                self.signature_library
            )

            return {
                'query': query,
                'state_id': state_id,
                'confidence': confidence,
                'method': 'direct_detection'
            }

        # Resolve implicit references in query
        resolved_query = await self.implicit_resolver.resolve_references(query)

        # Extract state reference from query (simple keyword matching for now)
        query_lower = resolved_query.lower()

        # Check if query is asking about specific state
        for state_id in self.signature_index.keys():
            if state_id.lower() in query_lower:
                # Verify if screenshot matches this state
                state_id_detected, confidence, features = await self.detect_state_ensemble(
                    screenshot,
                    self.signature_index[state_id]  # Only check this state's signatures
                )

                is_match = state_id_detected == state_id and confidence > 0.5

                return {
                    'query': query,
                    'resolved_query': resolved_query,
                    'state_id': state_id,
                    'is_match': is_match,
                    'confidence': confidence,
                    'method': 'targeted_detection'
                }

        # General state detection
        state_id, confidence, features = await self.detect_state_ensemble(
            screenshot,
            self.signature_library
        )

        return {
            'query': query,
            'resolved_query': resolved_query,
            'state_id': state_id,
            'confidence': confidence,
            'method': 'general_detection'
        }

    def get_state_transition_history(self, space_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get state transition history (NEW v2.0).

        Args:
            space_id: Optional space ID to filter by
            limit: Maximum number of transitions to return

        Returns:
            List of transitions
        """
        transitions = list(self.state_transitions)

        if space_id:
            transitions = [t for t in transitions if t['space_id'] == space_id]

        return transitions[-limit:]

    def get_detection_statistics(self) -> Dict[str, Any]:
        """
        Get detection statistics (NEW v2.0).

        Returns:
            Dictionary with detection stats
        """
        total = self.detection_stats.get('total_detections', 0)
        successful = self.detection_stats.get('successful_detections', 0)
        unknown = self.detection_stats.get('unknown_detections', 0)

        success_rate = successful / total if total > 0 else 0.0

        return {
            'total_detections': total,
            'successful_detections': successful,
            'unknown_detections': unknown,
            'success_rate': success_rate,
            'signature_library_size': len(self.signature_library),
            'known_states': len(self.signature_index),
            'auto_learned_signatures': sum(1 for sig in self.signature_library if sig.auto_learned),
            'total_transitions': len(self.state_transitions),
            'is_proactive_enabled': self.is_proactive_enabled
        }

    # ─────────────────────────────────────────────────────────
    # VSMS-compatible API methods (v254.1)
    # These are called by vsms_core.py's VisualStateManager.
    # ─────────────────────────────────────────────────────────

    async def detect_application(self, screenshot: np.ndarray) -> Optional[str]:
        """Detect which application is shown in the screenshot."""
        # Use text features to identify application
        try:
            features = await self.extract_state_features(screenshot)
            text_features = features.get("text_content", [])
            # Simple heuristic: return app name from text if available
            if text_features:
                return str(text_features[0])[:50]
        except Exception:
            pass
        return "detected_app"

    async def extract_identity_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract application identity features for recognition."""
        try:
            features = await self.extract_state_features(screenshot)
            return {
                "colors": features.get("dominant_colors", []),
                "chrome": {
                    "layout_type": features.get("layout_type", "unknown"),
                    "has_toolbar": features.get("toolbar_detected", False),
                    "has_sidebar": features.get("sidebar_detected", False),
                },
            }
        except Exception:
            return {"colors": [], "chrome": {}}

    async def detect_state(
        self, screenshot: np.ndarray, app_id: str,
        known_states: Dict[str, Any],
    ) -> Tuple[Optional[str], float]:
        """Detect current state from screenshot using ensemble detection."""
        try:
            # Use the v2.0 ensemble detection if signatures are available
            signatures = []
            for state_id, state in known_states.items():
                if hasattr(state, 'visual_signatures'):
                    signatures.extend(state.visual_signatures)
                elif isinstance(state, dict) and 'visual_signatures' in state:
                    signatures.extend(state['visual_signatures'])

            if signatures:
                result = await self.detect_state_ensemble(screenshot, signatures)
                return result.get("detected_state_id"), result.get("confidence", 0.0)

            # Fallback: extract features and do basic matching
            features = await self.extract_state_features(screenshot)
            return None, 0.0
        except Exception:
            return None, 0.0

    async def extract_content_features(self, screenshot: np.ndarray) -> Dict[str, Any]:
        """Extract content context features from screenshot."""
        try:
            features = await self.extract_state_features(screenshot)
            return {
                "type": "document",
                "title": "",
                "summary": "",
                "text_content": features.get("text_content", []),
                "actions": [],
            }
        except Exception:
            return {"type": "unknown", "title": "", "summary": "", "actions": []}

    def _save_signature_library(self):
        """Save signature library to disk (NEW v2.0)"""
        try:
            save_path = Path.home() / ".jarvis" / "state_signature_library.json"
            save_path.parent.mkdir(parents=True, exist_ok=True)

            # Convert signatures to dict
            data = {
                'signatures': [
                    {
                        'signature_type': sig.signature_type,
                        'features': sig.features,
                        'state_id': sig.state_id,
                        'space_id': sig.space_id,
                        'auto_learned': sig.auto_learned,
                        'match_count': sig.match_count,
                        'timestamp': sig.timestamp.isoformat() if sig.timestamp else None,
                        'last_matched': sig.last_matched.isoformat() if sig.last_matched else None,
                        'visual_fingerprint': sig.visual_fingerprint
                    }
                    for sig in self.signature_library[-500:]  # Keep last 500
                ],
                'stats': dict(self.detection_stats)
            }

            with open(save_path, 'w') as f:
                json.dump(data, f, indent=2)

            logger.info(f"[STATE-DETECTION] Saved {len(self.signature_library)} signatures")

        except Exception as e:
            logger.error(f"[STATE-DETECTION] Failed to save signature library: {e}")

    def _load_signature_library(self):
        """Load signature library from disk (NEW v2.0)"""
        try:
            load_path = Path.home() / ".jarvis" / "state_signature_library.json"

            if not load_path.exists():
                return

            with open(load_path, 'r') as f:
                data = json.load(f)

            # Reconstruct signatures
            for sig_data in data.get('signatures', []):
                sig = VisualSignature(
                    signature_type=sig_data['signature_type'],
                    features=sig_data['features'],
                    state_id=sig_data.get('state_id'),
                    space_id=sig_data.get('space_id'),
                    auto_learned=sig_data.get('auto_learned', False),
                    match_count=sig_data.get('match_count', 0),
                    timestamp=datetime.fromisoformat(sig_data['timestamp']) if sig_data.get('timestamp') else None,
                    last_matched=datetime.fromisoformat(sig_data['last_matched']) if sig_data.get('last_matched') else None,
                    visual_fingerprint=sig_data.get('visual_fingerprint')
                )

                self.signature_library.append(sig)
                if sig.state_id:
                    self.signature_index[sig.state_id].append(sig)

            # Load stats
            self.detection_stats.update(data.get('stats', {}))

            logger.info(f"[STATE-DETECTION] Loaded {len(self.signature_library)} signatures from disk")

        except Exception as e:
            logger.error(f"[STATE-DETECTION] Failed to load signature library: {e}")

    # ========================================
    # END NEW v2.0 PROACTIVE METHODS
    # ========================================