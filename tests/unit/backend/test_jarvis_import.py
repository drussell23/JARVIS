#!/usr/bin/env python3
"""Manual JARVIS import-chain diagnostic.

Run directly: ``python3 tests/unit/backend/test_jarvis_import.py``

NOT a pytest test — the entire body is ``__main__``-guarded. History
(a1-brain-20260706-014931 live-fire): the old module-level body did
``import torch`` + ``sys.exit(1)`` at import time; pytest collected the
file (``test_`` name) and its mainloop caught the SystemExit mid-
collection → INTERNALERROR rc=3 → the WHOLE suite aborted byte-
identically on every run on any torch-less host (the lean brain venv).
That silenced the A1 vector's FAILED line from every full-suite poll and
starved the TestFailureSensor's stability streak. A diagnostic script
must be import-inert; pytest safety is spine-pinned by
``test_import_diag_pytest_safe.py``.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    # Set environment variables
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    os.environ['USE_TORCH'] = '1'
    os.environ['USE_TF'] = '0'

    print("Testing JARVIS import chain...")
    print("=" * 50)

    # Test 1: Basic imports
    try:
        import torch
        import torchaudio
        print("✅ PyTorch and torchaudio imported successfully")
        print(f"   - torch version: {torch.__version__}")
        print(f"   - torchaudio version: {torchaudio.__version__}")
    except Exception as e:
        print(f"❌ Failed to import PyTorch/torchaudio: {e}")
        return 1

    # Test 2: ML Enhanced Voice System
    try:
        from backend.voice.ml_enhanced_voice_system import MLEnhancedVoiceSystem
        print("✅ ML Enhanced Voice System imported successfully")
    except Exception as e:
        print(f"❌ Failed to import ML Enhanced Voice System: {e}")

    # Test 3: JARVIS Voice
    try:
        from backend.voice.jarvis_voice import EnhancedJARVISVoiceAssistant
        print("✅ JARVIS Voice imported successfully")
    except Exception as e:
        print(f"❌ Failed to import JARVIS Voice: {e}")

    # Test 4: JARVIS API
    try:
        from backend.api.jarvis_voice_api import JARVISVoiceAPI
        print("✅ JARVIS Voice API imported successfully")
    except Exception as e:
        print(f"❌ Failed to import JARVIS Voice API: {e}")

    # Test 5: Initialize JARVIS
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            jarvis_api = JARVISVoiceAPI()
            print("✅ JARVIS Voice API initialized successfully")
            print(f"   - JARVIS available: {jarvis_api.jarvis_available}")
        else:
            print("⚠️  ANTHROPIC_API_KEY not set, skipping initialization test")
    except Exception as e:
        print(f"❌ Failed to initialize JARVIS Voice API: {e}")

    print("\nImport chain test complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
