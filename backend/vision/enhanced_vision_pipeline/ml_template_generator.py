#!/usr/bin/env python3
"""
ML-Powered Template Generator
==============================

Hybrid approach combining:
1. Traditional ML: HOG (Histogram of Oriented Gradients) + LBP (Local Binary Patterns)
2. Lightweight DL: MobileNetV3 feature extraction
3. Template synthesis and augmentation

Optimized for M1 MacBook Pro (16GB RAM):
- Memory-efficient processing
- Async operations
- Intelligent caching
- Dynamic template generation

Author: Derek J. Russell
Date: October 2025
"""

import asyncio
import logging
import cv2
import numpy as np
import torch
import torch.nn as nn
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from pathlib import Path
from PIL import Image
import hashlib
import pickle
from functools import lru_cache
from skimage.feature import hog, local_binary_pattern
from torchvision import transforms, models
from concurrent.futures import ThreadPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

import json

logger = logging.getLogger(__name__)


@dataclass
class TemplateFeatures:
    """Feature representation of a template"""
    hog_features: np.ndarray
    lbp_features: np.ndarray
    deep_features: Optional[np.ndarray] = None
    color_histogram: Optional[np.ndarray] = None
    edge_map: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = None


@dataclass
class GeneratedTemplate:
    """Generated template with metadata"""
    template: np.ndarray  # The actual template image
    features: TemplateFeatures  # Extracted features
    confidence: float  # Quality score
    variations: List[np.ndarray]  # Augmented variations
    target_name: str
    creation_timestamp: float
    metadata: Dict[str, Any]


class MobileNetV3FeatureExtractor(nn.Module):
    """
    Lightweight MobileNetV3 feature extractor
    Optimized for M1 Neural Engine
    """

    def __init__(self, use_mps: bool = True):
        super().__init__()

        # Load pre-trained MobileNetV3-Small (lighter than Large)
        mobilenet = models.mobilenet_v3_small(weights='DEFAULT')

        # Extract feature layers (before classifier)
        self.features = mobilenet.features
        self.avgpool = mobilenet.avgpool

        # Freeze weights for efficiency
        for param in self.parameters():
            param.requires_grad = False

        self.eval()

        # Device selection - prefer MPS on M1
        if use_mps and torch.backends.mps.is_available():
            self.device = torch.device('mps')
            logger.info("[TEMPLATE GEN] Using M1 Metal Performance Shaders (MPS)")
        elif torch.cuda.is_available():
            self.device = torch.device('cuda')
            logger.info("[TEMPLATE GEN] Using CUDA")
        else:
            self.device = torch.device('cpu')
            logger.info("[TEMPLATE GEN] Using CPU")

        self.to(self.device)

        # Transform for input images
        self.transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225]
            )
        ])

    @torch.no_grad()
    def extract_features(self, image: np.ndarray) -> np.ndarray:
        """
        Extract deep features from image

        Args:
            image: numpy array (H, W, C) in BGR format

        Returns:
            Feature vector (1D numpy array)
        """
        # Convert BGR to RGB
        if len(image.shape) == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        elif image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
        else:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        # Convert to PIL for transforms
        pil_image = Image.fromarray(image)

        # Apply transforms
        tensor = self.transform(pil_image).unsqueeze(0).to(self.device)

        # Extract features
        features = self.features(tensor)
        features = self.avgpool(features)
        features = torch.flatten(features, 1)

        # Convert to numpy
        # CRITICAL: Use .copy() to avoid memory corruption when tensor is GC'd
        return features.cpu().numpy().flatten().copy()


