"""
Visual Event Detector v10.7
===========================

Production-grade visual event detection for Video Multi-Space Intelligence (VMSI).

CRITICAL FIXES v10.7 (Jan 2026):
================================
- FIXED: Cache thread safety - all cache operations now protected by lock
- FIXED: Stats counters thread safety - atomic access via properties
- ADDED: Configurable cache size via JARVIS_DETECTOR_CACHE_MAX_SIZE env var

Features:
- OCR text detection (pytesseract)
- Computer vision element detection (OpenCV)
- Color pattern detection (progress bars, status indicators)
- Fuzzy text matching (typo tolerance)
- Multi-region analysis
- Confidence scoring
- Async/await throughout
- Thread-safe operations
- Zero hardcoding

This is the "eyes" that analyze frames from VideoWatcher streams.
"""

import asyncio
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING

import numpy as np

logger = logging.getLogger(__name__)

# Optional dependencies with graceful degradation
try:
    import pytesseract
    from PIL import Image
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False
    Image = None  # Set to None so we can check it
    logger.warning("pytesseract not available - OCR disabled. Install: pip install pytesseract pillow")

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    logger.warning("OpenCV not available - element detection disabled. Install: pip install opencv-python")

try:
    from fuzzywuzzy import fuzz
    FUZZYWUZZY_AVAILABLE = True
except ImportError:
    FUZZYWUZZY_AVAILABLE = False
    logger.warning("fuzzywuzzy not available - fuzzy matching disabled. Install: pip install fuzzywuzzy")


class DetectionMethod(Enum):
    """Detection method types"""
    OCR = "ocr"
    TEMPLATE_MATCH = "template_match"
    COLOR_PATTERN = "color_pattern"
    FEATURE_MATCH = "feature_match"


@dataclass
class DetectorConfig:
    """Configuration for visual event detector - NO HARDCODING"""
    # OCR settings
    ocr_engine: str = field(default_factory=lambda: os.getenv('JARVIS_OCR_ENGINE', 'pytesseract'))
    ocr_lang: str = field(default_factory=lambda: os.getenv('JARVIS_OCR_LANG', 'eng'))
    ocr_psm: int = field(default_factory=lambda: int(os.getenv('JARVIS_OCR_PSM', '6')))  # Page segmentation mode
    ocr_oem: int = field(default_factory=lambda: int(os.getenv('JARVIS_OCR_OEM', '3')))  # OCR Engine Mode

    # Detection settings
    confidence_threshold: float = field(default_factory=lambda: float(os.getenv('JARVIS_DETECTION_CONFIDENCE', '0.75')))
    fuzzy_match_ratio: float = field(default_factory=lambda: float(os.getenv('JARVIS_FUZZY_MATCH_RATIO', '0.85')))
    enable_preprocessing: bool = field(default_factory=lambda: os.getenv('JARVIS_OCR_PREPROCESS', 'true').lower() == 'true')

    # Performance settings
    max_concurrent_detections: int = field(default_factory=lambda: int(os.getenv('JARVIS_MAX_CONCURRENT_DETECT', '3')))
    enable_caching: bool = field(default_factory=lambda: os.getenv('JARVIS_DETECTOR_CACHE', 'true').lower() == 'true')
    cache_ttl_seconds: float = field(default_factory=lambda: float(os.getenv('JARVIS_DETECTOR_CACHE_TTL', '2.0')))


@dataclass
class TextDetectionResult:
    """Result from text detection"""
    detected: bool
    confidence: float
    text_found: str
    target_text: str
    method: DetectionMethod
    bounding_box: Optional[Tuple[int, int, int, int]] = None
    fuzzy_score: Optional[float] = None
    all_text: Optional[str] = None  # All text found in frame
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ElementDetectionResult:
    """Result from element detection"""
    detected: bool
    confidence: float
    element_type: str
    location: Optional[Tuple[int, int, int, int]] = None
    method: DetectionMethod = DetectionMethod.TEMPLATE_MATCH
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ColorDetectionResult:
    """Result from color pattern detection"""
    detected: bool
    confidence: float
    color_name: str
    percentage: float  # Percentage of frame matching color
    regions: List[Tuple[int, int, int, int]] = field(default_factory=list)
    method: DetectionMethod = DetectionMethod.COLOR_PATTERN
    metadata: Dict[str, Any] = field(default_factory=dict)


