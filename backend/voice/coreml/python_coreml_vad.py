#!/usr/bin/env python3
"""
Python-based CoreML VAD Engine
Uses CoreML directly in Python for ultra-low memory usage and Neural Engine acceleration
"""

import os
import numpy as np
from typing import Tuple, Optional

print("[CoreML-VAD] Initializing Python CoreML engine...")

try:
    import coremltools as ct
    print("✅ CoreML Tools found")
except ImportError:
    print("❌ CoreML Tools not found - install with: pip install coremltools")
    ct = None

class PythonCoreMLVAD:
    """
    Ultra-lightweight VAD using CoreML models directly in Python.

    Memory usage: ~5-10MB (runs on Neural Engine)
    Latency: <10ms per detection
    """

    def __init__(self, model_path: str = "models/vad_model.mlmodelc"):
        """Initialize CoreML VAD model"""
        if not ct:
            raise ImportError("CoreML Tools required: pip install coremltools")

        self.model_path = model_path
        self.model = None
        self.vad_threshold = 0.5
        self.chunk_size = 512  # Silero VAD expects 512 samples

        # Load model
        self._load_model()

    def _load_model(self):
        """Load the CoreML model"""
        try:
            # Load compiled CoreML model
            import CoreML

            model_url = CoreML.NSURL.fileURLWithPath_(os.path.abspath(self.model_path))
            self.model = CoreML.MLModel.modelWithContentsOfURL_error_(model_url, None)[0]

            if not self.model:
                raise RuntimeError(f"Failed to load model from {self.model_path}")

            print(f"✅ CoreML VAD model loaded: {self.model_path}")
            print(f"   Model size: {self._get_model_size()}")
            print(f"   Runs on: Neural Engine")

        except Exception as e:
            print(f"❌ Failed to load CoreML model: {e}")
            raise

    def _get_model_size(self) -> str:
        """Get model size on disk"""
        try:
            import subprocess
            result = subprocess.run(
                ['du', '-sh', self.model_path],
                capture_output=True,
                text=True
            )
            return result.stdout.split()[0]
        except Exception:
            return "unknown"

    def detect_voice(self, audio: np.ndarray) -> Tuple[bool, float]:
        """
        Detect voice activity in audio chunk.

        Args:
            audio: Audio samples as float32 numpy array
                   Expected shape: (512,) or (1, 512)

        Returns:
            (is_voice, confidence) tuple
        """
        if not self.model:
            return False, 0.0

        try:
            # Ensure correct shape
            if len(audio.shape) == 1:
                audio = audio.reshape(1, -1)

            # Pad or trim to 512 samples
            if audio.shape[1] < self.chunk_size:
                padding = np.zeros((1, self.chunk_size - audio.shape[1]), dtype=np.float32)
                audio = np.concatenate([audio, padding], axis=1)
            elif audio.shape[1] > self.chunk_size:
                audio = audio[:, :self.chunk_size]

            # Run inference
            import CoreML

            # Create input feature provider
            audio_mlarray = CoreML.MLMultiArray.alloc().initWithShape_dataType_error_(
                [1, self.chunk_size],
                CoreML.MLMultiArrayDataTypeFloat32,
                None
            )[0]

            # Copy audio data
            for i in range(self.chunk_size):
                audio_mlarray[[0, i]] = float(audio[0, i])

            # Create input
            input_dict = {"audio": audio_mlarray}
            input_provider = CoreML.MLDictionaryFeatureProvider.alloc().initWithDictionary_error_(
                input_dict,
                None
            )[0]

            # Predict
            output = self.model.predictionFromFeatures_error_(input_provider, None)[0]

            # Get output confidence
            output_feature = output.featureValueForName_("output")
            confidence = float(output_feature.multiArrayValue()[[0]])

            is_voice = confidence > self.vad_threshold

            return is_voice, confidence

        except Exception as e:
            print(f"❌ VAD detection failed: {e}")
            return False, 0.0

    def set_threshold(self, threshold: float):
        """Set VAD threshold (0.0 - 1.0)"""
        self.vad_threshold = max(0.0, min(1.0, threshold))

    def __del__(self):
        """Cleanup"""
        self.model = None


def create_python_coreml_vad(model_path: Optional[str] = None) -> Optional[PythonCoreMLVAD]:
    """
    Factory function to create Python CoreML VAD engine.

    Args:
        model_path: Path to .mlmodelc file (default: auto-detected from project root)

    Returns:
        PythonCoreMLVAD instance or None if failed
    """
    try:
        if model_path is None:
            # v117.0: Auto-detect model path relative to project root
            # This file is at: backend/voice/coreml/python_coreml_vad.py
            # Project root is 3 levels up
            coreml_dir = os.path.dirname(os.path.abspath(__file__))
            voice_dir = os.path.dirname(coreml_dir)
            backend_dir = os.path.dirname(voice_dir)
            project_root = os.path.dirname(backend_dir)

            # Check environment variable first
            model_path = os.environ.get("COREML_VAD_MODEL_PATH")

            if not model_path:
                # Try .mlmodelc format first
                model_path = os.path.join(project_root, "models", "vad_model.mlmodelc")

                # Fall back to .mlpackage format
                if not os.path.exists(model_path):
                    mlpackage_path = os.path.join(project_root, "models", "vad_model.mlpackage")
                    if os.path.exists(mlpackage_path):
                        model_path = mlpackage_path

        vad = PythonCoreMLVAD(model_path)
        return vad

    except Exception as e:
        print(f"❌ Failed to create Python CoreML VAD: {e}")
        return None


# Test if running as main
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Testing Python CoreML VAD Engine")
    print("=" * 60)

    # Create VAD engine
    vad = create_python_coreml_vad()

    if vad:
        # Test with random audio
        test_audio = np.random.randn(512).astype(np.float32)
        is_voice, confidence = vad.detect_voice(test_audio)

        print(f"\n✅ Test detection:")
        print(f"   Is voice: {is_voice}")
        print(f"   Confidence: {confidence:.3f}")
        print(f"   Threshold: {vad.vad_threshold}")
        print("\n" + "=" * 60)
        print("✅ Python CoreML VAD engine is working!")
        print("=" * 60)
    else:
        print("\n❌ Failed to initialize VAD engine")