class MLTemplateGenerator:
    """
    Advanced ML-powered template generator

    Combines traditional CV and deep learning for robust template generation
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize ML template generator"""
        self.config = config
        self.cache_dir = Path(config.get('cache_dir', Path.home() / '.jarvis' / 'template_cache'))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Initialize feature extractors
        self._init_extractors()

        # Template database
        self.template_db: Dict[str, GeneratedTemplate] = {}
        self.feature_index: Dict[str, List[Tuple[str, float]]] = {}  # For similarity search

        # Thread pool for CPU-bound operations
        if _HAS_MANAGED_EXECUTOR:

            self.executor = ManagedThreadPoolExecutor(max_workers=4, name='pool')

        else:

            self.executor = ThreadPoolExecutor(max_workers=4)

        # Memory budget (MB)
        self.max_memory_mb = config.get('max_memory_mb', 500)
        self.current_memory_mb = 0

        logger.info("[TEMPLATE GEN] ML Template Generator initialized")
        logger.info(f"[TEMPLATE GEN] Cache directory: {self.cache_dir}")

    def _init_extractors(self):
        """Initialize feature extractors"""
        try:
            # MobileNetV3 for deep features
            self.deep_extractor = MobileNetV3FeatureExtractor(use_mps=True)
            logger.info("[TEMPLATE GEN] ✅ MobileNetV3 feature extractor loaded")
        except Exception as e:
            logger.warning(f"[TEMPLATE GEN] Failed to load MobileNetV3: {e}")
            self.deep_extractor = None

        # HOG parameters (optimized for UI icons)
        self.hog_params = {
            'orientations': 9,
            'pixels_per_cell': (8, 8),
            'cells_per_block': (2, 2),
            'visualize': False,
            'channel_axis': -1
        }

        # LBP parameters
        self.lbp_params = {
            'P': 8,  # Number of circularly symmetric neighbor points
            'R': 1,  # Radius of circle
            'method': 'uniform'
        }

    async def generate_template(
        self,
        target: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[np.ndarray]:
        """
        Generate template for target icon using ML

        Args:
            target: Target icon name (e.g., 'control_center', 'screen_mirroring')
            context: Optional context (screen region, display info, etc.)

        Returns:
            Generated template as numpy array (H, W, C) or None
        """
        logger.info(f"[TEMPLATE GEN] Generating template for: {target}")

        try:
            # Check cache first
            cached = await self._load_from_cache(target)
            if cached is not None:
                logger.info(f"[TEMPLATE GEN] ✅ Loaded template from cache")
                return cached.template

            # Generate template based on target type
            if target == 'control_center':
                template = await self._generate_control_center_template(context)
            elif target == 'screen_mirroring':
                template = await self._generate_screen_mirroring_template(context)
            elif target.endswith('_icon'):
                template = await self._generate_generic_icon_template(target, context)
            else:
                # Synthesize from description
                template = await self._synthesize_from_description(target, context)

            if template is not None:
                # Extract features
                features = await self._extract_all_features(template)

                # Create augmented variations
                variations = await self._create_variations(template)

                # Calculate quality score
                confidence = await self._calculate_template_quality(template, features)

                # Create GeneratedTemplate object
                gen_template = GeneratedTemplate(
                    template=template,
                    features=features,
                    confidence=confidence,
                    variations=variations,
                    target_name=target,
                    creation_timestamp=asyncio.get_event_loop().time(),
                    metadata={
                        'context': context,
                        'method': 'ml_hybrid',
                        'augmentations': len(variations)
                    }
                )

                # Save to cache
                await self._save_to_cache(target, gen_template)

                # Add to database
                self.template_db[target] = gen_template

                logger.info(f"[TEMPLATE GEN] ✅ Template generated (confidence: {confidence:.2%})")
                return template

            logger.warning(f"[TEMPLATE GEN] Failed to generate template for {target}")
            return None

        except Exception as e:
            logger.error(f"[TEMPLATE GEN] Template generation failed: {e}", exc_info=True)
            return None

    async def _extract_all_features(self, template: np.ndarray) -> TemplateFeatures:
        """Extract all features from template"""
        # Run feature extraction in parallel
        tasks = [
            self._extract_hog_features(template),
            self._extract_lbp_features(template),
            self._extract_deep_features(template),
            self._extract_color_histogram(template),
            self._extract_edge_map(template)
        ]

        results = await asyncio.gather(*tasks)

        return TemplateFeatures(
            hog_features=results[0],
            lbp_features=results[1],
            deep_features=results[2],
            color_histogram=results[3],
            edge_map=results[4],
            metadata={
                'hog_dim': results[0].shape[0],
                'lbp_dim': results[1].shape[0],
                'deep_dim': results[2].shape[0] if results[2] is not None else 0
            }
        )

    async def _extract_hog_features(self, template: np.ndarray) -> np.ndarray:
        """Extract HOG features"""
        loop = asyncio.get_event_loop()

        def _compute_hog():
            # Convert to grayscale if needed
            if len(template.shape) == 3:
                gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            else:
                gray = template

            # Resize to standard size for consistency
            resized = cv2.resize(gray, (64, 64))

            # Compute HOG
            features = hog(resized, **self.hog_params)
            return features

        return await loop.run_in_executor(self.executor, _compute_hog)

    async def _extract_lbp_features(self, template: np.ndarray) -> np.ndarray:
        """Extract LBP features"""
        loop = asyncio.get_event_loop()

        def _compute_lbp():
            # Convert to grayscale if needed
            if len(template.shape) == 3:
                gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            else:
                gray = template

            # Resize to standard size
            resized = cv2.resize(gray, (64, 64))

            # Compute LBP
            lbp = local_binary_pattern(
                resized,
                self.lbp_params['P'],
                self.lbp_params['R'],
                self.lbp_params['method']
            )

            # Create histogram
            hist, _ = np.histogram(
                lbp.ravel(),
                bins=np.arange(0, self.lbp_params['P'] + 3),
                range=(0, self.lbp_params['P'] + 2)
            )

            # Normalize
            hist = hist.astype('float')
            hist /= (hist.sum() + 1e-7)

            return hist

        return await loop.run_in_executor(self.executor, _compute_lbp)

    async def _extract_deep_features(self, template: np.ndarray) -> Optional[np.ndarray]:
        """Extract deep features using MobileNetV3"""
        if self.deep_extractor is None:
            return None

        loop = asyncio.get_event_loop()

        def _compute_deep():
            return self.deep_extractor.extract_features(template)

        try:
            return await loop.run_in_executor(self.executor, _compute_deep)
        except Exception as e:
            logger.warning(f"[TEMPLATE GEN] Deep feature extraction failed: {e}")
            return None

    async def _extract_color_histogram(self, template: np.ndarray) -> np.ndarray:
        """Extract color histogram"""
        loop = asyncio.get_event_loop()

        def _compute_hist():
            # Convert to HSV for better color representation
            if len(template.shape) == 3:
                hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
            else:
                hsv = cv2.cvtColor(
                    cv2.cvtColor(template, cv2.COLOR_GRAY2BGR),
                    cv2.COLOR_BGR2HSV
                )

            # Calculate histogram for each channel
            h_hist = cv2.calcHist([hsv], [0], None, [32], [0, 180])
            s_hist = cv2.calcHist([hsv], [1], None, [32], [0, 256])
            v_hist = cv2.calcHist([hsv], [2], None, [32], [0, 256])

            # Concatenate and normalize
            hist = np.concatenate([h_hist, s_hist, v_hist]).flatten()
            hist = hist / (hist.sum() + 1e-7)

            return hist

        return await loop.run_in_executor(self.executor, _compute_hist)

    async def _extract_edge_map(self, template: np.ndarray) -> np.ndarray:
        """Extract edge map using Canny"""
        loop = asyncio.get_event_loop()

        def _compute_edges():
            # Convert to grayscale
            if len(template.shape) == 3:
                gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            else:
                gray = template

            # Resize
            resized = cv2.resize(gray, (64, 64))

            # Apply Gaussian blur
            blurred = cv2.GaussianBlur(resized, (5, 5), 0)

            # Canny edge detection
            edges = cv2.Canny(blurred, 50, 150)

            # Normalize to [0, 1]
            edges = edges.astype('float') / 255.0

            return edges.flatten()

        return await loop.run_in_executor(self.executor, _compute_edges)

    async def _generate_control_center_template(
        self,
        context: Optional[Dict[str, Any]]
    ) -> Optional[np.ndarray]:
        """Generate Control Center icon template"""
        # Create a synthetic Control Center icon
        # Two overlapping rounded rectangles (switch/toggle appearance)

        size = 28  # Standard macOS menu bar icon size
        template = np.zeros((size, size, 3), dtype=np.uint8)

        # Background (light gray)
        template[:] = (240, 240, 240)

        # Draw two overlapping rounded rectangles
        # Rectangle 1 (left)
        cv2.rectangle(template, (4, 8), (14, 20), (100, 100, 100), -1)
        cv2.circle(template, (4, 8), 2, (100, 100, 100), -1)
        cv2.circle(template, (14, 8), 2, (100, 100, 100), -1)
        cv2.circle(template, (4, 20), 2, (100, 100, 100), -1)
        cv2.circle(template, (14, 20), 2, (100, 100, 100), -1)

        # Rectangle 2 (right)
        cv2.rectangle(template, (14, 8), (24, 20), (100, 100, 100), -1)
        cv2.circle(template, (24, 8), 2, (100, 100, 100), -1)
        cv2.circle(template, (24, 20), 2, (100, 100, 100), -1)

        return template

    async def _generate_screen_mirroring_template(
        self,
        context: Optional[Dict[str, Any]]
    ) -> Optional[np.ndarray]:
        """Generate Screen Mirroring icon template"""
        # Create a synthetic Screen Mirroring icon
        # Monitor/display with wireless waves

        size = 64  # Larger icon for Control Center menu
        template = np.zeros((size, size, 3), dtype=np.uint8)

        # White background
        template[:] = (255, 255, 255)

        # Draw monitor/rectangle
        cv2.rectangle(template, (12, 20), (52, 44), (50, 50, 50), 2)

        # Draw wireless waves (3 arcs)
        for i in range(1, 4):
            radius = 8 + i * 6
            cv2.ellipse(
                template,
                (32, 32),
                (radius, radius),
                0, -45, 45,
                (50, 150, 255),
                2
            )

        return template

    async def _generate_generic_icon_template(
        self,
        target: str,
        context: Optional[Dict[str, Any]]
    ) -> Optional[np.ndarray]:
        """Generate generic icon template"""
        # Extract icon characteristics from name
        name_parts = target.replace('_icon', '').split('_')

        size = 48
        template = np.zeros((size, size, 3), dtype=np.uint8)
        template[:] = (255, 255, 255)

        # Draw a generic rounded square icon
        cv2.rectangle(template, (8, 8), (40, 40), (100, 100, 100), -1)
        cv2.circle(template, (8, 8), 3, (100, 100, 100), -1)
        cv2.circle(template, (40, 8), 3, (100, 100, 100), -1)
        cv2.circle(template, (8, 40), 3, (100, 100, 100), -1)
        cv2.circle(template, (40, 40), 3, (100, 100, 100), -1)

        return template

    async def _synthesize_from_description(
        self,
        description: str,
        context: Optional[Dict[str, Any]]
    ) -> Optional[np.ndarray]:
        """Synthesize template from text description"""
        # This would use text-to-image synthesis (future enhancement)
        # For now, return a generic template
        logger.debug(f"[TEMPLATE GEN] Synthesizing from description: {description}")
        return await self._generate_generic_icon_template(f"{description}_icon", context)

    async def _create_variations(self, template: np.ndarray) -> List[np.ndarray]:
        """Create augmented variations of template"""
        variations = []

        # Rotation variations
        for angle in [-5, 5]:
            M = cv2.getRotationMatrix2D(
                (template.shape[1] // 2, template.shape[0] // 2),
                angle,
                1.0
            )
            rotated = cv2.warpAffine(template, M, (template.shape[1], template.shape[0]))
            variations.append(rotated)

        # Brightness variations
        for factor in [0.9, 1.1]:
            adjusted = cv2.convertScaleAbs(template, alpha=factor, beta=0)
            variations.append(adjusted)

        # Slight blur
        blurred = cv2.GaussianBlur(template, (3, 3), 0)
        variations.append(blurred)

        # Slight sharpening
        kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        sharpened = cv2.filter2D(template, -1, kernel)
        variations.append(sharpened)

        return variations

    async def _calculate_template_quality(
        self,
        template: np.ndarray,
        features: TemplateFeatures
    ) -> float:
        """Calculate quality score for generated template"""
        score = 1.0

        # Check if features are well-distributed (not blank)
        if features.hog_features.std() < 0.01:
            score *= 0.7

        if features.lbp_features.std() < 0.01:
            score *= 0.7

        # Check if template has enough edges/details
        if features.edge_map is not None:
            edge_density = features.edge_map.mean()
            if edge_density < 0.1:  # Too few edges
                score *= 0.8
            elif edge_density > 0.8:  # Too many edges (noise)
                score *= 0.9

        # Check size
        if template.shape[0] < 16 or template.shape[1] < 16:
            score *= 0.8

        return min(score, 1.0)

    async def _load_from_cache(self, target: str) -> Optional[GeneratedTemplate]:
        """Load template from cache"""
        cache_file = self.cache_dir / f"{target}.pkl"

        if not cache_file.exists():
            return None

        try:
            loop = asyncio.get_event_loop()

            def _load():
                with open(cache_file, 'rb') as f:
                    return pickle.load(f)

            template = await loop.run_in_executor(self.executor, _load)

            # Update memory tracking
            template_size_mb = template.template.nbytes / (1024 * 1024)
            self.current_memory_mb += template_size_mb

            return template

        except Exception as e:
            logger.warning(f"[TEMPLATE GEN] Failed to load cache: {e}")
            return None

    async def _save_to_cache(self, target: str, template: GeneratedTemplate):
        """Save template to cache"""
        cache_file = self.cache_dir / f"{target}.pkl"

        try:
            loop = asyncio.get_event_loop()

            def _save():
                with open(cache_file, 'wb') as f:
                    pickle.dump(template, f)

            await loop.run_in_executor(self.executor, _save)

            logger.debug(f"[TEMPLATE GEN] Template cached: {cache_file}")

        except Exception as e:
            logger.warning(f"[TEMPLATE GEN] Failed to save cache: {e}")

    def cleanup(self):
        """Cleanup resources"""
        self.executor.shutdown(wait=False)
        logger.info("[TEMPLATE GEN] Resources cleaned up")


# Singleton instance
_generator_instance: Optional[MLTemplateGenerator] = None


def get_ml_template_generator(config: Optional[Dict[str, Any]] = None) -> MLTemplateGenerator:
    """Get singleton ML template generator instance"""
    global _generator_instance

    if _generator_instance is None:
        if config is None:
            config = {
                'max_memory_mb': 500,
                'cache_dir': Path.home() / '.jarvis' / 'template_cache'
            }
        _generator_instance = MLTemplateGenerator(config)

    return _generator_instance