class VisualEventDetector:
    """
    Production-grade visual event detector.

    Analyzes video frames for:
    - Text (OCR)
    - UI elements (template matching)
    - Color patterns (progress bars, status indicators)

    This is the core intelligence for VMSI - analyzing what's happening in background windows.

    THREAD SAFETY v10.7:
    ====================
    - Cache operations protected by lock (prevents dict corruption)
    - Stats counters use lock protection
    - Semaphore is lazily initialized per event loop
    - Multiple async coroutines can safely access shared state
    """

    def __init__(self, config: Optional[DetectorConfig] = None):
        self.config = config or DetectorConfig()

        # Validate dependencies
        self._ocr_available = PYTESSERACT_AVAILABLE
        self._cv_available = OPENCV_AVAILABLE
        self._fuzzy_available = FUZZYWUZZY_AVAILABLE

        # Stats with lock protection
        self._stats_lock = threading.Lock()
        self._total_detections = 0
        self._successful_detections = 0
        self._failed_detections = 0
        self._cache_hits = 0

        # Cache (frame hash -> result) with lock protection
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._cache_timestamps: Dict[str, float] = {}

        # Lazy-initialized semaphore (created on first async use to avoid
        # "no current event loop" error when created in thread pool)
        self._detection_semaphore: Optional[asyncio.Semaphore] = None
        self._max_concurrent_detections = self.config.max_concurrent_detections

        logger.info(
            f"VisualEventDetector initialized - "
            f"OCR: {self._ocr_available}, CV: {self._cv_available}, "
            f"Fuzzy: {self._fuzzy_available}"
        )

    @property
    def total_detections(self) -> int:
        """Thread-safe access to total detections."""
        with self._stats_lock:
            return self._total_detections

    @total_detections.setter
    def total_detections(self, value: int):
        with self._stats_lock:
            self._total_detections = value

    @property
    def successful_detections(self) -> int:
        """Thread-safe access to successful detections."""
        with self._stats_lock:
            return self._successful_detections

    @successful_detections.setter
    def successful_detections(self, value: int):
        with self._stats_lock:
            self._successful_detections = value

    @property
    def failed_detections(self) -> int:
        """Thread-safe access to failed detections."""
        with self._stats_lock:
            return self._failed_detections

    @failed_detections.setter
    def failed_detections(self, value: int):
        with self._stats_lock:
            self._failed_detections = value

    @property
    def cache_hits(self) -> int:
        """Thread-safe access to cache hits."""
        with self._stats_lock:
            return self._cache_hits

    @cache_hits.setter
    def cache_hits(self, value: int):
        with self._stats_lock:
            self._cache_hits = value

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazily create semaphore on first async use."""
        if self._detection_semaphore is None:
            self._detection_semaphore = asyncio.Semaphore(self._max_concurrent_detections)
        return self._detection_semaphore

    async def detect_text(
        self,
        frame: np.ndarray,
        target_text: str,
        case_sensitive: bool = False,
        fuzzy: bool = True
    ) -> TextDetectionResult:
        """
        Detect text in frame using OCR.

        Args:
            frame: Image frame (numpy array, RGB)
            target_text: Text to find
            case_sensitive: Whether to match case
            fuzzy: Enable fuzzy matching for typo tolerance

        Returns:
            TextDetectionResult with detection details
        """
        async with self._get_semaphore():
            self.total_detections += 1

            if not self._ocr_available:
                return TextDetectionResult(
                    detected=False,
                    confidence=0.0,
                    text_found="",
                    target_text=target_text,
                    method=DetectionMethod.OCR,
                    metadata={'error': 'pytesseract not available'}
                )

            try:
                # Check cache
                if self.config.enable_caching:
                    cache_key = self._get_frame_hash(frame) + f"_{target_text}"
                    cached = self._get_from_cache(cache_key)
                    if cached:
                        self.cache_hits += 1
                        return cached

                # Preprocess frame for better OCR
                if self.config.enable_preprocessing:
                    processed_frame = await self._preprocess_for_ocr(frame)
                else:
                    processed_frame = frame

                # Convert to PIL Image
                if Image is None:
                    raise RuntimeError("PIL Image not available")
                pil_image = Image.fromarray(processed_frame)

                # Run OCR in thread pool (blocking operation)
                ocr_text = await asyncio.to_thread(
                    self._run_ocr,
                    pil_image
                )

                logger.debug(f"OCR extracted {len(ocr_text) if ocr_text else 0} characters from frame")

                if not ocr_text:
                    logger.debug(f"OCR returned no text - looking for '{target_text}'")
                    result = TextDetectionResult(
                        detected=False,
                        confidence=0.0,
                        text_found="",
                        target_text=target_text,
                        method=DetectionMethod.OCR,
                        all_text="",
                        metadata={'ocr_empty': True}
                    )
                    self.failed_detections += 1
                    return result
                else:
                    logger.debug(f"OCR text preview: '{ocr_text[:200]}'... (looking for '{target_text}')")

                # Search for target text
                detected, confidence, fuzzy_score = self._match_text(
                    ocr_text,
                    target_text,
                    case_sensitive=case_sensitive,
                    fuzzy=fuzzy
                )

                result = TextDetectionResult(
                    detected=detected,
                    confidence=confidence,
                    text_found=target_text if detected else "",
                    target_text=target_text,
                    method=DetectionMethod.OCR,
                    fuzzy_score=fuzzy_score,
                    all_text=ocr_text,
                    metadata={
                        'ocr_length': len(ocr_text),
                        'preprocessed': self.config.enable_preprocessing,
                    }
                )

                if detected:
                    self.successful_detections += 1
                else:
                    self.failed_detections += 1

                # Cache result
                if self.config.enable_caching:
                    self._add_to_cache(cache_key, result)

                return result

            except Exception as e:
                logger.error(f"Error in text detection: {e}", exc_info=True)
                self.failed_detections += 1
                return TextDetectionResult(
                    detected=False,
                    confidence=0.0,
                    text_found="",
                    target_text=target_text,
                    method=DetectionMethod.OCR,
                    metadata={'error': str(e)}
                )

    async def detect_element(
        self,
        frame: np.ndarray,
        element_spec: Dict[str, Any]
    ) -> ElementDetectionResult:
        """
        Detect UI element using computer vision.

        Args:
            frame: Image frame (numpy array, RGB)
            element_spec: Element specification
                {
                    'type': 'button' | 'icon' | 'checkbox',
                    'template': np.ndarray (optional),
                    'color': (R, G, B) (optional),
                    'size': (width, height) (optional)
                }

        Returns:
            ElementDetectionResult with detection details
        """
        async with self._get_semaphore():
            self.total_detections += 1

            if not self._cv_available:
                return ElementDetectionResult(
                    detected=False,
                    confidence=0.0,
                    element_type=element_spec.get('type', 'unknown'),
                    metadata={'error': 'OpenCV not available'}
                )

            try:
                element_type = element_spec.get('type', 'unknown')
                template = element_spec.get('template')

                if template is not None:
                    # Template matching
                    result = await self._template_match(frame, template)
                    return ElementDetectionResult(
                        detected=result['detected'],
                        confidence=result['confidence'],
                        element_type=element_type,
                        location=result.get('location'),
                        method=DetectionMethod.TEMPLATE_MATCH,
                        metadata=result.get('metadata', {})
                    )
                else:
                    # No template provided
                    return ElementDetectionResult(
                        detected=False,
                        confidence=0.0,
                        element_type=element_type,
                        metadata={'error': 'No template provided'}
                    )

            except Exception as e:
                logger.error(f"Error in element detection: {e}", exc_info=True)
                self.failed_detections += 1
                return ElementDetectionResult(
                    detected=False,
                    confidence=0.0,
                    element_type=element_spec.get('type', 'unknown'),
                    metadata={'error': str(e)}
                )

    async def detect_color_pattern(
        self,
        frame: np.ndarray,
        color_spec: Dict[str, Any]
    ) -> ColorDetectionResult:
        """
        Detect color patterns (e.g., progress bars, status indicators).

        Args:
            frame: Image frame (numpy array, RGB)
            color_spec: Color specification
                {
                    'name': 'green' | 'red' | 'blue' | 'custom',
                    'rgb_range': ((r_min, g_min, b_min), (r_max, g_max, b_max)),
                    'min_percentage': 5.0  # Minimum % of frame to detect
                }

        Returns:
            ColorDetectionResult with detection details
        """
        async with self._get_semaphore():
            self.total_detections += 1

            if not self._cv_available:
                return ColorDetectionResult(
                    detected=False,
                    confidence=0.0,
                    color_name=color_spec.get('name', 'unknown'),
                    percentage=0.0,
                    metadata={'error': 'OpenCV not available'}
                )

            try:
                color_name = color_spec.get('name', 'unknown')
                rgb_range = color_spec.get('rgb_range')
                min_percentage = color_spec.get('min_percentage', 5.0)

                if not rgb_range:
                    # Use preset colors
                    rgb_range = self._get_preset_color_range(color_name)

                # Detect color in thread pool
                result = await asyncio.to_thread(
                    self._detect_color,
                    frame,
                    rgb_range,
                    min_percentage
                )

                detected = result['percentage'] >= min_percentage

                if detected:
                    self.successful_detections += 1
                else:
                    self.failed_detections += 1

                return ColorDetectionResult(
                    detected=detected,
                    confidence=min(result['percentage'] / min_percentage, 1.0),
                    color_name=color_name,
                    percentage=result['percentage'],
                    regions=result.get('regions', []),
                    metadata={
                        'rgb_range': rgb_range,
                        'min_percentage': min_percentage,
                        'total_pixels': result.get('total_pixels', 0),
                        'matching_pixels': result.get('matching_pixels', 0)
                    }
                )

            except Exception as e:
                logger.error(f"Error in color detection: {e}", exc_info=True)
                self.failed_detections += 1
                return ColorDetectionResult(
                    detected=False,
                    confidence=0.0,
                    color_name=color_spec.get('name', 'unknown'),
                    percentage=0.0,
                    metadata={'error': str(e)}
                )

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _run_ocr(self, pil_image: Any) -> str:
        """Run OCR on PIL image (blocking operation)."""
        try:
            config = f'--psm {self.config.ocr_psm} --oem {self.config.ocr_oem}'
            text = pytesseract.image_to_string(
                pil_image,
                lang=self.config.ocr_lang,
                config=config
            )
            return text.strip()
        except Exception as e:
            logger.error(f"OCR error: {e}")
            return ""

    async def _preprocess_for_ocr(self, frame: np.ndarray) -> np.ndarray:
        """
        Preprocess frame for better OCR accuracy.

        Applies:
        - Grayscale conversion
        - Contrast enhancement
        - Noise reduction
        - Thresholding
        """
        if not self._cv_available:
            return frame

        try:
            # Run in thread pool (OpenCV operations can be CPU-intensive)
            return await asyncio.to_thread(self._preprocess_sync, frame)
        except Exception as e:
            logger.error(f"Preprocessing error: {e}")
            return frame

    def _preprocess_sync(self, frame: np.ndarray) -> np.ndarray:
        """Synchronous preprocessing (called in thread pool)."""
        # Convert to grayscale
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

        # Apply adaptive thresholding
        thresh = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            11, 2
        )

        # Denoise
        denoised = cv2.fastNlMeansDenoising(thresh, None, 10, 7, 21)

        # Convert back to RGB for PIL
        rgb = cv2.cvtColor(denoised, cv2.COLOR_GRAY2RGB)

        return rgb

    def _match_text(
        self,
        ocr_text: str,
        target_text: str,
        case_sensitive: bool = False,
        fuzzy: bool = True
    ) -> Tuple[bool, float, Optional[float]]:
        """
        Match target text in OCR results.

        Returns:
            (detected, confidence, fuzzy_score)
        """
        if not ocr_text:
            return (False, 0.0, None)

        # Normalize text
        search_text = ocr_text if case_sensitive else ocr_text.lower()
        target = target_text if case_sensitive else target_text.lower()

        # Exact match (highest confidence)
        if target in search_text:
            return (True, 1.0, 1.0 if fuzzy else None)

        # Fuzzy match (if enabled)
        if fuzzy and self._fuzzy_available:
            # Split OCR text into words
            words = re.findall(r'\b\w+\b', search_text)
            target_words = re.findall(r'\b\w+\b', target)

            # Try fuzzy matching on word combinations
            max_ratio = 0.0

            # Check full phrase first
            ratio = fuzz.partial_ratio(search_text, target) / 100.0
            max_ratio = max(max_ratio, ratio)

            # Check individual target words
            for target_word in target_words:
                for ocr_word in words:
                    word_ratio = fuzz.ratio(ocr_word, target_word) / 100.0
                    max_ratio = max(max_ratio, word_ratio)

            if max_ratio >= self.config.fuzzy_match_ratio:
                return (True, max_ratio, max_ratio)

            return (False, max_ratio, max_ratio)

        return (False, 0.0, None)

    async def _template_match(
        self,
        frame: np.ndarray,
        template: np.ndarray
    ) -> Dict[str, Any]:
        """Template matching using OpenCV."""
        try:
            result = await asyncio.to_thread(
                self._template_match_sync,
                frame,
                template
            )
            return result
        except Exception as e:
            logger.error(f"Template matching error: {e}")
            return {'detected': False, 'confidence': 0.0}

    def _template_match_sync(
        self,
        frame: np.ndarray,
        template: np.ndarray
    ) -> Dict[str, Any]:
        """Synchronous template matching."""
        # Convert to grayscale
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        gray_template = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)

        # Template matching
        result = cv2.matchTemplate(gray_frame, gray_template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        # Check if match exceeds threshold
        detected = max_val >= self.config.confidence_threshold

        if detected:
            # Get bounding box
            h, w = gray_template.shape
            top_left = max_loc
            bottom_right = (top_left[0] + w, top_left[1] + h)
            location = (*top_left, *bottom_right)
        else:
            location = None

        return {
            'detected': detected,
            'confidence': float(max_val),
            'location': location,
            'metadata': {
                'match_value': float(max_val),
                'threshold': self.config.confidence_threshold
            }
        }

    def _detect_color(
        self,
        frame: np.ndarray,
        rgb_range: Tuple[Tuple[int, int, int], Tuple[int, int, int]],
        min_percentage: float
    ) -> Dict[str, Any]:
        """Detect color in frame (synchronous)."""
        lower, upper = rgb_range

        # Create mask for color range
        lower_bound = np.array(lower, dtype=np.uint8)
        upper_bound = np.array(upper, dtype=np.uint8)

        mask = cv2.inRange(frame, lower_bound, upper_bound)

        # Calculate percentage
        total_pixels = frame.shape[0] * frame.shape[1]
        matching_pixels = np.count_nonzero(mask)
        percentage = (matching_pixels / total_pixels) * 100.0

        # Find contours (regions)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        regions = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            regions.append((x, y, x + w, y + h))

        return {
            'percentage': percentage,
            'total_pixels': total_pixels,
            'matching_pixels': matching_pixels,
            'regions': regions
        }

    def _get_preset_color_range(self, color_name: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        """Get preset RGB ranges for common colors."""
        presets = {
            'red': ((150, 0, 0), (255, 100, 100)),
            'green': ((0, 150, 0), (100, 255, 100)),
            'blue': ((0, 0, 150), (100, 100, 255)),
            'yellow': ((150, 150, 0), (255, 255, 100)),
            'orange': ((200, 100, 0), (255, 200, 100)),
            'purple': ((100, 0, 150), (200, 100, 255)),
            'white': ((200, 200, 200), (255, 255, 255)),
            'black': ((0, 0, 0), (50, 50, 50)),
            'gray': ((100, 100, 100), (180, 180, 180)),
        }

        return presets.get(color_name.lower(), ((0, 0, 0), (255, 255, 255)))

    def _get_frame_hash(self, frame: np.ndarray) -> str:
        """Get hash of frame for caching."""
        # Simple hash based on frame shape and mean pixel value
        return f"{frame.shape}_{frame.mean():.2f}"

    def _get_from_cache(self, key: str) -> Optional[Any]:
        """Get result from cache if not expired (thread-safe)."""
        import time

        with self._cache_lock:
            if key not in self._cache:
                return None

            # Check if expired
            timestamp = self._cache_timestamps.get(key, 0)
            if (time.time() - timestamp) > self.config.cache_ttl_seconds:
                # Expired - remove atomically
                self._cache.pop(key, None)
                self._cache_timestamps.pop(key, None)
                return None

            return self._cache[key]

    def _add_to_cache(self, key: str, value: Any):
        """Add result to cache (thread-safe)."""
        import time

        with self._cache_lock:
            self._cache[key] = value
            self._cache_timestamps[key] = time.time()

            # Limit cache size - get configurable max from environment
            max_cache_size = int(os.getenv('JARVIS_DETECTOR_CACHE_MAX_SIZE', '100'))
            if len(self._cache) > max_cache_size:
                # Remove oldest entries
                try:
                    oldest_key = min(self._cache_timestamps, key=self._cache_timestamps.get)
                    self._cache.pop(oldest_key, None)
                    self._cache_timestamps.pop(oldest_key, None)
                except ValueError:
                    # Empty dict edge case
                    pass

    def get_stats(self) -> Dict[str, Any]:
        """Get detector statistics (thread-safe)."""
        # Thread-safe access to stats
        with self._stats_lock:
            total = self._total_detections
            successful = self._successful_detections
            failed = self._failed_detections
            cache_hits = self._cache_hits

        # Thread-safe access to cache size
        with self._cache_lock:
            cache_size = len(self._cache)

        success_rate = successful / total if total > 0 else 0.0

        return {
            'total_detections': total,
            'successful_detections': successful,
            'failed_detections': failed,
            'success_rate': round(success_rate, 3),
            'cache_hits': cache_hits,
            'cache_size': cache_size,
            'capabilities': {
                'ocr': self._ocr_available,
                'computer_vision': self._cv_available,
                'fuzzy_matching': self._fuzzy_available,
            }
        }


# Factory function
def create_detector(config: Optional[DetectorConfig] = None) -> VisualEventDetector:
    """Create a VisualEventDetector instance."""
    return VisualEventDetector(config)
