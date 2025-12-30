"""
OCR Strategy Manager for JARVIS
================================

Implements intelligent OCR with fallback strategies:

1. Primary: Claude Vision API
2. Fallback 1: Use cached OCR (if <5min old)
3. Fallback 2: Local OCR (Tesseract)
4. Fallback 3: Return image metadata only

Uses Error Handling Matrix for graceful degradation.

This module provides a comprehensive OCR solution with intelligent model selection,
caching, and robust error handling. It supports multiple OCR engines and provides
graceful degradation when primary methods fail.

Author: Derek Russell
Date: 2025-10-19
"""

import asyncio
import logging
import hashlib
import time
import subprocess
from typing import Dict, Optional, Any, Tuple, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image
import io

logger = logging.getLogger(__name__)

# Import Error Handling Matrix
try:
    from .error_handling_matrix import (
        get_error_handling_matrix,
        initialize_error_handling_matrix,
        FallbackChain,
        ExecutionReport,
        ErrorMessageGenerator,
        ResultQuality
    )
    ERROR_MATRIX_AVAILABLE = True
except ImportError:
    ERROR_MATRIX_AVAILABLE = False
    logger.warning("Error Handling Matrix not available")

# Import API Network Manager
try:
    from .api_network_manager import get_api_network_manager
    API_NETWORK_MANAGER_AVAILABLE = True
except ImportError:
    API_NETWORK_MANAGER_AVAILABLE = False
    logger.warning("API Network Manager not available")


# ============================================================================
# OCR CACHE
# ============================================================================

@dataclass
class CachedOCR:
    """Cached OCR result with metadata.
    
    Stores OCR results with timestamp and confidence information for efficient
    caching and retrieval of previously processed images.
    
    Attributes:
        text: Extracted text content
        image_hash: MD5 hash of the source image
        timestamp: When the OCR was performed
        method: OCR method used ("claude_vision", "tesseract", "metadata")
        confidence: Confidence score from 0.0 to 1.0
        metadata: Additional metadata about the OCR result
    """
    text: str
    image_hash: str
    timestamp: datetime
    method: str  # "claude_vision", "tesseract", "metadata"
    confidence: float  # 0.0-1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_valid(self, max_age_seconds: float = 300.0) -> bool:
        """Check if cache entry is still valid.
        
        Args:
            max_age_seconds: Maximum age in seconds (default 5 minutes)
            
        Returns:
            True if cache entry is still valid, False otherwise
        """
        age = (datetime.now() - self.timestamp).total_seconds()
        return age < max_age_seconds

    def age_seconds(self) -> float:
        """Get age of cache entry in seconds.
        
        Returns:
            Age in seconds since the OCR was performed
        """
        return (datetime.now() - self.timestamp).total_seconds()


