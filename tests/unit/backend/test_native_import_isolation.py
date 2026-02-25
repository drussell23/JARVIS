# tests/unit/backend/test_native_import_isolation.py
"""Tests verifying native C extension libraries are NOT imported at module level.

Root cause (v270.0): Module-level `import torch`, `import whisper`, `import
sounddevice` in voice_engine.py and centralized_model_manager.py triggered
native C extension loading (BLAS, LLVM, PortAudio) during
parallel_import_components(). This collided with the already-running CoreAudio
IO thread from AudioBus → SIGSEGV on the audio callback thread.

Fix: All native imports deferred to first real use via lazy-init functions.
These tests verify that importing the modules does NOT trigger heavy native
library loading.
"""
import ast
import sys
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Native modules that must NEVER be imported at module level in these files.
# Module-level import triggers native C extension loading which can collide
# with PortAudio/CoreAudio if AudioBus is already running.
DANGEROUS_MODULES = frozenset({
    "sounddevice", "torch", "whisper", "numba", "librosa",
    "pyaudio", "pyttsx3", "scipy", "torchaudio",
})


def _collect_module_level_imports(filepath: str) -> list:
    """Parse a Python file and return all module-level import names."""
    with open(filepath) as f:
        tree = ast.parse(f.read(), filename=filepath)

    imports = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append((node.lineno, node.module))
    return imports


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNativeImportIsolation:
    """Verify no module-level native imports in critical startup files."""

    def test_centralized_model_manager_no_native_imports(self):
        """centralized_model_manager.py must not import torch/whisper at module level."""
        imports = _collect_module_level_imports(
            "backend/utils/centralized_model_manager.py"
        )
        violations = [
            (line, name) for line, name in imports
            if any(name.startswith(d) for d in DANGEROUS_MODULES)
        ]
        assert violations == [], (
            f"Module-level native imports found in centralized_model_manager.py: {violations}"
        )

    def test_voice_engine_no_native_imports(self):
        """voice_engine.py must not import sounddevice/torch/whisper at module level."""
        imports = _collect_module_level_imports(
            "backend/engines/voice_engine.py"
        )
        violations = [
            (line, name) for line, name in imports
            if any(name.startswith(d) for d in DANGEROUS_MODULES)
        ]
        assert violations == [], (
            f"Module-level native imports found in voice_engine.py: {violations}"
        )

    def test_centralized_model_manager_import_clean(self):
        """Importing centralized_model_manager must not pull in torch/whisper."""
        before = set(sys.modules.keys())

        # Ensure backend is on path
        import importlib
        sys.path.insert(0, "backend")
        try:
            # Force fresh import
            mod_name = "utils.centralized_model_manager"
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            importlib.import_module(mod_name)
        finally:
            sys.path.pop(0)

        after = set(sys.modules.keys())
        new_modules = after - before

        dangerous_loaded = [
            m for m in new_modules
            if any(m.startswith(d) for d in DANGEROUS_MODULES)
        ]
        assert dangerous_loaded == [], (
            f"centralized_model_manager triggered native imports: {dangerous_loaded}"
        )

    def test_supervisor_numba_preload_before_audiobus(self):
        """Supervisor must preload numba BEFORE AudioBus init in _startup_impl."""
        with open("unified_supervisor.py") as f:
            source = f.read()

        # Find positions
        numba_preload_pos = source.find("NUMBA / NATIVE LIBRARY PRELOAD")
        audiobus_init_pos = source.find("AUDIO BUS EARLY INIT")

        assert numba_preload_pos > 0, "numba preload section not found in supervisor"
        assert audiobus_init_pos > 0, "AudioBus init section not found in supervisor"
        assert numba_preload_pos < audiobus_init_pos, (
            f"numba preload (pos {numba_preload_pos}) must come BEFORE "
            f"AudioBus init (pos {audiobus_init_pos}) in unified_supervisor.py"
        )
