"""
JARVIS Central Lazy Import Registry
====================================

This module provides pre-configured LazyProxy instances for heavy modules
that are commonly used across JARVIS. By importing from here instead of
directly importing heavy modules, startup time is dramatically reduced.

All proxies are JIT-loaded - they only incur import cost when first accessed.

Usage:
    # Instead of:
    import torch
    import numpy as np
    from transformers import AutoModel

    # Use:
    from backend.core.lazy_imports import torch, numpy as np, transformers

    # The modules are NOT loaded until you actually use them:
    tensor = torch.zeros(10)  # NOW torch loads

Version: 1.0.0 - Hyper-Speed Startup Edition
"""
from __future__ import annotations

from backend.core.lazy_proxy import LazyProxy, lazy_import

# =============================================================================
# ML/AI Heavy Modules (typically 2-8 seconds each)
# =============================================================================

# PyTorch - The big one (2-3 seconds)
torch = LazyProxy("torch", preload_hint=True)

# NumPy - Used everywhere (0.5-1 second)
numpy = LazyProxy("numpy", preload_hint=True)

# Transformers - Hugging Face (3-5 seconds)
transformers = LazyProxy("transformers", preload_hint=True)

# TensorFlow - If used (3-8 seconds)
tensorflow = LazyProxy("tensorflow", preload_hint=False)

# SciPy - Scientific computing (1-2 seconds)
scipy = LazyProxy("scipy", preload_hint=False)

# Pandas - Data manipulation (1-2 seconds)
pandas = LazyProxy("pandas", preload_hint=False)

# Scikit-learn - ML utilities (1-2 seconds)
sklearn = LazyProxy("sklearn", preload_hint=False)


# =============================================================================
# Audio/Vision Processing (1-3 seconds each)
# =============================================================================

# OpenCV - Image processing
cv2 = LazyProxy("cv2", preload_hint=False)

# PIL/Pillow - Image handling
PIL = LazyProxy("PIL", preload_hint=False)

# Librosa - Audio processing
librosa = LazyProxy("librosa", preload_hint=False)

# Sounddevice - Audio I/O
sounddevice = LazyProxy("sounddevice", preload_hint=False)

# Torchaudio - PyTorch audio
torchaudio = LazyProxy("torchaudio", preload_hint=False)


# =============================================================================
# NLP/LLM Libraries (1-4 seconds each)
# =============================================================================

# LangChain - LLM orchestration
langchain = LazyProxy("langchain", preload_hint=False)

# OpenAI - API client
openai = LazyProxy("openai", preload_hint=False)

# Anthropic - Claude API client
anthropic = LazyProxy("anthropic", preload_hint=True)

# Tiktoken - Token counting
tiktoken = LazyProxy("tiktoken", preload_hint=False)

# Sentence Transformers - Embeddings
sentence_transformers = LazyProxy("sentence_transformers", preload_hint=False)


# =============================================================================
# Database/Storage (0.5-2 seconds each)
# =============================================================================

# ChromaDB - Vector database
chromadb = LazyProxy("chromadb", preload_hint=False)

# SQLAlchemy - ORM
sqlalchemy = LazyProxy("sqlalchemy", preload_hint=False)

# Redis - Caching
redis = LazyProxy("redis", preload_hint=False)


# =============================================================================
# Web/API Libraries (0.3-1 second each)
# =============================================================================

# FastAPI - Web framework
fastapi = LazyProxy("fastapi", preload_hint=False)

# Uvicorn - ASGI server
uvicorn = LazyProxy("uvicorn", preload_hint=False)

# Requests - HTTP client (aiohttp is lighter, usually prefer that)
requests = LazyProxy("requests", preload_hint=False)


# =============================================================================
# JARVIS Internal Heavy Modules
# =============================================================================