class OCRCache:
    """Manages cached OCR results with TTL.

    Features:
    - Time-based expiration
    - Image hash-based caching
    - Automatic cleanup
    - Memory-efficient storage
    
    The cache uses MD5 hashes of images as keys to avoid storing duplicate
    OCR results for the same image content.
    """

    def __init__(self, default_ttl: float = 300.0, max_entries: int = 200):
        """Initialize OCR cache.

        Args:
            default_ttl: Default time-to-live in seconds (default 5 minutes)
            max_entries: Maximum number of cache entries to store
        """
        self.default_ttl = default_ttl
        self.max_entries = max_entries

        # Cache by image hash
        self._cache: Dict[str, CachedOCR] = {}

        logger.info(f"[OCR-CACHE] Initialized (ttl={default_ttl}s, max_entries={max_entries})")

    def get(self, image_hash: str, max_age: Optional[float] = None) -> Optional[CachedOCR]:
        """Get cached OCR result by image hash.

        Args:
            image_hash: MD5 hash of the image
            max_age: Maximum age in seconds (uses default if not provided)

        Returns:
            CachedOCR if valid entry exists, None otherwise
        """
        max_age = max_age or self.default_ttl

        cached = self._cache.get(image_hash)
        if cached and cached.is_valid(max_age):
            logger.info(f"[OCR-CACHE] ✅ Cache hit for {image_hash[:8]} (age={cached.age_seconds():.1f}s)")
            return cached

        # Remove stale cache
        if cached:
            logger.debug(f"[OCR-CACHE] Cache expired for {image_hash[:8]}")
            self._cache.pop(image_hash, None)

        return None

    def store(self, ocr_result: CachedOCR):
        """Store OCR result in cache.
        
        Args:
            ocr_result: CachedOCR instance to store
        """
        self._cache[ocr_result.image_hash] = ocr_result

        # Cleanup if too many entries
        self._cleanup_old_entries()

        logger.debug(f"[OCR-CACHE] Stored result for {ocr_result.image_hash[:8]} (method={ocr_result.method})")

    def _cleanup_old_entries(self):
        """Remove old entries if cache exceeds maximum size.
        
        Removes the oldest 10% of entries when the cache is full.
        """
        if len(self._cache) > self.max_entries:
            # Remove oldest entries
            entries = [(h, c.timestamp) for h, c in self._cache.items()]
            entries.sort(key=lambda x: x[1])

            # Remove oldest 10%
            remove_count = max(1, int(len(entries) * 0.1))
            for image_hash, _ in entries[:remove_count]:
                self._cache.pop(image_hash, None)

            logger.info(f"[OCR-CACHE] Cleaned up {remove_count} old entries")

    def clear(self):
        """Clear all cached entries."""
        self._cache.clear()
        logger.info("[OCR-CACHE] Cleared all cache")

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics.
        
        Returns:
            Dictionary containing cache statistics including total entries,
            maximum entries, and default TTL
        """
        return {
            "total_entries": len(self._cache),
            "max_entries": self.max_entries,
            "default_ttl": self.default_ttl
        }


# ============================================================================
# IMAGE UTILITIES
# ============================================================================

class ImageHasher:
    """Compute image hashes for cache keys.
    
    Provides methods to generate consistent hash values for images,
    used as cache keys to identify duplicate content.
    """

    @staticmethod
    def compute_hash(image_path: str) -> str:
        """Compute MD5 hash of image file.

        Args:
            image_path: Path to the image file

        Returns:
            MD5 hash as hexadecimal string
            
        Raises:
            Exception: If file cannot be read (returns fallback hash)
        """
        try:
            hasher = hashlib.md5()
            with open(image_path, 'rb') as f:
                # Read in chunks to handle large files
                while chunk := f.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            logger.error(f"Failed to compute image hash: {e}")
            # Fallback: hash the path + current timestamp
            fallback = hashlib.md5(f"{image_path}{time.time()}".encode()).hexdigest()
            return fallback


class ImageMetadataExtractor:
    """Extract metadata from images.
    
    Provides functionality to extract image properties and EXIF data
    for use in OCR processing and fallback scenarios.
    """

    @staticmethod
    async def extract_metadata(image_path: str) -> Dict[str, Any]:
        """Extract image metadata including dimensions, format, and EXIF data.

        Args:
            image_path: Path to the image file

        Returns:
            Dictionary containing image metadata including width, height,
            format, mode, file size, filename, and EXIF data if available
            
        Example:
            >>> metadata = await ImageMetadataExtractor.extract_metadata("image.jpg")
            >>> print(metadata["width"], metadata["height"])
            1920 1080
        """
        try:
            img = Image.open(image_path)

            metadata = {
                "width": img.width,
                "height": img.height,
                "format": img.format,
                "mode": img.mode,
                "size_bytes": Path(image_path).stat().st_size,
                "filename": Path(image_path).name
            }

            # Extract EXIF data if available
            if hasattr(img, '_getexif') and img._getexif():
                exif_data = img._getexif()
                if exif_data:
                    metadata["exif"] = {k: v for k, v in exif_data.items() if isinstance(v, (str, int, float))}

            return metadata

        except Exception as e:
            logger.error(f"Failed to extract image metadata: {e}")
            return {
                "filename": Path(image_path).name,
                "error": str(e)
            }


# ============================================================================
# OCR ENGINES
# ============================================================================

class ClaudeVisionOCR:
    """Claude Vision API for OCR.

    Primary OCR method with highest accuracy using Anthropic's Claude Vision
    model for text extraction from images.
    
    Attributes:
        api_client: Anthropic API client instance
    """

    def __init__(self, api_client: Any = None):
        """Initialize Claude Vision OCR.

        Args:
            api_client: Anthropic API client instance (optional)
        """
        self.api_client = api_client
        logger.info("[OCR] Claude Vision OCR initialized")

    async def extract_text(self, image_path: str, prompt: Optional[str] = None) -> Tuple[str, float]:
        """Extract text from image using Claude Vision API.

        Args:
            image_path: Path to the image file
            prompt: Optional custom prompt for OCR (uses default if not provided)

        Returns:
            Tuple of (extracted_text, confidence_score)
            
        Raises:
            Exception: If API client is not initialized or API call fails
            
        Example:
            >>> ocr = ClaudeVisionOCR(api_client)
            >>> text, confidence = await ocr.extract_text("document.jpg")
            >>> print(f"Extracted: {text} (confidence: {confidence})")
        """
        if not self.api_client:
            raise Exception("Claude API client not initialized")

        default_prompt = (
            "Extract all text from this image. "
            "Return ONLY the extracted text, preserving formatting and structure. "
            "If there is no text, return 'NO_TEXT_FOUND'."
        )

        prompt = prompt or default_prompt

        try:
            logger.info(f"[OCR] Extracting text with Claude Vision: {Path(image_path).name}")

            # Read image
            import base64
            with open(image_path, 'rb') as f:
                image_data = base64.standard_b64encode(f.read()).decode('utf-8')

            # Get image format
            img_format = Image.open(image_path).format.lower()
            media_type = f"image/{img_format}"

            # Call Claude Vision API
            response = await asyncio.to_thread(
                self.api_client.messages.create,
                model="claude-3-5-sonnet-20241022",
                max_tokens=2000,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }]
            )

            # Extract text from response
            extracted_text = response.content[0].text.strip()

            # Confidence is high for Claude Vision
            confidence = 0.95 if extracted_text and extracted_text != "NO_TEXT_FOUND" else 0.0

            logger.info(f"[OCR] ✅ Claude Vision extracted {len(extracted_text)} characters (confidence={confidence:.2f})")

            return extracted_text, confidence

        except Exception as e:
            logger.error(f"[OCR] Claude Vision OCR failed: {e}")
            raise


class TesseractOCR:
    """Local Tesseract OCR engine.

    Fallback OCR method using local Tesseract installation when
    Claude Vision is unavailable or fails.
    
    Attributes:
        is_available: Whether Tesseract is installed and available
    """

    def __init__(self):
        """Initialize Tesseract OCR and check availability."""
        self.is_available = self._check_tesseract()
        logger.info(f"[OCR] Tesseract OCR {'available' if self.is_available else 'not available'}")

    def _check_tesseract(self) -> bool:
        """Check if Tesseract is installed and accessible.
        
        Returns:
            True if Tesseract is available, False otherwise
        """
        try:
            result = subprocess.run(
                ["tesseract", "--version"],
                capture_output=True,
                text=True,
                timeout=5.0
            )
            return result.returncode == 0
        except Exception:
            return False

    async def extract_text(self, image_path: str) -> Tuple[str, float]:
        """Extract text from image using Tesseract OCR.

        Args:
            image_path: Path to the image file

        Returns:
            Tuple of (extracted_text, confidence_score)
            
        Raises:
            Exception: If Tesseract is not available or OCR fails
            
        Example:
            >>> ocr = TesseractOCR()
            >>> if ocr.is_available:
            ...     text, confidence = await ocr.extract_text("document.jpg")
        """
        if not self.is_available:
            raise Exception("Tesseract is not installed. Install with: brew install tesseract")

        try:
            logger.info(f"[OCR] Extracting text with Tesseract: {Path(image_path).name}")

            # Run Tesseract with TSV output for confidence scores
            result = await asyncio.to_thread(
                subprocess.run,
                ["tesseract", image_path, "stdout", "--oem", "3", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=30.0
            )

            if result.returncode != 0:
                raise Exception(f"Tesseract failed: {result.stderr}")

            extracted_text = result.stdout.strip()

            # Get confidence (run again with TSV output)
            confidence_result = await asyncio.to_thread(
                subprocess.run,
                ["tesseract", image_path, "stdout", "--oem", "3", "--psm", "6", "tsv"],
                capture_output=True,
                text=True,
                timeout=30.0
            )

            # Parse confidence from TSV output
            confidence = self._parse_confidence(confidence_result.stdout)

            logger.info(f"[OCR] ✅ Tesseract extracted {len(extracted_text)} characters (confidence={confidence:.2f})")

            return extracted_text, confidence

        except Exception as e:
            logger.error(f"[OCR] Tesseract OCR failed: {e}")
            raise

    def _parse_confidence(self, tsv_output: str) -> float:
        """Parse average confidence from Tesseract TSV output.
        
        Args:
            tsv_output: TSV format output from Tesseract
            
        Returns:
            Average confidence score from 0.0 to 1.0
        """
        try:
            lines = tsv_output.strip().split('\n')
            confidences = []

            for line in lines[1:]:  # Skip header
                parts = line.split('\t')
                if len(parts) >= 11:  # TSV has 12 columns
                    conf = parts[10]
                    if conf and conf != '-1':
                        try:
                            confidences.append(float(conf))
                        except ValueError:
                            pass

            if confidences:
                avg_confidence = sum(confidences) / len(confidences)
                return avg_confidence / 100.0  # Convert to 0.0-1.0 range
            else:
                return 0.5  # Default medium confidence

        except Exception as e:
            logger.debug(f"Failed to parse Tesseract confidence: {e}")
            return 0.5


# ============================================================================
# OCR STRATEGY MANAGER
# ============================================================================

@dataclass
class OCRResult:
    """OCR result with comprehensive metadata.
    
    Contains the result of an OCR operation including success status,
    extracted text, confidence score, and execution details.
    
    Attributes:
        success: Whether OCR operation succeeded
        text: Extracted text content
        confidence: Confidence score from 0.0 to 1.0
        method: OCR method used ("claude_vision", "tesseract", "cached", "metadata")
        image_hash: MD5 hash of the processed image
        metadata: Additional metadata about the operation
        error: Error message if operation failed
        execution_time: Time taken for the operation in seconds
    """
    success: bool
    text: str
    confidence: float
    method: str  # "claude_vision", "tesseract", "cached", "metadata"
    image_hash: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    execution_time: float = 0.0


class OCRStrategyManager:
    """Manages intelligent OCR with fallback strategies.

    Implements the OCR fallback chain:
    1. Primary: Claude Vision API
    2. Fallback 1: Use cached OCR (if <5min old)
    3. Fallback 2: Local OCR (Tesseract)
    4. Fallback 3: Return image metadata only

    Uses Error Handling Matrix for graceful degradation and supports
    intelligent model selection through the Hybrid Orchestrator.
    
    Attributes:
        cache: OCR result cache
        image_hasher: Image hash computation utility
        metadata_extractor: Image metadata extraction utility
        use_intelligent_selection: Whether to use intelligent model selection
        claude_vision: Claude Vision OCR engine
        tesseract: Tesseract OCR engine
        error_matrix: Error handling matrix for fallback management
    """

    def __init__(
        self,
        api_client: Any = None,
        cache_ttl: float = 300.0,  # 5 minutes
        max_cache_entries: int = 200,
        enable_error_matrix: bool = True,
        use_intelligent_selection: bool = True
    ):
        """Initialize OCR strategy manager.

        Args:
            api_client: Anthropic API client for Claude Vision
            cache_ttl: Cache time-to-live in seconds (default 5 minutes)
            max_cache_entries: Maximum number of cache entries
            enable_error_matrix: Whether to use Error Handling Matrix
            use_intelligent_selection: Whether to use intelligent model selection
        """
        self.cache = OCRCache(default_ttl=cache_ttl, max_entries=max_cache_entries)
        self.image_hasher = ImageHasher()
        self.metadata_extractor = ImageMetadataExtractor()
        self.use_intelligent_selection = use_intelligent_selection

        # Initialize OCR engines
        self.claude_vision = ClaudeVisionOCR(api_client=api_client) if api_client else None
        self.tesseract = TesseractOCR()

        # Initialize Error Handling Matrix
        self.error_matrix = None
        if enable_error_matrix and ERROR_MATRIX_AVAILABLE:
            try:
                self.error_matrix = get_error_handling_matrix()
                if not self.error_matrix:
                    self.error_matrix = initialize_error_handling_matrix(
                        default_timeout=60.0,
                        aggregation_strategy="first_success",
                        recovery_strategy="continue"
                    )
                logger.info("✅ Error Handling Matrix available for OCR strategies")
            except Exception as e:
                logger.warning(f"Failed to initialize Error Handling Matrix: {e}")

        logger.info(f"[OCR-STRATEGY] Initialized (cache_ttl={cache_ttl}s, matrix_enabled={self.error_matrix is not None})")

    async def _extract_with_intelligent_selection(
        self,
        image_path: str,
        image_hash: str,
        prompt: Optional[str] = None
    ) -> Tuple[str, float, str]:
        """Extract text using intelligent model selection.

        Uses the Hybrid Orchestrator to automatically select the best
        vision model for OCR based on image characteristics and context.

        Args:
            image_path: Path to the image file
            image_hash: MD5 hash of the image
            prompt: Optional custom prompt for OCR

        Returns:
            Tuple of (extracted_text, confidence, method_used)
            
        Raises:
            ImportError: If Hybrid Orchestrator is not available
            Exception: If OCR operation fails
        """
        try:
            from backend.core.hybrid_orchestrator import HybridOrchestrator

            orchestrator = HybridOrchestrator()
            if not orchestrator.is_running:
                await orchestrator.start()

            # Build OCR prompt
            default_prompt = (
                "Extract all text from this image. "
                "Return ONLY the extracted text, preserving formatting and structure. "
                "If there is no text, return 'NO_TEXT_FOUND'."
            )
            ocr_prompt = prompt or default_prompt

            # Build rich context for intelligent selection
            rich_context = {
                "task": "ocr_extraction",
                "image_hash": image_hash[:8],
                "image_path": Path(image_path).name,
                "cache_available": self.cache.get(image_hash) is not None,
                "tesseract_available": self.tesseract.is_available,
            }

            # Add image metadata to context
            metadata = await self.metadata_extractor.extract_metadata(image_path)
            rich_context.update({
                "image_width": metadata.get("width"),
                "image_height": metadata.get("height"),
                "image_format": metadata.get("format"),
                "image_size_bytes": metadata.get("size_bytes"),
            })

            # Read and encode image
            import base64
            with open(image_path, 'rb') as f:
                image_data = base64.standard_b64encode(f.read()).decode('utf-8')

            # Get image format
            img_format = Image.open(image_path).format.lower()
            media_type = f"image/{img_format}"

            # Build multimodal content
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data
                    }
                },
                {
                    "type": "text",
                    "text": ocr_prompt
                }
            ]

            # Execute with intelligent model selection
            result = await orchestrator.execute_with_intelligent_model_selection(
                query=content,  # Pass multimodal content
                intent="vision_analysis",
                required_capabilities={"vision", "ocr", "text_extraction"},
                context=rich_context,
                max_tokens=2000,
                temperature=0,
            )

            if not result.get("success"):
                raise Exception(result.get("error", "Unknown error"))

            extracted_text = result.get("text", "").strip()
            model_used = result.get("model_used", "intelligent_selection")

            # Confidence is high for vision models
            confidence = 0.95 if extracted_text and extracted_text != "NO_TEXT_FOUND" else 0.0

            logger.info(f"[OCR-STRATEGY] OCR completed using {model_used} (extracted {len(extracted_text)} chars)")

            # Cache the result
            cached = CachedOCR(
                text=extracted_text,
                image_hash=image_hash,
                timestamp=datetime.now(),
                method=f"intelligent_{model_used}",
                confidence=confidence,
                metadata={"model_used": model_used}
            )
            self.cache.store(cached)

            return extracted_text, confidence, f"intelligent_{model_used}"

        except ImportError:
            logger.warning("[OCR-STRATEGY] Hybrid orchestrator not available, using fallback")
            raise
        except Exception as e:
            logger.error(f"[OCR-STRATEGY] Error in intelligent selection: {e}")
            raise

    async def extract_text_with_fallbacks(
        self,
        image_path: str,
        prompt: Optional[str] = None,
        cache_max_age: Optional[float] = None,
        skip_cache: bool = False
    ) -> OCRResult:
        """Extract text with intelligent fallbacks.

        Main entry point for OCR operations. Attempts multiple strategies
        in order of preference with graceful degradation.

        Args:
            image_path: Path to the image file
            prompt: Optional custom prompt for Claude Vision
            cache_max_age: Maximum cache age in seconds (default 5 minutes)
            skip_cache: Whether to skip cache lookup

        Returns:
            OCRResult containing extracted text and operation metadata
            
        Example:
            >>> manager = OCRStrategyManager(api_client)
            >>> result = await manager.extract_text_with_fallbacks("document.jpg")
            >>> if result.success:
            ...     print(f"Extracted: {result.text}")
            ...     print(f"Method: {result.method}")
            ...     print(f"Confidence: {result.confidence}")
        """
        start_time = time.time()

        logger.info(f"[OCR-STRATEGY] Starting OCR for {Path(image_path).name}")

        # Compute image hash
        image_hash = self.image_hasher.compute_hash(image_path)

        cache_max_age = cache_max_age or self.cache.default_ttl

        # Try intelligent selection first if enabled
        if self.use_intelligent_selection and not skip_cache:
            try:
                text, confidence, method = await self._extract_with_intelligent_selection(
                    image_path, image_hash, prompt
                )

                result = OCRResult(
                    success=True,
                    text=text,
                    confidence=confidence,
                    method=method,
                    image_hash=image_hash,
                    execution_time=time.time() - start_time
                )

                logger.info(
                    f"[OCR-STRATEGY] Intelligent selection completed in {result.execution_time:.2f}s "
                    f"(method={method}, {len(text)} chars)"
                )

                return result
            except Exception as e:
                logger.warning(f"[OCR-STRATEGY] Intelligent selection failed, falling back to standard methods: {e}")

        # Use Error Handling Matrix if available
        if self.error_matrix:
            result = await self._extract_with_matrix(
                image_path,
                image_hash,
                prompt,
                cache_max_age,
                skip_cache
            )
        else:
            # Fallback to simple sequential extraction
            result = await self._extract_sequential(
                image_path,
                image_hash,
                prompt,
                cache_max_age,
                skip_cache
            )

        result.execution_time = time.time() - start_time
        logger.info(f"[OCR-STRATEGY] Completed in {result.execution_time:.2f}s (method={result.method})")

        return result

    async def _extract_with_matrix(
        self,
        image_path: str,
        image_hash: str,
        prompt: Optional[str],
        cache_max_age: float,
        skip_cache: bool
    ) -> OCRResult:
        """Extract text using Error Handling Matrix.
        
        Uses the Error Handling Matrix to manage fallback strategies
        with proper error handling and recovery.

        Args:
            image_path: Path to the image file
            image_hash: MD5 hash of the image
            prompt: Optional custom prompt for Claude Vision
            cache_max_age: Maximum cache age in seconds
            skip_cache: Whether to skip cache lookup

        Returns:
            OCRResult with extraction results and execution metadata
        """
        logger.info(f"[OCR-STRATEGY] Using Error Handling Matrix")

        # Build fallback chain
        chain = FallbackChain(f"ocr_{image_hash[:8]}")

        # 1. Primary: Claude Vision API
        if self.claude_vision:
            async def extract_with_claude():
                logger.info(f"[OCR-STRATEGY] Attempting Claude Vision OCR")
                text, confidence = await self.claude_vision.extract_text(image_path, prompt)

                # Cache the result
                cached = CachedOCR(
                    text=text,
                    image_hash=image_hash,
                    timestamp=datetime.now(),
                    method="claude_vision",
                    confidence=confidence
                )
                self.cache.store(cached)

                return (text, confidence, "claude_vision")

            chain.add_primary(extract_with_claude, name="claude_vision", timeout=60.0)

        # 2. Fallback 1: Check cache (but not as primary if skip_cache=False)
        if not skip_cache:
            async def use_cache():
                logger.info(f"[OCR-STRATEGY] Attempting cache lookup: {image_hash[:8]}")

                cached = self.cache.get(image_hash, max_age=cache_max_age)
                if cached:
                    logger.info(f"[OCR-STRATEGY] ✅ Using cached OCR (age={cached.age_seconds():.1f}s, method={cached.method})")
                    return (cached.text, cached.confidence, f"cached_{cached.method}")

                raise Exception(f"No valid cache for {image_hash[:8]} (max_age={cache_max_age}s)")

            # Add cache as fallback if Claude Vision is primary, otherwise as secondary
            if self.claude_vision:
                chain.add_fallback(use_cache, name="cache", timeout=1.0)
            else:
                chain.add_primary(use_cache, name="cache", timeout=1.0)

        # 3. Fallback 2: Local OCR
        if self.tesseract:
            async def extract_with_tesseract():
                logger.info(f"[OCR-STRATEGY] Attempting Tesseract OCR: {image_hash[:8]}")

                text, confidence = await self.tesseract.extract_text(image_path)

                # Cache Tesseract result
                cached = CachedOCR(
                    text=text,
                    image_hash=image_hash,
                    timestamp=datetime.now(),
                    method="tesseract",
                    confidence=confidence
                )
                self.cache.store(cached)

                return (text, confidence, "tesseract")

            chain.add_fallback(extract_with_tesseract, name="tesseract", timeout=30.0)

        # 4. Fallback 3: Image metadata only
        async def extract_metadata_only():
            logger.info(f"[OCR-STRATEGY] Falling back to metadata extraction: {image_hash[:8]}")

            metadata = self.metadata_extractor.extract(image_path)
            text = f"Image: {metadata['size']}px, {metadata['format']}"

            return (text, 0.1, "metadata")

        chain.add_fallback(extract_metadata_only, name="metadata", timeout=5.0)

        # Execute fallback chain
        report = await chain.execute()

        if report.final_result:
            text, confidence, method = report.final_result
            return OCRResult(
                success=True,
                text=text,
                confidence=confidence,
                method=method,
                image_hash=image_hash,
                error=None,
                execution_time=0.0  # Will be set by caller
            )
        else:
            return OCRResult(
                success=False,
                text="",
                confidence=0.0,
                method="none",
                image_hash=image_hash,
                error=f"All OCR methods failed: {report.errors}",
                execution_time=0.0
            )

    async def _extract_sequential(
        self,
        image_path: str,
        image_hash: str,
        prompt: Optional[str],
        cache_max_age: float,
        skip_cache: bool
    ) -> OCRResult:
        """Extract text using sequential fallback strategy (when Error Matrix unavailable).

        Attempts OCR methods in order:
        1. Check cache (if not skipped)
        2. Claude Vision API (if available)
        3. Tesseract OCR (if available)
        4. Image metadata extraction (fallback)

        Args:
            image_path: Path to the image file
            image_hash: MD5 hash of the image
            prompt: Optional custom prompt for Claude Vision
            cache_max_age: Maximum cache age in seconds
            skip_cache: Whether to skip cache lookup

        Returns:
            OCRResult with extraction results and execution metadata
        """
        logger.info(f"[OCR-STRATEGY] Using sequential fallback (no Error Matrix)")

        # Step 1: Try cache first (unless skipped)
        if not skip_cache:
            cached = self.cache.get(image_hash, max_age=cache_max_age)
            if cached:
                logger.info(f"[OCR-STRATEGY] ✅ Cache hit (age={cached.age_seconds():.1f}s)")
                return OCRResult(
                    success=True,
                    text=cached.text,
                    confidence=cached.confidence,
                    method=f"cached_{cached.method}",
                    image_hash=image_hash,
                    metadata=cached.metadata,
                )

        # Step 2: Try Claude Vision API
        if self.claude_vision:
            try:
                logger.info("[OCR-STRATEGY] Attempting Claude Vision OCR")
                text, confidence = await self.claude_vision.extract_text(image_path, prompt)

                # Cache the result
                cached_result = CachedOCR(
                    text=text,
                    image_hash=image_hash,
                    timestamp=datetime.now(),
                    method="claude_vision",
                    confidence=confidence,
                )
                self.cache.store(cached_result)

                return OCRResult(
                    success=True,
                    text=text,
                    confidence=confidence,
                    method="claude_vision",
                    image_hash=image_hash,
                )
            except Exception as e:
                logger.warning(f"[OCR-STRATEGY] Claude Vision failed: {e}")

        # Step 3: Try Tesseract OCR
        if self.tesseract and self.tesseract.is_available:
            try:
                logger.info("[OCR-STRATEGY] Attempting Tesseract OCR")
                text, confidence = await self.tesseract.extract_text(image_path)

                # Cache the result
                cached_result = CachedOCR(
                    text=text,
                    image_hash=image_hash,
                    timestamp=datetime.now(),
                    method="tesseract",
                    confidence=confidence,
                )
                self.cache.store(cached_result)

                return OCRResult(
                    success=True,
                    text=text,
                    confidence=confidence,
                    method="tesseract",
                    image_hash=image_hash,
                )
            except Exception as e:
                logger.warning(f"[OCR-STRATEGY] Tesseract failed: {e}")

        # Step 4: Fall back to metadata extraction
        try:
            logger.info("[OCR-STRATEGY] Falling back to metadata extraction")
            metadata = await self.metadata_extractor.extract_metadata(image_path)
            width = metadata.get("width", "?")
            height = metadata.get("height", "?")
            img_format = metadata.get("format", "unknown")
            text = f"Image: {width}x{height}px, format={img_format}"

            return OCRResult(
                success=True,
                text=text,
                confidence=0.1,
                method="metadata",
                image_hash=image_hash,
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"[OCR-STRATEGY] Metadata extraction failed: {e}")

        # All methods failed
        return OCRResult(
            success=False,
            text="",
            confidence=0.0,
            method="none",
            image_hash=image_hash,
            error="All OCR methods failed",
        )


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_manager: Optional[OCRStrategyManager] = None


def get_ocr_strategy_manager() -> Optional[OCRStrategyManager]:
    """Get the global OCR strategy manager instance.

    Returns:
        The global OCRStrategyManager instance if initialized, None otherwise
    """
    return _global_manager


def initialize_ocr_strategy_manager(
    cache_ttl: float = 300.0,
    max_cache_entries: int = 100,
    enable_error_matrix: bool = True,
    anthropic_api_key: Optional[str] = None,
    use_intelligent_selection: bool = True
) -> OCRStrategyManager:
    """Initialize the global OCR strategy manager.

    Args:
        cache_ttl: Cache time-to-live in seconds
        max_cache_entries: Maximum number of cache entries
        enable_error_matrix: Whether to enable Error Handling Matrix integration
        anthropic_api_key: Optional Anthropic API key for Claude Vision
        use_intelligent_selection: Whether to use intelligent model selection

    Returns:
        The initialized OCRStrategyManager instance

    Example:
        >>> manager = initialize_ocr_strategy_manager(
        ...     cache_ttl=600.0,
        ...     max_cache_entries=200,
        ...     enable_error_matrix=True
        ... )
        >>> print("OCR strategy manager initialized")
    """
    global _global_manager

    # Create Anthropic API client if API key is provided
    api_client = None
    if anthropic_api_key:
        try:
            import anthropic
            api_client = anthropic.Anthropic(api_key=anthropic_api_key)
            logger.info("[OCR-STRATEGY] ✅ Anthropic API client created for Claude Vision OCR")
        except ImportError:
            logger.warning("[OCR-STRATEGY] Anthropic library not installed - Claude Vision OCR unavailable")
        except Exception as e:
            logger.warning(f"[OCR-STRATEGY] Failed to create Anthropic client: {e}")

    _global_manager = OCRStrategyManager(
        api_client=api_client,
        cache_ttl=cache_ttl,
        max_cache_entries=max_cache_entries,
        enable_error_matrix=enable_error_matrix,
        use_intelligent_selection=use_intelligent_selection,
    )
    logger.info("✅ Global OCR strategy manager initialized")
    return _global_manager