def get_lazy_neural_mesh_coordinator():
    """Lazy loader for NeuralMeshCoordinator (heavy)."""
    return LazyProxy(
        "backend.neural_mesh.neural_mesh_coordinator",
        "NeuralMeshCoordinator",
        singleton=True,
        preload_hint=True
    )


def get_lazy_jarvis_bridge():
    """Lazy loader for JARVISBridge (heavy)."""
    return LazyProxy(
        "backend.neural_mesh.jarvis_bridge",
        "JARVISBridge",
        singleton=True,
        preload_hint=True
    )


def get_lazy_vision_engine():
    """Lazy loader for VisionEngine (heavy, uses torch/cv2)."""
    return LazyProxy(
        "backend.vision_engine.core",
        "VisionEngine",
        singleton=True,
        preload_hint=False
    )


def get_lazy_visual_monitor_agent():
    """Lazy loader for VisualMonitorAgent (medium)."""
    return LazyProxy(
        "backend.neural_mesh.agents.visual_monitor_agent",
        "VisualMonitorAgent",
        singleton=False,
        preload_hint=False
    )


def get_lazy_speaker_verification():
    """Lazy loader for SpeakerVerificationService (heavy, uses ML models)."""
    return LazyProxy(
        "backend.services.speaker_verification_service",
        "SpeakerVerificationService",
        singleton=True,
        preload_hint=False
    )


def get_lazy_agi_os():
    """Lazy loader for AGI OS coordinator (medium)."""
    return LazyProxy(
        "backend.agi_os",
        "get_agi_os",
        singleton=True,
        preload_hint=False
    )


# =============================================================================
# Convenience aliases for common patterns
# =============================================================================

# Alias for numpy (common convention)
np = numpy

# Common ML stack in one go
def get_ml_stack():
    """Get lazy proxies for the common ML stack (torch, numpy, transformers)."""
    return {
        "torch": torch,
        "numpy": numpy,
        "np": numpy,  # Alias
        "transformers": transformers,
    }


# =============================================================================
# Bulk Preload Functions
# =============================================================================

async def preload_ml_stack(concurrency: int = 2):
    """
    Preload the core ML stack in background.

    Call this during idle time to warm up ML modules before user interaction.
    """
    from backend.core.lazy_proxy import preload_proxies

    ml_proxies = [torch, numpy, transformers]
    return await preload_proxies(ml_proxies, concurrency=concurrency)


async def preload_audio_stack(concurrency: int = 2):
    """Preload audio processing modules."""
    from backend.core.lazy_proxy import preload_proxies

    audio_proxies = [librosa, sounddevice, torchaudio]
    return await preload_proxies(audio_proxies, concurrency=concurrency)


async def preload_all_hints(concurrency: int = 4):
    """Preload all modules marked with preload_hint=True."""
    from backend.core.lazy_proxy import preload_proxies

    return await preload_proxies(concurrency=concurrency)


# =============================================================================
# Stats and Diagnostics
# =============================================================================

def get_import_stats():
    """Get statistics about lazy import usage."""
    from backend.core.lazy_proxy import get_lazy_proxy_stats
    return get_lazy_proxy_stats()


def print_import_report():
    """Print a formatted report of lazy import usage."""
    stats = get_import_stats()

    print("\n" + "=" * 60)
    print("  JARVIS Lazy Import Report")
    print("=" * 60)
    print(f"  Total proxies: {stats['total_proxies']}")
    print(f"  Loaded: {stats['loaded_proxies']}")
    print(f"  Pending: {stats['pending_proxies']}")
    print(f"  Total load time: {stats['total_load_time_ms']:.1f}ms")
    print()

    if stats['slowest_loads']:
        print("  Slowest loads:")
        for module, time_ms in stats['slowest_loads'].items():
            print(f"    • {module}: {time_ms:.1f}ms")

    print()
    if stats['load_order']:
        print("  Load order:")
        for i, module in enumerate(stats['load_order'], 1):
            print(f"    {i}. {module}")

    print("=" * 60 + "\n")
