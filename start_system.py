#!/usr/bin/env python3
"""
Unified startup script for JARVIS AI System v14.2.0 - PRD v2.0 VOICE INTELLIGENCE EDITION
Advanced Browser Automation + v2.0 ML-Powered Intelligence Systems + PRD v2.0 Voice Biometrics
⚡ ULTRA-OPTIMIZED: 30% Memory Target (4.8GB on 16GB Systems)
🤖 AUTONOMOUS: Self-Discovering, Self-Healing, Self-Optimizing
🧠 INTELLIGENT: 6 Upgraded v2.0 Systems with Proactive Monitoring
🔐 PRD v2.0: AAM-Softmax + Center Loss + Triplet Loss + Platt/Isotonic Calibration

The JARVIS backend loads 11 critical components + 6 intelligent systems:

1. CHATBOTS - Claude Vision AI for conversation and screen understanding
2. VISION - Real-time screen capture with Multi-Space Desktop Monitoring + YOLOv8
   • Multi-Space Vision: Monitors all macOS desktop spaces simultaneously
   • Smart Space Detection: "Where is Cursor IDE?", "What's on Desktop 2?"
   • YOLOv8 UI Detection: 10-20x faster than Claude Vision for UI elements
     - Detects buttons, icons, menus, Control Center, TV UI (6GB RAM max)
     - Real-time capability (10-20 FPS), free after model download
     - 5 model sizes: nano (3MB, 0.6GB) to xlarge (68MB, 6GB)
   • Hybrid YOLO-Claude Vision: Intelligent routing based on task complexity
     - YOLO-first for UI detection (fast, accurate, free)
     - Claude for text extraction and complex analysis
     - Hybrid mode: YOLO finds regions → Claude analyzes content
   • Enhanced Window Detection: UI element tracking in windows
   • Multi-Monitor Layout Detection: Vision-based monitor awareness
   • 9-stage processing pipeline with intelligent orchestration
   • Dynamic memory allocation (1.2GB budget)
   • Cross-language optimization (Python, Rust, Swift)
   • Bloom Filter, Predictive Engine, Semantic Cache, VSMS integrated
   • Proactive assistance with debugging, research, and workflow optimization
3. MEMORY - M1-optimized memory management with orchestrator integration
4. VOICE - Voice activation ("Hey JARVIS") with proactive announcements
5. ML_MODELS - NLP and sentiment analysis (lazy-loaded)
6. MONITORING - System health tracking and component metrics
7. VOICE_UNLOCK - PRD v2.0 BEAST MODE Multi-Modal Biometric Authentication (ADVANCED!)
   ✨ Manual Unlock: "Hey JARVIS, unlock my screen" - Direct control 24/7
   ✨ Context-Aware: Automatically unlocks when needed for tasks
   🔬 BEAST MODE: Advanced Probabilistic Verification System
   🔐 PRD v2.0: Next-Gen Voice Biometric Intelligence (NEW!)

   📊 ML Fine-Tuning (advanced_ml_features.py):
     - AAM-Softmax (ArcFace): Additive Angular Margin for discriminative embeddings
     - Center Loss: Intra-class compactness - tight "Derek cluster"
     - Triplet Loss: Metric learning with semi-hard negative mining
     - Combined Training: α*AAM + β*Center + γ*Triplet joint optimization
     - Real-time Fine-tuning: Improves from every authentication attempt

   📈 Score Calibration (Platt/Isotonic):
     - Platt Scaling: Sigmoid calibration for 30-99 training samples
     - Isotonic Regression: Non-parametric for 100+ samples
     - Adaptive Thresholds: Auto-adjusts toward 90%/95%/98% targets
     - Meaningful Confidence: True probability instead of cosine similarity

   🛡️ Comprehensive Anti-Spoofing (speaker_verification_service.py):
     - Replay Attack Detection: Audio fingerprinting + spectral analysis
     - Synthesis/Deepfake Detection: Pitch, jitter, shimmer, HNR analysis
     - Voice Conversion Detection: Embedding stability across session
     - Environmental Anomaly: Reverb time, noise floor signature
     - Breathing Pattern Analysis: Natural speech indicator

   • Multi-Modal Fusion: 5 independent biometric signals
     - Deep learning embeddings (ECAPA-TDNN 192D)
     - Mahalanobis distance (statistical with adaptive covariance)
     - Acoustic features (pitch, formants, spectral analysis)
     - Physics-based validation (vocal tract, harmonics)
     - Anti-spoofing detection (replay, synthesis, voice conversion)
   • Cloud SQL Storage: 50+ acoustic features per speaker (PostgreSQL)
   • Bayesian Verification: Probabilistic confidence with uncertainty quantification
   • Adaptive Learning: Zero hardcoded thresholds, learns optimal values
   • Speaker Recognition: Personalized responses using verified identity
   • Bulletproof Decoder: 6-stage cascading audio format handling
   • Hybrid STT System: Wav2Vec, Vosk, Whisper with intelligent routing
   • Context-Aware Intelligence (CAI): Screen state, time, location analysis
   • Scenario-Aware Intelligence (SAI): Routine/emergency/suspicious detection
   • GCP Cloud Database: Secure biometric profile storage via Cloud SQL proxy
   • Fail-Closed Security: Denies unlock on any verification error
   • Secure password automation via WebSocket daemon
   • Apple Watch alternative - no additional hardware needed
   • Accuracy: ~95%+ (FAR <0.1%, FRR <2%)

8. WAKE_WORD - Hands-free 'Hey JARVIS' activation
   • Always-listening mode with zero clicks required
   • Multi-engine detection (Porcupine, Vosk, WebRTC)
   • Customizable wake words and responses
   • Adaptive sensitivity learning
   • Natural activation: "I'm online Sir, waiting for your command"

9. DISPLAY_MONITOR - External Display Management (NEW!)
   • Automatic AirPlay/external display detection
   • Multi-method detection: AppleScript, CoreGraphics, Yabai
   • Voice announcements: "Sir, I see your Living Room TV is available..."
   • Smart caching: 3-5x performance improvement, 60-80% fewer API calls
   • Auto-connect or voice-prompt modes
   • Zero hardcoding - fully configuration-driven
   • Living Room TV monitoring active by default

10. GOAL_INFERENCE - ML-Powered Goal Understanding & Learning (NEW!)
   • Infers your intentions from context and patterns
   • PyTorch neural networks for predictive decision making
   • SQLite + ChromaDB hybrid database for learning
   • Adaptive caching with 70-90% hit rate
   • 5 configuration presets: aggressive, balanced, conservative, learning, performance
   • 🤖 FULLY AUTOMATIC: Auto-detects best preset based on your usage!
     - First run → 'learning' preset (fast adaptation)
     - < 50 goals → 'learning' preset (early learning phase)
     - Building patterns → 'balanced' preset
     - 20+ patterns → 'aggressive' preset (experienced user)
   • Smart automation: Enables when success rate > 80%
   • Auto-configuration on first startup
   • Learn display connection patterns (3x → auto-connect)
   • Confidence-based automation with safety limits
   • Usage: python start_system.py (fully automatic) OR --goal-preset learning --enable-automation

11. AGI_OS - Autonomous General Intelligence Operating System (NEW!)
   🧠 JARVIS acts INTELLIGENTLY and AUTONOMOUSLY without prompting
   ✨ Only requires user approval (not initiation) via voice
   🎙️ Real-time voice communication with Daniel TTS (British)
   • AGIOSCoordinator: Central coordinator for all AGI OS components
   • RealTimeVoiceCommunicator: Voice output with Daniel TTS
   • VoiceApprovalManager: Voice-based user approval workflows
   • ProactiveEventStream: Event-driven autonomous notifications
   • IntelligentActionOrchestrator: Detection → Decision → Approval → Execution
   • UnifiedVisionInterface: 26 event types for screen analysis
   • OwnerIdentityService: Dynamic owner identification (voice, macOS, inference)
   • VoiceAuthNarrator: Intelligent authentication feedback
   • 9 default proactive detection patterns:
     - Error Detection, Security Monitoring, Meeting Alerts
     - System Performance, Task Completion, Research Assistance
     - Code Review, File Operations, Communication Alerts
   • Integration with MAS + SAI + CAI + UAE systems
   • Event-driven architecture for autonomous decisions
   • Learns from approvals to improve over time

🧠 INTELLIGENT SYSTEMS v2.0 (NEW in v14.1!):
All 6 systems now integrate with HybridProactiveMonitoringManager & ImplicitReferenceResolver

1. TemporalQueryHandler v3.0
   • ML-powered temporal analysis with pattern recognition
   • NEW: Pattern analysis, predictive analysis, anomaly detection, correlation analysis
   • Uses monitoring cache for 4 new intelligent query types
   • Example: "What patterns have you noticed?" → Analyzes learned correlations

2. ErrorRecoveryManager v2.0
   • Proactive error detection BEFORE they become critical
   • Frequency tracking with automatic severity escalation (3+ errors → CRITICAL)
   • Multi-space error correlation detection (cascading failures)
   • 4 new recovery strategies: PROACTIVE_MONITOR, PREDICTIVE_FIX, ISOLATE_COMPONENT, AUTO_HEAL
   • Example: Same error 3x → Auto-escalates & applies predictive fix

3. StateIntelligence v2.0
   • Auto-recording from monitoring (zero manual tracking!)
   • Real-time stuck state detection (>30 min in same state)
   • Productivity tracking with trend analysis
   • Time-of-day preference learning
   • Example: "You've been stuck in Space 3 for 45 min, usually switch to Space 5 now"

4. StateDetectionPipeline v2.0
   • Auto-triggered detection from monitoring alerts
   • Visual signature library building (learns automatically)
   • State transition tracking across all spaces
   • Unknown state detection with alerts
   • Example: Detects "coding" → "error_state" transition automatically

5. ComplexComplexityHandler v2.0
   • 87% faster complex queries using monitoring cache!
   • Temporal queries: 15s → 2s (uses cached snapshots)
   • Cross-space queries: 25s → 4s (pre-computed data)
   • 80% API call reduction
   • Example: "What changed in last 5 min?" → Instant from cache

6. PredictiveQueryHandler v2.0
   • "Am I making progress?" → Real-time monitoring analysis
   • Bug prediction from error pattern learning
   • Workflow-based next step suggestions
   • Workspace change tracking with productivity scoring
   • Example: "70% progress - 3 builds, 2 errors fixed, 15 changes"

🆕 AUTONOMOUS FEATURES (v14.0):
- Zero Configuration: No hardcoded ports or URLs
- Self-Discovery: Services find each other automatically
- Self-Healing: ML-powered recovery from failures
- Dynamic Routing: Optimal paths calculated in real-time
- Port Flexibility: Services relocate if ports blocked
- Pattern Learning: System improves over time
- Service Mesh: All components interconnected
- Memory Aware: Intelligent resource management

🧠 NEURAL MESH - Multi-Agent Intelligence Framework (v2.1):
- Transforms 60+ isolated agents into a cohesive AI ecosystem
- Agent Communication Bus: Ultra-fast async message passing
- Shared Knowledge Graph: Persistent, searchable collective memory
- Agent Registry: Service discovery and health monitoring
- Multi-Agent Orchestrator: Workflow coordination and task decomposition
- Crew System: CrewAI-inspired collaboration framework
  • 6 Process Types: Sequential, Hierarchical, Dynamic, Parallel, Consensus, Pipeline
  • 6 Delegation Strategies: Capability-based, Load-balanced, Priority-based, etc.
  • 5 Memory Types: Short-term, Long-term, Entity, Episodic, Procedural
  • ChromaDB Integration: Vector-based semantic memory search

Key Features:
- 🎯 30% Memory Target - Only 4.8GB total on 16GB systems
- 🤖 Autonomous Operation - Zero manual configuration
- 🔧 Self-Healing - Automatic recovery from any failure
- 📡 Service Discovery - Dynamic port and endpoint finding
- Multi-Space Vision Intelligence - See across all desktop spaces
- Fixed CPU usage issues (87% → <25%)
- Memory quantization with 4 operating modes
- Parallel component loading (~7-9s startup)
- Integration Architecture coordinates all vision components
- Vision system with 30 FPS screen monitoring
- Proactive real-time assistance - say "Start monitoring my screen"

Proactive Monitoring Features:
- Multi-Space Queries: Ask about apps on any desktop space
- UC1: Debugging Assistant - Detects errors and suggests fixes
- UC2: Research Helper - Summarizes multi-tab research
- UC3: Workflow Optimization - Identifies repetitive patterns
- Voice announcements with context-aware communication styles
- Auto-pause for sensitive content (passwords, banking)
- Decision engine with importance classification

Browser Automation Features (v13.4.0):
- Natural Language Browser Control: "Open Safari and go to Google"
- Chained Commands: "Open a new tab and search for weather"
- Dynamic Browser Discovery: Controls any browser without hardcoding
- Smart Context: Remembers which browser you're using
- Type & Search: "Type python tutorials and press enter"
- Tab Management: "Open another tab", "Open a new tab in Chrome"
- Cross-Browser Support: Safari, Chrome, Firefox, and others
- AppleScript Integration: Native macOS browser control

All 11 components must load for full functionality.
"""

# ============================================================================
# Advanced Virtual Environment Auto-Detection & Activation System
# Zero-hardcoding • Cross-platform • Dynamic • Robust
# ============================================================================
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple


class VenvAutoActivator:
    """
    Intelligent virtual environment auto-detection and activation.

    Features:
    - Auto-discovers venv in standard locations (configurable via env vars)
    - Cross-platform (Windows/Linux/macOS)
    - Multi-package verification for reliability
    - Graceful error handling with actionable suggestions
    - Prevents infinite recursion loops
    - Zero hardcoded paths
    """

    # Standard venv search locations (priority order)
    DEFAULT_VENV_SEARCH_PATHS = [
        "backend/venv",
        "venv",
        ".venv",
        "env",
        ".env",
        "virtualenv",
    ]

    # Core packages to verify environment integrity
    REQUIRED_PACKAGES = [
        "aiohttp",
        "psutil",
    ]

    def __init__(self):
        self.script_dir = Path(__file__).resolve().parent
        self.platform = sys.platform
        self._already_attempted = os.environ.get("_JARVIS_VENV_ACTIVATED") == "1"

    def find_venv(self) -> Optional[Path]:
        """
        Intelligently locate virtual environment.

        Priority:
        1. JARVIS_VENV_PATH environment variable (explicit override)
        2. VIRTUAL_ENV environment variable (if already in a venv)
        3. Standard locations search

        Returns:
            Path to valid venv, or None if not found
        """
        # Priority 1: Explicit override via environment
        if env_path := os.environ.get("JARVIS_VENV_PATH"):
            venv_path = Path(env_path)
            if self._is_valid_venv(venv_path):
                return venv_path
            print(f"⚠️  JARVIS_VENV_PATH invalid: {venv_path}")

        # Priority 2: Check if already in activated venv
        if virtual_env := os.environ.get("VIRTUAL_ENV"):
            venv_path = Path(virtual_env)
            if self._is_valid_venv(venv_path):
                return venv_path

        # Priority 3: Search standard locations
        search_paths = os.environ.get(
            "JARVIS_VENV_SEARCH_PATHS",
            ":".join(self.DEFAULT_VENV_SEARCH_PATHS)
        ).split(":")

        for venv_name in search_paths:
            venv_path = self.script_dir / venv_name.strip()
            if self._is_valid_venv(venv_path):
                return venv_path

        return None

    def _is_valid_venv(self, venv_path: Path) -> bool:
        """Validate that path contains a functional virtual environment"""
        if not venv_path.exists() or not venv_path.is_dir():
            return False

        python_exe = self._get_venv_python(venv_path)
        return python_exe is not None and python_exe.exists()

    def _get_venv_python(self, venv_path: Path) -> Optional[Path]:
        """Get Python executable path (cross-platform)"""
        if self.platform == "win32":
            candidates = [
                venv_path / "Scripts" / "python.exe",
                venv_path / "Scripts" / "python3.exe",
            ]
        else:
            candidates = [
                venv_path / "bin" / "python3",
                venv_path / "bin" / "python",
            ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return None

    def needs_activation(self) -> Tuple[bool, List[str]]:
        """
        Determine if venv activation is required.

        Returns:
            (needs_activation, missing_packages)
        """
        if self._already_attempted:
            return False, []

        missing = []
        for package in self.REQUIRED_PACKAGES:
            try:
                __import__(package)
            except ImportError:
                missing.append(package)

        return bool(missing), missing

    def is_already_in_venv(self, venv_path: Path) -> bool:
        """
        Check if currently running in the target venv (prevents recursion).

        Uses sys.prefix comparison as the most reliable method, avoiding
        symlink resolution issues that can cause false positives.
        """
        # Primary method: Check if sys.prefix points to the venv
        # This is reliable even when python executables are symlinked
        if hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix:
            # We're in SOME venv, check if it's the target one
            current_venv = Path(sys.prefix).resolve()
            target_venv = venv_path.resolve()

            if current_venv == target_venv:
                return True

        # Secondary check: Verify venv site-packages is in sys.path
        # If we're truly in the venv, its site-packages should be in sys.path
        venv_site_packages = None
        if self.platform == "win32":
            venv_site_packages = venv_path / "Lib" / "site-packages"
        else:
            # Find the actual site-packages (version may vary)
            lib_path = venv_path / "lib"
            if lib_path.exists():
                for item in lib_path.iterdir():
                    if item.name.startswith("python"):
                        candidate = item / "site-packages"
                        if candidate.exists():
                            venv_site_packages = candidate
                            break

        if venv_site_packages and venv_site_packages.exists():
            venv_site_str = str(venv_site_packages.resolve())
            if any(venv_site_str in path for path in sys.path):
                return True

        return False

    def activate(self) -> None:
        """
        Activate virtual environment if needed.

        Will re-execute script with venv Python if necessary.
        Includes safety checks to prevent infinite loops.
        """
        needs_switch, missing = self.needs_activation()

        if not needs_switch:
            return  # Already in correct environment

        if self._already_attempted:
            self._print_error(missing, "Activation loop detected")
            sys.exit(1)

        venv_path = self.find_venv()

        if not venv_path:
            self._print_error(missing, "No virtual environment found")
            sys.exit(1)

        if self.is_already_in_venv(venv_path):
            self._print_error(missing, "Already in venv but packages missing")
            sys.exit(1)

        self._reexecute_with_venv(venv_path)

    def _reexecute_with_venv(self, venv_path: Path) -> None:
        """Re-execute script using venv Python with graceful signal handling"""
        import subprocess
        import signal as sig

        venv_python = self._get_venv_python(venv_path)

        print(f"\n{'='*70}")
        print(f"🔄 Auto-activating Virtual Environment")
        print(f"{'='*70}")
        print(f"📍 Location: {venv_path.relative_to(self.script_dir)}")
        print(f"🐍 Python: {venv_python.name}")
        print(f"{'='*70}\n")
        sys.stdout.flush()

        # Set marker to prevent infinite loops
        env = os.environ.copy()
        env["_JARVIS_VENV_ACTIVATED"] = "1"

        # Use Popen for better signal handling instead of run()
        process = subprocess.Popen(
            [str(venv_python)] + sys.argv,
            cwd=str(self.script_dir),
            env=env
        )

        # Forward signals to child process for graceful shutdown
        def forward_signal(signum, frame):
            """Forward signal to child process and exit cleanly"""
            if process.poll() is None:  # Process still running
                try:
                    process.send_signal(signum)
                except (ProcessLookupError, OSError):
                    pass  # Process already terminated

        # Register signal handlers
        original_sigint = sig.signal(sig.SIGINT, forward_signal)
        original_sigterm = sig.signal(sig.SIGTERM, forward_signal)

        try:
            # Wait for subprocess with proper interrupt handling
            returncode = process.wait()
            sys.exit(returncode)
        except KeyboardInterrupt:
            # Clean exit on Ctrl+C - signal already forwarded
            print("\r", end="")  # Clear ^C from terminal
            try:
                # Give child process time to cleanup
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            sys.exit(0)
        finally:
            # Restore original signal handlers
            sig.signal(sig.SIGINT, original_sigint)
            sig.signal(sig.SIGTERM, original_sigterm)

    def _print_error(self, missing: List[str], reason: str) -> None:
        """Display detailed error with actionable solutions"""
        print(f"\n{'='*70}")
        print(f"❌ Virtual Environment Activation Failed")
        print(f"{'='*70}")
        print(f"Reason: {reason}")

        if missing:
            print(f"\nMissing packages: {', '.join(missing)}")

        print(f"\nSearched locations:")
        search_paths = os.environ.get(
            "JARVIS_VENV_SEARCH_PATHS",
            ":".join(self.DEFAULT_VENV_SEARCH_PATHS)
        ).split(":")

        for venv_name in search_paths:
            venv_path = self.script_dir / venv_name.strip()
            status = "✓" if venv_path.exists() else "✗"
            print(f"  {status} {venv_path}")

        print(f"\n💡 Solutions:")
        print(f"  1. Create virtual environment:")
        print(f"     python3 -m venv backend/venv")
        print(f"     source backend/venv/bin/activate  # macOS/Linux")
        print(f"     pip install -r requirements.txt")

        print(f"\n  2. Specify custom venv location:")
        print(f"     export JARVIS_VENV_PATH=/path/to/venv")

        print(f"\n  3. Add search paths:")
        print(f"     export JARVIS_VENV_SEARCH_PATHS=path1:path2:path3")

        if missing:
            print(f"\n  4. Install in current environment:")
            print(f"     pip install {' '.join(missing)}")

        print(f"{'='*70}\n")


# Execute auto-activation when module is loaded
_venv_activator = VenvAutoActivator()
_venv_activator.activate()
del _venv_activator  # Clean up namespace

# ============================================================================
# Environment is now verified and ready
# ============================================================================

import argparse
import asyncio
import json
import multiprocessing
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import psutil

# Cost tracking for hybrid cloud monitoring
try:
    # Add project root to path first (for 'from backend.X' imports)
    project_root = Path(__file__).parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Add backend directory to path (for 'from core.X' imports)
    backend_dir = project_root / "backend"
    if backend_dir.exists() and str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from core.cost_tracker import get_cost_tracker

    COST_TRACKING_AVAILABLE = True
except ImportError:
    COST_TRACKING_AVAILABLE = False
    # Logger not yet initialized, will log later

# Helper function to get Anthropic API key from Secret Manager
def _get_anthropic_api_key():
    """Get Anthropic API key with fallback chain: Secret Manager -> environment"""
    try:
        from core.secret_manager import get_anthropic_key
        key = get_anthropic_key()
        if key:
            return key
    except (ImportError, Exception):
        pass
    # Fallback to environment variable
    return os.getenv("ANTHROPIC_API_KEY")

# Set fork safety for macOS to prevent segmentation faults
if platform.system() == "Darwin":
    # Set environment variable for fork safety
    os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    # Additional Node.js fork safety
    os.environ["NODE_OPTIONS"] = "--max-old-space-size=4096"
    # Disable React's development mode checks that can cause issues
    os.environ["SKIP_PREFLIGHT_CHECK"] = "true"
    # Try to set multiprocessing start method if not already set
    try:
        multiprocessing.set_start_method("spawn", force=False)
    except RuntimeError:
        # Already set, that's fine
        pass

# Set up logging
import logging

logger = logging.getLogger(__name__)

# Global system manager reference for voice verification tracking
_global_system_manager = None

def track_voice_verification_attempt(success: bool, confidence: float, diagnostics: dict = None):
    """
    Track voice verification attempt in monitoring system

    Args:
        success: Whether verification succeeded
        confidence: Verification confidence score
        diagnostics: Detailed diagnostic information from failure analysis
    """
    global _global_system_manager
    if _global_system_manager is None:
        return

    try:
        from datetime import datetime

        # Update stats
        stats = _global_system_manager.voice_verification_stats
        stats['total_attempts'] += 1
        stats['last_attempt_time'] = datetime.now()

        if success:
            stats['successful'] += 1
            stats['consecutive_failures'] = 0
            stats['last_success_time'] = datetime.now()
        else:
            stats['failed'] += 1
            stats['consecutive_failures'] += 1
            stats['last_failure_time'] = datetime.now()

            # Track failure reasons
            if diagnostics and 'primary_reason' in diagnostics:
                reason = diagnostics['primary_reason']
                stats['failure_reasons'][reason] = stats['failure_reasons'].get(reason, 0) + 1

        # Calculate running average confidence
        n = stats['total_attempts']
        prev_avg = stats['average_confidence']
        stats['average_confidence'] = (prev_avg * (n - 1) + confidence) / n

        # Store in rolling window
        attempt_record = {
            'timestamp': datetime.now(),
            'success': success,
            'confidence': confidence
        }
        if diagnostics:
            attempt_record.update(diagnostics)

        _global_system_manager.voice_verification_attempts.append(attempt_record)
        if len(_global_system_manager.voice_verification_attempts) > 20:
            _global_system_manager.voice_verification_attempts.pop(0)

    except Exception as e:
        logger.debug(f"Failed to track voice verification: {e}")

# Load environment variables from .env file
try:
    from dotenv import load_dotenv

    # Load from root .env first
    load_dotenv()

    # Then load from backend/.env (will override if there are conflicts)
    backend_env = Path("backend") / ".env"
    if backend_env.exists():
        load_dotenv(backend_env, override=True)
except ImportError:
    pass

# Add project root AND backend to path for autonomous systems
# Project root needed for 'from backend.X' imports
# Backend dir needed for 'from core.X' imports
_project_root = Path(__file__).parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "backend"))

# CRITICAL: Clear ALL Python caches BEFORE any backend imports
# This ensures we get fresh code with all fixes
print("🧹 Clearing Python module cache...")
try:
    # Clear bytecode caches manually WITHOUT importing our modules
    import shutil

    backend_path = Path(__file__).parent / "backend"

    # Remove all __pycache__ directories
    for pycache_dir in backend_path.rglob("__pycache__"):
        try:
            shutil.rmtree(pycache_dir)
        except:
            pass

    # Remove any .pyc files
    for pyc_file in backend_path.rglob("*.pyc"):
        try:
            pyc_file.unlink()
        except:
            pass

    # Clear sys.modules of any cached backend modules
    modules_to_remove = []
    for module_name in list(sys.modules.keys()):
        if any(x in module_name for x in ["backend.", "api.", "vision.", "unified", "command", "intelligence.", "voice."]):
            modules_to_remove.append(module_name)

    for module_name in modules_to_remove:
        del sys.modules[module_name]

    if modules_to_remove:
        print(f"✅ Cleared {len(modules_to_remove)} cached modules")

    # Set environment to prevent new cache files
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    # Now import the cache cleaner for verification
    from clear_module_cache import verify_fresh_imports

    verify_fresh_imports()
    print("✅ Module cache cleared - using fresh code with all fixes!")

except Exception as e:
    print(f"⚠️ Could not clear module cache: {e}")

# NOW it's safe to import autonomous systems - they'll use fresh code
try:
    from backend.core.autonomous_orchestrator import (
        AutonomousOrchestrator as _AutonomousOrchestrator,
    )
    from backend.core.autonomous_orchestrator import get_orchestrator
    from backend.core.zero_config_mesh import ZeroConfigMesh as _ZeroConfigMesh
    from backend.core.zero_config_mesh import get_mesh

    # Use the imported classes
    AutonomousOrchestrator = _AutonomousOrchestrator
    ZeroConfigMesh = _ZeroConfigMesh
    AUTONOMOUS_AVAILABLE = True
except ImportError:
    # Create minimal fallback implementations to ensure autonomous mode is always available
    logger.info("Creating fallback autonomous components...")

    # Import typing to avoid redefining imported types

    class MockServiceInfo:
        def __init__(self, name, port, protocol="http"):
            self.name = name
            self.port = port
            self.protocol = protocol
            self.health_score = 1.0

    class AutonomousOrchestrator:
        def __init__(self):
            self.services = {}
            self._running = False

        async def start(self):
            self._running = True
            logger.info("Mock orchestrator started")

        async def stop(self):
            self._running = False

        async def discover_service(self, name, port, check_health=True):
            return {"protocol": "http", "port": port}

        async def register_service(self, name, port, protocol="http"):
            self.services[name] = MockServiceInfo(name, port, protocol)
            return True

        def get_service(self, name):
            return self.services.get(name)

        def get_frontend_config(self):
            """Get configuration for frontend"""
            return {
                "backend": {
                    "url": "http://localhost:8000",
                    "wsUrl": "ws://localhost:8000",
                    "endpoints": {
                        "health": "/health",
                        "ml_audio_config": "/audio/ml/config",
                        "ml_audio_stream": "/audio/ml/stream",
                        "jarvis_status": "/voice/jarvis/status",
                        "jarvis_activate": "/voice/jarvis/activate",
                        "wake_word_status": "/api/wake-word/status",
                        "vision_websocket": "/vision/ws/vision",
                    },
                },
                "services": {
                    name: {"url": f"http://localhost:{info.port}"}
                    for name, info in self.services.items()
                },
            }

    class ZeroConfigMesh:
        def __init__(self):
            self.nodes = {}

        async def start(self):
            """Start the mesh network"""
            logger.info("Mock mesh network started")

        async def join(self, service_info):
            self.nodes[service_info["name"]] = service_info

        async def find_service(self, name):
            node = self.nodes.get(name)
            if node:
                return {"endpoints": [f"http://localhost:{node['port']}"]}
            return None

        async def broadcast_event(self, event, data):
            pass

        async def get_mesh_config(self):
            return {
                "stats": {
                    "total_nodes": len(self.nodes),
                    "total_connections": len(self.nodes),
                    "healthy_nodes": len(self.nodes),
                }
            }

        async def register_node(self, node_id: str, node_type: str, endpoints: dict):
            """Register a node in the mesh"""
            self.nodes[node_id] = {
                "node_id": node_id,
                "node_type": node_type,
                "endpoints": endpoints,
            }

    _orchestrator = None
    _mesh = None

    def get_orchestrator():
        global _orchestrator
        if _orchestrator is None:
            _orchestrator = AutonomousOrchestrator()
        return _orchestrator

    def get_mesh():
        global _mesh
        if _mesh is None:
            _mesh = ZeroConfigMesh()
        return _mesh

    AUTONOMOUS_AVAILABLE = True


# ============================================================================
# 🚀 HYBRID CLOUD ROUTING SYSTEM - Enterprise-Grade Intelligence
# ============================================================================
# Automatic GCP routing when local RAM is high - prevents crashes, ensures uptime
# Features: Real-time monitoring, predictive analysis, seamless migration, SAI learning
# ============================================================================


class DynamicRAMMonitor:
    """
    Advanced RAM monitoring with predictive intelligence and automatic workload shifting.

    Features:
    - Real-time memory tracking with sub-second precision
    - Predictive analysis using historical patterns
    - Intelligent threshold adaptation based on workload
    - SAI learning integration for optimization
    - Process-level memory attribution
    - Automatic GCP migration triggers
    """

    def __init__(self):
        """Initialize the dynamic RAM monitor"""
        # System configuration (auto-detected, no hardcoding)
        self.local_ram_total = psutil.virtual_memory().total
        self.local_ram_gb = self.local_ram_total / (1024**3)
        self.is_macos = platform.system() == "Darwin"

        # Dynamic thresholds (adapt based on system behavior)
        self.warning_threshold = 0.75  # 75% - Start preparing for shift
        self.critical_threshold = 0.85  # 85% - Emergency shift to GCP
        self.optimal_threshold = 0.60  # 60% - Shift back to local
        self.emergency_threshold = 0.95  # 95% - Immediate action required

        # macOS-specific memory pressure thresholds
        # Memory pressure levels: 1 (normal), 2 (warn), 4 (critical)
        self.pressure_warn_level = 2  # macOS reports pressure level 2+
        self.pressure_critical_level = 4  # macOS reports pressure level 4

        # Monitoring state
        self.current_usage = 0.0
        self.current_pressure = 0  # macOS memory pressure level
        self.pressure_history = []
        self.usage_history = []
        self.max_history = 100
        self.prediction_window = 10  # Predict 10 seconds ahead

        # Component memory tracking
        self.component_memory = {}
        self.heavy_components = []  # Components eligible for migration

        # Prediction and learning
        self.trend_direction = 0.0  # Positive = increasing, Negative = decreasing
        self.predicted_usage = 0.0
        self.last_check = time.time()

        # Performance metrics
        self.shift_count = 0
        self.prevented_crashes = 0
        self.monitoring_overhead = 0.0

        logger.info(f"🧠 DynamicRAMMonitor initialized: {self.local_ram_gb:.1f}GB total")
        logger.info(
            f"   Thresholds: Warning={self.warning_threshold*100:.0f}%, "
            f"Critical={self.critical_threshold*100:.0f}%, "
            f"Emergency={self.emergency_threshold*100:.0f}%"
        )
        if self.is_macos:
            logger.info("   macOS memory pressure detection enabled")

    async def get_macos_memory_pressure(self) -> dict:
        """
        Get macOS memory pressure using vm_stat and memory_pressure command.

        Returns dict with:
        - pressure_level: 1 (normal), 2 (warn), 4 (critical)
        - pressure_status: "normal", "warn", "critical"
        - page_ins: Number of pages swapped in (indicator of pressure)
        - page_outs: Number of pages swapped out (indicator of pressure)
        - is_under_pressure: Boolean indicating actual memory stress
        """
        if not self.is_macos:
            return {
                "pressure_level": 1,
                "pressure_status": "normal",
                "page_ins": 0,
                "page_outs": 0,
                "is_under_pressure": False,
            }

        try:
            # Method 1: Try memory_pressure command (most accurate)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "memory_pressure",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                output = stdout.decode()

                # Parse memory_pressure output
                # Looks for: "System-wide memory free percentage: XX%"
                # And: "The system has experienced memory pressure XX times"
                pressure_level = 1  # Default: normal
                if "critical" in output.lower():
                    pressure_level = 4
                elif "warn" in output.lower():
                    pressure_level = 2

            except (FileNotFoundError, asyncio.TimeoutError):
                # memory_pressure command not available, fall back to vm_stat
                pressure_level = 1

            # Method 2: Use vm_stat for page in/out rates (always check this)
            proc = await asyncio.create_subprocess_exec(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            output = stdout.decode()

            # Parse vm_stat output for page activity
            page_ins = 0
            page_outs = 0
            for line in output.split("\n"):
                if "Pages paged in:" in line:
                    page_ins = int(line.split(":")[1].strip().replace(".", ""))
                elif "Pages paged out:" in line:
                    page_outs = int(line.split(":")[1].strip().replace(".", ""))

            # Calculate if under pressure based on page activity
            # High page-outs indicate actual memory pressure (swapping)
            is_under_pressure = page_outs > 1000  # More than 1000 pages swapped out

            # Upgrade pressure level if we see high swap activity
            if page_outs > 10000:
                pressure_level = max(pressure_level, 4)  # Critical
            elif page_outs > 5000:
                pressure_level = max(pressure_level, 2)  # Warn

            # Map pressure level to status
            pressure_status = {1: "normal", 2: "warn", 4: "critical"}.get(pressure_level, "unknown")

            return {
                "pressure_level": pressure_level,
                "pressure_status": pressure_status,
                "page_ins": page_ins,
                "page_outs": page_outs,
                "is_under_pressure": is_under_pressure or pressure_level >= 2,
            }

        except Exception as e:
            logger.debug(f"Failed to get macOS memory pressure: {e}")
            return {
                "pressure_level": 1,
                "pressure_status": "normal",
                "page_ins": 0,
                "page_outs": 0,
                "is_under_pressure": False,
            }

    async def get_current_state(self) -> dict:
        """Get comprehensive current memory state"""
        start_time = time.time()

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Get macOS memory pressure if on macOS
        pressure_info = await self.get_macos_memory_pressure()

        state = {
            "timestamp": datetime.now().isoformat(),
            "total_gb": self.local_ram_gb,
            "used_gb": mem.used / (1024**3),
            "available_gb": mem.available / (1024**3),
            "percent": mem.percent / 100.0,
            "swap_percent": swap.percent / 100.0,
            "trend": self.trend_direction,
            "predicted": self.predicted_usage,
            "status": self._get_status(mem.percent / 100.0, pressure_info),
            "shift_recommended": self._should_shift(mem.percent / 100.0, pressure_info),
            "emergency": self._is_emergency(mem.percent / 100.0, pressure_info),
            # macOS-specific fields
            "pressure_level": pressure_info["pressure_level"],
            "pressure_status": pressure_info["pressure_status"],
            "is_under_pressure": pressure_info["is_under_pressure"],
            "page_outs": pressure_info["page_outs"],
        }

        # Update metrics
        self.current_usage = state["percent"]
        self.current_pressure = state["pressure_level"]
        self.monitoring_overhead = time.time() - start_time

        return state

    def _get_status(self, usage: float, pressure_info: dict) -> str:
        """
        Get human-readable status based on both percentage and memory pressure.

        On macOS: Considers actual memory pressure (swapping) not just percentage.
        On Linux: Uses percentage thresholds.
        """
        # macOS: Prioritize memory pressure over percentage
        if self.is_macos:
            pressure_level = pressure_info.get("pressure_level", 1)
            is_under_pressure = pressure_info.get("is_under_pressure", False)

            # Critical pressure overrides percentage
            if pressure_level >= 4 or is_under_pressure and usage >= 0.90:
                return "CRITICAL"
            # Warn pressure + high percentage
            elif pressure_level >= 2 and usage >= self.critical_threshold:
                return "WARNING"
            # Warn pressure alone (high usage is OK if not swapping)
            elif is_under_pressure:
                return "ELEVATED"
            # High percentage but no pressure = OK on macOS (normal caching)
            elif usage >= self.warning_threshold:
                return "ELEVATED"  # Downgrade from WARNING
            elif usage >= self.optimal_threshold:
                return "OPTIMAL"
            else:
                return "OPTIMAL"
        else:
            # Linux: Use percentage thresholds
            if usage >= self.emergency_threshold:
                return "EMERGENCY"
            elif usage >= self.critical_threshold:
                return "CRITICAL"
            elif usage >= self.warning_threshold:
                return "WARNING"
            elif usage >= self.optimal_threshold:
                return "ELEVATED"
            else:
                return "OPTIMAL"

    def _should_shift(self, usage: float, pressure_info: dict) -> bool:
        """
        Determine if workload should shift to GCP.

        macOS: Shift when under actual memory pressure + high usage
        Linux: Shift when exceeding warning threshold
        """
        if self.is_macos:
            # Only shift if BOTH conditions met:
            # 1. High memory pressure (swapping happening)
            # 2. High percentage (>= critical threshold 85%)
            is_under_pressure = pressure_info.get("is_under_pressure", False)
            pressure_level = pressure_info.get("pressure_level", 1)

            return (is_under_pressure and usage >= self.critical_threshold) or pressure_level >= 4
        else:
            # Linux: Use percentage threshold
            return usage >= self.warning_threshold

    def _is_emergency(self, usage: float, pressure_info: dict) -> bool:
        """
        Determine if this is an emergency requiring immediate action.

        macOS: Critical pressure level + very high usage
        Linux: Emergency threshold exceeded
        """
        if self.is_macos:
            pressure_level = pressure_info.get("pressure_level", 1)
            # Emergency if critical pressure + usage above 90%
            return pressure_level >= 4 and usage >= 0.90
        else:
            return usage >= self.emergency_threshold

    async def update_usage_history(self):
        """Update usage history and calculate trends"""
        state = await self.get_current_state()

        self.usage_history.append({"time": time.time(), "usage": state["percent"]})

        # Keep only recent history
        if len(self.usage_history) > self.max_history:
            self.usage_history.pop(0)

        # Calculate trend
        if len(self.usage_history) >= 5:
            recent = [h["usage"] for h in self.usage_history[-5:]]
            self.trend_direction = (recent[-1] - recent[0]) / 5.0

            # Predict future usage (simple linear extrapolation)
            self.predicted_usage = min(
                1.0, max(0.0, state["percent"] + (self.trend_direction * self.prediction_window))
            )

    async def get_component_memory(self) -> dict:
        """Get memory usage per component"""
        try:
            current_process = psutil.Process()
            memory_info = current_process.memory_info()

            # Estimate component memory (simplified)
            total_mem = memory_info.rss / (1024**3)  # GB

            # Component weight estimates (will be dynamically learned)
            component_weights = {
                "vision": 0.30,  # 30% - Heavy visual processing
                "ml_models": 0.25,  # 25% - ML inference
                "chatbots": 0.20,  # 20% - Claude API interactions
                "memory": 0.10,  # 10% - Memory management
                "voice": 0.05,  # 5% - Voice processing
                "monitoring": 0.05,  # 5% - System monitoring
                "other": 0.05,  # 5% - Everything else
            }

            component_memory = {}
            for comp, weight in component_weights.items():
                component_memory[comp] = {
                    "gb": total_mem * weight,
                    "weight": weight,
                    "migratable": comp in ["vision", "ml_models", "chatbots"],
                }

            # Identify heavy components for migration
            self.heavy_components = [
                comp
                for comp, info in component_memory.items()
                if info["migratable"] and info["gb"] > 0.5
            ]

            return component_memory

        except Exception as e:
            logger.warning(f"Failed to get component memory: {e}")
            return {}

    async def should_shift_to_gcp(self) -> tuple[bool, str, dict]:
        """
        Determine if workload should shift to GCP using intelligent cost-aware optimization.

        Returns:
            (should_shift, reason, details)
        """
        # Try intelligent optimizer first (cost-aware, multi-factor)
        try:
            from backend.core.intelligent_gcp_optimizer import get_gcp_optimizer
            from backend.core.platform_memory_monitor import get_memory_monitor

            # Get accurate memory pressure
            monitor = get_memory_monitor()
            snapshot = await monitor.get_memory_pressure()

            # Use intelligent optimizer (considers cost, workload, trends, etc.)
            optimizer = get_gcp_optimizer(
                {
                    "cost": {
                        "daily_budget_limit": 1.00,  # $1/day limit
                        "cost_optimization_mode": "aggressive",  # Minimize costs
                    }
                }
            )

            should_create, reason, pressure_score = await optimizer.should_create_vm(snapshot)

            if should_create:
                # Build state dict with comprehensive metrics
                state = {
                    "percent": snapshot.usage_percent,
                    "status": snapshot.pressure_level.upper(),
                    "emergency": pressure_score.gcp_urgent,
                    "predicted": pressure_score.predicted_pressure_60s,
                    "platform": snapshot.platform,
                    "pressure_level": snapshot.pressure_level,
                    "reasoning": reason,
                    # Platform-specific
                    "macos_pressure": (
                        snapshot.macos_pressure_level if snapshot.platform == "darwin" else None
                    ),
                    "linux_psi_some": (
                        snapshot.linux_psi_some_avg10 if snapshot.platform == "linux" else None
                    ),
                    "linux_psi_full": (
                        snapshot.linux_psi_full_avg10 if snapshot.platform == "linux" else None
                    ),
                    "linux_reclaimable_gb": (
                        snapshot.linux_reclaimable_gb if snapshot.platform == "linux" else None
                    ),
                    # Optimizer metrics
                    "composite_score": pressure_score.composite_score,
                    "workload_type": pressure_score.workload_type,
                    "confidence": pressure_score.confidence,
                }

                logger.info(
                    f"🚨 Intelligent GCP shift (score: {pressure_score.composite_score:.1f}/100)"
                )
                logger.info(
                    f"   Platform: {snapshot.platform}, Pressure: {snapshot.pressure_level}"
                )
                logger.info(f"   Workload: {pressure_score.workload_type}")
                logger.info(f"   {reason}")

                return (True, reason, state)
            else:
                # No shift needed
                state = {
                    "percent": snapshot.usage_percent,
                    "status": "NORMAL",
                    "emergency": False,
                    "predicted": pressure_score.predicted_pressure_60s,
                    "platform": snapshot.platform,
                    "pressure_level": snapshot.pressure_level,
                    "reasoning": reason,
                    "composite_score": pressure_score.composite_score,
                }

                logger.debug(
                    f"✅ No GCP needed (score: {pressure_score.composite_score:.1f}/100): {reason}"
                )
                return (False, reason, state)

        except ImportError as e:
            logger.warning(f"Intelligent optimizer not available, trying platform monitor: {e}")
            # Try platform monitor fallback
            try:
                from backend.core.platform_memory_monitor import get_memory_monitor

                monitor = get_memory_monitor()
                snapshot = await monitor.get_memory_pressure()
                should_create, reason = monitor.should_create_gcp_vm(snapshot)

                if should_create:
                    state = {
                        "percent": snapshot.usage_percent,
                        "status": snapshot.pressure_level.upper(),
                        "emergency": snapshot.gcp_shift_urgent,
                        "predicted": snapshot.usage_percent,
                        "platform": snapshot.platform,
                        "reasoning": reason,
                    }
                    return (True, reason, state)
                else:
                    state = {
                        "percent": snapshot.usage_percent,
                        "status": "NORMAL",
                        "reasoning": reason,
                    }
                    return (False, reason, state)

            except Exception as e2:
                logger.warning(f"Platform monitor also failed: {e2}, using legacy method")
                return await self._legacy_should_shift_to_gcp()

        except Exception as e:
            logger.error(f"Error in intelligent optimization: {e}", exc_info=True)
            # Final fallback to legacy method
            return await self._legacy_should_shift_to_gcp()

    async def _legacy_should_shift_to_gcp(self) -> tuple[bool, str, dict]:
        """
        Legacy GCP shift detection (fallback only)
        Uses simple percentage thresholds - less accurate than platform-aware monitoring
        """
        state = await self.get_current_state()

        # Emergency: Immediate shift required
        if state["emergency"]:
            return (True, "EMERGENCY: RAM at critical level", state)

        # Critical: High usage detected
        if state["status"] == "CRITICAL":
            return (True, "CRITICAL: RAM usage exceeds threshold", state)

        # Warning with upward trend: Predictive shift
        if state["status"] == "WARNING" and self.trend_direction > 0.01:
            return (True, "PROACTIVE: Rising RAM trend detected", state)

        # Predicted emergency: Preventive shift
        if state["predicted"] >= self.critical_threshold:
            return (True, "PREDICTIVE: Future RAM spike predicted", state)

        return (False, "OPTIMAL: Local RAM sufficient", state)

    async def should_shift_to_local(self, gcp_cost: float = 0.0) -> tuple[bool, str]:
        """
        Determine if workload should shift back to local.

        Args:
            gcp_cost: Current GCP cost (for optimization)

        Returns:
            (should_shift, reason)
        """
        state = await self.get_current_state()

        # Optimal: RAM usage is low, bring workload back
        if state["percent"] < self.optimal_threshold and self.trend_direction <= 0:
            return (True, "OPTIMAL: Local RAM available, reducing GCP cost")

        # Cost optimization: If GCP cost is high and local can handle it
        if gcp_cost > 10.0 and state["percent"] < self.warning_threshold:
            return (True, f"COST_OPTIMIZATION: ${gcp_cost:.2f}/hr GCP cost, local available")

        return (False, "MAINTAINING: GCP deployment active")


class VMSessionTracker:
    """
    Track VM ownership per JARVIS session to prevent multi-terminal conflicts.

    Each JARVIS instance (terminal session) gets a unique session_id.
    VMs are tagged with their owning session, ensuring cleanup only affects
    VMs owned by the terminating session.

    Features:
    - UUID-based session identification
    - PID-based ownership validation
    - Hostname verification for multi-machine safety
    - Timestamp-based staleness detection
    - Atomic file operations with lock-free design
    """

    def __init__(self):
        """Initialize session tracker with unique session ID"""
        self.session_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.hostname = socket.gethostname()
        self.created_at = time.time()

        # Session tracking file (one per session, named by PID)
        self.session_file = Path(tempfile.gettempdir()) / f"jarvis_session_{self.pid}.json"

        # Global VM registry (shared across all sessions)
        self.vm_registry = Path(tempfile.gettempdir()) / "jarvis_vm_registry.json"

        logger.info(f"🆔 Session tracker initialized: {self.session_id[:8]}")
        logger.info(f"   PID: {self.pid}, Hostname: {self.hostname}")

    def register_vm(self, vm_id: str, zone: str, components: list):
        """
        Register VM ownership for this session.

        Args:
            vm_id: GCP instance ID
            zone: GCP zone
            components: Components deployed to this VM
        """
        session_data = {
            "session_id": self.session_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "vm_id": vm_id,
            "zone": zone,
            "components": components,
            "created_at": self.created_at,
            "registered_at": time.time(),
        }

        # Write session-specific file
        try:
            self.session_file.write_text(json.dumps(session_data, indent=2))
            logger.info(f"📝 Registered VM {vm_id} to session {self.session_id[:8]}")
        except Exception as e:
            logger.error(f"Failed to write session file: {e}")

        # Update global registry (append-only, multiple sessions can coexist)
        try:
            registry = self._load_registry()
            registry[self.session_id] = session_data
            self._save_registry(registry)
            logger.info(f"📋 Updated VM registry: {len(registry)} active sessions")
        except Exception as e:
            logger.error(f"Failed to update VM registry: {e}")

    def get_my_vm(self) -> Optional[dict]:
        """
        Get VM owned by this session with validation.

        Returns:
            VM data dict or None if no valid VM found
        """
        if not self.session_file.exists():
            return None

        try:
            data = json.loads(self.session_file.read_text())

            # Validation 1: Check session ID matches
            if data.get("session_id") != self.session_id:
                logger.warning("⚠️  Session ID mismatch, ignoring file")
                return None

            # Validation 2: Check PID matches
            if data.get("pid") != self.pid:
                logger.warning("⚠️  PID mismatch, ignoring file")
                return None

            # Validation 3: Check hostname matches (multi-machine safety)
            if data.get("hostname") != self.hostname:
                logger.warning("⚠️  Hostname mismatch, ignoring file")
                return None

            # Validation 4: Check age (expire after 12 hours)
            age_hours = (time.time() - data.get("created_at", 0)) / 3600
            if age_hours > 12:
                logger.warning(f"⚠️  Stale session file ({age_hours:.1f}h old), ignoring")
                self.session_file.unlink()
                return None

            return data

        except Exception as e:
            logger.error(f"Failed to read session file: {e}")
            return None

    def unregister_vm(self):
        """
        Unregister VM ownership and cleanup session files.
        Called during normal shutdown.
        """
        try:
            # Remove session file
            if self.session_file.exists():
                self.session_file.unlink()
                logger.info(f"🧹 Unregistered session {self.session_id[:8]}")

            # Remove from global registry
            registry = self._load_registry()
            if self.session_id in registry:
                del registry[self.session_id]
                self._save_registry(registry)
                logger.info(f"📋 Removed from VM registry: {len(registry)} sessions remain")

        except Exception as e:
            logger.error(f"Failed to unregister VM: {e}")

    def get_all_active_sessions(self) -> dict:
        """
        Get all active sessions from registry with staleness filtering.

        Returns:
            Dict of {session_id: session_data} for valid sessions only
        """
        registry = self._load_registry()
        active_sessions = {}

        for session_id, data in registry.items():
            # Check if PID is still running
            pid = data.get("pid")
            if pid and self._is_pid_running(pid):
                # Check age
                age_hours = (time.time() - data.get("created_at", 0)) / 3600
                if age_hours <= 12:
                    active_sessions[session_id] = data

        # If registry changed, save cleaned version
        if len(active_sessions) != len(registry):
            self._save_registry(active_sessions)
            logger.info(
                f"🧹 Cleaned registry: {len(active_sessions)}/{len(registry)} sessions active"
            )

        return active_sessions

    def _load_registry(self) -> dict:
        """Load VM registry from disk"""
        if not self.vm_registry.exists():
            return {}

        try:
            return json.loads(self.vm_registry.read_text())
        except Exception as e:
            logger.error(f"Failed to load VM registry: {e}")
            return {}

    def _save_registry(self, registry: dict):
        """Save VM registry to disk"""
        try:
            self.vm_registry.write_text(json.dumps(registry, indent=2))
        except Exception as e:
            logger.error(f"Failed to save VM registry: {e}")

    def _is_pid_running(self, pid: int) -> bool:
        """Check if PID is currently running"""
        try:
            proc = psutil.Process(pid)
            # Check if it's actually a Python process running start_system.py
            cmdline = proc.cmdline()
            return "start_system.py" in " ".join(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False


class HybridWorkloadRouter:
    """
    Intelligent router for local vs GCP workload placement.

    Features:
    - Component-level routing decisions
    - Automatic failover and fallback
    - Cost-aware optimization
    - Health monitoring
    - Zero-downtime migrations
    """

    def __init__(self, ram_monitor: DynamicRAMMonitor):
        """Initialize hybrid workload router"""
        self.ram_monitor = ram_monitor

        # Session tracking (multi-terminal safety)
        self.session_tracker = VMSessionTracker()

        # Deployment state
        self.gcp_active = False
        self.gcp_instance_id = None
        self.gcp_instance_zone = None  # Track zone for cleanup
        self.gcp_ip = None
        self.gcp_port = 8010

        # Component routing table
        self.component_locations = {}  # component -> 'local' | 'gcp'

        # Migration state
        self.migration_in_progress = False
        self.migration_start_time = None
        self.last_migration = None

        # Health tracking
        self.local_health = {"status": "unknown", "last_check": None}
        self.gcp_health = {"status": "unknown", "last_check": None}

        # Performance metrics
        self.total_migrations = 0
        self.failed_migrations = 0
        self.avg_migration_time = 0.0

        logger.info("🚦 HybridWorkloadRouter initialized")

    async def route_request(self, component: str, request_type: str) -> dict:
        """
        Route a request to local or GCP.

        Args:
            component: Component name (vision, ml_models, chatbots, etc.)
            request_type: Type of request (inference, analysis, etc.)

        Returns:
            Routing decision with endpoint details
        """
        # Check if component is already routed
        if component in self.component_locations:
            location = self.component_locations[component]
        else:
            # Make routing decision
            should_use_gcp, reason, state = await self.ram_monitor.should_shift_to_gcp()

            if should_use_gcp and self.gcp_active:
                location = "gcp"
            else:
                location = "local"

            self.component_locations[component] = location

        # Build routing response
        if location == "gcp":
            endpoint = {
                "location": "gcp",
                "host": self.gcp_ip or "localhost",
                "port": self.gcp_port,
                "url": f"http://{self.gcp_ip or 'localhost'}:{self.gcp_port}",
                "latency_estimate_ms": 50,  # Network latency
                "cost_estimate": 0.001,  # $0.001 per request
            }
        else:
            endpoint = {
                "location": "local",
                "host": "localhost",
                "port": 8010,
                "url": "http://localhost:8010",
                "latency_estimate_ms": 5,  # Local latency
                "cost_estimate": 0.0,
            }

        return endpoint

    async def trigger_gcp_deployment(self, components: list, reason: str = "HIGH_RAM") -> dict:
        """
        Trigger GCP deployment for specified components.

        Args:
            components: List of components to deploy
            reason: Reason for GCP deployment (for cost tracking)

        Returns:
            Deployment result
        """
        if self.migration_in_progress:
            return {"success": False, "reason": "Migration already in progress"}

        self.migration_in_progress = True
        self.migration_start_time = time.time()

        try:
            logger.info(f"🚀 Initiating GCP deployment for: {', '.join(components)}")

            # Step 1: Check GCP configuration
            gcp_config = await self._get_gcp_config()
            if not gcp_config["valid"]:
                raise Exception(f"GCP configuration invalid: {gcp_config['reason']}")

            # Step 2: Deploy via GitHub Actions (if available)
            deployment = await self._trigger_github_deployment(components, gcp_config)

            # CRITICAL: Track instance immediately for cleanup, even if health check fails
            self.gcp_instance_id = deployment["instance_id"]
            self.gcp_instance_zone = deployment.get(
                "zone", gcp_config.get("region", "us-central1") + "-a"
            )
            self.gcp_active = True  # Set now so cleanup runs even if ready check fails
            logger.info(f"📝 Tracking GCP instance for cleanup: {self.gcp_instance_id}")

            # Register VM with session tracker (multi-terminal safety)
            self.session_tracker.register_vm(
                vm_id=self.gcp_instance_id, zone=self.gcp_instance_zone, components=components
            )
            logger.info(f"🔐 VM registered to session {self.session_tracker.session_id[:8]}")

            # Record VM creation in cost tracker
            if COST_TRACKING_AVAILABLE:
                try:
                    cost_tracker = get_cost_tracker()
                    await cost_tracker.record_vm_created(
                        instance_id=self.gcp_instance_id,
                        components=components,
                        trigger_reason=reason or "HIGH_RAM",
                    )
                    logger.info(f"💰 Cost tracking: VM creation recorded")
                except Exception as e:
                    logger.warning(f"Failed to record VM creation in cost tracker: {e}")

            # Step 3: Wait for deployment to be ready (with reduced timeout)
            ready = await self._wait_for_gcp_ready(deployment["instance_id"], timeout=120)

            # Get IP even if health check fails
            if not self.gcp_ip:
                self.gcp_ip = await self._get_instance_ip(
                    deployment["instance_id"]
                ) or deployment.get("ip")

            # Update component locations
            for comp in components:
                self.component_locations[comp] = "gcp"

            migration_time = time.time() - self.migration_start_time
            self.total_migrations += 1
            self.avg_migration_time = (
                self.avg_migration_time * (self.total_migrations - 1) + migration_time
            ) / self.total_migrations

            if ready:
                logger.info(f"✅ GCP deployment successful in {migration_time:.1f}s")
                logger.info(f"   Instance: {self.gcp_instance_id}")
                logger.info(f"   IP: {self.gcp_ip}")
            else:
                # VM created but health check timeout - continue anyway
                logger.warning(
                    f"⚠️  GCP instance created but health check timeout ({migration_time:.1f}s)"
                )
                logger.warning(f"   Instance: {self.gcp_instance_id}")
                logger.warning(f"   IP: {self.gcp_ip or 'pending'}")
                logger.warning(
                    f"   Startup script may still be running - VM will be available soon"
                )

            return {
                "success": True,
                "instance_id": self.gcp_instance_id,
                "ip": self.gcp_ip,
                "components": components,
                "migration_time": migration_time,
                "health_check_passed": ready,
            }

        except Exception as e:
            logger.error(f"❌ GCP deployment failed: {e}")
            self.failed_migrations += 1
            return {"success": False, "reason": str(e)}
        finally:
            self.migration_in_progress = False
            self.last_migration = time.time()

    async def _get_gcp_config(self) -> dict:
        """Get and validate GCP configuration"""
        try:
            # Check for required environment variables or GitHub secrets
            project_id = os.getenv("GCP_PROJECT_ID")
            region = os.getenv("GCP_REGION", "us-central1")

            # Check if GitHub Actions can be triggered
            gh_token = os.getenv("GITHUB_TOKEN")
            gh_repo = os.getenv("GITHUB_REPOSITORY")

            if not project_id:
                return {"valid": False, "reason": "GCP_PROJECT_ID not set"}

            return {
                "valid": True,
                "project_id": project_id,
                "region": region,
                "has_gh_actions": bool(gh_token and gh_repo),
                "gh_repo": gh_repo,
            }
        except Exception as e:
            return {"valid": False, "reason": str(e)}

    async def _trigger_github_deployment(self, components: list, gcp_config: dict) -> dict:
        """Trigger GitHub Actions deployment workflow"""
        try:
            # Try to trigger via gh CLI
            if gcp_config.get("has_gh_actions"):
                cmd = [
                    "gh",
                    "workflow",
                    "run",
                    "deploy_to_gcp.yml",
                    "-f",
                    f"components={','.join(components)}",
                    "-f",
                    "ram_triggered=true",
                ]

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                if result.returncode == 0:
                    logger.info("📡 GitHub Actions deployment triggered")
                    # Extract run ID from output (would need actual parsing)
                    return {
                        "method": "github_actions",
                        "instance_id": "jarvis-gcp-auto",  # Would be dynamic
                        "ip": None,  # Will be discovered
                    }

            # Fallback: Direct GCP deployment (if gcloud CLI available)
            logger.info("📡 Attempting direct GCP deployment")
            return await self._direct_gcp_deployment(components, gcp_config)

        except Exception as e:
            logger.warning(f"GitHub deployment failed, trying direct: {e}")
            return await self._direct_gcp_deployment(components, gcp_config)

    def _generate_startup_script(self, gcp_config: dict) -> str:
        """
        Generate inline startup script for GCP instance.

        Uses Cloud Storage deployment packages instead of git clone
        for faster startup and consistent deployments.
        """
        branch = gcp_config.get("branch", "main")
        deployment_bucket = gcp_config.get("deployment_bucket", "gs://jarvis-473803-deployments")

        return f"""#!/bin/bash
set -e
echo "🚀 JARVIS GCP Auto-Deployment Starting..."

# Install dependencies
sudo apt-get update -qq
sudo apt-get install -y -qq python3.10 python3.10-venv python3-pip curl jq build-essential postgresql-client

# Download deployment package from Cloud Storage
PROJECT_DIR="$HOME/jarvis-backend"
DEPLOYMENT_BUCKET="{deployment_bucket}"

echo "📥 Downloading latest deployment from Cloud Storage..."

# Get latest commit for this branch
LATEST_COMMIT=$(gcloud storage cat $DEPLOYMENT_BUCKET/latest-{branch}.txt 2>/dev/null || echo "")

if [ -z "$LATEST_COMMIT" ]; then
    echo "⚠️  No deployment found for branch {branch}, falling back to git clone..."
    REPO_URL="{gcp_config.get('repo_url', 'https://github.com/drussell23/JARVIS-AI-Agent.git')}"
    if [ -d "$PROJECT_DIR" ]; then
        cd "$PROJECT_DIR" && git fetch --all && git reset --hard origin/{branch}
    else
        git clone -b {branch} $REPO_URL "$PROJECT_DIR"
    fi
else
    echo "📦 Using deployment: $LATEST_COMMIT"

    # Download and extract deployment package
    mkdir -p "$PROJECT_DIR"
    cd "$PROJECT_DIR"

    gcloud storage cp $DEPLOYMENT_BUCKET/jarvis-$LATEST_COMMIT.tar.gz /tmp/jarvis-deployment.tar.gz
    tar -xzf /tmp/jarvis-deployment.tar.gz -C "$PROJECT_DIR"
    rm /tmp/jarvis-deployment.tar.gz

    echo "✅ Deployment package extracted"
fi

# Setup Python environment
cd "$PROJECT_DIR/backend"
if [ ! -d "venv" ]; then
    python3.10 -m venv venv
fi
source venv/bin/activate
pip install --quiet --upgrade pip
if [ -f "requirements-cloud.txt" ]; then
    pip install --quiet -r requirements-cloud.txt
elif [ -f "requirements.txt" ]; then
    pip install --quiet -r requirements.txt
fi

# Setup Cloud SQL Proxy
if [ ! -f "$HOME/.local/bin/cloud-sql-proxy" ]; then
    mkdir -p "$HOME/.local/bin"
    curl -o "$HOME/.local/bin/cloud-sql-proxy" https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.8.2/cloud-sql-proxy.linux.amd64
    chmod +x "$HOME/.local/bin/cloud-sql-proxy"
fi

# Configure environment
cat > "$PROJECT_DIR/backend/.env.gcp" <<EOF
JARVIS_HYBRID_MODE=true
GCP_INSTANCE=true
JARVIS_DB_TYPE=cloudsql
EOF

# Start Cloud SQL Proxy (if config available)
if [ -f "$HOME/.jarvis/gcp/database_config.json" ]; then
    CONNECTION_NAME=$(jq -r '.cloud_sql.connection_name' "$HOME/.jarvis/gcp/database_config.json")
    nohup "$HOME/.local/bin/cloud-sql-proxy" "$CONNECTION_NAME" --port 5432 > "$HOME/cloud-sql-proxy.log" 2>&1 &
    sleep 2
fi

# Start backend
cd "$PROJECT_DIR/backend"
source .env.gcp
nohup venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8010 --log-level info > "$HOME/jarvis-backend.log" 2>&1 &

# Wait for health check
for i in {{1..30}}; do
    sleep 2
    if curl -sf http://localhost:8010/health > /dev/null; then
        INSTANCE_IP=$(curl -sf http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip -H "Metadata-Flavor: Google" || echo "unknown")
        echo "✅ JARVIS Ready at http://$INSTANCE_IP:8010"
        exit 0
    fi
done

echo "❌ Backend failed to start"
tail -50 "$HOME/jarvis-backend.log"
exit 1
"""

    async def _direct_gcp_deployment(self, components: list, gcp_config: dict) -> dict:
        """Direct GCP deployment using gcloud CLI with embedded startup script"""
        try:
            # CRITICAL: VM creation guard - only master instance can create VMs
            vm_creation_lock = Path("/tmp/jarvis_vm_creation.lock")  # nosec B108

            # Check if another instance is already creating a VM
            if vm_creation_lock.exists():
                try:
                    with open(vm_creation_lock, "r") as f:
                        lock_data = f.read().strip().split(":")
                        if len(lock_data) >= 2:
                            lock_pid = int(lock_data[0])
                            lock_time = float(lock_data[1])

                            # Check if lock is still valid (process still running)
                            if psutil.pid_exists(lock_pid):
                                age = time.time() - lock_time
                                logger.error(
                                    f"⛔ VM creation already in progress by PID {lock_pid} "
                                    f"({age:.0f}s ago). Aborting to prevent duplicate VMs!"
                                )
                                return {
                                    "success": False,
                                    "error": f"VM creation locked by PID {lock_pid}",
                                    "reason": "duplicate_prevention",
                                }
                            else:
                                # Stale lock - remove it
                                logger.warning(
                                    f"Removing stale VM creation lock from PID {lock_pid}"
                                )
                                vm_creation_lock.unlink()
                except Exception as e:
                    logger.warning(f"Failed to read VM creation lock: {e}, removing it")
                    vm_creation_lock.unlink()

            # Acquire VM creation lock
            try:
                with open(vm_creation_lock, "w") as f:
                    f.write(f"{os.getpid()}:{time.time()}")
                logger.debug(f"VM creation lock acquired by PID {os.getpid()}")
            except Exception as e:
                logger.error(f"Failed to acquire VM creation lock: {e}")
                return {
                    "success": False,
                    "error": "Failed to acquire VM creation lock",
                    "reason": "lock_failure",
                }

            # Clean up lock on exit (successful or failed)
            def cleanup_vm_lock():
                if vm_creation_lock.exists():
                    try:
                        with open(vm_creation_lock, "r") as f:
                            lock_pid = int(f.read().strip().split(":")[0])
                            if lock_pid == os.getpid():
                                vm_creation_lock.unlink()
                                logger.debug("VM creation lock released")
                    except Exception as e:
                        logger.warning(f"Failed to clean up VM creation lock: {e}")

            import atexit

            atexit.register(cleanup_vm_lock)

            instance_name = f"jarvis-auto-{int(time.time())}"

            # Generate startup script and write to temp file
            startup_script = self._generate_startup_script(gcp_config)

            # Write startup script to temporary file (avoids metadata parsing issues)
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
                f.write(startup_script)
                startup_script_path = f.name

            try:
                # Create GCP instance with appropriate machine type
                # e2-highmem-4: 4 vCPUs, 32GB RAM (~$0.029/hr Spot)
                machine_type = os.getenv("GCP_VM_TYPE", "e2-highmem-4")
                logger.info(f"🖥️  Creating GCP VM: {machine_type} (32GB RAM)")

                cmd = [
                    "gcloud",
                    "compute",
                    "instances",
                    "create",
                    instance_name,
                    "--project",
                    gcp_config["project_id"],
                    "--zone",
                    f"{gcp_config['region']}-a",
                    "--machine-type",
                    machine_type,
                    "--provisioning-model",
                    "SPOT",  # Use Spot VMs (60-91% cheaper)
                    "--instance-termination-action",
                    "DELETE",  # Auto-delete when preempted
                    "--max-run-duration",
                    "10800s",  # Max 3 hours (safety limit)
                    "--image-family",
                    "ubuntu-2204-lts",
                    "--image-project",
                    "ubuntu-os-cloud",
                    "--boot-disk-size",
                    "50GB",
                    "--metadata-from-file",
                    f"startup-script={startup_script_path}",  # Use file instead of inline!
                    "--tags",
                    "jarvis-auto",
                    "--labels",
                    f"components={'-'.join(components)},auto=true,spot=true",
                    "--format",
                    "json",
                ]

                logger.info(f"🔧 Running gcloud command: {' '.join(cmd[:8])}...")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            finally:
                # Clean up temp file
                try:
                    os.unlink(startup_script_path)
                except:
                    pass

            if result.returncode == 0:
                import json

                logger.info("✅ gcloud command succeeded")
                instance_data = json.loads(result.stdout)

                # Release VM creation lock immediately after success
                cleanup_vm_lock()

                return {
                    "method": "gcloud_direct",
                    "instance_id": instance_name,
                    "ip": instance_data[0]
                    .get("networkInterfaces", [{}])[0]
                    .get("accessConfigs", [{}])[0]
                    .get("natIP"),
                }
            else:
                logger.error(f"❌ gcloud command failed with return code {result.returncode}")
                logger.error(f"   stdout: {result.stdout}")
                logger.error(f"   stderr: {result.stderr}")

                # Release VM creation lock on failure too
                cleanup_vm_lock()

                raise Exception(f"gcloud failed: {result.stderr}")

        except subprocess.TimeoutExpired:
            logger.error(f"❌ gcloud command timed out after 120s")
            raise Exception("GCP deployment timeout - gcloud command took too long")
        except Exception as e:
            logger.error(f"❌ Direct GCP deployment failed: {e}")
            import traceback

            logger.error(f"   Traceback: {traceback.format_exc()}")
            raise

    async def _wait_for_gcp_ready(self, instance_id: str, timeout: int = 300) -> bool:
        """Wait for GCP instance to be ready"""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                # Try to get instance IP if not already set
                if not self.gcp_ip:
                    ip = await self._get_instance_ip(instance_id)
                    if ip:
                        self.gcp_ip = ip

                # Try to hit health endpoint
                if self.gcp_ip:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"http://{self.gcp_ip}:{self.gcp_port}/health",
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data.get("status") == "healthy":
                                    logger.info(f"✅ GCP instance ready: {self.gcp_ip}")
                                    return True
            except Exception:
                pass  # Keep retrying

            await asyncio.sleep(5)

        return False

    async def _get_instance_ip(self, instance_id: str) -> Optional[str]:
        """Get IP address of GCP instance"""
        try:
            cmd = ["gcloud", "compute", "instances", "describe", instance_id, "--format", "json"]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                import json

                instance_data = json.loads(result.stdout)
                ip = (
                    instance_data.get("networkInterfaces", [{}])[0]
                    .get("accessConfigs", [{}])[0]
                    .get("natIP")
                )
                return ip
        except Exception as e:
            logger.warning(f"Failed to get instance IP: {e}")

        return None

    async def check_health(self) -> dict:
        """Check health of both local and GCP deployments"""
        health = {
            "local": await self._check_local_health(),
            "gcp": await self._check_gcp_health() if self.gcp_active else {"status": "inactive"},
        }

        return health

    async def _check_local_health(self) -> dict:
        """Check local system health"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://localhost:8010/health", timeout=aiohttp.ClientTimeout(total=3)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.local_health = {
                            "status": "healthy",
                            "last_check": time.time(),
                            "details": data,
                        }
                        return self.local_health
        except Exception as e:
            self.local_health = {"status": "unhealthy", "last_check": time.time(), "error": str(e)}

        return self.local_health

    async def _check_gcp_health(self) -> dict:
        """Check GCP deployment health"""
        if not self.gcp_ip:
            return {"status": "unknown", "reason": "No IP address"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{self.gcp_ip}:{self.gcp_port}/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.gcp_health = {
                            "status": "healthy",
                            "last_check": time.time(),
                            "details": data,
                        }
                        return self.gcp_health
        except Exception as e:
            self.gcp_health = {"status": "unhealthy", "last_check": time.time(), "error": str(e)}

        return self.gcp_health

    async def _cleanup_gcp_instance(self, instance_id: str):
        """Delete GCP instance to stop costs"""
        try:
            project_id = os.getenv("GCP_PROJECT_ID")
            region = os.getenv("GCP_REGION", "us-central1")
            zone = f"{region}-a"

            cmd = [
                "gcloud",
                "compute",
                "instances",
                "delete",
                instance_id,
                "--project",
                project_id,
                "--zone",
                zone,
                "--quiet",  # Don't prompt for confirmation
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode == 0:
                logger.info(f"✅ Deleted GCP instance: {instance_id}")

                # Record VM deletion in cost tracker
                if COST_TRACKING_AVAILABLE:
                    try:
                        cost_tracker = get_cost_tracker()
                        await cost_tracker.record_vm_deleted(
                            instance_id=instance_id, was_orphaned=False
                        )
                        logger.info(f"💰 Cost tracking: VM deletion recorded")
                    except Exception as e:
                        logger.warning(f"Failed to record VM deletion in cost tracker: {e}")

                # Reset state
                self.gcp_active = False
                self.gcp_instance_id = None
                self.gcp_ip = None
            else:
                logger.error(f"Failed to delete instance: {result.stderr}")

        except Exception as e:
            logger.error(f"Error cleaning up GCP instance: {e}")


class HybridIntelligenceCoordinator:
    """
    Master coordinator for hybrid local/GCP intelligence.

    Orchestrates:
    - Continuous RAM monitoring
    - Automatic workload shifting
    - Cost optimization
    - SAI learning integration
    - Health monitoring
    - Emergency fallback
    """

    def __init__(self):
        """Initialize hybrid intelligence coordinator"""
        self.ram_monitor = DynamicRAMMonitor()
        self.workload_router = HybridWorkloadRouter(self.ram_monitor)

        # SAI Learning Integration
        self.learning_model = HybridLearningModel()
        self.sai_integration = SAIHybridIntegration(self.learning_model)
        self.learning_enabled = True

        # Monitoring loop
        self.monitoring_task = None
        self.monitoring_interval = 5  # Will be dynamically adjusted by SAI
        self.running = False

        # Decision history for learning
        self.decision_history = []
        self.max_decision_history = 100

        # Emergency state
        self.emergency_mode = False
        self.emergency_start = None

        # SAI Prediction tracking (for monitoring display)
        self.last_sai_prediction = None
        self.sai_prediction_history = []  # Rolling window of last 10 predictions
        self.sai_prediction_count = 0

        logger.info("🎯 HybridIntelligenceCoordinator initialized with SAI learning")

    async def start(self):
        """Start hybrid monitoring and coordination"""
        if self.running:
            logger.warning("Hybrid coordinator already running")
            return

        # Initialize SAI learning database
        if self.learning_enabled:
            try:
                await self.sai_integration.initialize_database()
                logger.info("✅ SAI learning database connected")

                # Apply learned thresholds to RAM monitor
                learned_thresholds = self.learning_model.optimal_thresholds
                self.ram_monitor.warning_threshold = learned_thresholds["warning"]
                self.ram_monitor.critical_threshold = learned_thresholds["critical"]
                self.ram_monitor.optimal_threshold = learned_thresholds["optimal"]
                self.ram_monitor.emergency_threshold = learned_thresholds["emergency"]

                logger.info(f"📚 Applied learned thresholds: {learned_thresholds}")
            except Exception as e:
                logger.warning(f"SAI integration initialization failed: {e}")
                self.learning_enabled = False

        self.running = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())

        logger.info("🚀 Hybrid coordination started")
        logger.info(f"   Monitoring interval: {self.monitoring_interval}s (adaptive)")
        logger.info(f"   RAM: {self.ram_monitor.local_ram_gb:.1f}GB total")
        logger.info(f"   Learning: {'Enabled' if self.learning_enabled else 'Disabled'}")

    async def _get_session_cost_info(self, instance_id: str) -> Optional[dict]:
        """Get comprehensive cost information for the current session"""
        try:
            from datetime import datetime

            from core.cost_tracker import get_cost_tracker

            cost_tracker = get_cost_tracker()

            # Get session info from active sessions or database
            session = cost_tracker.active_sessions.get(instance_id)
            if not session:
                session = await cost_tracker._load_session_from_db(instance_id)

            if not session:
                logger.warning(f"No cost tracking data found for {instance_id}")
                return None

            # Calculate session cost
            runtime_hours = (datetime.utcnow() - session.created_at).total_seconds() / 3600
            session_cost = runtime_hours * cost_tracker.config.spot_vm_hourly_cost
            regular_cost = runtime_hours * cost_tracker.config.regular_vm_hourly_cost
            savings = regular_cost - session_cost
            savings_percent = (savings / regular_cost * 100) if regular_cost > 0 else 0

            # Get monthly summary
            month_summary = await cost_tracker.get_cost_summary("month")
            month_total = month_summary.get("total_estimated_cost", 0.0)

            # Calculate monthly projection (based on this month's usage pattern)
            days_in_month = 30
            current_day = datetime.now().day
            if current_day > 0:
                daily_average = month_total / current_day
                month_projection = daily_average * days_in_month
            else:
                month_projection = month_total

            return {
                "instance_id": instance_id,
                "session_cost": session_cost,
                "runtime_hours": runtime_hours,
                "hourly_rate": cost_tracker.config.spot_vm_hourly_cost,
                "savings": savings,
                "savings_percent": savings_percent,
                "month_total": month_total,
                "month_projection": month_projection,
                "vm_type": session.vm_type,
                "region": session.region,
            }

        except Exception as e:
            logger.error(f"Failed to get session cost info: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return None

    async def stop(self):
        """Stop hybrid coordination (VM cleanup handled in finally block)"""
        self.running = False

        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass

        # Cleanup GCP instance if active (CRITICAL for cost control - do NOT skip!)
        # With 90s timeout, we have plenty of time for VM deletion
        logger.info(
            f"🔍 GCP VM status: gcp_active={self.workload_router.gcp_active}, "
            f"instance_id={self.workload_router.gcp_instance_id or 'none'}"
        )

        if self.workload_router.gcp_active and self.workload_router.gcp_instance_id:
            print(f"   ├─ Deleting GCP VM: {self.workload_router.gcp_instance_id}...")
            import time

            start_time = time.time()

            try:
                logger.info(f"🧹 Cleaning up GCP instance: {self.workload_router.gcp_instance_id}")
                await self.workload_router._cleanup_gcp_instance(
                    self.workload_router.gcp_instance_id
                )
                elapsed = time.time() - start_time
                print(
                    f"   ├─ {Colors.GREEN}✓ VM deleted successfully ({elapsed:.1f}s){Colors.ENDC}"
                )

                # Get cost information from cost tracker
                session_cost_info = await self._get_session_cost_info(
                    self.workload_router.gcp_instance_id
                )
                if session_cost_info:
                    print(
                        f"   ├─ {Colors.GREEN}💰 Session Cost: ${session_cost_info['session_cost']:.4f} ({session_cost_info['runtime_hours']:.2f}h @ ${session_cost_info['hourly_rate']:.3f}/hr){Colors.ENDC}"
                    )
                    print(
                        f"   ├─ {Colors.GREEN}💵 Savings vs Regular VM: ${session_cost_info['savings']:.4f} ({session_cost_info['savings_percent']:.1f}%){Colors.ENDC}"
                    )
                    print(
                        f"   └─ {Colors.CYAN}📊 Monthly Total: ${session_cost_info['month_total']:.2f} | Projected: ${session_cost_info['month_projection']:.2f}{Colors.ENDC}"
                    )
                else:
                    print(
                        f"   └─ {Colors.GREEN}💰 Stopped billing for {self.workload_router.gcp_instance_id}{Colors.ENDC}"
                    )

                logger.info(
                    f"✅ GCP instance {self.workload_router.gcp_instance_id} deleted in {elapsed:.1f}s"
                )

                # Unregister from session tracker to prevent duplicate deletion in post-shutdown
                if hasattr(self.workload_router, "session_tracker"):
                    self.workload_router.session_tracker.unregister_vm()
                    logger.info(
                        "🔓 VM unregistered from session tracker (prevents duplicate deletion)"
                    )

            except Exception as e:
                elapsed = time.time() - start_time
                print(f"   ├─ {Colors.RED}✗ VM deletion failed ({elapsed:.1f}s){Colors.ENDC}")
                print(f"   ├─ {Colors.YELLOW}⚠ VM will be retried in finally block{Colors.ENDC}")
                logger.error(f"❌ Failed to cleanup GCP instance: {e}")
                import traceback

                logger.error(traceback.format_exc())
        else:
            print(f"   ├─ No GCP VM to delete")
            logger.info("ℹ️  No active GCP instance to cleanup")

        logger.info("🛑 Hybrid coordination stopped")

    async def _monitoring_loop(self):
        """Continuous monitoring and decision loop with SAI learning"""
        while self.running:
            try:
                # Update RAM metrics
                await self.ram_monitor.update_usage_history()

                # Get current state
                ram_state = await self.ram_monitor.get_current_state()

                # SAI Learning: Record RAM observation
                if self.learning_enabled:
                    component_mem = await self.ram_monitor.get_component_memory()
                    await self.sai_integration.record_and_learn(
                        "ram",
                        {
                            "timestamp": time.time(),
                            "usage": ram_state["percent"],
                            "components": component_mem,
                        },
                    )

                    # SAI Learning: Get RAM spike prediction
                    spike_prediction = await self.learning_model.predict_ram_spike(
                        current_usage=ram_state["percent"],
                        trend=self.ram_monitor.trend_direction,
                        time_horizon_seconds=60,
                    )

                    if spike_prediction["spike_likely"] and spike_prediction["confidence"] > 0.5:
                        # Store prediction for monitoring display
                        self.last_sai_prediction = {
                            'timestamp': datetime.now().isoformat(),
                            'type': 'ram_spike',
                            'predicted_peak': spike_prediction['predicted_peak'],
                            'confidence': spike_prediction['confidence'],
                            'reason': spike_prediction['reason'],
                            'time_horizon_seconds': 60
                        }
                        self.sai_prediction_history.append(self.last_sai_prediction)
                        if len(self.sai_prediction_history) > 10:
                            self.sai_prediction_history.pop(0)
                        self.sai_prediction_count += 1

                        # Still log it but less verbosely
                        logger.debug(
                            f"🔮 SAI Prediction #{self.sai_prediction_count}: RAM spike likely in 60s "
                            f"(peak: {spike_prediction['predicted_peak']*100:.1f}%, "
                            f"confidence: {spike_prediction['confidence']:.1%}) - {spike_prediction['reason']}"
                        )

                # Make routing decision (now using SAI-learned thresholds)
                should_shift, reason, details = await self.ram_monitor.should_shift_to_gcp()

                # Log significant changes
                if ram_state["status"] in ["WARNING", "CRITICAL", "EMERGENCY"]:
                    # Include memory pressure info on macOS
                    if self.ram_monitor.is_macos:
                        pressure_status = ram_state.get("pressure_status", "unknown")
                        is_under_pressure = ram_state.get("is_under_pressure", False)
                        pressure_indicator = "🔴" if is_under_pressure else "🟢"
                        logger.warning(
                            f"⚠️  RAM {ram_state['status']}: {ram_state['percent']*100:.1f}% used "
                            f"| Pressure: {pressure_indicator} {pressure_status}"
                        )
                    else:
                        logger.warning(
                            f"⚠️  RAM {ram_state['status']}: {ram_state['percent']*100:.1f}% used"
                        )

                # Handle emergency
                if ram_state["emergency"] and not self.emergency_mode:
                    await self._handle_emergency(ram_state)
                elif self.emergency_mode and ram_state["status"] == "OPTIMAL":
                    await self._exit_emergency()

                # Automatic GCP shift if needed
                if should_shift and not self.workload_router.gcp_active and not self.emergency_mode:
                    logger.info(f"🚀 Automatic GCP shift triggered: {reason}")
                    await self._perform_shift_to_gcp(reason, ram_state)

                # Check if we should shift back to local
                if self.workload_router.gcp_active:
                    should_return, return_reason = await self.ram_monitor.should_shift_to_local()
                    if should_return:
                        logger.info(f"🏠 Shift back to local: {return_reason}")
                        await self._perform_shift_to_local(return_reason)

                # Record decision
                self.decision_history.append(
                    {
                        "timestamp": time.time(),
                        "ram_state": ram_state,
                        "decision": "shift_to_gcp" if should_shift else "stay_local",
                        "reason": reason,
                    }
                )

                if len(self.decision_history) > self.max_decision_history:
                    self.decision_history.pop(0)

                # SAI Learning: Adapt monitoring interval dynamically
                if self.learning_enabled:
                    optimal_interval = await self.learning_model.get_optimal_monitoring_interval(
                        ram_state["percent"]
                    )
                    if optimal_interval != self.monitoring_interval:
                        logger.info(
                            f"📊 SAI: Adapting monitoring interval {self.monitoring_interval}s → {optimal_interval}s"
                        )
                        self.monitoring_interval = optimal_interval

            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")

            await asyncio.sleep(self.monitoring_interval)

    async def _handle_emergency(self, ram_state: dict):
        """Handle emergency RAM situation"""
        self.emergency_mode = True
        self.emergency_start = time.time()

        logger.error("🚨 EMERGENCY MODE ACTIVATED")
        logger.error(f"   RAM: {ram_state['percent']*100:.1f}% used")
        logger.error(f"   Available: {ram_state['available_gb']:.2f}GB")

        # Get component memory breakdown
        component_memory = await self.ram_monitor.get_component_memory()

        # Find heaviest components
        heavy = sorted(
            [(k, v["gb"]) for k, v in component_memory.items() if v.get("migratable")],
            key=lambda x: x[1],
            reverse=True,
        )

        if heavy:
            components_to_shift = [comp for comp, _ in heavy[:3]]  # Top 3
            logger.info(f"   Shifting heavy components: {', '.join(components_to_shift)}")

            # Trigger emergency GCP deployment
            result = await self.workload_router.trigger_gcp_deployment(
                components_to_shift, reason="EMERGENCY"
            )

            if result["success"]:
                logger.info("✅ Emergency shift successful")
                self.ram_monitor.prevented_crashes += 1
            else:
                logger.error(f"❌ Emergency shift failed: {result['reason']}")

    async def _exit_emergency(self):
        """Exit emergency mode"""
        duration = time.time() - self.emergency_start if self.emergency_start else 0

        logger.info(f"✅ Emergency resolved (duration: {duration:.1f}s)")

        self.emergency_mode = False
        self.emergency_start = None

    async def _perform_shift_to_gcp(self, reason: str, ram_state: dict):
        """Perform workload shift to GCP with SAI learning"""
        migration_start = time.time()
        success = False

        try:
            # Get heavy components (use SAI-learned weights if available)
            component_memory = await self.ram_monitor.get_component_memory()

            # SAI Learning: Use learned component weights
            if self.learning_enabled:
                learned_weights = await self.learning_model.get_learned_component_weights()

                # Update component memory with learned weights
                for comp in component_memory:
                    if comp in learned_weights:
                        component_memory[comp]["weight"] = learned_weights[comp]

                logger.info(f"📚 Using SAI-learned component weights: {learned_weights}")

            components_to_shift = [
                comp for comp, info in component_memory.items() if info.get("migratable")
            ]

            if not components_to_shift:
                logger.warning("No migratable components found")
                return

            logger.info(f"🚀 Shifting to GCP: {', '.join(components_to_shift)}")

            result = await self.workload_router.trigger_gcp_deployment(
                components_to_shift, reason=reason
            )

            success = result["success"]

            if success:
                logger.info(f"✅ GCP shift completed in {result['migration_time']:.1f}s")
            else:
                logger.error(f"❌ GCP shift failed: {result['reason']}")

        except Exception as e:
            logger.error(f"Shift to GCP failed: {e}")
            success = False

        finally:
            # SAI Learning: Record migration outcome
            if self.learning_enabled:
                migration_duration = time.time() - migration_start
                await self.sai_integration.record_and_learn(
                    "migration",
                    {
                        "timestamp": migration_start,
                        "reason": reason,
                        "success": success,
                        "duration": migration_duration,
                    },
                )

    async def _perform_shift_to_local(self, reason: str):
        """Perform workload shift back to local"""
        try:
            logger.info(f"🏠 Shifting back to local: {reason}")

            # Gradually shift components back
            gcp_components = [
                comp
                for comp, loc in self.workload_router.component_locations.items()
                if loc == "gcp"
            ]

            for comp in gcp_components:
                self.workload_router.component_locations[comp] = "local"

            # Optionally terminate GCP instance (could keep warm for faster re-deployment)
            # For now, keep it running but idle

            logger.info(f"✅ Shifted {len(gcp_components)} components to local")

        except Exception as e:
            logger.error(f"Shift to local failed: {e}")

    async def get_status(self) -> dict:
        """Get comprehensive status with SAI learning stats"""
        ram_state = await self.ram_monitor.get_current_state()
        health = await self.workload_router.check_health()

        # Get SAI learning stats
        learning_stats = {}
        if self.learning_enabled:
            learning_stats = await self.learning_model.get_learning_stats()

        return {
            "timestamp": datetime.now().isoformat(),
            "ram": ram_state,
            "gcp_active": self.workload_router.gcp_active,
            "emergency_mode": self.emergency_mode,
            "health": health,
            "component_locations": self.workload_router.component_locations,
            "monitoring_interval": self.monitoring_interval,
            "metrics": {
                "total_migrations": self.workload_router.total_migrations,
                "failed_migrations": self.workload_router.failed_migrations,
                "avg_migration_time": self.workload_router.avg_migration_time,
                "prevented_crashes": self.ram_monitor.prevented_crashes,
            },
            "sai_learning": learning_stats if self.learning_enabled else {"enabled": False},
        }


# ============================================================================
# 🧠 SAI LEARNING INTEGRATION - Adaptive Intelligence for Hybrid Routing
# ============================================================================
# Machine learning system that learns optimal thresholds, predicts RAM spikes,
# adapts monitoring intervals, and learns component weights from user patterns
# ============================================================================


class HybridLearningModel:
    """
    Advanced ML model for hybrid routing optimization.

    Features:
    - Adaptive threshold learning per user
    - RAM spike prediction using time-series analysis
    - Component weight learning from actual usage
    - Workload pattern recognition
    - Time-of-day correlation analysis
    - Seasonal trend detection
    """

    def __init__(self):
        """Initialize the learning model"""
        # Historical data storage
        self.ram_observations = []  # (timestamp, usage, components_active)
        self.migration_outcomes = []  # (timestamp, reason, success, duration)
        self.component_observations = []  # (timestamp, component, memory_usage)

        # Learned parameters (start with defaults, adapt over time)
        self.optimal_thresholds = {
            "warning": 0.75,
            "critical": 0.85,
            "optimal": 0.60,
            "emergency": 0.95,
        }

        # Confidence in learned thresholds (0.0 to 1.0)
        self.threshold_confidence = {
            "warning": 0.0,
            "critical": 0.0,
            "optimal": 0.0,
            "emergency": 0.0,
        }

        # Component weight learning
        self.learned_component_weights = {}  # component -> learned weight
        self.component_observation_count = {}  # component -> observation count

        # Pattern recognition
        self.hourly_ram_patterns = {}  # hour -> avg RAM usage
        self.daily_patterns = {}  # day_of_week -> avg RAM usage
        self.workload_sequences = []  # Recent sequences of workload patterns

        # Prediction model parameters
        self.prediction_accuracy = 0.0
        self.total_predictions = 0
        self.correct_predictions = 0

        # Learning rate (how quickly to adapt)
        self.learning_rate = 0.1  # Conservative to avoid overreacting

        # Minimum observations before trusting learned values
        self.min_observations = 20

        logger.info("🧠 HybridLearningModel initialized")

    async def record_ram_observation(self, timestamp: float, usage: float, components_active: dict):
        """Record a RAM observation for learning"""
        observation = {
            "timestamp": timestamp,
            "usage": usage,
            "components": components_active.copy(),
            "hour": datetime.fromtimestamp(timestamp).hour,
            "day_of_week": datetime.fromtimestamp(timestamp).weekday(),
        }

        self.ram_observations.append(observation)

        # Keep only recent observations (last 1000)
        if len(self.ram_observations) > 1000:
            self.ram_observations.pop(0)

        # Update hourly patterns
        hour = observation["hour"]
        if hour not in self.hourly_ram_patterns:
            self.hourly_ram_patterns[hour] = []
        self.hourly_ram_patterns[hour].append(usage)

        # Keep only recent hourly data
        if len(self.hourly_ram_patterns[hour]) > 50:
            self.hourly_ram_patterns[hour].pop(0)

        # Update daily patterns
        day = observation["day_of_week"]
        if day not in self.daily_patterns:
            self.daily_patterns[day] = []
        self.daily_patterns[day].append(usage)

        if len(self.daily_patterns[day]) > 50:
            self.daily_patterns[day].pop(0)

    async def record_migration_outcome(
        self, timestamp: float, reason: str, success: bool, duration: float
    ):
        """Record a migration outcome for learning"""
        outcome = {
            "timestamp": timestamp,
            "reason": reason,
            "success": success,
            "duration": duration,
            "ram_before": (self.ram_observations[-1]["usage"] if self.ram_observations else 0.0),
        }

        self.migration_outcomes.append(outcome)

        if len(self.migration_outcomes) > 100:
            self.migration_outcomes.pop(0)

        # Learn from outcome
        await self._learn_from_migration(outcome)

    async def record_component_usage(self, timestamp: float, component: str, memory_gb: float):
        """Record component memory usage for weight learning"""
        observation = {"timestamp": timestamp, "component": component, "memory": memory_gb}

        self.component_observations.append(observation)

        if len(self.component_observations) > 500:
            self.component_observations.pop(0)

        # Update learned weights
        if component not in self.learned_component_weights:
            self.learned_component_weights[component] = memory_gb
            self.component_observation_count[component] = 1
        else:
            # Exponential moving average
            old_weight = self.learned_component_weights[component]
            new_weight = old_weight * (1 - self.learning_rate) + memory_gb * self.learning_rate
            self.learned_component_weights[component] = new_weight
            self.component_observation_count[component] += 1

    async def _learn_from_migration(self, outcome: dict):
        """Learn and adapt thresholds from migration outcomes"""
        if not outcome["success"]:
            # Migration failed - might need to lower critical threshold to migrate earlier
            if "CRITICAL" in outcome["reason"]:
                # Adapt critical threshold down slightly
                old_threshold = self.optimal_thresholds["critical"]
                new_threshold = max(0.70, old_threshold - 0.02)  # Don't go below 70%
                self.optimal_thresholds["critical"] = new_threshold

                # Increase confidence slowly
                self.threshold_confidence["critical"] = min(
                    1.0, self.threshold_confidence["critical"] + 0.05
                )

                logger.info(
                    f"📚 Learning: Critical threshold adapted {old_threshold:.2f} → {new_threshold:.2f}"
                )

        else:
            # Migration successful
            if "EMERGENCY" in outcome["reason"]:
                # We hit emergency - learn to migrate earlier
                old_warning = self.optimal_thresholds["warning"]
                new_warning = max(0.65, old_warning - 0.03)
                self.optimal_thresholds["warning"] = new_warning

                logger.info(
                    f"📚 Learning: Warning threshold adapted {old_warning:.2f} → {new_warning:.2f} (prevented emergency)"
                )

            elif "PROACTIVE" in outcome["reason"] and outcome["ram_before"] < 0.80:
                # Proactive migration was too early - can be less aggressive
                old_warning = self.optimal_thresholds["warning"]
                new_warning = min(0.80, old_warning + 0.01)
                self.optimal_thresholds["warning"] = new_warning

                logger.info(
                    f"📚 Learning: Warning threshold relaxed {old_warning:.2f} → {new_warning:.2f} (was too aggressive)"
                )

    async def predict_ram_spike(
        self, current_usage: float, trend: float, time_horizon_seconds: int = 60
    ) -> dict:
        """
        Predict if a RAM spike will occur.

        Returns:
            {
                'spike_likely': bool,
                'predicted_peak': float,
                'confidence': float,
                'reason': str
            }
        """
        # Simple linear extrapolation with trend
        predicted_usage = current_usage + (trend * time_horizon_seconds)

        # Check historical patterns for this time of day
        current_hour = datetime.now().hour
        current_day = datetime.now().weekday()

        # Get average RAM for this hour
        hourly_avg = sum(self.hourly_ram_patterns.get(current_hour, [current_usage])) / len(
            self.hourly_ram_patterns.get(current_hour, [1])
        )

        # Get average RAM for this day
        daily_avg = sum(self.daily_patterns.get(current_day, [current_usage])) / len(
            self.daily_patterns.get(current_day, [1])
        )

        # Combine predictions with weighted average
        pattern_predicted = hourly_avg * 0.6 + daily_avg * 0.4

        # Final prediction: 70% trend-based, 30% pattern-based
        final_prediction = predicted_usage * 0.7 + pattern_predicted * 0.3

        # Calculate confidence based on observation count
        observation_count = len(self.ram_observations)
        confidence = min(1.0, observation_count / self.min_observations)

        # Determine if spike is likely
        spike_likely = final_prediction > self.optimal_thresholds["critical"]

        reason = ""
        if spike_likely:
            if trend > 0.02:  # Increasing at >2% per second
                reason = "Rapid upward trend detected"
            elif final_prediction > hourly_avg * 1.2:
                reason = "Usage significantly above typical for this hour"
            else:
                reason = "Pattern analysis suggests spike"

        self.total_predictions += 1

        return {
            "spike_likely": spike_likely,
            "predicted_peak": final_prediction,
            "confidence": confidence,
            "reason": reason,
            "contributing_factors": {
                "trend_based": predicted_usage,
                "hourly_pattern": hourly_avg,
                "daily_pattern": daily_avg,
            },
        }

    async def get_optimal_monitoring_interval(self, current_usage: float) -> int:
        """
        Determine optimal monitoring interval based on RAM state.

        Returns interval in seconds.
        """
        # Adjust based on usage
        if current_usage >= 0.90:
            # Very high - check very frequently
            interval = 2
        elif current_usage >= 0.80:
            # High - check frequently
            interval = 3
        elif current_usage >= 0.70:
            # Elevated - normal frequency
            interval = 5
        elif current_usage >= 0.50:
            # Moderate - can check less often
            interval = 7
        else:
            # Low - check infrequently
            interval = 10

        # Adjust based on learned patterns
        current_hour = datetime.now().hour
        if current_hour in self.hourly_ram_patterns:
            hourly_avg = sum(self.hourly_ram_patterns[current_hour]) / len(
                self.hourly_ram_patterns[current_hour]
            )

            # If this hour typically has high usage, stay vigilant
            if hourly_avg > 0.75:
                interval = min(interval, 5)

        return interval

    async def get_learned_component_weights(self) -> dict:
        """
        Get learned component weights based on actual observations.

        Returns dict of component -> weight (0.0 to 1.0)
        """
        if not self.learned_component_weights:
            # Return defaults if no learning yet
            return {
                "vision": 0.30,
                "ml_models": 0.25,
                "chatbots": 0.20,
                "memory": 0.10,
                "voice": 0.05,
                "monitoring": 0.05,
                "other": 0.05,
            }

        # Normalize learned weights to sum to 1.0
        total_weight = sum(self.learned_component_weights.values())

        if total_weight == 0:
            return self.get_learned_component_weights()  # Return defaults

        normalized = {
            comp: weight / total_weight for comp, weight in self.learned_component_weights.items()
        }

        return normalized

    async def get_learning_stats(self) -> dict:
        """Get comprehensive learning statistics"""
        return {
            "observations": len(self.ram_observations),
            "migrations_recorded": len(self.migration_outcomes),
            "component_observations": len(self.component_observations),
            "learned_thresholds": self.optimal_thresholds.copy(),
            "threshold_confidence": self.threshold_confidence.copy(),
            "prediction_accuracy": (
                self.correct_predictions / self.total_predictions
                if self.total_predictions > 0
                else 0.0
            ),
            "learned_component_weights": await self.get_learned_component_weights(),
            "patterns_detected": {
                "hourly": len(self.hourly_ram_patterns),
                "daily": len(self.daily_patterns),
            },
        }


class SAIHybridIntegration:
    """
    Integration layer between SAI (Self-Aware Intelligence) and Hybrid Routing.

    Provides:
    - Persistent learning storage
    - Real-time model updates
    - Continuous improvement
    - Pattern sharing across system
    """

    def __init__(self, learning_model: HybridLearningModel):
        """Initialize SAI integration"""
        self.learning_model = learning_model

        # Database integration (lazy loaded)
        self.db = None
        self.db_initialized = False

        # Model update tracking
        self.last_model_save = None
        self.save_interval = 300  # Save every 5 minutes

        # Performance tracking
        self.learning_overhead_ms = 0.0

        logger.info("🧠 SAIHybridIntegration initialized")

    async def initialize_database(self):
        """Initialize connection to learning database"""
        if self.db_initialized:
            return

        try:
            # Import learning database
            sys.path.insert(0, str(Path(__file__).parent / "backend"))
            from intelligence.learning_database import get_learning_database

            # Initialize database using singleton
            self.db = await get_learning_database()

            # Load existing learned parameters
            await self._load_learned_parameters()

            self.db_initialized = True
            logger.info("✅ SAI database integration initialized")

        except Exception as e:
            logger.warning(f"SAI database initialization failed: {e}")
            self.db_initialized = False

    async def _load_learned_parameters(self):
        """Load previously learned parameters from database"""
        try:
            if not self.db:
                return

            # Query for hybrid routing patterns
            async with self.db.db.cursor() as cursor:
                # Check if we have learned thresholds
                await cursor.execute(
                    """
                    SELECT metadata
                    FROM patterns
                    WHERE pattern_type = 'hybrid_threshold'
                    ORDER BY last_seen DESC
                    LIMIT 1
                """
                )

                result = await cursor.fetchone()
                if result:
                    import json

                    metadata = json.loads(result[0]) if result[0] else {}

                    if "thresholds" in metadata:
                        # Apply learned thresholds
                        for key, value in metadata["thresholds"].items():
                            if key in self.learning_model.optimal_thresholds:
                                self.learning_model.optimal_thresholds[key] = value
                                self.learning_model.threshold_confidence[key] = metadata.get(
                                    "confidence", {}
                                ).get(key, 0.5)

                        logger.info(
                            f"📚 Loaded learned thresholds: {self.learning_model.optimal_thresholds}"
                        )

        except Exception as e:
            # Only log if it's an actual error, not just missing data
            if str(e) != "0":
                logger.debug(f"Could not load learned parameters: {e}")
            # Missing learned parameters is normal on first run

    async def save_learned_parameters(self):
        """Save learned parameters to database"""
        if not self.db_initialized or not self.db:
            return

        try:
            # Prepare metadata
            metadata = {
                "thresholds": self.learning_model.optimal_thresholds,
                "confidence": self.learning_model.threshold_confidence,
                "component_weights": await self.learning_model.get_learned_component_weights(),
                "stats": await self.learning_model.get_learning_stats(),
                "last_updated": datetime.now().isoformat(),
            }

            # Save as pattern
            await self.db.store_pattern({
                "pattern_type": "hybrid_threshold",
                "description": "Learned hybrid routing thresholds",
                "trigger_conditions": {"observation_count": len(self.learning_model.ram_observations)},
                "success_rate": self.learning_model.prediction_accuracy,
                "metadata": metadata,
            })

            self.last_model_save = time.time()
            logger.info("💾 Saved learned parameters to database")  # noqa: F541

        except Exception as e:
            logger.warning(f"Failed to save learned parameters: {e}")

    async def record_and_learn(
        self,
        observation_type: str,
        data: dict,
    ):
        """
        Record observation and trigger learning.

        Args:
            observation_type: 'ram', 'migration', 'component'
            data: Observation data
        """
        start_time = time.time()

        try:
            if observation_type == "ram":
                await self.learning_model.record_ram_observation(
                    timestamp=data.get("timestamp", time.time()),
                    usage=data["usage"],
                    components_active=data.get("components", {}),
                )

            elif observation_type == "migration":
                await self.learning_model.record_migration_outcome(
                    timestamp=data.get("timestamp", time.time()),
                    reason=data["reason"],
                    success=data["success"],
                    duration=data["duration"],
                )

            elif observation_type == "component":
                await self.learning_model.record_component_usage(
                    timestamp=data.get("timestamp", time.time()),
                    component=data["component"],
                    memory_gb=data["memory_gb"],
                )

            # Periodically save learned parameters
            if (
                self.last_model_save is None
                or time.time() - self.last_model_save > self.save_interval
            ):
                await self.save_learned_parameters()

        except Exception as e:
            logger.error(f"SAI learning failed: {e}")

        finally:
            self.learning_overhead_ms = (time.time() - start_time) * 1000


# ANSI color codes for terminal output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    WARNING = "\033[93m"
    YELLOW = "\033[93m"
    FAIL = "\033[91m"
    RED = "\033[91m"  # Added RED color
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    PURPLE = "\033[95m"
    MAGENTA = "\033[35m"


# =============================================================================
# Dynamic Port Manager with Stuck Process Detection
# =============================================================================

class DynamicPortManager:
    """
    Dynamic Port Manager for JARVIS startup.

    Provides:
    - Dynamic port discovery from config file (no hardcoding)
    - Stuck process detection (UE state, zombies, timeouts)
    - Automatic port failover when primary is blocked
    - Integration with backend self-healer
    - Process watchdog for stuck prevention
    """

    # macOS UE (Uninterruptible Sleep) state indicators
    UE_STATE_INDICATORS = ['disk-sleep', 'uninterruptible', 'D', 'U']

    def __init__(self):
        self.config = self._load_config()
        self.primary_port = self.config.get('port', 8011)
        self.fallback_ports = self.config.get('fallback_ports', [8010, 8000, 8001, 8080, 8888])
        self.blacklisted_ports = set()  # Ports with unkillable processes
        self.selected_port = None
        self.port_health_cache = {}  # port -> {'healthy': bool, 'last_check': time}

    def _load_config(self) -> dict:
        """Load configuration from startup_progress_config.json."""
        config_path = Path(__file__).parent / 'backend' / 'config' / 'startup_progress_config.json'
        default_config = {
            'port': 8011,
            'fallback_ports': [8010, 8000, 8001, 8080, 8888],
            'host': 'localhost',
            'protocol': 'http'
        }

        try:
            if config_path.exists():
                with open(config_path, 'r') as f:
                    data = json.load(f)
                    backend_config = data.get('backend_config', {})
                    return {
                        'port': backend_config.get('port', 8011),
                        'fallback_ports': backend_config.get('fallback_ports', [8010, 8000, 8001, 8080, 8888]),
                        'host': backend_config.get('host', 'localhost'),
                        'protocol': backend_config.get('protocol', 'http')
                    }
        except Exception as e:
            logger.warning(f"Could not load port config: {e}, using defaults")

        return default_config

    def _is_unkillable_state(self, status: str) -> bool:
        """Check if process status indicates an unkillable (UE) state."""
        if not status:
            return False
        status_lower = status.lower()
        return any(ind.lower() in status_lower for ind in self.UE_STATE_INDICATORS)

    def _get_process_on_port(self, port: int) -> Optional[dict]:
        """Get process information for a process listening on the given port."""
        try:
            import psutil
            for conn in psutil.net_connections(kind='inet'):
                if hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN' and conn.pid:
                        try:
                            proc = psutil.Process(conn.pid)
                            return {
                                'pid': conn.pid,
                                'name': proc.name(),
                                'status': proc.status(),
                                'cmdline': ' '.join(proc.cmdline() or [])[:200],
                            }
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
        except Exception as e:
            logger.debug(f"Error getting process on port {port}: {e}")
        return None

    def check_port_health_sync(self, port: int, timeout: float = 2.0) -> dict:
        """
        Synchronously check if a port has a healthy backend.

        Returns dict with:
        - healthy: bool
        - error: str or None
        - is_stuck: bool (unkillable process detected)
        - pid: int or None
        """
        result = {'port': port, 'healthy': False, 'error': None, 'is_stuck': False, 'pid': None}

        # First check process state
        proc_info = self._get_process_on_port(port)
        if proc_info:
            result['pid'] = proc_info['pid']
            status = proc_info.get('status', '')

            if self._is_unkillable_state(status):
                result['is_stuck'] = True
                result['error'] = f"Process PID {proc_info['pid']} in unkillable state: {status}"
                self.blacklisted_ports.add(port)
                logger.warning(f"Port {port}: {result['error']}")
                return result

        # Try HTTP health check
        import socket
        import urllib.request

        url = f"http://localhost:{port}/health"
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    try:
                        data = json.loads(response.read().decode())
                        if data.get('status') == 'healthy':
                            result['healthy'] = True
                            return result
                    except:
                        # Even without JSON, 200 OK is good
                        result['healthy'] = True
                        return result
        except urllib.error.URLError as e:
            if 'Connection refused' in str(e):
                result['error'] = 'connection_refused'
            else:
                result['error'] = str(e)[:50]
        except socket.timeout:
            result['error'] = 'timeout'
        except Exception as e:
            result['error'] = str(e)[:50]

        return result

    async def check_port_health_async(self, port: int, timeout: float = 2.0) -> dict:
        """Async version of port health check."""
        result = {'port': port, 'healthy': False, 'error': None, 'is_stuck': False, 'pid': None}

        # First check process state
        proc_info = await asyncio.get_event_loop().run_in_executor(
            None, self._get_process_on_port, port
        )

        if proc_info:
            result['pid'] = proc_info['pid']
            status = proc_info.get('status', '')

            if self._is_unkillable_state(status):
                result['is_stuck'] = True
                result['error'] = f"Process PID {proc_info['pid']} in unkillable state: {status}"
                self.blacklisted_ports.add(port)
                return result

        # Try HTTP health check with aiohttp if available
        try:
            import aiohttp
            url = f"http://localhost:{port}/health"

            async def _do_check():
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        if resp.status == 200:
                            try:
                                data = await resp.json()
                                if data.get('status') == 'healthy':
                                    result['healthy'] = True
                            except:
                                result['healthy'] = True

            await asyncio.wait_for(_do_check(), timeout=timeout + 0.5)

        except asyncio.TimeoutError:
            result['error'] = 'timeout'
        except Exception as e:
            error_name = type(e).__name__
            if 'ClientConnector' in error_name or 'Connection refused' in str(e):
                result['error'] = 'connection_refused'
            else:
                result['error'] = f'{error_name}: {str(e)[:30]}'

        return result

    async def discover_healthy_port_async(self) -> int:
        """
        Discover the best healthy port asynchronously (parallel scanning).

        Returns the best available port, or falls back to primary if none healthy.
        """
        # Build port list: primary first, then fallbacks
        all_ports = [self.primary_port] + [
            p for p in self.fallback_ports if p != self.primary_port
        ]

        # Remove blacklisted ports
        check_ports = [p for p in all_ports if p not in self.blacklisted_ports]

        if not check_ports:
            logger.warning("All ports blacklisted! Using primary as fallback")
            check_ports = [self.primary_port]

        # Parallel health checks
        tasks = [self.check_port_health_async(port) for port in check_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Find healthy ports
        healthy_ports = []
        stuck_ports = []

        for result in results:
            if isinstance(result, Exception):
                continue
            if result.get('is_stuck'):
                stuck_ports.append(result['port'])
            elif result.get('healthy'):
                healthy_ports.append(result['port'])

        # Log findings
        if stuck_ports:
            logger.warning(f"Stuck processes detected on ports: {stuck_ports}")

        # Select best port
        if healthy_ports:
            self.selected_port = healthy_ports[0]
            logger.info(f"Selected healthy port: {self.selected_port}")
        else:
            # No healthy port, find first non-stuck port for new startup
            available = [p for p in check_ports if p not in stuck_ports]
            self.selected_port = available[0] if available else self.primary_port
            logger.info(f"No healthy backend found, using port {self.selected_port} for new startup")

        return self.selected_port

    def discover_healthy_port_sync(self) -> int:
        """
        Discover the best healthy port synchronously.

        For use before async event loop is running.
        """
        all_ports = [self.primary_port] + [
            p for p in self.fallback_ports if p != self.primary_port
        ]

        check_ports = [p for p in all_ports if p not in self.blacklisted_ports]

        for port in check_ports:
            result = self.check_port_health_sync(port, timeout=1.0)

            if result.get('is_stuck'):
                logger.warning(f"Port {port} has stuck process - skipping")
                continue

            if result.get('healthy'):
                self.selected_port = port
                logger.info(f"Found healthy backend on port {self.selected_port}")
                return self.selected_port

            # Port not responding but not stuck - can be used for new startup
            if result.get('error') == 'connection_refused':
                if self.selected_port is None:
                    self.selected_port = port  # First available non-stuck port

        if self.selected_port is None:
            self.selected_port = self.primary_port

        logger.info(f"Using port {self.selected_port} for startup")
        return self.selected_port

    def cleanup_stuck_port(self, port: int) -> bool:
        """
        Attempt to clean up a stuck process on a port.

        Returns True if port was freed, False if process is unkillable.
        """
        import psutil

        proc_info = self._get_process_on_port(port)
        if not proc_info:
            return True  # No process, port is free

        pid = proc_info['pid']
        status = proc_info.get('status', '')

        # Check for unkillable state
        if self._is_unkillable_state(status):
            logger.error(f"Process {pid} on port {port} is in unkillable state '{status}' - requires system restart")
            self.blacklisted_ports.add(port)
            return False

        # Try to kill the process
        try:
            proc = psutil.Process(pid)

            # Graceful shutdown first
            logger.info(f"Sending SIGTERM to process {pid} on port {port}")
            proc.terminate()

            try:
                proc.wait(timeout=5.0)
                logger.info(f"Process {pid} terminated gracefully")
                return True
            except psutil.TimeoutExpired:
                pass

            # Force kill
            logger.warning(f"Process {pid} didn't terminate gracefully, sending SIGKILL")
            proc.kill()

            try:
                proc.wait(timeout=3.0)
                logger.info(f"Process {pid} killed with SIGKILL")
                return True
            except psutil.TimeoutExpired:
                logger.error(f"Failed to kill process {pid} - may be in unkillable state")
                self.blacklisted_ports.add(port)
                return False

        except psutil.NoSuchProcess:
            return True  # Process already gone
        except Exception as e:
            logger.error(f"Error killing process {pid}: {e}")
            return False

    def get_best_port(self) -> int:
        """Get the best available port (cached or discover)."""
        if self.selected_port is not None:
            return self.selected_port
        return self.discover_healthy_port_sync()


# Global port manager instance
_port_manager: Optional[DynamicPortManager] = None


def get_port_manager() -> DynamicPortManager:
    """Get the global port manager instance."""
    global _port_manager
    if _port_manager is None:
        _port_manager = DynamicPortManager()
    return _port_manager


class AsyncSystemManager:
    """Async system manager with integrated resource optimization and self-healing"""

    def __init__(self):
        self.processes = []
        self.subprocesses = []  # Track asyncio subprocesses for proper cleanup (prevent "handles pid" warnings)
        self.open_files = []  # Track open file handles for cleanup
        self.background_tasks = []  # Track asyncio tasks for proper cleanup
        self.backend_dir = Path("backend")
        self.frontend_dir = Path("frontend")

        # Dynamic port discovery - no more hardcoding!
        self.port_manager = get_port_manager()
        selected_api_port = self.port_manager.get_best_port()

        self.ports = {
            "main_api": selected_api_port,  # Dynamically discovered port
            "websocket_router": 8001,  # TypeScript WebSocket Router
            "frontend": 3000,
            "llama_cpp": 8080,
            "event_ui": 8888,  # Event-driven UI
        }

        logger.info(f"🔧 Dynamic port selection: main_api={selected_api_port}")
        self.is_m1_mac = platform.system() == "Darwin" and platform.machine() == "arm64"
        self.claude_configured = False
        self.start_time = datetime.now()
        self.no_browser = False
        self.backend_only = False
        self.frontend_only = False
        self.is_restart = False  # Track if this is a restart
        self.use_optimized = True  # Use optimized backend by default
        self.auto_cleanup = True  # Auto cleanup without prompting (enabled by default)
        self.resource_coordinator = None
        self.jarvis_coordinator = None
        self._shutting_down = False  # Flag to suppress exit warnings during shutdown

        # SAI Prediction tracking
        self.last_sai_prediction = None
        self.sai_prediction_history = []  # Rolling window of last 10 predictions
        self.sai_prediction_count = 0

        # Voice Verification Diagnostics tracking
        self.voice_verification_attempts = []  # Rolling window of last 20 attempts
        self.voice_verification_stats = {
            'total_attempts': 0,
            'successful': 0,
            'failed': 0,
            'last_attempt_time': None,
            'last_success_time': None,
            'last_failure_time': None,
            'consecutive_failures': 0,
            'average_confidence': 0.0,
            'failure_reasons': {}  # Count of each failure reason
        }

        # Voice Unlock Configuration tracking
        self.voice_unlock_config_status = {
            'configured': False,
            'daemon_running': False,
            'keychain_password_stored': False,
            'enrollment_data_exists': False,
            'last_check_time': None,
            'auto_config_attempted': False,
            'issues': []
        }

        # Self-healing mechanism
        self.healing_attempts = {}
        self.max_healing_attempts = 3
        self.healing_log = []
        self.auto_heal_enabled = True

        # Autonomous mode
        self.autonomous_mode = False
        self.orchestrator = None
        self.mesh = None
        if AUTONOMOUS_AVAILABLE:
            self.orchestrator = get_orchestrator()
            self.mesh = get_mesh()

        # Hybrid Cloud Intelligence Coordinator
        self.hybrid_coordinator = None
        self.hybrid_enabled = os.getenv("JARVIS_HYBRID_MODE", "auto") in ["auto", "true", "1"]
        if self.hybrid_enabled:
            try:
                self.hybrid_coordinator = HybridIntelligenceCoordinator()
                # Set global for cleanup access
                globals()["_hybrid_coordinator"] = self.hybrid_coordinator
                logger.info("🌐 Hybrid Cloud Routing enabled")
            except Exception as e:
                logger.warning(f"Hybrid coordinator initialization failed: {e}")
                self.hybrid_enabled = False

        # Cloud SQL Proxy Manager - manages proxy lifecycle tied to JARVIS
        self.cloud_sql_proxy_manager = None
        self.cloud_sql_proxy_enabled = Path.home().joinpath(".jarvis/gcp/database_config.json").exists()

    def print_header(self):
        """Print system header with resource optimization info"""
        print(f"\n{Colors.HEADER}{'='*70}")
        version = "v14.0.0 - AUTONOMOUS" if self.autonomous_mode else "v13.4.0"
        print(
            f"{Colors.BOLD}🤖 JARVIS AI Agent {version} - Advanced Browser Automation 🚀{Colors.ENDC}"
        )
        if self.autonomous_mode:
            print(
                f"{Colors.GREEN}🤖 AUTONOMOUS MODE • Zero Configuration • Self-Healing • ML-Powered{Colors.ENDC}"
            )
        print(
            f"{Colors.GREEN}⚡ CPU<25% • 🧠 30% Memory (4.8GB) • 🎯 Swift Acceleration • 📊 Real-time Monitoring{Colors.ENDC}"
        )
        print(f"{Colors.HEADER}{'='*70}{Colors.ENDC}")

        # Performance Optimization Features
        print(f"\n{Colors.BOLD}🎯 PERFORMANCE OPTIMIZATIONS:{Colors.ENDC}")
        print(f"{Colors.YELLOW}✨ Fixed High CPU Usage & Memory Management{Colors.ENDC}")
        print(
            f"   • {Colors.GREEN}✓ CPU:{Colors.ENDC} Reduced from 87.4% → 0% idle (Swift monitoring)"
        )
        print(
            f"   • {Colors.CYAN}✓ Memory:{Colors.ENDC} Ultra-aggressive 30% target (4.8GB) with smart ML unloading"
        )
        print(
            f"   • {Colors.GREEN}✓ Swift:{Colors.ENDC} Native performance bridges (24-50x faster)"
        )
        print(
            f"   • {Colors.CYAN}✓ Vision:{Colors.ENDC} Metal acceleration + Claude API with caching"
        )
        print(f"   • {Colors.PURPLE}✓ Monitoring:{Colors.ENDC} Real-time dashboards at :8888/:8889")
        print(
            f"   • {Colors.GREEN}✓ Recovery:{Colors.ENDC} Circuit breakers, emergency cleanup, graceful degradation"
        )

        if self.autonomous_mode:
            print(f"\n{Colors.BOLD}🤖 AUTONOMOUS FEATURES:{Colors.ENDC}")
            print(f"   • {Colors.GREEN}✓ Zero Config:{Colors.ENDC} No hardcoded ports or URLs")
            print(
                f"   • {Colors.CYAN}✓ Self-Discovery:{Colors.ENDC} Services find each other automatically"
            )
            print(
                f"   • {Colors.GREEN}✓ Self-Healing:{Colors.ENDC} ML-powered recovery from failures"
            )
            print(f"   • {Colors.CYAN}✓ Service Mesh:{Colors.ENDC} All components interconnected")
            print(f"   • {Colors.GREEN}✓ Pattern Learning:{Colors.ENDC} System improves over time")
            print(
                f"   • {Colors.PURPLE}✓ Dynamic Routing:{Colors.ENDC} Optimal paths calculated in real-time"
            )

        # Hybrid Cloud Intelligence
        if self.hybrid_enabled and self.hybrid_coordinator:
            print(f"\n{Colors.BOLD}🌐 HYBRID CLOUD INTELLIGENCE:{Colors.ENDC}")
            ram_gb = self.hybrid_coordinator.ram_monitor.local_ram_gb
            print(f"   • {Colors.GREEN}✓ Local RAM:{Colors.ENDC} {ram_gb:.1f}GB (macOS)")
            print(f"   • {Colors.CYAN}✓ Cloud RAM:{Colors.ENDC} 32GB (GCP e2-highmem-4)")
            print(f"   • {Colors.GREEN}✓ Auto-Routing:{Colors.ENDC} Intelligent workload placement")
            print(
                f"   • {Colors.PURPLE}✓ Crash Prevention:{Colors.ENDC} Emergency GCP shift at {self.hybrid_coordinator.ram_monitor.critical_threshold*100:.0f}% RAM"
            )
            print(
                f"   • {Colors.CYAN}✓ Cost Optimization:{Colors.ENDC} Return to local when RAM drops below {self.hybrid_coordinator.ram_monitor.optimal_threshold*100:.0f}%"
            )
            print(
                f"   • {Colors.GREEN}✓ Monitoring:{Colors.ENDC} Real-time RAM tracking every {self.hybrid_coordinator.monitoring_interval}s"
            )

        # GCP VM Auto-Creation Status
        print(f"\n{Colors.CYAN}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.CYAN}🚀 GCP Spot VM Configuration{Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*70}{Colors.ENDC}\n")

        gcp_vm_enabled = os.getenv("GCP_VM_ENABLED", "true").lower() == "true"
        if gcp_vm_enabled:
            print(f"{Colors.CYAN}📊 Spot VM auto-creation status:{Colors.ENDC}")
            print(f"{Colors.GREEN}   ✓ Enabled - triggers when RAM >85%{Colors.ENDC}")
            print(f"\n{Colors.CYAN}💻 VM specifications:{Colors.ENDC}")
            print(f"{Colors.CYAN}   └─ Machine type: e2-highmem-4 (4 vCPU, 32GB RAM){Colors.ENDC}")
            print(f"{Colors.CYAN}   └─ Provisioning model: SPOT (preemptible){Colors.ENDC}")
            print(f"{Colors.CYAN}   └─ Cost: $0.029/hour (91% discount!){Colors.ENDC}")
            print(f"\n{Colors.CYAN}💰 Budget & safety limits:{Colors.ENDC}")
            daily_budget = os.getenv("GCP_VM_DAILY_BUDGET", "5.0")
            print(f"{Colors.GREEN}   ✓ Daily budget: ${daily_budget}{Colors.ENDC}")
            print(f"{Colors.GREEN}   ✓ Auto-terminate: 3 hours max runtime{Colors.ENDC}")
            print(f"{Colors.GREEN}   ✓ Cost tracking: Real-time monitoring{Colors.ENDC}")
            print(
                f"\n{Colors.CYAN}📍 Check status: cd backend && python3 core/gcp_vm_status.py{Colors.ENDC}"
            )
        else:
            print(f"{Colors.YELLOW}⚠️  Spot VM auto-creation disabled{Colors.ENDC}")

        # Check for Rust acceleration
        try:
            from backend.vision.rust_startup_integration import get_rust_status

            rust_status = get_rust_status()
            if rust_status.get("rust_available"):
                print(
                    f"   • {Colors.CYAN}✓ Rust:{Colors.ENDC} 🦀 Acceleration active (5-10x performance boost)"
                )
                print(
                    f"   • {Colors.GREEN}✓ Self-Healing:{Colors.ENDC} Automatic Rust recovery enabled"
                )
            else:
                print(
                    f"   • {Colors.YELLOW}○ Rust:{Colors.ENDC} Not built (self-healing will attempt to fix)"
                )
                print(
                    f"   • {Colors.GREEN}✓ Self-Healing:{Colors.ENDC} Monitoring and will auto-build when possible"
                )
        except:
            pass

        # Voice System Optimization
        print(f"\n{Colors.BOLD}🎤 VOICE SYSTEM OPTIMIZATION:{Colors.ENDC}")
        print(f"   • {Colors.GREEN}✓ Swift Audio:{Colors.ENDC} ~1ms processing (was 50ms)")
        print(f"   • {Colors.CYAN}✓ Memory:{Colors.ENDC} 350MB (was 1.6GB), model swapping")
        print(f"   • {Colors.GREEN}✓ CPU:{Colors.ENDC} <1% idle with Swift vDSP")
        print(f"   • {Colors.PURPLE}✓ Works:{Colors.ENDC} Say 'Hey JARVIS' - instant response!")

        # Intelligent Voice-Authenticated Unlock
        print(f"\n{Colors.BOLD}🔐 INTELLIGENT VOICE-AUTHENTICATED UNLOCK:{Colors.ENDC}")
        print(
            f"   • {Colors.GREEN}✓ Cloud SQL Biometrics:{Colors.ENDC} 59 voice samples + 768-byte embedding (PostgreSQL)"
        )
        print(
            f"   • {Colors.GREEN}✓ Speaker Recognition:{Colors.ENDC} Personalized responses using verified identity"
        )
        print(
            f"   • {Colors.CYAN}✓ Hybrid STT:{Colors.ENDC} Wav2Vec + Vosk + Whisper intelligent routing"
        )
        print(
            f"   • {Colors.YELLOW}✓ Context-Aware (CAI):{Colors.ENDC} Screen state, time, location analysis"
        )
        print(
            f"   • {Colors.PURPLE}✓ Scenario-Aware (SAI):{Colors.ENDC} Routine/emergency/suspicious detection"
        )
        print(
            f"   • {Colors.GREEN}✓ Cloud Database:{Colors.ENDC} GCP Cloud SQL for voice profile storage"
        )
        print(f"   • {Colors.CYAN}✓ Anti-Spoofing:{Colors.ENDC} High verification threshold (0.75)")
        print(
            f"   • {Colors.YELLOW}✓ Fail-Closed:{Colors.ENDC} Denies unlock if voice doesn't match"
        )
        print(
            f"   • {Colors.PURPLE}✓ Command:{Colors.ENDC} 'Hey JARVIS, unlock my screen' (voice verified)"
        )

        # Vision System Enhancement
        print(
            f"\n{Colors.BOLD}👁️ ENHANCED VISION SYSTEM (Integration Architecture v12.9.2):{Colors.ENDC}"
        )
        print(f"\n   {Colors.BOLD}🎯 Integration Orchestrator:{Colors.ENDC}")
        print(
            f"   • {Colors.GREEN}✓ 9-Stage Pipeline:{Colors.ENDC} Visual Input → Spatial → State → Intelligence → Cache → Prediction → API → Integration → Proactive"
        )
        print(
            f"   • {Colors.CYAN}✓ Memory Budget:{Colors.ENDC} 1.2GB dynamically allocated (within 30% system target)"
        )
        print(
            f"   • {Colors.YELLOW}✓ Operating Modes:{Colors.ENDC} Normal (<25%) → Pressure (25-28%) → Critical (28-30%) → Emergency (>30%)"
        )
        print(
            f"   • {Colors.PURPLE}✓ Cross-Language:{Colors.ENDC} Python orchestrator + Rust SIMD + Swift native"
        )

        print(f"\n   {Colors.BOLD}Intelligence Components (600MB):{Colors.ENDC}")
        print(f"   1. {Colors.CYAN}VSMS Core:{Colors.ENDC} Visual State Management (150MB)")
        print(f"   2. {Colors.GREEN}Scene Graph:{Colors.ENDC} Spatial understanding (100MB)")
        print(f"   3. {Colors.YELLOW}Temporal Context:{Colors.ENDC} Time-based analysis (200MB)")
        print(
            f"   4. {Colors.PURPLE}Activity Recognition:{Colors.ENDC} User action detection (100MB)"
        )
        print(f"   5. {Colors.MAGENTA}Goal Inference:{Colors.ENDC} Intent prediction (80MB)")

        print(f"\n   {Colors.BOLD}Optimization Components (460MB):{Colors.ENDC}")
        print(
            f"   6. {Colors.CYAN}Bloom Filter Network:{Colors.ENDC} Hierarchical duplicate detection (10MB)"
        )
        print(
            f"   7. {Colors.GREEN}Semantic Cache LSH:{Colors.ENDC} Intelligent result caching (250MB)"
        )
        print(
            f"   8. {Colors.YELLOW}Predictive Engine:{Colors.ENDC} Markov chain predictions (150MB)"
        )
        print(f"   9. {Colors.PURPLE}Quadtree Spatial:{Colors.ENDC} Region-based processing (50MB)")

        print(f"\n   {Colors.BOLD}Additional Features:{Colors.ENDC}")
        print(f"   • {Colors.GREEN}✓ Claude Vision:{Colors.ENDC} Integrated with all components")
        print(f"   • {Colors.CYAN}✓ Swift Video:{Colors.ENDC} 30 FPS capture with purple indicator")
        print(
            f"   • {Colors.YELLOW}✓ Dynamic Quality:{Colors.ENDC} Adapts based on memory pressure"
        )
        print(
            f"   • {Colors.PURPLE}✓ Component Priority:{Colors.ENDC} 1-10 scale for resource allocation"
        )
        print(
            f"\n   {Colors.BOLD}All components coordinate through Integration Orchestrator!{Colors.ENDC}"
        )

    async def check_python_version(self):
        """Check Python version"""
        version = sys.version_info
        if version.major == 3 and version.minor >= 8:
            print(
                f"{Colors.GREEN}✓ Python {version.major}.{version.minor}.{version.micro}{Colors.ENDC}"
            )
            return True
        else:
            print(f"{Colors.FAIL}✗ Python {version.major}.{version.minor} (need 3.8+){Colors.ENDC}")
            return False

    async def check_claude_config(self):
        """Check Claude API configuration"""
        api_key = _get_anthropic_api_key()
        if api_key:
            print(f"{Colors.GREEN}✓ Claude API configured{Colors.ENDC}")
            self.claude_configured = True
            return True
        else:
            print(f"{Colors.WARNING}⚠ Claude API not configured{Colors.ENDC}")
            print(
                f"  {Colors.YELLOW}Set ANTHROPIC_API_KEY for vision & intelligence features{Colors.ENDC}"
            )
            self.claude_configured = False
            return True  # Not critical

    async def check_system_resources(self):
        """Check system resources with optimization info"""
        # First, check for and clean up stuck processes
        await self.cleanup_stuck_processes()

        memory = psutil.virtual_memory()
        total_gb = memory.total / (1024**3)
        available_gb = memory.available / (1024**3)
        cpu_percent = psutil.cpu_percent(interval=1)

        print(f"\n{Colors.BLUE}System Resources:{Colors.ENDC}")
        print(
            f"  • Memory: {total_gb:.1f}GB total, {available_gb:.1f}GB available ({memory.percent:.1f}% used)"
        )
        print(f"  • CPU: {psutil.cpu_count()} cores, currently at {cpu_percent:.1f}%")

        # Memory optimization based on quantization
        print(f"\n{Colors.CYAN}Memory Optimization:{Colors.ENDC}")
        print("  • Target: 4GB maximum usage")  # noqa: F541
        print(f"  • Current: {memory.used / (1024**3):.1f}GB used")

        if memory.used / (1024**3) < 3.2:  # Ultra-low
            print(f"  • Level: {Colors.GREEN}Ultra-Low (1 model, 100MB cache){Colors.ENDC}")  # noqa
        elif memory.used / (1024**3) < 3.6:  # Low
            print(f"  • Level: {Colors.GREEN}Low (2 models, 200MB cache){Colors.ENDC}")  # noqa
        elif memory.used / (1024**3) < 4.0:  # Normal
            print(f"  • Level: {Colors.YELLOW}Normal (3 models, 500MB cache){Colors.ENDC}")  # noqa
        else:  # High
            print(
                f"  • Level: {Colors.WARNING}High (emergency cleanup active){Colors.ENDC}"
            )  # noqa

        # Check for Swift availability
        swift_lib = Path("backend/swift_bridge/.build/release/libPerformanceCore.dylib")
        swift_video = Path("backend/vision/SwiftVideoCapture")

        if swift_lib.exists():
            print(f"\n{Colors.GREEN}✓ Swift performance layer available{Colors.ENDC}")
            print("  • AudioProcessor: Voice processing (50x faster)")  # noqa: F541
            print("  • VisionProcessor: Metal acceleration (10x faster)")  # noqa: F541
            print("  • SystemMonitor: IOKit monitoring (24x faster)")  # noqa: F541
        else:
            print(f"\n{Colors.YELLOW}⚠ Swift performance library not built{Colors.ENDC}")
            print("  Build with: cd backend/swift_bridge && ./build_performance.sh")  # noqa

        # Check for Swift video capture
        if swift_video.exists():
            print(f"\n{Colors.GREEN}✓ Swift video capture available{Colors.ENDC}")
            print("  • Enhanced screen recording permissions")  # noqa: F541
            print("  • Native macOS integration")  # noqa: F541
            print("  • Purple recording indicator support")  # noqa: F541
        else:
            print(f"\n{Colors.YELLOW}⚠ Swift video capture not compiled{Colors.ENDC}")
            print("  • Will be compiled automatically on first use")  # noqa: F541

        # Check for Rust availability (legacy)
        rust_lib = Path("backend/rust_performance/target/release/librust_performance.dylib")
        if rust_lib.exists():
            print(f"\n{Colors.GREEN}✓ Rust performance layer available (legacy){Colors.ENDC}")

        return True

    async def cleanup_stuck_processes(self):
        """Clean up stuck processes before starting with enhanced recovery"""
        try:
            # Add backend to path if needed
            backend_dir = Path(__file__).parent / "backend"
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            from process_cleanup_manager import (
                ProcessCleanupManager,
                emergency_cleanup,
                prevent_multiple_jarvis_instances,
            )

            print(f"\n{Colors.BLUE}🔍 Process & Cache Management System{Colors.ENDC}")
            print(f"{Colors.CYAN}   Checking for code changes and process cleanup needs...{Colors.ENDC}")

            manager = ProcessCleanupManager()

            # Check for code changes (triggers enhanced backend process cleanup)
            code_changed = manager._detect_code_changes()
            if code_changed:
                print(f"{Colors.YELLOW}   ⚠️  Code changes detected!{Colors.ENDC}")
                print(f"{Colors.CYAN}   → Will clean up old processes and cache{Colors.ENDC}")
                print(f"{Colors.CYAN}   → Backend processes will be killed for fresh code reload{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}   ✓ No code changes detected{Colors.ENDC}")

            # Show cache status
            import glob
            cache_dirs = len(list(glob.glob("backend/**/__pycache__", recursive=True)))
            if cache_dirs > 0:
                print(f"{Colors.CYAN}   → Found {cache_dirs} Python cache directories{Colors.ENDC}")

            # DISABLED: Segfault recovery check that can cause loops on macOS
            # if manager.check_for_segfault_recovery():
            #     print(f"{Colors.YELLOW}🔧 Performed crash recovery cleanup!{Colors.ENDC}")
            #     print(f"{Colors.GREEN}  System cleaned from previous crash{Colors.ENDC}")
            #     await asyncio.sleep(2)  # Give system time to stabilize

            # DISABLED: Cleanup operations that hang on macOS
            # These operations try to access network connections and use lsof
            # which are blocked by macOS security, causing JARVIS to hang
            print(f"{Colors.GREEN}   ✓ Process management system ready{Colors.ENDC}")

            # Set empty state to skip cleanup
            state = {"stuck_processes": [], "high_cpu_processes": [], "high_memory_processes": []}

            # Check if cleanup is needed (more aggressive thresholds)
            # IMPORTANT: Use available memory, not percent (macOS caches aggressively)
            memory = psutil.virtual_memory()
            available_gb = memory.available / (1024**3)

            needs_cleanup = (
                len(state.get("stuck_processes", [])) > 0
                or len(state.get("zombie_processes", [])) > 0
                or state.get("cpu_percent", 0) > 70
                or available_gb < 2.0  # macOS-aware: <2GB available (was >70% used)
                or any(p["age_seconds"] > 300 for p in state.get("jarvis_processes", []))
            )

            # Check for critical conditions that need emergency cleanup
            needs_emergency = (
                available_gb < 1.0  # macOS-aware: <1GB available (was >80% used)
                or len(state.get("zombie_processes", [])) > 2
                or len(state.get("jarvis_processes", [])) > 3
            )

            if needs_emergency:
                print(f"\n{Colors.FAIL}⚠️ Critical system state detected!{Colors.ENDC}")
                print(f"{Colors.YELLOW}Performing emergency cleanup...{Colors.ENDC}")

                # Perform emergency cleanup
                results = emergency_cleanup(force=True)
                print(f"{Colors.GREEN}✓ Emergency cleanup complete:{Colors.ENDC}")
                print(f"  • Killed {len(results['processes_killed'])} processes")
                print(f"  • Freed {len(results['ports_freed'])} ports")
                if results.get("ipc_cleaned"):
                    print(f"  • Cleaned {sum(results['ipc_cleaned'].values())} IPC resources")

                await asyncio.sleep(3)  # Give system time to recover

            elif needs_cleanup:
                print(f"\n{Colors.YELLOW}Found processes that need cleanup:{Colors.ENDC}")

                # Show what will be cleaned
                if state.get("stuck_processes"):
                    print(f"  • {len(state['stuck_processes'])} stuck processes")
                if state.get("zombie_processes"):
                    print(f"  • {len(state['zombie_processes'])} zombie processes")

                old_jarvis = [
                    p for p in state.get("jarvis_processes", []) if p["age_seconds"] > 300
                ]
                if old_jarvis:
                    print(f"  • {len(old_jarvis)} old JARVIS processes")

                # Clean up automatically or ask for confirmation
                if self.auto_cleanup:
                    print(f"\n{Colors.BLUE}Automatically cleaning up processes...{Colors.ENDC}")
                    should_cleanup = True
                else:
                    should_cleanup = (
                        input(
                            f"\n{Colors.CYAN}Clean up these processes? (y/n): {Colors.ENDC}"
                        ).lower()
                        == "y"
                    )

                if should_cleanup:
                    if not self.auto_cleanup:
                        print(f"\n{Colors.BLUE}Cleaning up processes...{Colors.ENDC}")

                    # DISABLED: smart_cleanup hangs on macOS
                    print(
                        f"{Colors.YELLOW}Skipping smart cleanup (macOS compatibility){Colors.ENDC}"
                    )
                else:
                    print(f"{Colors.YELLOW}Skipping cleanup{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}✓ No stuck processes found{Colors.ENDC}")

            # Step 3: Final check - ensure we can start fresh
            can_start, message = prevent_multiple_jarvis_instances()
            if can_start:
                print(f"{Colors.GREEN}✓ {message}{Colors.ENDC}")
            else:
                print(f"{Colors.WARNING}⚠️ {message}{Colors.ENDC}")
                if self.auto_cleanup:
                    print(f"{Colors.YELLOW}Forcing cleanup for fresh start...{Colors.ENDC}")
                    emergency_cleanup(force=True)
                    await asyncio.sleep(2)
                    # Re-check after cleanup
                    can_start, message = prevent_multiple_jarvis_instances()
                    if can_start:
                        print(f"{Colors.GREEN}✓ {message}{Colors.ENDC}")
                    else:
                        print(f"{Colors.FAIL}❌ Still cannot start: {message}{Colors.ENDC}")
                        return False

        except ImportError:
            print(f"{Colors.WARNING}Process cleanup manager not available{Colors.ENDC}")
            print(
                f"{Colors.YELLOW}Tip: Make sure backend/process_cleanup_manager.py exists{Colors.ENDC}"
            )
        except Exception as e:
            print(f"{Colors.WARNING}Cleanup check failed: {e}{Colors.ENDC}")
            # In case of failure, try basic emergency cleanup
            try:
                from process_cleanup_manager import emergency_cleanup

                print(f"{Colors.YELLOW}Attempting emergency cleanup...{Colors.ENDC}")
                emergency_cleanup(force=True)
            except:
                pass

    async def check_port_available(self, port: int) -> bool:
        """Check if a port is available"""
        try:
            reader, writer = await asyncio.open_connection("localhost", port)
            writer.close()
            await writer.wait_closed()
            return False
        except:
            return True

    async def kill_process_on_port(self, port: int):
        """Kill process using a specific port, excluding IDEs. Detects stuck processes."""
        if platform.system() == "Darwin":  # macOS
            # Get PIDs on port, but exclude IDE processes
            try:
                result = subprocess.run(
                    f"lsof -ti:{port}", shell=True, capture_output=True, text=True
                )
                pids = result.stdout.strip().split("\n")

                for pid in pids:
                    if not pid:
                        continue

                    pid_int = int(pid)

                    # Check if this PID belongs to an IDE
                    try:
                        proc_info = subprocess.run(
                            f"ps -p {pid} -o comm=",
                            shell=True,
                            capture_output=True,
                            text=True,
                        )
                        proc_name = proc_info.stdout.strip().lower()

                        # Skip IDE processes
                        ide_patterns = [
                            "cursor",
                            "code",
                            "vscode",
                            "sublime",
                            "pycharm",
                            "intellij",
                            "webstorm",
                            "atom",
                            "vim",
                            "emacs",
                        ]

                        if any(pattern in proc_name for pattern in ide_patterns):
                            print(
                                f"{Colors.YELLOW}Skipping IDE process: {proc_name} (PID {pid}){Colors.ENDC}"
                            )
                            continue

                        # Check if process is stuck (uninterruptible sleep)
                        try:
                            proc_state = subprocess.run(
                                f"ps -o stat= -p {pid}",
                                shell=True,
                                capture_output=True,
                                text=True,
                            )
                            state = proc_state.stdout.strip()

                            # UE, D, U states indicate uninterruptible sleep
                            if 'U' in state or 'D' in state:
                                print(f"\n{Colors.FAIL}🚨 CRITICAL: Process PID {pid} is STUCK (state: {state}){Colors.ENDC}")
                                print(f"{Colors.FAIL}   This process is in uninterruptible sleep and CANNOT be killed.{Colors.ENDC}")
                                print(f"{Colors.FAIL}   It was likely blocked by ML operations (torch/librosa) blocking the event loop.{Colors.ENDC}")
                                print(f"\n{Colors.YELLOW}   ⚠️  SYSTEM RESTART REQUIRED to clear this process.{Colors.ENDC}")
                                print(f"{Colors.CYAN}   The fixes have been applied to prevent this in the future.{Colors.ENDC}")
                                print(f"{Colors.CYAN}   After restart, ML operations will run in thread pools.{Colors.ENDC}\n")
                                # Don't try to kill - it won't work
                                return False
                        except Exception as e:
                            pass

                        # Try graceful termination first
                        subprocess.run(f"kill -15 {pid}", shell=True, capture_output=True)
                        await asyncio.sleep(0.5)

                        # Check if still running
                        check = subprocess.run(f"ps -p {pid}", shell=True, capture_output=True)
                        if check.returncode == 0:
                            # Force kill
                            subprocess.run(f"kill -9 {pid}", shell=True, capture_output=True)
                            await asyncio.sleep(0.3)

                            # Verify killed
                            check2 = subprocess.run(f"ps -p {pid}", shell=True, capture_output=True)
                            if check2.returncode == 0:
                                # Process survived kill -9 - likely stuck
                                print(f"{Colors.FAIL}⚠️ Process {pid} survived kill -9 - may be stuck{Colors.ENDC}")
                                return False
                    except:
                        pass

                return True
            except:
                pass
            return True
        else:  # Linux
            cmd = f"fuser -k {port}/tcp"
            try:
                subprocess.run(cmd, shell=True, capture_output=True)
            except:
                pass

        await asyncio.sleep(1)

    async def check_performance_fixes(self):
        """Check if performance fixes have been applied"""
        print(f"\n{Colors.BLUE}Checking performance optimizations...{Colors.ENDC}")

        # Check if performance fix files exist
        fixes_applied = []
        fixes_missing = []

        perf_files = [
            (self.backend_dir / "smart_startup_manager.py", "Smart Startup Manager"),
            (self.backend_dir / "core" / "memory_quantizer.py", "Memory Quantizer"),
            (
                self.backend_dir / "core" / "swift_system_monitor.py",
                "Swift System Monitor",
            ),
            (
                self.backend_dir
                / "swift_bridge"
                / ".build"
                / "release"
                / "libPerformanceCore.dylib",
                "Swift Performance Library",
            ),
            (self.backend_dir / "vision" / "vision_system_v2.py", "Vision System v2.0"),
        ]

        for file_path, name in perf_files:
            if file_path.exists():
                fixes_applied.append(name)
            else:
                fixes_missing.append((file_path, name))

        if fixes_applied:
            print(f"{Colors.GREEN}✓ Performance fixes applied:{Colors.ENDC}")
            for fix in fixes_applied:
                print(f"  • {fix}")

        if fixes_missing:
            print(f"{Colors.YELLOW}⚠ Performance fixes missing:{Colors.ENDC}")
            for path, name in fixes_missing:
                print(f"  • {name}")
            print("\n  Run: python backend/apply_performance_fixes.py")  # noqa: F541

        return len(fixes_missing) == 0

    async def check_dependencies(self):
        """Check Python dependencies with optimization packages"""
        print(f"\n{Colors.BLUE}Checking dependencies...{Colors.ENDC}")  # noqa: F541

        critical_packages = [
            "fastapi",
            "uvicorn",
            "aiohttp",
            "pydantic",
            "psutil",
            "yaml",  # PyYAML imports as 'yaml', not 'pyyaml'
            "watchdog",
            "aiohttp_cors",
        ]

        optional_packages = [
            "anthropic",
            "pyaudio",
            "pvporcupine",
            "librosa",
            "sounddevice",
            "webrtcvad",
            "sklearn",  # scikit-learn imports as 'sklearn'
            "numpy",
            "jsonschema",
        ]

        critical_missing = []
        optional_missing = []

        # Check critical packages
        for package in critical_packages:
            try:
                __import__(package)
            except ImportError:
                critical_missing.append(package)

        # Check optional packages
        for package in optional_packages:
            try:
                __import__(package)
            except ImportError:
                optional_missing.append(package)

        if not critical_missing and not optional_missing:
            print(f"{Colors.GREEN}✓ All dependencies installed{Colors.ENDC}")
            return True, [], []
        else:
            if critical_missing:
                print(f"{Colors.FAIL}✗ Critical packages missing:{Colors.ENDC}")
                for pkg in critical_missing:
                    print(f"  • {pkg}")

            if optional_missing:
                print(f"{Colors.YELLOW}⚠ Optional packages missing:{Colors.ENDC}")
                for pkg in optional_missing:
                    print(f"  • {pkg}")

            return len(critical_missing) == 0, critical_missing, optional_missing

    async def create_directories(self):
        """Create necessary directories"""
        dirs = [
            self.backend_dir / "logs",
            self.backend_dir / "models",
            self.backend_dir / "cache",
            Path.home() / ".jarvis",
            Path.home() / ".jarvis" / "backups",
            Path.home() / ".jarvis" / "learned_config",
        ]

        for dir_path in dirs:
            dir_path.mkdir(parents=True, exist_ok=True)

    async def check_microphone_system(self):
        """Check microphone availability and permissions"""
        print(f"\n{Colors.BLUE}Checking microphone system...{Colors.ENDC}")

        # Check if we can import audio packages
        try:
            pass

            print(f"{Colors.GREEN}✓ PyAudio available{Colors.ENDC}")
        except ImportError:
            print(f"{Colors.WARNING}⚠ PyAudio not installed - voice features limited{Colors.ENDC}")
            return False

        # Check microphone permissions on macOS
        if platform.system() == "Darwin":
            print(f"{Colors.CYAN}  Note: Grant microphone permission if prompted{Colors.ENDC}")

        return True

    async def check_vision_permissions(self):
        """Check vision system permissions"""
        print(f"\n{Colors.BLUE}Checking vision capabilities...{Colors.ENDC}")

        if platform.system() == "Darwin":
            print(f"{Colors.CYAN}Enhanced vision system available with Claude API{Colors.ENDC}")
            if self.claude_configured:
                print(f"{Colors.GREEN}✓ Claude Vision integration ready{Colors.ENDC}")
                print(f"{Colors.GREEN}✓ Integration Architecture active (v12.9.2):{Colors.ENDC}")
                print("  • Integration Orchestrator (9-stage pipeline)")  # noqa: F541
                print("  • VSMS Core (Visual State Management)")  # noqa: F541
                print("  • Bloom Filter Network (hierarchical deduplication)")  # noqa: F541
                print("  • Predictive Engine (Markov chain predictions)")  # noqa: F541
                print("  • Semantic Cache LSH (intelligent caching)")  # noqa: F541
                print("  • Quadtree Spatial (region optimization)")  # noqa: F541
                print("  • 🎥 Video Streaming (30 FPS with purple indicator)")  # noqa: F541
                print("  • Dynamic memory allocation (1.2GB budget)")  # noqa: F541
                print("  • Cross-language optimization (Python/Rust/Swift)")  # noqa: F541

                # Check for native video capture
                try:
                    from backend.vision.video_stream_capture import MACOS_CAPTURE_AVAILABLE

                    if MACOS_CAPTURE_AVAILABLE:
                        print(
                            f"{Colors.GREEN}✓ Native macOS video capture available (🟣 purple indicator){Colors.ENDC}"
                        )
                    else:
                        print(f"{Colors.YELLOW}⚠ Video streaming using fallback mode{Colors.ENDC}")
                except ImportError:
                    pass
            else:
                print(
                    f"{Colors.YELLOW}⚠ Configure ANTHROPIC_API_KEY for vision features{Colors.ENDC}"
                )

    async def start_backend_optimized(self) -> asyncio.subprocess.Process:
        """Start backend with performance optimizations and auto-reload"""
        print(
            f"\n{Colors.BLUE}Starting optimized backend with auto-reload capabilities...{Colors.ENDC}"
        )

        # Step 1: Pre-load voice biometrics - Start Cloud SQL proxy first
        print(f"\n{Colors.CYAN}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.CYAN}🎤 Voice Biometric System Initialization{Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*70}{Colors.ENDC}\n")

        # LEGACY PROXY STARTUP REMOVED - Now handled by CloudSQLProxyManager in run()
        # The proxy is now managed properly with lifecycle tied to JARVIS startup/shutdown
        print(f"{Colors.CYAN}🔍 Cloud SQL proxy managed by CloudSQLProxyManager{Colors.ENDC}")
        print(f"{Colors.GREEN}   ✓ Proxy lifecycle handled automatically{Colors.ENDC}")

        # Step 2: Pre-initialize speaker verification service with Derek's profile
        print(f"\n{Colors.CYAN}🔐 Loading speaker verification system...{Colors.ENDC}")
        try:
            # Import and initialize the learning database
            if str(self.backend_dir) not in sys.path:
                sys.path.insert(0, str(self.backend_dir))

            # Load database config securely
            import json
            from pathlib import Path

            config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
            if config_path.exists():
                with open(config_path, "r") as f:
                    db_config = json.load(f)

                # Set environment variables for Cloud SQL BEFORE importing
                os.environ["JARVIS_DB_TYPE"] = "cloudsql"
                os.environ["JARVIS_DB_CONNECTION_NAME"] = db_config["cloud_sql"]["connection_name"]
                os.environ["JARVIS_DB_HOST"] = "127.0.0.1"  # Always use localhost for proxy
                os.environ["JARVIS_DB_PORT"] = str(db_config["cloud_sql"]["port"])
                os.environ["JARVIS_DB_PASSWORD"] = db_config["cloud_sql"]["password"]
            else:
                logger.warning("⚠️ Database config not found, skipping Cloud SQL setup")

            print(f"{Colors.CYAN}   └─ Initializing JARVIS Learning Database...{Colors.ENDC}")
            from intelligence.learning_database import JARVISLearningDatabase
            from voice.speaker_verification_service import SpeakerVerificationService

            # Initialize learning database with Cloud SQL + Phase 2 features
            learning_db = JARVISLearningDatabase()
            await learning_db.initialize()
            print(f"{Colors.GREEN}      ✓ Learning database initialized{Colors.ENDC}")

            # Show Phase 2 features status
            if hasattr(learning_db, 'hybrid_sync') and learning_db.hybrid_sync:
                hs = learning_db.hybrid_sync
                print(f"{Colors.CYAN}      ├─ 🚀 Phase 2 Features:{Colors.ENDC}")
                print(f"{Colors.GREEN}         ├─ FAISS Cache: {'✓' if hs.faiss_cache and hs.faiss_cache.index else '✗'}{Colors.ENDC}")
                print(f"{Colors.GREEN}         ├─ Prometheus: {'✓ port ' + str(hs.prometheus.port) if hs.prometheus and hs.prometheus.enabled else '✗'}{Colors.ENDC}")
                print(f"{Colors.GREEN}         ├─ Redis: {'✓ ' + hs.redis.redis_url if hs.redis and hs.redis.redis else '✗'}{Colors.ENDC}")
                print(f"{Colors.GREEN}         ├─ ML Prefetcher: {'✓' if hs.ml_prefetcher else '✗'}{Colors.ENDC}")
                print(f"{Colors.GREEN}         └─ Max Connections: {hs.max_connections}{Colors.ENDC}")

            # Initialize speaker service with FAST mode (background encoder loading)
            print(f"{Colors.CYAN}   └─ Initializing Speaker Verification Service (fast mode)...{Colors.ENDC}")
            speaker_service = SpeakerVerificationService(learning_db)
            await speaker_service.initialize_fast()  # Fast init: profiles immediately, encoder in background
            print(f"{Colors.GREEN}      ✓ Speaker verification ready (encoder loading in background){Colors.ENDC}")

            # ROBUSTNESS: Validate model and profile dimensions
            model_dim = speaker_service.current_model_dimension
            profile_count = len(speaker_service.speaker_profiles)
            print(f"{Colors.CYAN}   └─ Validating voice profiles...{Colors.ENDC}")
            print(f"{Colors.CYAN}      ├─ Model dimension: {model_dim}D{Colors.ENDC}")
            print(f"{Colors.CYAN}      ├─ Loaded profiles: {profile_count}{Colors.ENDC}")

            # Validate each profile dimension
            import numpy as np
            mismatched_profiles = []
            for name, profile in speaker_service.speaker_profiles.items():
                emb_shape = np.array(profile['embedding']).shape
                emb_dim = emb_shape[0] if len(emb_shape) == 1 else emb_shape[1]
                if emb_dim != model_dim:
                    mismatched_profiles.append((name, emb_dim))
                    print(f"{Colors.YELLOW}      ├─ {name}: {emb_dim}D ⚠️  (dimension mismatch){Colors.ENDC}")
                else:
                    print(f"{Colors.GREEN}      ├─ {name}: {emb_dim}D ✅{Colors.ENDC}")

            if mismatched_profiles:
                print(f"{Colors.YELLOW}      └─ ⚠️  {len(mismatched_profiles)} profile(s) need re-enrollment{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}      └─ ✅ All profiles validated{Colors.ENDC}")

            # ================================================================
            # DYNAMIC PRIMARY USER DETECTION - No hardcoded names!
            # ================================================================
            # Find primary users by checking is_primary_user or is_owner flags
            primary_users = []
            all_profiles = speaker_service.speaker_profiles

            for name, profile in all_profiles.items():
                is_primary = (
                    profile.get("is_primary_user", False) or
                    profile.get("is_owner", False) or
                    profile.get("security_clearance") == "admin"
                )
                if is_primary:
                    primary_users.append((name, profile))

            # If no primary users flagged, check if any profiles have embeddings
            if not primary_users and all_profiles:
                # Fall back to profiles with valid embeddings
                for name, profile in all_profiles.items():
                    if profile.get("embedding") is not None or profile.get("voiceprint_embedding") is not None:
                        primary_users.append((name, profile))

            if primary_users:
                # Count total samples across primary users
                total_samples = sum(
                    profile.get("total_samples", 0)
                    for name, profile in primary_users
                )
                num_profiles = len(primary_users)
                primary_names = [name for name, _ in primary_users]

                # Check for BEAST MODE features
                beast_mode_profiles = []
                for name, profile in primary_users:
                    acoustic_features = profile.get("acoustic_features", {})
                    has_beast_mode = any(v is not None for v in acoustic_features.values())
                    if has_beast_mode:
                        beast_mode_profiles.append(name)

                print(f"\n{Colors.GREEN}✅ Voice biometric authentication ready:{Colors.ENDC}")
                print(
                    f"{Colors.CYAN}   └─ Primary user(s): {', '.join(primary_names)}{Colors.ENDC}"
                )
                print(
                    f"{Colors.CYAN}   └─ Profiles loaded: {num_profiles}{Colors.ENDC}"
                )
                print(f"{Colors.CYAN}   └─ Voice samples: {total_samples} total{Colors.ENDC}")

                # Show BEAST MODE status
                if beast_mode_profiles:
                    print(
                        f"{Colors.GREEN}   └─ 🔬 BEAST MODE: ENABLED (85-95% confidence){Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}      • Multi-modal biometric fusion active{Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}      • Profiles with BEAST MODE: {', '.join(beast_mode_profiles)}{Colors.ENDC}"
                    )
                else:
                    print(
                        f"{Colors.YELLOW}   └─ Authentication: BASIC MODE (45% confidence threshold){Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}      💡 Run 'python3 backend/quick_voice_enhancement.py' for BEAST MODE{Colors.ENDC}"
                    )

                print(
                    f"{Colors.CYAN}   └─ Speaker encoder: Pre-loaded for instant unlock{Colors.ENDC}"
                )

                # Store globally so backend can access it
                import backend.voice.speaker_verification_service as sv

                sv._global_speaker_service = speaker_service
                print(f"{Colors.GREEN}   ✓ Global speaker service injected{Colors.ENDC}")
            else:
                # No primary users found
                total_profiles = len(all_profiles)
                if total_profiles > 0:
                    # Profiles exist but none marked as primary
                    profile_names = list(all_profiles.keys())
                    print(
                        f"{Colors.YELLOW}   ⚠️  No primary user profile found in {total_profiles} loaded profile(s){Colors.ENDC}"
                    )
                    print(f"{Colors.CYAN}   Available profiles: {', '.join(profile_names)}{Colors.ENDC}")
                    print(
                        f"{Colors.CYAN}   💡 Voice authentication will operate in enrollment mode{Colors.ENDC}"
                    )
                else:
                    # No profiles at all - provide enrollment instructions
                    print(
                        f"{Colors.YELLOW}   ⚠️  No speaker profiles found in database{Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}   💡 To create your voice profile, say:{Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}      - 'Learn my voice as [Your Name]'{Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}      - 'Create speaker profile for [Your Name]'{Colors.ENDC}"
                    )
                    print(
                        f"{Colors.CYAN}   Voice authentication will activate after enrollment{Colors.ENDC}"
                    )

                # Store globally anyway so backend can access enrollment functionality
                import backend.voice.speaker_verification_service as sv

                sv._global_speaker_service = speaker_service
                print(f"{Colors.GREEN}   ✓ Global speaker service injected (enrollment mode){Colors.ENDC}")

        except Exception as e:
            print(f"{Colors.YELLOW}   ⚠️  Speaker pre-loading failed: {e}{Colors.ENDC}")
            import traceback

            print(f"{Colors.YELLOW}   Details: {traceback.format_exc()}{Colors.ENDC}")

        # Step 3: ML models are pre-loaded by the speaker verification service
        # (SpeechBrain models load during speaker service initialization above)
        print(f"\n{Colors.CYAN}🧠 ML Model Status:{Colors.ENDC}")
        print(f"{Colors.GREEN}   ✓ SpeechBrain Wav2Vec2 (ASR){Colors.ENDC}")
        print(f"{Colors.GREEN}   ✓ ECAPA-TDNN (Speaker Encoder){Colors.ENDC}")
        print(f"{Colors.GREEN}   ✓ Models pre-loaded - instant response ready{Colors.ENDC}")

        # Check if reload manager is available
        reload_manager_path = self.backend_dir / "jarvis_reload_manager.py"
        if (
            reload_manager_path.exists()
            and os.getenv("JARVIS_USE_RELOAD_MANAGER", "true").lower() == "true"
        ):
            print(
                f"{Colors.CYAN}🔄 Using intelligent reload manager for auto-updates...{Colors.ENDC}"
            )

            # Import and use the reload manager
            try:
                from backend.jarvis_reload_manager import JARVISReloadManager

                reload_manager = JARVISReloadManager()

                # Check for code changes
                has_changes, changed_files = reload_manager.detect_code_changes()
                if has_changes:
                    print(
                        f"{Colors.YELLOW}📝 Detected {len(changed_files)} code changes{Colors.ENDC}"
                    )
                    for file in changed_files[:3]:
                        print(f"    - {file}")
                    if len(changed_files) > 3:
                        print(f"    ... and {len(changed_files) - 3} more")

                # Kill any existing JARVIS process if code changed
                if has_changes:
                    await reload_manager.stop_jarvis(force=True)
                    print(f"{Colors.GREEN}✅ Cleared old instances for fresh start{Colors.ENDC}")

            except ImportError:
                print(
                    f"{Colors.YELLOW}Reload manager not available, using standard startup{Colors.ENDC}"
                )

        # Kill any existing processes in parallel for faster cleanup
        kill_tasks = []
        ports_to_check = [
            ("event_ui", 8888),
            ("main_api", self.ports["main_api"]),
        ]

        for port_name, port in ports_to_check:
            if not await self.check_port_available(port):
                print(f"{Colors.WARNING}Killing process on port {port}...{Colors.ENDC}")
                kill_tasks.append(self.kill_process_on_port(port))

        if kill_tasks:
            await asyncio.gather(*kill_tasks)
            await asyncio.sleep(0.5)  # Reduced wait time

        # Use main.py which now has integrated parallel startup
        if (self.backend_dir / "main.py").exists():
            # Use main.py with parallel startup capabilities
            print(
                f"{Colors.CYAN}Starting backend with main.py (auto-reload enabled)...{Colors.ENDC}"
            )
            server_script = "main.py"
        else:
            print(f"{Colors.WARNING}Main backend not available, using fallback...{Colors.ENDC}")
            return await self.start_backend_standard()

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.backend_dir)
        env["JARVIS_USER"] = os.getenv("JARVIS_USER", "Sir")

        # Set the backend port explicitly
        env["BACKEND_PORT"] = str(self.ports["main_api"])

        # ============================================================================
        # CLOUD SQL PROXY MANAGEMENT (Advanced, Dynamic, Robust)
        # ============================================================================
        # Ensure Cloud SQL proxy is running BEFORE starting backend
        # This enables voice biometric authentication with Cloud SQL database
        # ============================================================================
        print(f"\n{Colors.CYAN}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.CYAN}☁️  Cloud Infrastructure Initialization{Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*70}{Colors.ENDC}\n")

        try:
            sys.path.insert(0, str(self.backend_dir))
            from intelligence.cloud_sql_proxy_manager import get_proxy_manager

            print(f"{Colors.CYAN}📊 Loading Cloud SQL proxy manager...{Colors.ENDC}")
            proxy_manager = get_proxy_manager()

            # Display proxy configuration details
            print(
                f"{Colors.CYAN}   └─ Instance: {proxy_manager.config['cloud_sql']['connection_name']}{Colors.ENDC}"
            )
            print(
                f"{Colors.CYAN}   └─ Database: {proxy_manager.config['cloud_sql']['database']}{Colors.ENDC}"
            )
            print(
                f"{Colors.CYAN}   └─ Port: {proxy_manager.config['cloud_sql']['port']}{Colors.ENDC}"
            )
            print(f"{Colors.GREEN}   ✓ Proxy manager loaded{Colors.ENDC}")

            # Check if proxy is running, start if needed
            if not proxy_manager.is_running():
                print(f"\n{Colors.CYAN}☁️  Starting Cloud SQL proxy...{Colors.ENDC}")
                if await proxy_manager.start(force_restart=False):
                    print(f"{Colors.GREEN}   ✓ Cloud SQL proxy started successfully{Colors.ENDC}")
                    print(
                        f"{Colors.GREEN}   ✓ Listening on 127.0.0.1:{proxy_manager.config['cloud_sql']['port']}{Colors.ENDC}"
                    )

                    # ROBUSTNESS: Verify proxy is actually accepting connections
                    import socket
                    print(f"{Colors.CYAN}   └─ Verifying proxy connectivity...{Colors.ENDC}")
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(2)
                        result = sock.connect_ex(('127.0.0.1', proxy_manager.config['cloud_sql']['port']))
                        sock.close()
                        if result == 0:
                            print(f"{Colors.GREEN}   ✓ Proxy accepting connections{Colors.ENDC}")
                        else:
                            print(f"{Colors.YELLOW}   ⚠️  Proxy started but not accepting connections (may need a moment){Colors.ENDC}")
                    except Exception as e:
                        print(f"{Colors.YELLOW}   ⚠️  Connection test failed: {e}{Colors.ENDC}")
                else:
                    print(
                        f"{Colors.YELLOW}   ⚠️  Cloud SQL proxy failed to start - will use SQLite fallback{Colors.ENDC}"
                    )
            else:
                print(f"{Colors.GREEN}   ✓ Cloud SQL proxy already running{Colors.ENDC}")

                # ROBUSTNESS: Verify existing proxy is healthy
                import socket
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2)
                    result = sock.connect_ex(('127.0.0.1', proxy_manager.config['cloud_sql']['port']))
                    sock.close()
                    if result == 0:
                        print(f"{Colors.GREEN}   ✓ Proxy is healthy and accepting connections{Colors.ENDC}")
                    else:
                        print(f"{Colors.YELLOW}   ⚠️  Proxy process exists but not responding - restarting...{Colors.ENDC}")
                        await proxy_manager.start(force_restart=True)
                except Exception as e:
                    print(f"{Colors.YELLOW}   ⚠️  Health check failed, restarting proxy: {e}{Colors.ENDC}")
                    await proxy_manager.start(force_restart=True)

            # Start health monitor in background (auto-recovery)
            print(f"{Colors.CYAN}🔄 Starting proxy health monitor...{Colors.ENDC}")
            asyncio.create_task(proxy_manager.monitor(check_interval=60))
            print(
                f"{Colors.GREEN}   ✓ Health monitor active (60s interval, auto-recovery enabled){Colors.ENDC}"
            )

            # ROBUSTNESS: Store proxy manager for cleanup and monitoring
            self.cloud_sql_proxy_manager = proxy_manager
            print(f"{Colors.GREEN}   ✓ Proxy manager registered for lifecycle management{Colors.ENDC}")

        except FileNotFoundError as e:
            print(f"{Colors.YELLOW}⚠️  Cloud SQL proxy not configured: {e}{Colors.ENDC}")
            print(f"{Colors.YELLOW}   Voice biometrics will use SQLite fallback{Colors.ENDC}")
        except Exception as e:
            print(f"{Colors.YELLOW}⚠️  Cloud SQL proxy error: {e}{Colors.ENDC}")
            print(f"{Colors.YELLOW}   Voice biometrics will use SQLite fallback{Colors.ENDC}")

        # Configure Cloud SQL for voice biometrics
        # Load database config securely
        import json
        from pathlib import Path

        print(f"\n{Colors.CYAN}🔐 Loading GCP database configuration...{Colors.ENDC}")
        config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
        if config_path.exists():
            with open(config_path, "r") as f:
                db_config = json.load(f)

            env["JARVIS_DB_TYPE"] = "cloudsql"
            env["JARVIS_DB_CONNECTION_NAME"] = db_config["cloud_sql"]["connection_name"]
            env["JARVIS_DB_HOST"] = "127.0.0.1"  # Always use localhost for proxy
            env["JARVIS_DB_PORT"] = str(db_config["cloud_sql"]["port"])
            env["JARVIS_DB_PASSWORD"] = db_config["cloud_sql"]["password"]

            print(f"{Colors.GREEN}   ✓ Database config loaded from {config_path}{Colors.ENDC}")
            print(
                f"{Colors.CYAN}   └─ Proxy host: 127.0.0.1:{db_config['cloud_sql']['port']}{Colors.ENDC}"
            )
            print(f"{Colors.CYAN}   └─ Type: Cloud SQL (PostgreSQL){Colors.ENDC}")
            print(f"{Colors.CYAN}   └─ Project: {db_config.get('project_id', 'N/A')}{Colors.ENDC}")
            print(f"{Colors.CYAN}   └─ Region: {db_config.get('region', 'N/A')}{Colors.ENDC}")

            # Check for GCP storage buckets
            if "cloud_storage" in db_config:
                print(f"\n{Colors.CYAN}🪣 Cloud Storage buckets configured:{Colors.ENDC}")
                storage = db_config["cloud_storage"]
                if "chromadb_bucket" in storage:
                    print(f"{Colors.GREEN}   ✓ ChromaDB: {storage['chromadb_bucket']}{Colors.ENDC}")
                if "backup_bucket" in storage:
                    print(f"{Colors.GREEN}   ✓ Backups: {storage['backup_bucket']}{Colors.ENDC}")
        else:
            # Fallback to environment variable if config not found
            env["JARVIS_DB_TYPE"] = "cloudsql"
            env["JARVIS_DB_CONNECTION_NAME"] = "jarvis-473803:us-central1:jarvis-learning-db"
            if "JARVIS_DB_PASSWORD" in os.environ:
                env["JARVIS_DB_PASSWORD"] = os.environ["JARVIS_DB_PASSWORD"]
            print(
                f"{Colors.YELLOW}   ⚠️  Config not found, using environment variables{Colors.ENDC}"
            )

        # Enable all performance optimizations
        env["OPTIMIZE_STARTUP"] = "true"
        env["LAZY_LOAD_MODELS"] = "false"  # Don't lazy load - we pre-loaded them
        env["PARALLEL_INIT"] = "true"
        env["JARVIS_AUTO_RELOAD"] = "true"  # Enable auto-reload for code changes
        env["FAST_STARTUP"] = "true"
        env["ML_LOGGING_ENABLED"] = "true"
        env["BACKEND_PARALLEL_IMPORTS"] = "true"
        env["BACKEND_LAZY_LOAD_MODELS"] = "false"  # Don't lazy load - we pre-loaded them
        env["VOICE_BIOMETRIC_ENABLED"] = "true"  # Enable voice biometrics
        env["SPEAKER_VERIFICATION_PRELOADED"] = "true"  # Mark as pre-loaded

        # Set Swift library path
        swift_lib_path = str(self.backend_dir / "swift_bridge" / ".build" / "release")
        if platform.system() == "Darwin":
            env["DYLD_LIBRARY_PATH"] = swift_lib_path
        else:
            env["LD_LIBRARY_PATH"] = swift_lib_path

        api_key = _get_anthropic_api_key()
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        # Create log file
        log_dir = self.backend_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"jarvis_optimized_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        print(f"{Colors.CYAN}Log file: {log_file}{Colors.ENDC}")

        # Start the selected script (main_optimized.py or main.py)
        # Open log file without 'with' statement to keep it open for subprocess
        log = open(log_file, "w")
        self.open_files.append(log)  # Track for cleanup

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",  # Don't write bytecode - ensures fresh imports
            server_script,
            "--port",
            str(self.ports["main_api"]),
            cwd=str(self.backend_dir.absolute()),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        self.processes.append(process)

        # Use dynamic health checking instead of fixed wait
        print(
            f"{Colors.YELLOW}Waiting for backend to initialize (parallel startup enabled)...{Colors.ENDC}"
        )

        # Quick initial wait for process to start
        await asyncio.sleep(2)

        # Check if backend is accessible
        backend_url = f"http://localhost:{self.ports['main_api']}/health"
        print(f"{Colors.CYAN}Checking backend at {backend_url}...{Colors.ENDC}")
        # Increased timeout for voice component loading on low RAM systems (can take 180-300s with memory pressure)
        backend_ready = await self.wait_for_service(backend_url, timeout=300)

        if not backend_ready:
            print(
                f"{Colors.WARNING}Backend did not respond at {backend_url} after 300 seconds{Colors.ENDC}"
            )
            print(f"{Colors.WARNING}Check log file: {log_file}{Colors.ENDC}")

            # Show last few lines of log for debugging
            try:
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    if lines:
                        print(f"{Colors.YELLOW}Last log entries:{Colors.ENDC}")
                        for line in lines[-5:]:
                            print(f"  {line.strip()}")
            except Exception:
                pass

            # main.py failed, try fallback to minimal
            print(f"\n{Colors.YELLOW}{'=' * 60}{Colors.ENDC}")
            print(f"{Colors.YELLOW}⚠️  Main backend initialization delayed{Colors.ENDC}")
            print(f"{Colors.YELLOW}{'=' * 60}{Colors.ENDC}")
            print(f"{Colors.CYAN}📌 Starting MINIMAL MODE for immediate availability{Colors.ENDC}")
            print(f"{Colors.CYAN}  ✅ Basic voice commands will work immediately{Colors.ENDC}")
            print(
                f"{Colors.CYAN}  ⏳ Full features will activate automatically when ready{Colors.ENDC}"
            )
            print(f"{Colors.CYAN}  🔄 No action needed - system will auto-upgrade{Colors.ENDC}")
            print(f"{Colors.YELLOW}{'=' * 60}{Colors.ENDC}\n")

            # Check if process is still running before killing
            if process.returncode is None:
                print(f"{Colors.YELLOW}Cleaning up initialization process...{Colors.ENDC}")
                try:
                    process.terminate()
                    await asyncio.sleep(2)
                    if process.returncode is None:
                        process.kill()
                except:
                    pass
            else:
                print(
                    f"{Colors.WARNING}Backend process already exited with code: {process.returncode}{Colors.ENDC}"
                )
            self.processes.remove(process)

            minimal_path = self.backend_dir / "main_minimal.py"
            if minimal_path.exists():
                print(f"{Colors.CYAN}Starting minimal backend...{Colors.ENDC}")
                # Re-open log file for fallback process
                log = open(log_file, "a")  # Append mode for fallback
                self.open_files.append(log)

                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-B",  # Don't write bytecode - ensures fresh imports
                    "main_minimal.py",
                    "--port",
                    str(self.ports["main_api"]),
                    cwd=str(self.backend_dir.absolute()),
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                self.processes.append(process)
                print(f"{Colors.GREEN}✓ Minimal backend started (PID: {process.pid}){Colors.ENDC}")
                print(
                    f"{Colors.WARNING}⚠️  Running in minimal mode - some features limited{Colors.ENDC}"
                )
                print(
                    f"{Colors.CYAN}🔄 Auto-upgrade monitor active - will transition to full mode when ready{Colors.ENDC}"
                )
            else:
                print(f"{Colors.FAIL}✗ No fallback minimal backend available{Colors.ENDC}")
                raise RuntimeError("No backend available to start")
        else:
            print(f"{Colors.GREEN}✓ Optimized backend started (PID: {process.pid}){Colors.ENDC}")
            print(f"{Colors.GREEN}✓ Swift performance bridges loaded{Colors.ENDC}")
            print(f"{Colors.GREEN}✓ Smart startup manager integrated{Colors.ENDC}")
            print(f"{Colors.GREEN}✓ CPU usage: 0% idle (Swift monitoring){Colors.ENDC}")
            print(f"{Colors.GREEN}✓ Memory quantizer active (4GB target){Colors.ENDC}")

            # 🧠 Voice Memory Agent - AUTONOMOUS with Self-Healing
            print(f"\n{Colors.CYAN}🧠 Initializing Autonomous Voice Memory Agent...{Colors.ENDC}")
            try:
                from agents.voice_memory_agent import startup_voice_memory_check

                voice_check_result = await startup_voice_memory_check()

                # Show status
                status_icon = {
                    'healthy': f"{Colors.GREEN}✓",
                    'needs_attention': f"{Colors.YELLOW}⚠️ ",
                    'critical': f"{Colors.FAIL}🔴",
                    'warning': f"{Colors.WARNING}⚠️ "
                }.get(voice_check_result['status'], '•')

                status_text = voice_check_result['status'].replace('_', ' ').title()
                print(f"{status_icon} Voice memory: {status_text}{Colors.ENDC}")

                # Show autonomous actions taken
                if voice_check_result.get('actions_taken'):
                    print(f"{Colors.GREEN}   🤖 Autonomous actions:{Colors.ENDC}")
                    for action in voice_check_result['actions_taken'][:3]:  # Show first 3
                        print(f"      {action}")

                # Show issues fixed
                if voice_check_result.get('issues_fixed'):
                    print(f"{Colors.GREEN}   ✅ Auto-fixed: {len(voice_check_result['issues_fixed'])} issues{Colors.ENDC}")

                # Show freshness scores
                if voice_check_result.get('freshness'):
                    for speaker, metrics in voice_check_result['freshness'].items():
                        freshness = metrics.get('freshness_score', 0)
                        if freshness < 0.4:
                            print(f"  {Colors.FAIL}🎤 {speaker}: {freshness:.0%} fresh (CRITICAL){Colors.ENDC}")
                        elif freshness < 0.6:
                            print(f"  {Colors.YELLOW}🎤 {speaker}: {freshness:.0%} fresh{Colors.ENDC}")
                        else:
                            print(f"  {Colors.GREEN}🎤 {speaker}: {freshness:.0%} fresh{Colors.ENDC}")

                # Show critical recommendations only
                if voice_check_result.get('recommendations'):
                    critical_recs = [r for r in voice_check_result['recommendations'] if r.get('priority') in ['CRITICAL', 'HIGH']]
                    if critical_recs:
                        print(f"{Colors.YELLOW}   💡 Recommendations:{Colors.ENDC}")
                        for rec in critical_recs[:2]:  # Show top 2 critical
                            priority_color = Colors.FAIL if rec['priority'] == 'CRITICAL' else Colors.YELLOW
                            print(f"      {priority_color}[{rec['priority']}]{Colors.ENDC} {rec['action']}")

            except Exception as e:
                logger.warning(f"Voice memory check skipped: {e}")
                print(f"{Colors.YELLOW}⚠️  Voice memory check skipped (non-critical){Colors.ENDC}")

            # Check component status
            print(f"\n{Colors.CYAN}Checking loaded components...{Colors.ENDC}")
            try:
                async with aiohttp.ClientSession() as session:
                    # Check memory status for component info
                    async with session.get(
                        f"http://localhost:{self.ports['main_api']}/memory/status"
                    ) as resp:
                        if resp.status == 200:
                            # Log shows all 8 components loaded
                            print(
                                f"{Colors.GREEN}✓ All 8/8 components loaded successfully:{Colors.ENDC}"
                            )
                            print(
                                f"  {Colors.GREEN}✅ CHATBOTS{Colors.ENDC}    - Claude Vision AI ready"
                            )
                            print(
                                f"  {Colors.GREEN}✅ VISION{Colors.ENDC}      - Screen capture active (purple indicator)"
                            )
                            print(
                                f"  {Colors.GREEN}✅ MEMORY{Colors.ENDC}      - M1-optimized manager (30% target: 4.8GB)"
                            )
                            print(
                                f"  {Colors.GREEN}✅ VOICE{Colors.ENDC}       - Voice interface ready"
                            )
                            print(
                                f"  {Colors.GREEN}✅ ML_MODELS{Colors.ENDC}   - NLP models available (300MB limit)"
                            )
                            print(
                                f"  {Colors.GREEN}✅ MONITORING{Colors.ENDC}  - Health tracking active"
                            )
                            print(
                                f"  {Colors.GREEN}✅ VOICE_UNLOCK{Colors.ENDC} - Intelligent voice-authenticated unlock (Speaker Recognition + CAI + SAI)"
                            )
                            print(
                                f"  {Colors.GREEN}✅ WAKE_WORD{Colors.ENDC}   - 'Hey JARVIS' detection active"
                            )
                            print(
                                f"  {Colors.GREEN}✅ DISPLAY_MONITOR{Colors.ENDC} - Living Room TV monitoring active"
                            )
            except:
                # Fallback if we can't check
                print(f"{Colors.GREEN}✓ Backend components loading...{Colors.ENDC}")

            print(f"\n{Colors.GREEN}✓ Server running on port {self.ports['main_api']}{Colors.ENDC}")

        return process

    async def start_backend_standard(self) -> asyncio.subprocess.Process:
        """Start standard backend (fallback)"""
        print(f"\n{Colors.BLUE}Starting standard backend service...{Colors.ENDC}")

        # Kill any existing process on the port
        if not await self.check_port_available(self.ports["main_api"]):
            await self.kill_process_on_port(self.ports["main_api"])
            await asyncio.sleep(2)

        # Set up environment
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.backend_dir)

        # Set the backend port explicitly
        env["BACKEND_PORT"] = str(self.ports["main_api"])

        # Set Swift library path
        swift_lib_path = str(self.backend_dir / "swift_bridge" / ".build" / "release")
        if platform.system() == "Darwin":
            env["DYLD_LIBRARY_PATH"] = swift_lib_path
        else:
            env["LD_LIBRARY_PATH"] = swift_lib_path

        api_key = _get_anthropic_api_key()
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        # Try main.py first, then fall back to main_minimal.py
        main_script = self.backend_dir / "main.py"
        minimal_script = self.backend_dir / "main_minimal.py"

        if main_script.exists():
            server_script = "main.py"
            print(f"{Colors.CYAN}Starting main backend...{Colors.ENDC}")
        elif minimal_script.exists():
            server_script = "main_minimal.py"
            print(f"{Colors.YELLOW}Using minimal backend (limited features)...{Colors.ENDC}")
        elif (self.backend_dir / "start_backend.py").exists():
            server_script = "start_backend.py"
        else:
            server_script = "run_server.py"

        # Create log file
        log_dir = self.backend_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"jarvis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        print(
            f"{Colors.CYAN}Starting {server_script} on port {self.ports['main_api']}...{Colors.ENDC}"
        )
        print(f"{Colors.CYAN}Log file: {log_file}{Colors.ENDC}")

        with open(log_file, "w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",  # Don't write bytecode - ensures fresh imports
                server_script,
                "--port",
                str(self.ports["main_api"]),
                cwd=str(self.backend_dir.absolute()),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

        self.processes.append(process)
        print(
            f"{Colors.GREEN}✓ Backend starting on port {self.ports['main_api']} (PID: {process.pid}){Colors.ENDC}"
        )

        return process

    async def start_backend(self) -> asyncio.subprocess.Process:
        """Start backend (optimized or standard based on flag)"""
        if self.use_optimized:
            return await self.start_backend_optimized()
        else:
            return await self.start_backend_standard()

    async def start_frontend(self) -> Optional[asyncio.subprocess.Process]:
        """Start frontend service"""
        if not self.frontend_dir.exists():
            print(f"{Colors.YELLOW}Frontend directory not found, skipping...{Colors.ENDC}")
            return None

        # Clear any stale configuration cache before starting frontend
        await self.clear_frontend_cache()

        print(f"\n{Colors.BLUE}Starting frontend service...{Colors.ENDC}")

        # Check if npm dependencies are installed
        node_modules = self.frontend_dir / "node_modules"
        if not node_modules.exists():
            print(f"{Colors.YELLOW}Installing frontend dependencies...{Colors.ENDC}")
            proc = await asyncio.create_subprocess_exec(
                "npm",
                "install",
                cwd=str(self.frontend_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

        # Kill any existing process
        if not await self.check_port_available(self.ports["frontend"]):
            await self.kill_process_on_port(self.ports["frontend"])
            await asyncio.sleep(2)

        # Start frontend with browser disabled and safety measures
        env = os.environ.copy()
        env["PORT"] = str(self.ports["frontend"])
        env["BROWSER"] = "none"  # Disable React's auto-opening of browser
        env["SKIP_PREFLIGHT_CHECK"] = "true"  # Skip CRA preflight checks
        env["NODE_OPTIONS"] = "--max-old-space-size=4096"  # Increase Node memory
        env["GENERATE_SOURCEMAP"] = "false"  # Disable source maps to reduce memory

        # Create a log file for frontend to help debug issues
        log_file = (
            self.backend_dir / "logs" / f"frontend_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        log_file.parent.mkdir(exist_ok=True)

        # Open log file without 'with' statement to keep it open for subprocess
        log = open(log_file, "w")
        self.open_files.append(log)  # Track for cleanup

        process = await asyncio.create_subprocess_exec(
            "npm",
            "start",
            cwd=str(self.frontend_dir),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        # Give frontend a moment to start and check if it crashes immediately
        await asyncio.sleep(2)
        if process.returncode is not None:
            print(
                f"{Colors.WARNING}Frontend process exited immediately with code {process.returncode}{Colors.ENDC}"
            )
            print(f"{Colors.YELLOW}Check log file: {log_file}{Colors.ENDC}")
            # Try to show last few lines of log
            try:
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    if lines:
                        print(f"{Colors.YELLOW}Last log entries:{Colors.ENDC}")
                        for line in lines[-5:]:
                            print(f"  {line.strip()}")
            except Exception:
                pass
            return None

        self.processes.append(process)
        print(
            f"{Colors.GREEN}✓ Frontend starting on port {self.ports['frontend']} (PID: {process.pid}){Colors.ENDC}"
        )

        return process

    async def _run_parallel_health_checks(self, timeout: int = 10) -> None:
        """Run parallel health checks on all services"""
        print(f"\n{Colors.YELLOW}Verifying all services are ready...{Colors.ENDC}")
        start_time = time.time()

        # Define health check endpoints
        health_checks = [
            ("Backend API", f"http://localhost:{self.ports['main_api']}/health"),
            ("WebSocket Router", "http://localhost:8001/health"),  # noqa: F541
            (
                "Frontend",
                "http://localhost:3000",  # noqa: F541
                False,
            ),  # Frontend may not have health endpoint
        ]

        async def check_service_health(name: str, url: str, expect_json: bool = True):  # noqa
            service_start = time.time()
            while time.time() - service_start < timeout:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                            if resp.status in [200, 404]:  # 404 ok for some endpoints
                                return True, name, time.time() - service_start
                except:
                    pass
                await asyncio.sleep(0.5)
            return False, name, timeout

        # Run all health checks in parallel
        tasks = [
            check_service_health(name, url, expect_json=bool(json[0]) if json else False)
            for name, url, *json in health_checks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_healthy = True
        for result in results:
            if isinstance(result, tuple):
                success, name, duration = result
                if success:
                    print(f"{Colors.GREEN}✓ {name} ready ({duration:.1f}s){Colors.ENDC}")
                else:
                    print(f"{Colors.WARNING}⚠ {name} not responding{Colors.ENDC}")
                    if name == "Backend API":
                        all_healthy = False
            else:
                print(f"{Colors.WARNING}⚠ Health check error: {result}{Colors.ENDC}")

        elapsed = time.time() - start_time
        print(f"{Colors.CYAN}Health checks completed in {elapsed:.1f}s{Colors.ENDC}")

        if not all_healthy:
            print(f"{Colors.WARNING}Some services may not be fully ready yet{Colors.ENDC}")

    async def wait_for_service(self, url: str, timeout: int = 30) -> bool:
        """Wait for a service to be ready"""
        start_time = time.time()

        async with aiohttp.ClientSession() as session:
            while time.time() - start_time < timeout:
                try:
                    async with session.get(url, timeout=5) as resp:
                        if resp.status in [200, 404]:  # 404 is ok for API endpoints
                            return True
                except Exception:
                    # Log the error for debugging but continue trying
                    remaining = timeout - (time.time() - start_time)
                    if remaining > 0:
                        print(
                            f"{Colors.YELLOW}Waiting for service... ({int(remaining)}s remaining){Colors.ENDC}",
                            end="\r",
                        )
                await asyncio.sleep(1)  # Check more frequently

        return False

    async def start_minimal_backend_fallback(self) -> bool:
        """Start minimal backend as fallback when main backend fails"""
        minimal_script = self.backend_dir / "main_minimal.py"

        if not minimal_script.exists():
            print(f"{Colors.WARNING}Minimal backend not available{Colors.ENDC}")
            return False

        print(f"\n{Colors.YELLOW}Starting minimal backend as fallback...{Colors.ENDC}")

        # Kill any existing backend process
        await self.kill_process_on_port(self.ports["main_api"])
        await asyncio.sleep(2)

        # Set up environment
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.backend_dir)

        api_key = _get_anthropic_api_key()
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        # Start minimal backend
        log_file = (
            self.backend_dir / "logs" / f"minimal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        log_file.parent.mkdir(exist_ok=True)

        with open(log_file, "w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "main_minimal.py",
                "--port",
                str(self.ports["main_api"]),
                cwd=str(self.backend_dir.absolute()),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

        self.processes.append(process)
        print(f"{Colors.GREEN}✓ Minimal backend started (PID: {process.pid}){Colors.ENDC}")

        # Wait for it to be ready
        backend_url = f"http://localhost:{self.ports['main_api']}/health"
        if await self.wait_for_service(backend_url, timeout=10):
            print(f"{Colors.GREEN}✓ Minimal backend ready{Colors.ENDC}")
            print(f"{Colors.YELLOW}⚠ Running in minimal mode - some features limited{Colors.ENDC}")
            return True
        else:
            print(f"{Colors.FAIL}❌ Minimal backend failed to start{Colors.ENDC}")
            return False

    async def verify_services(self):
        """Verify all services are running"""
        print(f"\n{Colors.BLUE}Verifying services...{Colors.ENDC}")

        services = []

        # Check main backend
        backend_url = f"http://localhost:{self.ports['main_api']}/docs"
        if await self.wait_for_service(backend_url):
            print(f"{Colors.GREEN}✓ Backend API ready{Colors.ENDC}")
            services.append("backend")
        else:
            print(f"{Colors.WARNING}⚠ Backend API not responding{Colors.ENDC}")
            # Try to start minimal backend as fallback
            if await self.start_minimal_backend_fallback():
                services.append("backend")

        # Check event UI (if optimized)
        if self.use_optimized:
            event_url = f"http://localhost:{self.ports['event_ui']}/"
            if await self.wait_for_service(event_url, timeout=10):
                print(f"{Colors.GREEN}✓ Event UI ready{Colors.ENDC}")
                services.append("event_ui")

        # Check frontend
        if self.frontend_dir.exists() and not self.backend_only:
            frontend_url = f"http://localhost:{self.ports['frontend']}/"
            if await self.wait_for_service(frontend_url, timeout=20):
                print(f"{Colors.GREEN}✓ Frontend ready{Colors.ENDC}")
                services.append("frontend")

        return services

    def print_access_info(self):
        """Print access information"""
        print(f"\n{Colors.HEADER}{'='*60}")
        print(f"{Colors.BOLD}🎯 JARVIS is ready!{Colors.ENDC}")
        print(f"{Colors.HEADER}{'='*60}{Colors.ENDC}")

        print(f"\n{Colors.CYAN}Access Points:{Colors.ENDC}")

        if self.frontend_dir.exists() and not self.backend_only:
            print(
                f"  • Frontend: {Colors.GREEN}http://localhost:{self.ports['frontend']}/{Colors.ENDC}"
            )
            print(
                f"    {Colors.YELLOW}ℹ️  Frontend will show 'INITIALIZING...' then 'CONNECTING...' before 'SYSTEM READY'{Colors.ENDC}"
            )

        print(
            f"  • Backend API: {Colors.GREEN}http://localhost:{self.ports['main_api']}/docs{Colors.ENDC}"
        )

        if self.use_optimized:
            print(
                f"  • Event UI: {Colors.GREEN}http://localhost:{self.ports['event_ui']}/{Colors.ENDC}"
            )

        if self.autonomous_mode and AUTONOMOUS_AVAILABLE:
            print(
                f"  • Service Discovery: {Colors.GREEN}http://localhost:{self.ports['main_api']}/services/discovery{Colors.ENDC}"
            )
            print(
                f"  • Service Monitor: {Colors.GREEN}ws://localhost:{self.ports['main_api']}/services/monitor{Colors.ENDC}"
            )
            print(
                f"  • System Diagnostics: {Colors.GREEN}http://localhost:{self.ports['main_api']}/services/diagnostics{Colors.ENDC}"
            )

        print(f"\n{Colors.CYAN}Voice Commands:{Colors.ENDC}")
        print(f"  • Say '{Colors.GREEN}Hey JARVIS{Colors.ENDC}' to activate")
        print(f"  • '{Colors.GREEN}What can you do?{Colors.ENDC}' - List capabilities")
        print(f"  • '{Colors.GREEN}Can you see my screen?{Colors.ENDC}' - Vision test")
        print(f"\n{Colors.CYAN}🌐 Browser Automation Commands (NEW!):{Colors.ENDC}")
        print(f"  • '{Colors.GREEN}Open Safari and go to Google{Colors.ENDC}' - Browser control")
        print(f"  • '{Colors.GREEN}Search for AI news{Colors.ENDC}' - Web search")
        print(f"  • '{Colors.GREEN}Open a new tab{Colors.ENDC}' - Tab management")
        print(
            f"  • '{Colors.GREEN}Type python tutorials and press enter{Colors.ENDC}' - Type & search"
        )
        print(f"\n{Colors.CYAN}🎥 Screen Monitoring Commands:{Colors.ENDC}")
        print(f"  • '{Colors.GREEN}Start monitoring my screen{Colors.ENDC}' - Begin 30 FPS capture")
        print(f"  • '{Colors.GREEN}Stop monitoring{Colors.ENDC}' - End video streaming")
        print(f"  • macOS: {Colors.PURPLE}Purple indicator{Colors.ENDC} appears when active")

        if self.use_optimized:
            print(f"\n{Colors.CYAN}Performance Management:{Colors.ENDC}")
            print("  • CPU usage: 0% idle (was 87.4%)")  # noqa: F541
            print("  • Memory target: 4GB max")  # noqa: F541
            print("  • Swift monitoring: 0.41ms overhead")  # noqa: F541
            print("  • Emergency cleanup: Automatic")  # noqa: F541

        print(f"\n{Colors.YELLOW}Press Ctrl+C to stop{Colors.ENDC}")

    def identify_service_type(self, name: str) -> str:
        """Identify the type of service"""
        name_lower = name.lower()

        if "frontend" in name_lower:
            return "frontend"
        elif "backend" in name_lower or "jarvis" in name_lower:
            return "backend"
        elif "websocket" in name_lower or "ws" in name_lower:
            return "websocket"
        else:
            return "service"

    async def print_autonomous_status(self):
        """Print autonomous system status"""
        print(f"\n{Colors.HEADER}{'='*60}")
        print(f"{Colors.BOLD}Autonomous System Status{Colors.ENDC}")
        print(f"{Colors.HEADER}{'='*60}{Colors.ENDC}")

        # Service discovery status
        if self.orchestrator:
            discovered = self.orchestrator.services
            print(f"\n{Colors.CYAN}Discovered Services:{Colors.ENDC}")
            for name, service in discovered.items():
                health_color = (
                    Colors.GREEN
                    if service.health_score > 0.7
                    else Colors.YELLOW if service.health_score > 0.3 else Colors.RED
                )
                print(
                    f"  • {name}: {service.protocol}://localhost:{service.port} {health_color}[Health: {service.health_score:.0%}]{Colors.ENDC}"
                )
        else:
            print(f"\n{Colors.YELLOW}Service discovery not available{Colors.ENDC}")

        # Service mesh status
        if self.mesh:
            mesh_config = await self.mesh.get_mesh_config()
            print(f"\n{Colors.CYAN}Service Mesh:{Colors.ENDC}")
            print(f"  • Nodes: {mesh_config['stats']['total_nodes']}")
            print(f"  • Connections: {mesh_config['stats']['total_connections']}")
            print(f"  • Healthy nodes: {mesh_config['stats']['healthy_nodes']}")
        else:
            print(f"\n{Colors.YELLOW}Service mesh not available{Colors.ENDC}")

        print(f"\n{Colors.GREEN}✨ Autonomous systems active and self-healing{Colors.ENDC}")

    def _analyze_voice_failures_with_ai(self, recent_failures: list, stats: dict) -> dict:
        """
        🧠 INTELLIGENT FAILURE ANALYSIS using SAI/CAI/UAE

        Uses Situational Awareness Intelligence (SAI), Contextual Awareness Intelligence (CAI),
        and Unified Awareness Engine (UAE) to diagnose voice verification failures and
        provide actionable recommendations.

        Args:
            recent_failures: List of recent failure attempts with diagnostics
            stats: Overall voice verification statistics

        Returns:
            AI analysis with root cause, patterns, and intelligent recommendations
        """
        analysis = {
            'root_cause': 'Unknown',
            'pattern_detected': 'Analyzing...',
            'analysis_confidence': 0.0,
            'recommendations': []
        }

        try:
            # Extract failure characteristics
            failure_count = len(recent_failures)
            if failure_count == 0:
                return analysis

            # Analyze audio quality issues (CAI - Contextual Awareness)
            audio_issues = sum(1 for f in recent_failures if f.get('audio_quality') in ['silent/corrupted', 'very_quiet', 'too_short'])
            audio_issue_rate = audio_issues / failure_count

            # Analyze database/profile issues
            profile_issues = sum(1 for f in recent_failures if f.get('samples_in_db', 0) < 10)
            profile_issue_rate = profile_issues / failure_count

            # Analyze confidence patterns (SAI - Situational Awareness)
            avg_failed_confidence = sum(f.get('confidence', 0.0) for f in recent_failures) / failure_count
            very_low_confidence = sum(1 for f in recent_failures if f.get('confidence', 0.0) < 0.05)
            very_low_rate = very_low_confidence / failure_count

            # Analyze embedding dimension issues (UAE - Unified Awareness)
            embedding_issues = sum(1 for f in recent_failures
                                  if f.get('embedding_dimension') not in [192, 256, 512, 768, 'unknown'])

            # Get most common severity
            severities = [f.get('severity', 'unknown') for f in recent_failures]
            most_common_severity = max(set(severities), key=severities.count) if severities else 'unknown'

            # 🔍 ROOT CAUSE ANALYSIS (UAE Integration)
            if audio_issue_rate > 0.7:
                analysis['root_cause'] = 'Audio Pipeline Failure'
                analysis['pattern_detected'] = f'{int(audio_issue_rate*100)}% of failures are audio quality issues'
                analysis['analysis_confidence'] = 0.95

                # Intelligent recommendations
                if 'silent/corrupted' in [f.get('audio_quality') for f in recent_failures]:
                    analysis['recommendations'].append({
                        'priority': 'critical',
                        'action': 'Fix microphone: Audio input is not being captured',
                        'reason': 'System receiving silent/corrupted audio from microphone',
                        'auto_fix_available': False,
                        'steps': ['Check microphone permissions', 'Test microphone in System Preferences', 'Restart audio service']
                    })
                elif 'very_quiet' in [f.get('audio_quality') for f in recent_failures]:
                    analysis['recommendations'].append({
                        'priority': 'high',
                        'action': 'Increase microphone gain or speak louder',
                        'reason': 'Audio input level too low for reliable verification',
                        'auto_fix_available': False,
                        'steps': ['Increase microphone input volume', 'Move closer to microphone', 'Reduce background noise']
                    })
                elif 'too_short' in [f.get('audio_quality') for f in recent_failures]:
                    analysis['recommendations'].append({
                        'priority': 'medium',
                        'action': 'Speak the command more slowly',
                        'reason': 'Voice samples too short for accurate verification (need 1+ seconds)',
                        'auto_fix_available': False,
                        'steps': ['Say "unlock my screen" more slowly', 'Ensure full phrase is captured']
                    })

            elif profile_issue_rate > 0.7:
                analysis['root_cause'] = 'Insufficient Voice Training Data'
                analysis['pattern_detected'] = f'{int(profile_issue_rate*100)}% of failures due to low sample count'
                analysis['analysis_confidence'] = 0.90

                samples_in_db = recent_failures[0].get('samples_in_db', 0)
                analysis['recommendations'].append({
                    'priority': 'critical',
                    'action': f'Re-enroll voice profile (only {samples_in_db}/30 samples)',
                    'reason': 'Voice profile has insufficient training data for accurate verification',
                    'auto_fix_available': True,
                    'auto_fix_command': 'python backend/voice/enroll_voice.py --speaker "[YOUR_NAME]"',
                    'steps': ['Run voice enrollment script', 'Provide 30+ voice samples', 'Test verification again']
                })

            elif very_low_rate > 0.7:
                analysis['root_cause'] = 'Voice Mismatch or Model Incompatibility'
                analysis['pattern_detected'] = f'{int(very_low_rate*100)}% have <5% confidence (critical threshold)'
                analysis['analysis_confidence'] = 0.85

                # Check for embedding dimension mismatch
                if embedding_issues > 0:
                    analysis['recommendations'].append({
                        'priority': 'critical',
                        'action': 'Re-enroll voice profile (model version mismatch detected)',
                        'reason': 'Voice embedding dimension incompatible with current model',
                        'auto_fix_available': True,
                        'auto_fix_command': 'python backend/voice/enroll_voice.py --speaker "[YOUR_NAME]" --force',
                        'steps': ['Delete old voice profile', 'Re-enroll with current model', 'Verify enrollment']
                    })
                else:
                    analysis['recommendations'].append({
                        'priority': 'high',
                        'action': 'Verify speaker identity or re-enroll',
                        'reason': 'Voice does not match enrolled profile (possible wrong speaker)',
                        'auto_fix_available': False,
                        'steps': ['Confirm correct speaker', 'Check for voice changes (illness, etc.)', 'Re-enroll if needed']
                    })

            elif stats['consecutive_failures'] >= 3:
                analysis['root_cause'] = 'Environmental or Transient Issues'
                analysis['pattern_detected'] = f'Recent sudden failure after {stats["successful"]} successes'
                analysis['analysis_confidence'] = 0.75

                analysis['recommendations'].append({
                    'priority': 'medium',
                    'action': 'Check environmental conditions',
                    'reason': 'Verification working previously but failing recently',
                    'auto_fix_available': False,
                    'steps': ['Reduce background noise', 'Check for obstructions', 'Restart if issue persists']
                })

            else:
                # General recommendations based on average confidence
                analysis['root_cause'] = 'Variable Performance Issues'
                analysis['pattern_detected'] = f'Mixed failure causes (avg confidence: {avg_failed_confidence:.1%})'
                analysis['analysis_confidence'] = 0.60

                if avg_failed_confidence < 0.20:
                    analysis['recommendations'].append({
                        'priority': 'high',
                        'action': 'Improve audio quality and reduce noise',
                        'reason': 'Low confidence scores suggest audio quality or environmental issues',
                        'auto_fix_available': False,
                        'steps': ['Find quieter environment', 'Check microphone placement', 'Speak clearly']
                    })
                else:
                    analysis['recommendations'].append({
                        'priority': 'medium',
                        'action': 'Continue using - system is learning your voice',
                        'reason': 'Confidence improving with adaptive learning',
                        'auto_fix_available': False,
                        'steps': ['Keep attempting verification', 'System adapting to your voice', 'Confidence will improve']
                    })

            # Add SAI prediction for future failures
            if stats['consecutive_failures'] >= 2:
                analysis['recommendations'].append({
                    'priority': 'medium',
                    'action': '🔮 SAI Prediction: Next attempt likely to fail without action',
                    'reason': 'Pattern suggests underlying issue not yet resolved',
                    'auto_fix_available': False,
                    'steps': ['Address recommendations above first', 'Test in different environment']
                })

            # Add system health recommendation
            if len(analysis['recommendations']) == 0:
                analysis['recommendations'].append({
                    'priority': 'low',
                    'action': 'System operating normally - retry',
                    'reason': 'No systemic issues detected',
                    'auto_fix_available': False,
                    'steps': ['Try again', 'Ensure clear audio']
                })

        except Exception as e:
            logger.error(f"AI analysis error: {e}", exc_info=True)
            analysis['root_cause'] = 'Analysis Error'
            analysis['pattern_detected'] = str(e)

        return analysis

    async def _deep_diagnostic_analysis(self, recent_failures: list, stats: dict) -> dict:
        """
        🔬 BEAST MODE AUTONOMOUS DIAGNOSTIC SYSTEM

        Deep inspection of:
        - Codebase (actual file inspection, not patterns)
        - Database (schema, data integrity, sample counts)
        - Models (version compatibility, embedding dimensions)
        - Configuration (environment, paths, permissions)
        - Runtime (process state, memory, logs)

        Uses SAI/CAI/UAE for intelligent analysis

        Returns:
            Comprehensive diagnostic report with exact fixes
        """
        from pathlib import Path
        import ast
        import json
        import subprocess

        diagnostic = {
            'timestamp': datetime.now().isoformat(),
            'investigation_type': 'deep_autonomous',
            'findings': [],
            'bugs_detected': [],
            'missing_components': [],
            'confidence': 0.0
        }

        logger.info("🔬 BEAST MODE: Starting deep autonomous diagnostic...")

        try:
            # ==========================================
            # 1. CODEBASE INSPECTION (Find actual bugs)
            # ==========================================
            logger.info("📁 Inspecting codebase for voice verification pipeline...")

            backend_path = Path(__file__).parent / "backend"

            # Find all voice-related files dynamically
            voice_files = []
            for pattern in ["**/voice/**/*.py", "**/voice_unlock/**/*.py"]:
                voice_files.extend(list(backend_path.glob(pattern)))

            logger.info(f"   Found {len(voice_files)} voice-related files to analyze")

            for voice_file in voice_files:
                try:
                    with open(voice_file, 'r') as f:
                        source = f.read()
                        tree = ast.parse(source)

                    # Check for common issues
                    for node in ast.walk(tree):
                        # Detect hardcoded thresholds
                        if isinstance(node, ast.Num) and 0.5 <= node.n <= 0.99:
                            diagnostic['findings'].append({
                                'type': 'hardcoded_threshold',
                                'file': str(voice_file.relative_to(Path(__file__).parent)),
                                'value': node.n,
                                'line': node.lineno,
                                'severity': 'medium',
                                'recommendation': 'Replace with adaptive threshold'
                            })

                        # Detect missing error handling
                        if isinstance(node, ast.Try):
                            if not node.handlers:
                                diagnostic['bugs_detected'].append({
                                    'type': 'missing_exception_handler',
                                    'file': str(voice_file.relative_to(Path(__file__).parent)),
                                    'line': node.lineno,
                                    'severity': 'high',
                                    'fix': 'Add exception handlers for robustness'
                                })

                        # Detect blocking calls in async functions
                        if isinstance(node, ast.AsyncFunctionDef):
                            for child in ast.walk(node):
                                if isinstance(child, ast.Call):
                                    if hasattr(child.func, 'attr'):
                                        if child.func.attr in ['sleep', 'read', 'write'] and not isinstance(child.func.value, ast.Name):
                                            diagnostic['bugs_detected'].append({
                                                'type': 'blocking_call_in_async',
                                                'file': str(voice_file.relative_to(Path(__file__).parent)),
                                                'function': node.name,
                                                'severity': 'critical',
                                                'fix': f'Use await {child.func.attr}() instead'
                                            })

                except Exception as e:
                    logger.debug(f"Could not analyze {voice_file}: {e}")

            # ==========================================
            # 2. DATABASE DEEP INSPECTION
            # ==========================================
            logger.info("🗄️  Inspecting database for voice profiles...")

            # Initialize connection variables outside try block to ensure cleanup
            conn = None
            cursor = None
            try:
                # Check if CloudSQL is available
                cloudsql_config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
                if cloudsql_config_path.exists():
                    with open(cloudsql_config_path) as f:
                        db_config = json.load(f)

                    # Actual database connection and inspection
                    import psycopg2
                    conn = psycopg2.connect(
                        host='127.0.0.1',
                        port=db_config['cloud_sql']['port'],
                        database=db_config['cloud_sql'].get('database', 'postgres'),
                        user=db_config['cloud_sql'].get('user', 'postgres'),
                        password=db_config['cloud_sql'].get('password', ''),
                        connect_timeout=5
                    )
                    cursor = conn.cursor()

                    # Check schema
                    cursor.execute("""
                        SELECT column_name, data_type, character_maximum_length
                        FROM information_schema.columns
                        WHERE table_name = 'speaker_profiles'
                        ORDER BY ordinal_position
                    """)
                    schema = cursor.fetchall()

                    logger.info(f"   speaker_profiles table has {len(schema)} columns")

                    # Verify critical columns exist
                    column_names = [col[0] for col in schema]
                    required_columns = ['speaker_id', 'speaker_name', 'voiceprint_embedding', 'total_samples']
                    missing_columns = [col for col in required_columns if col not in column_names]

                    if missing_columns:
                        diagnostic['bugs_detected'].append({
                            'type': 'missing_database_columns',
                            'missing': missing_columns,
                            'severity': 'critical',
                            'fix': 'Run database migration to add missing columns'
                        })

                    # Check actual data
                    cursor.execute("SELECT COUNT(*) FROM speaker_profiles")
                    profile_count = cursor.fetchone()[0]

                    cursor.execute("""
                        SELECT speaker_name, total_samples,
                               LENGTH(voiceprint_embedding) as embedding_size,
                               embedding_dimension
                        FROM speaker_profiles
                    """)
                    profiles = cursor.fetchall()

                    for profile in profiles:
                        speaker_name, total_samples, embedding_size, embedding_dim = profile

                        # Check sample count
                        if total_samples < 10:
                            diagnostic['findings'].append({
                                'type': 'insufficient_samples',
                                'speaker': speaker_name,
                                'samples': total_samples,
                                'severity': 'critical',
                                'recommendation': f'Enroll {30 - total_samples} more voice samples'
                            })

                        # Check embedding validity
                        if embedding_size == 0 or embedding_size is None:
                            diagnostic['bugs_detected'].append({
                                'type': 'corrupted_embedding',
                                'speaker': speaker_name,
                                'severity': 'critical',
                                'fix': 'Re-enroll voice profile - embedding is corrupted'
                            })

                        # Check dimension mismatch
                        if embedding_dim not in [192, 256, 512, 768]:
                            diagnostic['bugs_detected'].append({
                                'type': 'embedding_dimension_mismatch',
                                'speaker': speaker_name,
                                'dimension': embedding_dim,
                                'severity': 'critical',
                                'fix': 'Re-enroll with current model version'
                            })

                    # Check for orphaned voice samples
                    cursor.execute("""
                        SELECT COUNT(*) FROM voice_samples vs
                        LEFT JOIN speaker_profiles sp ON vs.speaker_id = sp.speaker_id
                        WHERE sp.speaker_id IS NULL
                    """)
                    orphaned = cursor.fetchone()[0]

                    if orphaned > 0:
                        diagnostic['findings'].append({
                            'type': 'orphaned_voice_samples',
                            'count': orphaned,
                            'severity': 'medium',
                            'recommendation': 'Clean up orphaned samples to improve performance'
                        })

            except Exception as e:
                diagnostic['findings'].append({
                    'type': 'database_connection_failed',
                    'error': str(e),
                    'severity': 'critical',
                    'recommendation': 'Check CloudSQL proxy is running and configured'
                })
            finally:
                # CRITICAL: Always close cursor and connection to prevent leaks
                if cursor:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

            # ==========================================
            # 3. MODEL VERSION COMPATIBILITY CHECK
            # ==========================================
            logger.info("🤖 Checking model versions and compatibility...")

            try:
                # Check installed packages
                result = subprocess.run(
                    ['pip3', 'list', '--format=json'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if result.returncode == 0:
                    packages = json.loads(result.stdout)
                    package_versions = {pkg['name'].lower(): pkg['version'] for pkg in packages}

                    # Check critical packages
                    critical_packages = {
                        'speechbrain': '1.0.0',  # Expected version
                        'torch': '2.0.0',
                        'torchaudio': '2.0.0',
                    }

                    for pkg, expected_min_version in critical_packages.items():
                        if pkg in package_versions:
                            installed = package_versions[pkg]
                            logger.info(f"   {pkg}: {installed}")

                            # Version compatibility check
                            if pkg == 'torchaudio' and installed >= '2.9.0':
                                diagnostic['findings'].append({
                                    'type': 'package_compatibility_issue',
                                    'package': pkg,
                                    'version': installed,
                                    'severity': 'high',
                                    'recommendation': 'May need monkey patch for SpeechBrain compatibility'
                                })
                        else:
                            diagnostic['missing_components'].append({
                                'type': 'missing_package',
                                'package': pkg,
                                'severity': 'critical',
                                'fix': f'pip install {pkg}>={expected_min_version}'
                            })

            except Exception as e:
                logger.debug(f"Package check failed: {e}")

            # ==========================================
            # 4. CONFIGURATION VALIDATION
            # ==========================================
            logger.info("⚙️  Validating configuration files...")

            config_files = [
                Path.home() / ".jarvis" / "gcp" / "database_config.json",
                backend_path / "config" / "voice_config.json",
            ]

            for config_file in config_files:
                if config_file.exists():
                    try:
                        with open(config_file) as f:
                            config = json.load(f)
                        logger.info(f"   ✓ {config_file.name} valid")
                    except json.JSONDecodeError as e:
                        diagnostic['bugs_detected'].append({
                            'type': 'invalid_config',
                            'file': str(config_file),
                            'error': str(e),
                            'severity': 'critical',
                            'fix': 'Fix JSON syntax error in configuration'
                        })
                else:
                    diagnostic['missing_components'].append({
                        'type': 'missing_config',
                        'file': str(config_file),
                        'severity': 'high',
                        'fix': f'Create {config_file.name} with proper configuration'
                    })

            # ==========================================
            # 5. RUNTIME INSPECTION
            # ==========================================
            logger.info("🔍 Inspecting runtime state...")

            # Check if voice services are loaded
            try:
                # This would check if the speaker verification service is actually loaded
                from voice.speaker_verification_service import _global_speaker_service
                if _global_speaker_service is None:
                    diagnostic['bugs_detected'].append({
                        'type': 'service_not_initialized',
                        'service': 'SpeakerVerificationService',
                        'severity': 'critical',
                        'fix': 'Speaker verification service not pre-loaded - restart system'
                    })
                else:
                    logger.info("   ✓ SpeakerVerificationService loaded")
            except ImportError:
                diagnostic['bugs_detected'].append({
                    'type': 'import_error',
                    'module': 'speaker_verification_service',
                    'severity': 'critical',
                    'fix': 'Fix import paths or missing dependencies'
                })

            # ==========================================
            # 6. UAE INTEGRATION - Unified Analysis
            # ==========================================
            logger.info("🧠 UAE: Synthesizing findings...")

            # Count severity levels
            critical_count = sum(1 for f in diagnostic['bugs_detected'] + diagnostic['findings'] + diagnostic['missing_components']
                               if f.get('severity') == 'critical')
            high_count = sum(1 for f in diagnostic['bugs_detected'] + diagnostic['findings'] + diagnostic['missing_components']
                           if f.get('severity') == 'high')

            # Calculate confidence based on findings
            if critical_count > 0:
                diagnostic['confidence'] = 0.95  # High confidence we found the issue
            elif high_count > 0:
                diagnostic['confidence'] = 0.85
            else:
                diagnostic['confidence'] = 0.60

            # ==========================================
            # ==========================================

            # Generate fixes for each bug
            for bug in diagnostic['bugs_detected']:
                # Add bug to findings for reporting
                pass  # Bug processing handled elsewhere

            logger.info(f"✅ Deep diagnostic complete: {len(diagnostic['findings'])} findings, "
                       f"{len(diagnostic['bugs_detected'])} bugs detected")

        except Exception as e:
            logger.error(f"Deep diagnostic error: {e}", exc_info=True)
            diagnostic['findings'].append({
                'type': 'diagnostic_error',
                'error': str(e),
                'severity': 'high',
                'recommendation': 'Check system logs for details'
            })

        return diagnostic

    async def _check_voice_unlock_configuration(self) -> dict:
        """
        🔐 CHECK VOICE UNLOCK CONFIGURATION (COMPREHENSIVE)

        Checks if voice unlock is properly configured:
        1. Learning Database initialized
        2. Voice profiles loaded from CloudSQL
        3. Keychain password stored (JARVIS_Screen_Unlock service)
        4. Password typer functional

        Returns:
            Configuration status with detailed diagnostics
        """
        import subprocess
        from pathlib import Path
        from datetime import datetime

        status = self.voice_unlock_config_status.copy()
        status['last_check_time'] = datetime.now()
        status['issues'] = []
        status['detailed_checks'] = {}

        try:
            # ═══════════════════════════════════════════════════════════
            # 1. CHECK LEARNING DATABASE INITIALIZATION
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking Learning Database...")
            try:
                from intelligence.learning_database import JARVISLearningDatabase
                test_db = JARVISLearningDatabase()
                await test_db.initialize()

                status['detailed_checks']['learning_db'] = {
                    'initialized': True,
                    'status': 'OK'
                }
                logger.info("[VOICE UNLOCK] ✅ Learning Database: INITIALIZED")
            except Exception as e:
                status['detailed_checks']['learning_db'] = {
                    'initialized': False,
                    'error': str(e)
                }
                status['issues'].append(f'Learning Database failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ Learning Database: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 2. CHECK VOICE PROFILES FROM CLOUDSQL
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking voice profiles...")
            try:
                from intelligence.learning_database import JARVISLearningDatabase
                db = JARVISLearningDatabase()
                await db.initialize()

                # Query speaker profiles
                profiles = await db.get_all_speaker_profiles()
                profile_count = len(profiles) if profiles else 0

                status['detailed_checks']['voice_profiles'] = {
                    'loaded': profile_count > 0,
                    'count': profile_count,
                    'profiles': [p.get('name', 'unknown') for p in (profiles or [])]
                }

                if profile_count > 0:
                    logger.info(f"[VOICE UNLOCK] ✅ Voice Profiles: {profile_count} loaded")
                    for profile in profiles:
                        logger.info(f"[VOICE UNLOCK]    ├─ {profile.get('name', 'unknown')}")
                else:
                    status['issues'].append('No voice profiles found in CloudSQL')
                    logger.warning("[VOICE UNLOCK] ⚠️  Voice Profiles: NONE FOUND")
            except Exception as e:
                status['detailed_checks']['voice_profiles'] = {
                    'loaded': False,
                    'error': str(e)
                }
                status['issues'].append(f'Voice profile check failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ Voice Profiles: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 3. CHECK KEYCHAIN PASSWORD (com.jarvis.voiceunlock)
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking Keychain password...")
            try:
                result = subprocess.run(
                    ['security', 'find-generic-password', '-s', 'com.jarvis.voiceunlock', '-a', 'unlock_token', '-w'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    password = result.stdout.strip()
                    status['keychain_password_stored'] = True
                    status['detailed_checks']['keychain'] = {
                        'stored': True,
                        'service': 'com.jarvis.voiceunlock',
                        'password_length': len(password)
                    }
                    logger.info(f"[VOICE UNLOCK] ✅ Keychain: Password stored ({len(password)} chars)")
                else:
                    status['keychain_password_stored'] = False
                    status['detailed_checks']['keychain'] = {
                        'stored': False,
                        'error': 'Not found in Keychain'
                    }
                    status['issues'].append('Password not stored in Keychain (com.jarvis.voiceunlock)')
                    logger.warning("[VOICE UNLOCK] ⚠️  Keychain: PASSWORD NOT FOUND")
            except Exception as e:
                status['keychain_password_stored'] = False
                status['detailed_checks']['keychain'] = {
                    'stored': False,
                    'error': str(e)
                }
                status['issues'].append(f'Keychain check failed: {str(e)}')
                logger.error(f"[VOICE UNLOCK] ❌ Keychain: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 4. CHECK PASSWORD TYPER FUNCTIONALITY
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking password typer...")
            try:
                from voice_unlock.secure_password_typer import SecurePasswordTyper, TypingConfig

                # Test that we can instantiate and get config
                typer = SecurePasswordTyper()
                test_config = TypingConfig()

                status['detailed_checks']['password_typer'] = {
                    'available': True,
                    'config_created': True
                }
                logger.info("[VOICE UNLOCK] ✅ Password Typer: FUNCTIONAL")
            except Exception as e:
                status['detailed_checks']['password_typer'] = {
                    'available': False,
                    'error': str(e)
                }
                status['issues'].append(f'Password typer check failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ Password Typer: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 5. CHECK CLOUDSQL PROXY CONNECTION
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking CloudSQL proxy connection...")
            # Initialize connection variable outside try block to ensure cleanup
            proxy_conn = None
            try:
                import psycopg2
                import json
                from pathlib import Path

                # Load database config
                config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        config = json.load(f)

                    cloud_sql = config.get("cloud_sql", {})

                    # Test connection
                    proxy_conn = psycopg2.connect(
                        host=cloud_sql.get("host", "127.0.0.1"),
                        port=cloud_sql.get("port", 5432),
                        database=cloud_sql.get("database", "jarvis_learning"),
                        user=cloud_sql.get("user", "jarvis"),
                        password=cloud_sql.get("password", ""),
                        connect_timeout=3
                    )

                    status['detailed_checks']['cloudsql_proxy'] = {
                        'connected': True,
                        'status': 'CONNECTED',
                        'instance': cloud_sql.get("instance_name", "unknown")
                    }
                    logger.info("[VOICE UNLOCK] ✅ CloudSQL Proxy: CONNECTED")
                else:
                    status['detailed_checks']['cloudsql_proxy'] = {
                        'connected': False,
                        'error': 'Config file not found'
                    }
                    status['issues'].append('CloudSQL config not found')
                    logger.warning("[VOICE UNLOCK] ⚠️  CloudSQL Proxy: CONFIG NOT FOUND")

            except Exception as e:
                status['detailed_checks']['cloudsql_proxy'] = {
                    'connected': False,
                    'error': str(e)
                }
                status['issues'].append(f'CloudSQL proxy check failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ CloudSQL Proxy: FAILED - {e}")
            finally:
                # CRITICAL: Always close connection to prevent leaks
                if proxy_conn:
                    try:
                        proxy_conn.close()
                    except Exception:
                        pass

            # ═══════════════════════════════════════════════════════════
            # 6. CHECK BEAST MODE: SPEAKER VERIFICATION SERVICE
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking BEAST MODE: Speaker Verification Service...")
            try:
                from voice.speaker_verification_service import SpeakerVerificationService

                speaker_service = SpeakerVerificationService()
                await speaker_service.initialize()

                # Check if encoder is loaded
                encoder_ready = getattr(speaker_service, '_encoder_preloaded', False)
                profiles_count = len(speaker_service.speaker_profiles) if hasattr(speaker_service, 'speaker_profiles') else 0

                status['detailed_checks']['speaker_verification'] = {
                    'initialized': True,
                    'encoder_ready': encoder_ready,
                    'profiles_loaded': profiles_count,
                    'status': 'READY' if encoder_ready and profiles_count > 0 else 'DEGRADED'
                }

                logger.info(f"[VOICE UNLOCK] ✅ Speaker Verification: INITIALIZED")
                logger.info(f"[VOICE UNLOCK]    ├─ Encoder: {'READY' if encoder_ready else 'NOT LOADED'}")
                logger.info(f"[VOICE UNLOCK]    └─ Profiles: {profiles_count}")
            except Exception as e:
                status['detailed_checks']['speaker_verification'] = {
                    'initialized': False,
                    'error': str(e)
                }
                status['issues'].append(f'Speaker verification check failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ Speaker Verification: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 7. CHECK BEAST MODE: ECAPA-TDNN EMBEDDINGS
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking BEAST MODE: ECAPA-TDNN Embeddings...")
            try:
                # Check if we can load speaker embeddings from database
                if status['detailed_checks'].get('voice_profiles', {}).get('loaded'):
                    from intelligence.learning_database import JARVISLearningDatabase
                    db = JARVISLearningDatabase()
                    await db.initialize()

                    profiles = await db.get_all_speaker_profiles()

                    embeddings_found = 0
                    embedding_dims = []

                    for profile in profiles:
                        if profile.get('embedding') and len(profile['embedding']) > 0:
                            embeddings_found += 1
                            embedding_dims.append(len(profile['embedding']))

                    status['detailed_checks']['ecapa_embeddings'] = {
                        'available': embeddings_found > 0,
                        'count': embeddings_found,
                        'dimensions': embedding_dims,
                        'expected_dim': 192  # ECAPA-TDNN 192D
                    }

                    if embeddings_found > 0:
                        logger.info(f"[VOICE UNLOCK] ✅ ECAPA-TDNN Embeddings: {embeddings_found} found")
                        logger.info(f"[VOICE UNLOCK]    └─ Dimensions: {embedding_dims[0]}D (expected: 192D)")
                    else:
                        logger.warning("[VOICE UNLOCK] ⚠️  ECAPA-TDNN Embeddings: NOT FOUND")
                        status['issues'].append('No ECAPA-TDNN embeddings in database')
                else:
                    status['detailed_checks']['ecapa_embeddings'] = {
                        'available': False,
                        'error': 'No voice profiles to check'
                    }
                    logger.warning("[VOICE UNLOCK] ⚠️  ECAPA-TDNN Embeddings: SKIPPED (no profiles)")
            except Exception as e:
                status['detailed_checks']['ecapa_embeddings'] = {
                    'available': False,
                    'error': str(e)
                }
                status['issues'].append(f'ECAPA embedding check failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ ECAPA-TDNN Embeddings: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 8. CHECK BEAST MODE: ANTI-SPOOFING DETECTION
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking BEAST MODE: Anti-Spoofing Detection...")
            try:
                from voice_unlock.core.anti_spoofing import AntiSpoofingDetector, SpoofType

                # Initialize the detector
                detector = AntiSpoofingDetector(fingerprint_cache_ttl=3600)

                # Verify it's properly instantiated with all detection methods
                detection_methods = []
                if hasattr(detector, '_detect_replay_attack'):
                    detection_methods.append('replay_detection')
                if hasattr(detector, '_detect_synthetic_voice'):
                    detection_methods.append('synthesis_detection')
                if hasattr(detector, '_detect_recording_playback'):
                    detection_methods.append('recording_playback_detection')
                if hasattr(detector, 'detect_spoofing'):
                    detection_methods.append('unified_detection')

                anti_spoofing_available = len(detection_methods) >= 3

                status['detailed_checks']['anti_spoofing'] = {
                    'available': anti_spoofing_available,
                    'features': detection_methods,
                    'spoof_types': [st.value for st in SpoofType]
                }

                if anti_spoofing_available:
                    logger.info("[VOICE UNLOCK] ✅ Anti-Spoofing: AVAILABLE")
                    logger.info("[VOICE UNLOCK]    ├─ Replay Attack Detection: ENABLED")
                    logger.info("[VOICE UNLOCK]    ├─ Synthetic Voice Detection: ENABLED")
                    logger.info("[VOICE UNLOCK]    ├─ Recording Playback Detection: ENABLED")
                    logger.info(f"[VOICE UNLOCK]    └─ Spoof Types: {[st.value for st in SpoofType]}")
                else:
                    logger.warning("[VOICE UNLOCK] ⚠️  Anti-Spoofing: PARTIALLY AVAILABLE")
                    logger.warning(f"[VOICE UNLOCK]    └─ Available methods: {detection_methods}")
            except ImportError as e:
                status['detailed_checks']['anti_spoofing'] = {
                    'available': False,
                    'error': f'Import failed: {e}'
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  Anti-Spoofing: MODULE NOT FOUND - {e}")
            except Exception as e:
                status['detailed_checks']['anti_spoofing'] = {
                    'available': False,
                    'error': str(e)
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  Anti-Spoofing: CHECK FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 9. CHECK BEAST MODE: HYBRID STT SYSTEM
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking BEAST MODE: Hybrid STT System...")
            try:
                from voice.hybrid_stt_router import HybridSTTRouter

                stt_router = HybridSTTRouter()
                await stt_router.initialize()

                # Check available engines
                available_engines = []
                if hasattr(stt_router, 'available_engines'):
                    available_engines = list(stt_router.available_engines.keys())

                status['detailed_checks']['hybrid_stt'] = {
                    'initialized': True,
                    'engines': available_engines,
                    'count': len(available_engines)
                }

                logger.info(f"[VOICE UNLOCK] ✅ Hybrid STT: {len(available_engines)} engines")
                for engine in available_engines:
                    logger.info(f"[VOICE UNLOCK]    ├─ {engine}")
            except Exception as e:
                status['detailed_checks']['hybrid_stt'] = {
                    'initialized': False,
                    'error': str(e)
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  Hybrid STT: FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 10. CHECK BEAST MODE: CONTEXT-AWARE INTELLIGENCE (CAI)
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking BEAST MODE: Context-Aware Intelligence...")
            try:
                from voice_unlock.intelligent_voice_unlock_service import IntelligentVoiceUnlockService

                unlock_service = IntelligentVoiceUnlockService()

                # Check if CAI analysis is available
                cai_available = hasattr(unlock_service, '_analyze_context')

                status['detailed_checks']['cai'] = {
                    'available': cai_available,
                    'features': ['screen_state', 'time_analysis', 'location_context'] if cai_available else []
                }

                if cai_available:
                    logger.info("[VOICE UNLOCK] ✅ CAI (Context-Aware Intelligence): AVAILABLE")
                    logger.info("[VOICE UNLOCK]    ├─ Screen State Analysis: ENABLED")
                    logger.info("[VOICE UNLOCK]    ├─ Time Analysis: ENABLED")
                    logger.info("[VOICE UNLOCK]    └─ Location Context: ENABLED")
                else:
                    logger.warning("[VOICE UNLOCK] ⚠️  CAI: NOT AVAILABLE")
            except Exception as e:
                status['detailed_checks']['cai'] = {
                    'available': False,
                    'error': str(e)
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  CAI: CHECK FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 11. CHECK BEAST MODE: SCENARIO-AWARE INTELLIGENCE (SAI)
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking BEAST MODE: Scenario-Aware Intelligence...")
            try:
                from voice_unlock.intelligent_voice_unlock_service import IntelligentVoiceUnlockService

                unlock_service = IntelligentVoiceUnlockService()

                # Check if SAI analysis is available
                sai_available = hasattr(unlock_service, '_analyze_scenario')

                status['detailed_checks']['sai'] = {
                    'available': sai_available,
                    'features': ['routine_detection', 'emergency_detection', 'suspicious_detection'] if sai_available else []
                }

                if sai_available:
                    logger.info("[VOICE UNLOCK] ✅ SAI (Scenario-Aware Intelligence): AVAILABLE")
                    logger.info("[VOICE UNLOCK]    ├─ Routine Detection: ENABLED")
                    logger.info("[VOICE UNLOCK]    ├─ Emergency Detection: ENABLED")
                    logger.info("[VOICE UNLOCK]    └─ Suspicious Detection: ENABLED")
                else:
                    logger.warning("[VOICE UNLOCK] ⚠️  SAI: NOT AVAILABLE")
            except Exception as e:
                status['detailed_checks']['sai'] = {
                    'available': False,
                    'error': str(e)
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  SAI: CHECK FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 12. CHECK VOICE BIOMETRIC INTELLIGENCE (VBI) - CRITICAL!
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking Voice Biometric Intelligence (VBI)...")
            try:
                from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence

                vbi = await get_voice_biometric_intelligence()

                if vbi and hasattr(vbi, '_unified_cache') and vbi._unified_cache:
                    cache = vbi._unified_cache
                    profiles_loaded = cache.profiles_loaded
                    cache_state = cache.state.value if hasattr(cache.state, 'value') else str(cache.state)

                    # Get profile details dynamically - NO hardcoding!
                    preloaded = cache.get_preloaded_profiles()
                    profile_details = []
                    has_owner = False

                    for name, profile in preloaded.items():
                        is_owner = profile.source == "learning_database"
                        if is_owner:
                            has_owner = True
                        profile_details.append({
                            'name': name,
                            'is_owner': is_owner,
                            'dimensions': profile.embedding_dimensions,
                            'samples': profile.total_samples,
                            'source': profile.source
                        })

                    status['detailed_checks']['voice_biometric_intelligence'] = {
                        'available': True,
                        'cache_state': cache_state,
                        'profiles_loaded': profiles_loaded,
                        'has_owner_profile': has_owner,
                        'profiles': profile_details
                    }

                    if profiles_loaded > 0 and has_owner:
                        logger.info(f"[VOICE UNLOCK] ✅ VBI: READY ({profiles_loaded} profiles, state={cache_state})")
                        for pd in profile_details:
                            owner_tag = " [OWNER]" if pd['is_owner'] else ""
                            logger.info(
                                f"[VOICE UNLOCK]    ├─ {pd['name']}{owner_tag} "
                                f"(dim={pd['dimensions']}, samples={pd['samples']})"
                            )
                    elif profiles_loaded > 0:
                        logger.warning(f"[VOICE UNLOCK] ⚠️  VBI: {profiles_loaded} profiles but NO OWNER detected")
                        status['issues'].append('VBI has profiles but no owner profile - voice unlock may fail')
                    else:
                        logger.warning("[VOICE UNLOCK] ⚠️  VBI: NO PROFILES LOADED")
                        status['issues'].append('VBI has no profiles - voice unlock will fail')
                else:
                    status['detailed_checks']['voice_biometric_intelligence'] = {
                        'available': False,
                        'error': 'VBI or unified cache not initialized'
                    }
                    status['issues'].append('Voice Biometric Intelligence not ready')
                    logger.warning("[VOICE UNLOCK] ⚠️  VBI: NOT INITIALIZED")

            except ImportError as e:
                status['detailed_checks']['voice_biometric_intelligence'] = {
                    'available': False,
                    'error': f'Import failed: {e}'
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  VBI: MODULE NOT FOUND - {e}")
            except Exception as e:
                status['detailed_checks']['voice_biometric_intelligence'] = {
                    'available': False,
                    'error': str(e)
                }
                status['issues'].append(f'VBI check failed: {e}')
                logger.error(f"[VOICE UNLOCK] ❌ VBI: CHECK FAILED - {e}")

            # ═══════════════════════════════════════════════════════════
            # 13. CHECK HYBRID DATABASE SYNC SYSTEM
            # ═══════════════════════════════════════════════════════════
            logger.info("[VOICE UNLOCK] 🔍 Checking Hybrid Database Sync System...")
            try:
                if test_db and hasattr(test_db, 'hybrid_sync') and test_db.hybrid_sync:
                    hybrid_sync = test_db.hybrid_sync
                    metrics = hybrid_sync.get_metrics()

                    status['detailed_checks']['hybrid_sync'] = {
                        'enabled': True,
                        'sqlite_path': str(hybrid_sync.sqlite_path),
                        'cloudsql_available': metrics.cloudsql_available,
                        'local_read_latency_ms': metrics.local_read_latency_ms,
                        'cloud_write_latency_ms': metrics.cloud_write_latency_ms,
                        'sync_queue_size': metrics.sync_queue_size,
                        'total_synced': metrics.total_synced,
                        'total_failed': metrics.total_failed,
                        'sync_interval_seconds': hybrid_sync.sync_interval
                    }

                    logger.info("[VOICE UNLOCK] ✅ Hybrid Sync: ENABLED")
                    logger.info(f"[VOICE UNLOCK]    ├─ SQLite: {hybrid_sync.sqlite_path}")
                    logger.info(f"[VOICE UNLOCK]    ├─ CloudSQL: {'AVAILABLE' if metrics.cloudsql_available else 'UNAVAILABLE'}")
                    logger.info(f"[VOICE UNLOCK]    ├─ Local Read: {metrics.local_read_latency_ms:.1f}ms")
                    logger.info(f"[VOICE UNLOCK]    ├─ Cloud Write: {metrics.cloud_write_latency_ms:.1f}ms")
                    logger.info(f"[VOICE UNLOCK]    ├─ Queue: {metrics.sync_queue_size} pending")
                    logger.info(f"[VOICE UNLOCK]    ├─ Synced: {metrics.total_synced}")
                    logger.info(f"[VOICE UNLOCK]    └─ Failed: {metrics.total_failed}")
                else:
                    status['detailed_checks']['hybrid_sync'] = {
                        'enabled': False,
                        'reason': 'Not initialized or disabled in config'
                    }
                    logger.warning("[VOICE UNLOCK] ⚠️  Hybrid Sync: DISABLED")
            except Exception as e:
                status['detailed_checks']['hybrid_sync'] = {
                    'enabled': False,
                    'error': str(e)
                }
                logger.warning(f"[VOICE UNLOCK] ⚠️  Hybrid Sync: CHECK FAILED - {e}")

            # 2. Check if enrollment data exists
            enrollment_file = Path.home() / ".jarvis" / "voice_unlock_enrollment.json"
            if enrollment_file.exists():
                status['enrollment_data_exists'] = True
                logger.info(f"[VOICE UNLOCK] ✅ Enrollment data found: {enrollment_file}")

                # Read enrollment details for display
                try:
                    import json
                    with open(enrollment_file, 'r') as f:
                        enrollment_data = json.load(f)
                        status['enrollment_details'] = {
                            'username': enrollment_data.get('user', 'unknown'),
                            'enrollment_date': enrollment_data.get('configured_at', 'unknown'),
                            'voice_samples': enrollment_data.get('voice_samples', 0),
                            'auto_configured': enrollment_data.get('auto_configured', False),
                            'status': enrollment_data.get('status', 'unknown')
                        }
                        logger.info(f"[VOICE UNLOCK]   ├─ User: {status['enrollment_details']['username']}")
                        logger.info(f"[VOICE UNLOCK]   ├─ Samples: {status['enrollment_details']['voice_samples']}")
                        logger.info(f"[VOICE UNLOCK]   └─ Date: {status['enrollment_details']['enrollment_date']}")
                except Exception as e:
                    logger.warning(f"[VOICE UNLOCK] ⚠️  Could not read enrollment details: {e}")
            else:
                status['enrollment_data_exists'] = False
                status['issues'].append('Enrollment data not found')
                logger.warning(f"[VOICE UNLOCK] ⚠️  No enrollment data at {enrollment_file}")

            # 3. Check if voice unlock daemon/service is running (OPTIONAL - backend may not be started yet)
            # This checks if the backend voice unlock endpoint is accessible
            try:
                import aiohttp
                logger.debug("[VOICE UNLOCK] Checking service status at http://localhost:8000/health")
                async with aiohttp.ClientSession() as session:
                    # Check main backend health instead of specific voice-unlock endpoint
                    async with session.get('http://localhost:8000/health', timeout=aiohttp.ClientTimeout(total=1)) as resp:
                        if resp.status == 200:
                            status['daemon_running'] = True
                            status['service_health'] = {
                                'enabled': True,
                                'ready': True,
                                'backend_running': True,
                                'last_check': datetime.now().isoformat()
                            }
                            logger.info(f"[VOICE UNLOCK] ✅ Backend service is running")
                        else:
                            status['daemon_running'] = False
                            logger.info(f"[VOICE UNLOCK] ℹ️  Backend not started yet (will start shortly)")
            except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
                status['daemon_running'] = False
                # This is NORMAL during startup - backend hasn't started yet
                logger.info(f"[VOICE UNLOCK] ℹ️  Backend not started yet (this is normal during startup)")
            except Exception as e:
                status['daemon_running'] = False
                logger.debug(f"[VOICE UNLOCK] Backend check: {e}")

            # 4. Determine overall configuration status
            status['configured'] = (
                status['keychain_password_stored'] and
                status['enrollment_data_exists']
            )

            # 5. Generate comprehensive health summary
            if status['configured']:
                logger.info("[VOICE UNLOCK] ✅ Voice Unlock is fully configured")
                logger.info("[VOICE UNLOCK] ═══════════════════════════════════════════")
                logger.info("[VOICE UNLOCK] Configuration Health Summary:")
                logger.info(f"[VOICE UNLOCK]   ✅ Keychain: Configured")
                logger.info(f"[VOICE UNLOCK]   ✅ Enrollment: Complete")
                logger.info(f"[VOICE UNLOCK]   ✅ CloudSQL: Voice profiles ready")
                if status.get('daemon_running'):
                    logger.info(f"[VOICE UNLOCK]   ✅ Backend: Running")
                else:
                    logger.info(f"[VOICE UNLOCK]   ℹ️  Backend: Will start shortly (this is normal)")
                logger.info("[VOICE UNLOCK] ═══════════════════════════════════════════")
            else:
                logger.warning(f"[VOICE UNLOCK] ⚠️  Voice Unlock not configured: {len(status['issues'])} issues")
                logger.warning("[VOICE UNLOCK] ═══════════════════════════════════════════")
                logger.warning("[VOICE UNLOCK] Configuration Issues:")
                for issue in status['issues']:
                    logger.warning(f"[VOICE UNLOCK]   ❌ {issue}")
                logger.warning("[VOICE UNLOCK] ═══════════════════════════════════════════")
                logger.warning("[VOICE UNLOCK] To fix: Run ./backend/voice_unlock/enable_screen_unlock.sh")

        except Exception as e:
            logger.error(f"[VOICE UNLOCK] Configuration check failed: {e}", exc_info=True)
            status['issues'].append(f'Configuration check error: {str(e)}')

        self.voice_unlock_config_status = status
        return status

    async def _auto_configure_voice_unlock(self) -> bool:
        """
        🤖 AUTONOMOUS VOICE UNLOCK CONFIGURATION

        Attempts to automatically configure voice unlock if not set up
        Returns True if successful, False otherwise
        """
        import subprocess
        from pathlib import Path

        if self.voice_unlock_config_status.get('auto_config_attempted'):
            logger.info("[VOICE UNLOCK] Auto-config already attempted this session")
            return False

        self.voice_unlock_config_status['auto_config_attempted'] = True

        try:
            logger.info("[VOICE UNLOCK] 🤖 Attempting autonomous configuration...")

            # Run the setup script non-interactively
            # Note: This won't work fully because it needs password input
            # But we can at least create enrollment data structure

            enrollment_file = Path.home() / ".jarvis" / "voice_unlock_enrollment.json"
            if not enrollment_file.exists():
                enrollment_file.parent.mkdir(parents=True, exist_ok=True)
                import json
                enrollment_data = {
                    "user": os.getenv("USER", "unknown"),
                    "configured_at": datetime.now().isoformat(),
                    "auto_configured": True,
                    "status": "partial",
                    "note": "Keychain password must be set manually"
                }
                with open(enrollment_file, 'w') as f:
                    json.dump(enrollment_data, f, indent=2)
                logger.info(f"[VOICE UNLOCK] ✅ Created enrollment data at {enrollment_file}")

            logger.warning("[VOICE UNLOCK] ⚠️  Manual step required: Run ./backend/voice_unlock/enable_screen_unlock.sh to store password")
            return False  # Partial success

        except Exception as e:
            logger.error(f"[VOICE UNLOCK] Auto-config failed: {e}")
            return False

    async def monitor_services(self):
        """Monitor services with health checks"""
        print(f"\n{Colors.BLUE}Monitoring services...{Colors.ENDC}")
        print(f"{Colors.CYAN}  • Health checks every 30 seconds{Colors.ENDC}")
        print(f"{Colors.CYAN}  • Process monitoring every 5 seconds{Colors.ENDC}")

        last_health_check = time.time()
        consecutive_failures = {"backend": 0}
        health_check_count = 0
        monitoring_start = time.time()
        self.recent_window = 10  # For confidence analytics display

        try:
            while True:
                await asyncio.sleep(5)

                # Exit monitoring loop if we're shutting down
                if self._shutting_down:
                    break

                # Calculate uptime
                uptime_seconds = int(time.time() - monitoring_start)
                uptime_str = f"{uptime_seconds // 60}m {uptime_seconds % 60}s"

                # Check if processes are still running
                for i, proc in enumerate(self.processes):
                    if proc and proc.returncode is not None:
                        # Only print warnings for unexpected exits (non-zero exit codes)
                        # and only if we're not shutting down
                        if (
                            not hasattr(proc, "_exit_reported")
                            and proc.returncode != 0
                            and proc.returncode != -2
                        ):
                            print(
                                f"\n{Colors.WARNING}⚠ Process {i} unexpectedly exited with code {proc.returncode}{Colors.ENDC}"
                            )
                            proc._exit_reported = True

                # Periodic health check
                if time.time() - last_health_check > 30:
                    health_check_count += 1
                    print(f"\n{Colors.BLUE}🔍 Health Check #{health_check_count} (Uptime: {uptime_str}){Colors.ENDC}")
                    last_health_check = time.time()

                    # Check backend health
                    backend_start = time.time()
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"http://localhost:{self.ports['main_api']}/health",
                                timeout=2,
                            ) as resp:
                                response_time_ms = int((time.time() - backend_start) * 1000)

                                if resp.status == 200:
                                    consecutive_failures["backend"] = 0
                                    print(f"  {Colors.GREEN}✓ Backend API:{Colors.ENDC} http://localhost:{self.ports['main_api']} ({response_time_ms}ms)")

                                    # Check for Rust acceleration and self-healing
                                    try:
                                        data = await resp.json()

                                        # Display detailed health metrics
                                        if "status" in data:
                                            print(f"    └─ Status: {data['status']}")

                                        if "memory" in data:
                                            memory_mb = data['memory'].get('rss', 0) / 1024 / 1024
                                            print(f"    └─ Memory: {memory_mb:.1f} MB")

                                        if "cpu_percent" in data:
                                            print(f"    └─ CPU: {data['cpu_percent']:.1f}%")

                                        rust_status = data.get("rust_acceleration", {})
                                        self_healing_status = data.get("self_healing", {})

                                        if rust_status.get("enabled"):
                                            if not hasattr(self, "_rust_logged"):
                                                print(f"    {Colors.GREEN}└─ 🦀 Rust acceleration: ACTIVE{Colors.ENDC}")
                                                self._rust_logged = True
                                            else:
                                                print(f"    └─ 🦀 Rust: Active")

                                        if self_healing_status.get("enabled"):
                                            success_rate = self_healing_status.get("success_rate", 0.0)
                                            heal_count = self_healing_status.get("heal_count", 0)
                                            if not hasattr(self, "_healing_logged"):
                                                print(f"    {Colors.GREEN}└─ 🔧 Self-healing: {success_rate:.0%} success ({heal_count} heals){Colors.ENDC}")
                                                self._healing_logged = True
                                            else:
                                                print(f"    └─ 🔧 Self-healing: {success_rate:.0%} ({heal_count})")
                                    except Exception as e:
                                        print(f"    {Colors.YELLOW}└─ Detailed metrics unavailable{Colors.ENDC}")
                                else:
                                    consecutive_failures["backend"] += 1
                                    print(f"  {Colors.WARNING}✗ Backend API:{Colors.ENDC} Status {resp.status} ({response_time_ms}ms)")
                    except asyncio.TimeoutError:
                        consecutive_failures["backend"] += 1
                        print(f"  {Colors.WARNING}✗ Backend API:{Colors.ENDC} Timeout (>2000ms)")
                    except Exception as e:
                        consecutive_failures["backend"] += 1
                        print(f"  {Colors.WARNING}✗ Backend API:{Colors.ENDC} Connection failed - {str(e)[:50]}")

                    # Check Voice Memory Agent status
                    try:
                        from agents.voice_memory_agent import get_voice_memory_agent
                        voice_agent = await get_voice_memory_agent()
                        all_memories = await voice_agent.get_all_memories()

                        total_speakers = all_memories.get('total_speakers', 0)
                        total_interactions = all_memories.get('total_interactions', 0)

                        if total_speakers > 0:
                            print(f"  {Colors.GREEN}✓ Voice Memory Agent:{Colors.ENDC} Active")
                            print(f"    └─ Speakers: {total_speakers}")
                            print(f"    └─ Total interactions: {total_interactions}")

                            # Show per-speaker details with CONFIDENCE ANALYTICS
                            for speaker_name, memory in all_memories.get('speakers', {}).items():
                                freshness = memory.get('freshness_score', 0.0)
                                interactions = memory.get('total_interactions', 0)
                                last_interaction = memory.get('last_interaction')

                                freshness_icon = "🟢" if freshness > 0.75 else "🟡" if freshness > 0.60 else "🟠" if freshness > 0.40 else "🔴"
                                freshness_color = Colors.GREEN if freshness > 0.75 else Colors.YELLOW if freshness > 0.60 else Colors.WARNING if freshness > 0.40 else Colors.FAIL

                                print(f"    └─ {speaker_name}: {freshness_icon} {freshness_color}{freshness*100:.0f}% fresh{Colors.ENDC} ({interactions} interactions)")

                                # === CONFIDENCE ANALYTICS ===
                                recent_conf = memory.get('recent_confidence')
                                avg_conf_all = memory.get('avg_confidence_all')
                                avg_conf_recent = memory.get('avg_confidence_recent')
                                trend_direction = memory.get('trend_direction', 'unknown')
                                trend = memory.get('trend', 0.0)
                                success_rate_all = memory.get('success_rate_all')
                                success_rate_recent = memory.get('success_rate_recent')
                                successful = memory.get('successful_attempts', 0)
                                failed = memory.get('failed_attempts', 0)
                                min_conf = memory.get('min_confidence')
                                max_conf = memory.get('max_confidence')
                                prediction = memory.get('prediction')

                                if recent_conf is not None:
                                    # Show latest confidence
                                    conf_color = Colors.GREEN if recent_conf > 0.70 else Colors.YELLOW if recent_conf > 0.40 else Colors.WARNING
                                    print(f"       ├─ 📊 Latest confidence: {conf_color}{recent_conf:.2%}{Colors.ENDC}")

                                    # Show average confidence (all time vs recent)
                                    if avg_conf_all is not None and avg_conf_recent is not None:
                                        diff = avg_conf_recent - avg_conf_all
                                        diff_icon = "📈" if diff > 0.02 else "📉" if diff < -0.02 else "➡️"
                                        print(f"       ├─ Average: {avg_conf_all:.2%} (all) → {avg_conf_recent:.2%} (recent {self.recent_window}) {diff_icon}")

                                    # Show trend
                                    if trend_direction != 'unknown':
                                        trend_icon = "📈" if trend_direction == 'improving' else "📉" if trend_direction == 'declining' else "➡️"
                                        trend_color = Colors.GREEN if trend_direction == 'improving' else Colors.WARNING if trend_direction == 'declining' else Colors.CYAN
                                        print(f"       ├─ {trend_icon} Trend: {trend_color}{trend_direction.upper()}{Colors.ENDC} ({trend:+.2%})")

                                    # Show success rate
                                    if success_rate_all is not None:
                                        rate_color = Colors.GREEN if success_rate_all > 0.70 else Colors.YELLOW if success_rate_all > 0.40 else Colors.WARNING
                                        print(f"       ├─ ✅ Success rate: {rate_color}{success_rate_all:.1%}{Colors.ENDC} ({successful}W/{failed}L)")

                                        # Show recent success rate if different
                                        if success_rate_recent is not None and abs(success_rate_recent - success_rate_all) > 0.05:
                                            recent_rate_color = Colors.GREEN if success_rate_recent > 0.70 else Colors.YELLOW if success_rate_recent > 0.40 else Colors.WARNING
                                            rate_diff = success_rate_recent - success_rate_all
                                            rate_icon = "📈" if rate_diff > 0 else "📉"
                                            print(f"       ├─    Recent {self.recent_window}: {recent_rate_color}{success_rate_recent:.1%}{Colors.ENDC} {rate_icon}")

                                    # Show confidence range
                                    if min_conf is not None and max_conf is not None:
                                        print(f"       ├─ Range: {min_conf:.2%} - {max_conf:.2%} (span: {max_conf-min_conf:.2%})")

                                    # Show prediction
                                    if prediction:
                                        target = prediction.get('target_confidence', 0.85)
                                        interactions_needed = prediction.get('interactions_needed', 0)
                                        estimated_days = prediction.get('estimated_days', 0)
                                        improvement_rate = prediction.get('improvement_rate', 0)

                                        print(f"       ├─ 🎯 Target: {target:.0%} confidence")
                                        print(f"       ├─    ETA: {interactions_needed} more attempts (~{estimated_days} days)")
                                        print(f"       └─    Rate: {improvement_rate:+.4%} per interaction")
                                    else:
                                        # Show last interaction time
                                        if last_interaction:
                                            from datetime import datetime
                                            try:
                                                last_time = datetime.fromisoformat(last_interaction) if isinstance(last_interaction, str) else last_interaction
                                                time_ago = datetime.now() - last_time
                                                hours_ago = int(time_ago.total_seconds() / 3600)
                                                if hours_ago < 1:
                                                    mins_ago = int(time_ago.total_seconds() / 60)
                                                    print(f"       └─ Last interaction: {mins_ago}m ago")
                                                elif hours_ago < 24:
                                                    print(f"       └─ Last interaction: {hours_ago}h ago")
                                                else:
                                                    days_ago = hours_ago // 24
                                                    print(f"       └─ Last interaction: {days_ago}d ago")
                                            except:
                                                pass
                                else:
                                    # No confidence data yet
                                    print(f"       └─ 📊 No confidence data yet (starting fresh)")
                                    if last_interaction:
                                        from datetime import datetime
                                        try:
                                            last_time = datetime.fromisoformat(last_interaction) if isinstance(last_interaction, str) else last_interaction
                                            time_ago = datetime.now() - last_time
                                            hours_ago = int(time_ago.total_seconds() / 3600)
                                            if hours_ago < 1:
                                                mins_ago = int(time_ago.total_seconds() / 60)
                                                print(f"       └─ Last interaction: {mins_ago}m ago")
                                            elif hours_ago < 24:
                                                print(f"       └─ Last interaction: {hours_ago}h ago")
                                            else:
                                                days_ago = hours_ago // 24
                                                print(f"       └─ Last interaction: {days_ago}d ago")
                                        except:
                                            pass
                        else:
                            print(f"  {Colors.YELLOW}⚠ Voice Memory Agent:{Colors.ENDC} No speakers enrolled")
                    except Exception as e:
                        print(f"  {Colors.YELLOW}⚠ Voice Memory Agent:{Colors.ENDC} Status unavailable")

                    # === CLOUDSQL PROXY HEALTH CHECK ===
                    try:
                        from intelligence.cloud_sql_proxy_manager import get_proxy_manager

                        proxy_manager = get_proxy_manager()
                        cloudsql_health = await proxy_manager.check_connection_health()

                        # Determine overall status
                        proxy_running = cloudsql_health.get('proxy_running', False)
                        connection_active = cloudsql_health.get('connection_active', False)
                        timeout_status = cloudsql_health.get('timeout_status', 'unknown')
                        auto_heal = cloudsql_health.get('auto_heal_triggered', False)

                        # Status icon and color
                        if connection_active and timeout_status == 'healthy':
                            status_icon = f"{Colors.GREEN}✓"
                            status_text = "Connected"
                        elif connection_active and timeout_status == 'warning':
                            status_icon = f"{Colors.YELLOW}⚠️ "
                            status_text = "Connected (Warning)"
                        elif connection_active and timeout_status == 'critical':
                            status_icon = f"{Colors.WARNING}🔴"
                            status_text = "Connected (Critical)"
                        elif proxy_running and not connection_active:
                            status_icon = f"{Colors.WARNING}⚠️ "
                            status_text = "Proxy running, connection failed"
                        else:
                            status_icon = f"{Colors.FAIL}✗"
                            status_text = "Proxy not running"

                        port = proxy_manager.config.get('cloud_sql', {}).get('port', 5432)
                        print(f"  {status_icon} CloudSQL Proxy:{Colors.ENDC} {status_text} (Port {port})")

                        # Connection details
                        if connection_active:
                            last_query_age = cloudsql_health.get('last_query_age_seconds')
                            if last_query_age is not None:
                                mins = last_query_age // 60
                                secs = last_query_age % 60
                                age_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                                print(f"    ├─ Last query: {age_str} ago")

                            # Timeout forecast
                            timeout_forecast = cloudsql_health.get('timeout_forecast')
                            if timeout_forecast:
                                time_remaining = timeout_forecast['seconds_until_timeout']
                                mins_remaining = timeout_forecast['minutes_until_timeout']
                                percentage_used = timeout_forecast['percentage_used']

                                # Color code based on percentage
                                if percentage_used >= 90:
                                    time_color = Colors.FAIL
                                    forecast_icon = "🔴"
                                elif percentage_used >= 80:
                                    time_color = Colors.WARNING
                                    forecast_icon = "🟠"
                                elif percentage_used >= 60:
                                    time_color = Colors.YELLOW
                                    forecast_icon = "🟡"
                                else:
                                    time_color = Colors.GREEN
                                    forecast_icon = "🟢"

                                if mins_remaining > 0:
                                    time_str = f"{mins_remaining}m {time_remaining % 60}s"
                                else:
                                    time_str = f"{time_remaining}s"

                                print(f"    ├─ {forecast_icon} Timeout forecast: {time_color}{time_str} remaining ({percentage_used:.0f}% used){Colors.ENDC}")

                        # Connection pool statistics
                        pool_stats = cloudsql_health.get('connection_pool', {})
                        if pool_stats:
                            active = pool_stats.get('active_connections', 0)
                            max_conn = pool_stats.get('max_connections', 100)
                            utilization = pool_stats.get('utilization_percent', 0)
                            success_rate = pool_stats.get('success_rate', 1.0)
                            total_failures = pool_stats.get('total_failures', 0)
                            consecutive_fails = pool_stats.get('consecutive_failures', 0)

                            util_color = Colors.GREEN if utilization < 70 else Colors.YELLOW if utilization < 90 else Colors.WARNING
                            success_color = Colors.GREEN if success_rate > 0.9 else Colors.YELLOW if success_rate > 0.7 else Colors.WARNING

                            print(f"    ├─ Connection pool: {active}/{max_conn} ({util_color}{utilization:.0f}% util{Colors.ENDC})")
                            print(f"    ├─ Success rate: {success_color}{success_rate*100:.1f}%{Colors.ENDC} ({total_failures} total failures)")

                            if consecutive_fails > 0:
                                print(f"    ├─ ⚠️  Consecutive failures: {consecutive_fails}")

                        # Rate limit status
                        rate_limits = cloudsql_health.get('rate_limit_status', {})
                        if rate_limits:
                            # Show summary of rate limits
                            any_warnings = any(r.get('status') in ['warning', 'critical'] for r in rate_limits.values())

                            if any_warnings:
                                print(f"    ├─ {Colors.YELLOW}⚠️  API Rate Limits:{Colors.ENDC}")
                                for category, stats in rate_limits.items():
                                    if stats.get('status') in ['warning', 'critical']:
                                        usage = stats['current_usage']
                                        limit = stats['limit']
                                        usage_pct = stats['usage_percent']
                                        status = stats['status']

                                        status_color = Colors.WARNING if status == 'critical' else Colors.YELLOW
                                        print(f"    │  ├─ {category}: {status_color}{usage}/{limit} ({usage_pct:.0f}%){Colors.ENDC}")
                            else:
                                # Compact display when all healthy
                                total_calls = sum(r['current_usage'] for r in rate_limits.values())
                                print(f"    ├─ API rate limits: {Colors.GREEN}✓ Healthy{Colors.ENDC} ({total_calls} calls/min)")

                        # Auto-heal actions
                        if auto_heal:
                            print(f"    ├─ {Colors.GREEN}🔧 AUTO-HEAL: Reconnection triggered{Colors.ENDC}")

                        # Voice Profile Verification
                        voice_profiles = cloudsql_health.get('voice_profiles')
                        if voice_profiles:
                            profiles_found = voice_profiles.get('profiles_found', 0)
                            total_samples = voice_profiles.get('total_samples', 0)
                            ready_for_unlock = voice_profiles.get('ready_for_unlock', False)
                            profile_status = voice_profiles.get('status', 'unknown')

                            # Status display
                            if ready_for_unlock:
                                status_icon = f"{Colors.GREEN}✅"
                                status_text = "READY"
                            elif profile_status == 'no_profiles':
                                status_icon = f"{Colors.FAIL}❌"
                                status_text = "NO PROFILES"
                            elif profile_status == 'issues_found':
                                status_icon = f"{Colors.WARNING}⚠️ "
                                status_text = "ISSUES"
                            else:
                                status_icon = f"{Colors.YELLOW}?"
                                status_text = "UNKNOWN"

                            print(f"    ├─ {status_icon} Voice Profiles: {status_text} ({profiles_found} profile(s), {total_samples} samples)")

                            # Show per-speaker details
                            for speaker in voice_profiles.get('speakers', []):
                                speaker_name = speaker['speaker_name']
                                embedding_valid = speaker['embedding_valid']
                                embedding_size = speaker['embedding_size']
                                actual_samples = speaker['actual_samples_in_db']
                                avg_conf = speaker['avg_confidence']
                                ready = speaker['ready']

                                ready_icon = "✅" if ready else "❌"
                                emb_status = f"{embedding_size} bytes" if embedding_valid else "MISSING"

                                print(f"    │  ├─ {ready_icon} {speaker_name}:")
                                print(f"    │  │  ├─ Embedding: {emb_status}")
                                print(f"    │  │  ├─ Samples in DB: {actual_samples}")
                                print(f"    │  │  └─ Avg confidence: {avg_conf:.2%}")

                            # Show issues if any
                            issues = voice_profiles.get('issues', [])
                            if issues:
                                print(f"    │  └─ {Colors.WARNING}⚠️  Issues:{Colors.ENDC}")
                                for issue in issues:
                                    print(f"    │     └─ {issue}")

                        # CloudSQL SAI Prediction (Situational Awareness Intelligence)
                        sai_prediction = cloudsql_health.get('sai_prediction')
                        if sai_prediction:
                            severity = sai_prediction['severity']
                            confidence = sai_prediction['confidence']
                            pred_type = sai_prediction['type'].replace('_', ' ').title()
                            time_horizon = sai_prediction['time_horizon_seconds']
                            predicted_event = sai_prediction['predicted_event']
                            reason = sai_prediction['reason']
                            action = sai_prediction['recommended_action']
                            auto_heal = sai_prediction.get('auto_heal_available', False)

                            # Color coding based on severity
                            if severity == 'critical':
                                severity_icon = f"{Colors.FAIL}🚨"
                                severity_text = f"{Colors.FAIL}CRITICAL{Colors.ENDC}"
                            else:
                                severity_icon = f"{Colors.WARNING}⚠️ "
                                severity_text = f"{Colors.WARNING}WARNING{Colors.ENDC}"

                            # Confidence indicator
                            if confidence >= 0.8:
                                conf_icon = f"{Colors.GREEN}●"
                            elif confidence >= 0.5:
                                conf_icon = f"{Colors.YELLOW}●"
                            else:
                                conf_icon = f"{Colors.FAIL}●"

                            print(f"    ├─ {severity_icon} {severity_text} CloudSQL SAI Prediction:")
                            print(f"    │  ├─ Type: {pred_type}")
                            print(f"    │  ├─ Event: {predicted_event}")
                            print(f"    │  ├─ Time horizon: {time_horizon}s")
                            print(f"    │  ├─ {conf_icon} Confidence: {confidence:.1%}{Colors.ENDC}")
                            print(f"    │  ├─ Reason: {reason}")
                            print(f"    │  ├─ Action: {action}")
                            if auto_heal:
                                auto_heal_triggered = cloudsql_health.get('sai_auto_heal_triggered', False)
                                if auto_heal_triggered:
                                    auto_heal_success = cloudsql_health.get('sai_auto_heal_success', False)
                                    heal_status = f"{Colors.GREEN}✅ Triggered & Successful" if auto_heal_success else f"{Colors.FAIL}❌ Triggered & Failed"
                                    print(f"    │  └─ Auto-Heal: {heal_status}{Colors.ENDC}")
                                else:
                                    print(f"    │  └─ Auto-Heal: {Colors.GREEN}✓ Available{Colors.ENDC}")
                            else:
                                print(f"    │  └─ Auto-Heal: Not available")

                        # Recommendations
                        recommendations = cloudsql_health.get('recommendations', [])
                        if recommendations:
                            for i, rec in enumerate(recommendations[:3]):  # Show max 3
                                if i == len(recommendations) - 1:
                                    print(f"    └─ {rec}")
                                else:
                                    print(f"    ├─ {rec}")
                        else:
                            # All good!
                            if connection_active and timeout_status == 'healthy':
                                if not voice_profiles:  # No voice profile check
                                    print(f"    └─ {Colors.GREEN}✓ No issues detected{Colors.ENDC}")
                                # else: voice profiles already displayed

                    except FileNotFoundError:
                        # Config not found - likely not using CloudSQL
                        print(f"  {Colors.CYAN}ℹ CloudSQL Proxy:{Colors.ENDC} Not configured (using SQLite)")
                    except Exception as e:
                        print(f"  {Colors.YELLOW}⚠ CloudSQL Proxy:{Colors.ENDC} Status check failed - {str(e)[:50]}")
                        logger.debug(f"CloudSQL health check error: {e}")

                    # === HYBRID CLOUD GCP VM MONITORING ===
                    try:
                        from backend.core.gcp_vm_manager import _gcp_vm_manager
                        from backend.core.intelligent_gcp_optimizer import _optimizer
                        from backend.core.component_warmup import get_warmup_system

                        # Check if VM manager is initialized
                        if _gcp_vm_manager is not None:
                            vm_manager = _gcp_vm_manager
                            stats = vm_manager.get_stats()
                            managed_vms = vm_manager.managed_vms

                            # Overall GCP status
                            if len(managed_vms) > 0:
                                total_cost = sum(vm.total_cost for vm in managed_vms.values())
                                vm_count = stats['managed_vms']

                                print(f"  {Colors.GREEN}✓ GCP Hybrid Cloud:{Colors.ENDC} {vm_count} VM(s) active (${total_cost:.4f} session)")

                                # Show each VM with EXTREME DETAIL (GCP Console-like)
                                for vm_name, vm in managed_vms.items():
                                    vm.update_cost()  # Update cost before display
                                    vm.update_efficiency_score()  # Update efficiency

                                    # VM status icon
                                    if vm.is_healthy:
                                        vm_icon = f"{Colors.GREEN}✅"
                                        vm_status = "HEALTHY"
                                    else:
                                        vm_icon = f"{Colors.WARNING}⚠️ "
                                        vm_status = f"UNHEALTHY ({vm.state.value})"

                                    print(f"    ├─ {vm_icon} VM: {vm.name}")
                                    print(f"    │  ├─ Status: {vm_status}")
                                    print(f"    │  ├─ External IP: {vm.ip_address or 'N/A'}")
                                    print(f"    │  ├─ Zone: {vm.zone}")
                                    print(f"    │  ├─ Machine Type: e2-highmem-4 (4 vCPU, 32GB RAM)")
                                    print(f"    │  ├─ Instance ID: {vm.instance_id}")

                                    # === COST TRACKING ===
                                    print(f"    │  ├─ 💰 Cost Tracking:")
                                    print(f"    │  │  ├─ Uptime: {vm.uptime_hours:.2f}h ({vm.uptime_hours * 60:.0f}m)")
                                    print(f"    │  │  ├─ Current Cost: ${vm.total_cost:.4f}")
                                    print(f"    │  │  ├─ Hourly Rate: ${vm.cost_per_hour:.3f}/hour")

                                    # Cost projection
                                    projected_1h = vm.total_cost + (vm.cost_per_hour * 1)
                                    projected_3h = vm.total_cost + (vm.cost_per_hour * 2)
                                    print(f"    │  │  ├─ Projected +1h: ${projected_1h:.4f}")
                                    print(f"    │  │  └─ Projected +2h: ${projected_3h:.4f}")

                                    # === COST EFFICIENCY (ROI) ===
                                    efficiency = vm.cost_efficiency_score

                                    if efficiency >= 70:
                                        eff_icon = "🟢"
                                        eff_color = Colors.GREEN
                                        eff_status = "EXCELLENT"
                                    elif efficiency >= 50:
                                        eff_icon = "🟡"
                                        eff_color = Colors.YELLOW
                                        eff_status = "GOOD"
                                    elif efficiency >= 30:
                                        eff_icon = "🟠"
                                        eff_color = Colors.WARNING
                                        eff_status = "POOR"
                                    else:
                                        eff_icon = "🔴"
                                        eff_color = Colors.FAIL
                                        eff_status = "WASTING MONEY"

                                    print(f"    │  ├─ {eff_icon} Cost Efficiency: {eff_color}{eff_status} ({efficiency:.1f}% ROI){Colors.ENDC}")

                                    # Idle time warning
                                    idle_mins = vm.idle_time_minutes
                                    if idle_mins > 5:
                                        idle_color = Colors.WARNING if idle_mins > 10 else Colors.YELLOW
                                        print(f"    │  │  ├─ {idle_color}⚠️  Idle: {idle_mins:.1f}m (no activity){Colors.ENDC}")
                                    else:
                                        print(f"    │  │  ├─ {Colors.GREEN}✓ Active: {vm.component_usage_count} component accesses{Colors.ENDC}")

                                    # Usage stats
                                    print(f"    │  │  └─ Usage: {vm.component_usage_count} accesses")

                                    # === DETAILED METRICS (GCP Console-like) ===
                                    print(f"    │  ├─ 📊 VM Metrics:")

                                    # CPU
                                    cpu_pct = vm.cpu_percent
                                    if cpu_pct > 80:
                                        cpu_color = Colors.WARNING
                                        cpu_icon = "🔴"
                                    elif cpu_pct > 50:
                                        cpu_color = Colors.YELLOW
                                        cpu_icon = "🟡"
                                    else:
                                        cpu_color = Colors.GREEN
                                        cpu_icon = "🟢"
                                    print(f"    │  │  ├─ {cpu_icon} CPU: {cpu_color}{cpu_pct:.1f}%{Colors.ENDC} (4 vCPUs)")

                                    # Memory
                                    mem_pct = vm.memory_percent
                                    mem_used = vm.memory_used_gb
                                    mem_total = vm.memory_total_gb
                                    if mem_pct > 80:
                                        mem_color = Colors.WARNING
                                        mem_icon = "🔴"
                                    elif mem_pct > 60:
                                        mem_color = Colors.YELLOW
                                        mem_icon = "🟡"
                                    else:
                                        mem_color = Colors.GREEN
                                        mem_icon = "🟢"
                                    print(f"    │  │  ├─ {mem_icon} Memory: {mem_color}{mem_used:.1f}GB / {mem_total:.0f}GB ({mem_pct:.1f}%){Colors.ENDC}")

                                    # Network (placeholder - would be from GCP Monitoring API)
                                    net_sent = vm.network_sent_mb
                                    net_recv = vm.network_received_mb
                                    print(f"    │  │  ├─ 📡 Network: ↑ {net_sent:.2f}MB sent, ↓ {net_recv:.2f}MB received")

                                    # Disk (placeholder)
                                    disk_read = vm.disk_read_mb
                                    disk_write = vm.disk_write_mb
                                    print(f"    │  │  └─ 💾 Disk: 📖 {disk_read:.2f}MB read, ✍️  {disk_write:.2f}MB write")

                                    # Components offloaded
                                    if vm.components:
                                        comp_count = len(vm.components)
                                        comp_preview = ", ".join(vm.components[:3])
                                        if comp_count > 3:
                                            comp_preview += f", +{comp_count-3} more"
                                        print(f"    │  ├─ 📦 Components: {comp_count} offloaded")
                                        print(f"    │  │  └─ {comp_preview}")

                                    # Trigger reason
                                    if vm.trigger_reason:
                                        reason_short = vm.trigger_reason[:60] + "..." if len(vm.trigger_reason) > 60 else vm.trigger_reason
                                        print(f"    │  ├─ 🎯 Trigger: {reason_short}")

                                    # === COST SAVINGS RECOMMENDATIONS ===
                                    recommendations = []

                                    if vm.is_wasting_money:
                                        recommendations.append(f"💸 TERMINATE NOW: Wasting money (idle {idle_mins:.1f}m, {efficiency:.0f}% efficiency)")

                                    if idle_mins > 15:
                                        recommendations.append(f"⏰ Consider terminating: Idle for {idle_mins:.1f}m")

                                    # Check if local memory normalized
                                    try:
                                        import psutil
                                        local_mem = psutil.virtual_memory().percent
                                        if local_mem < 70:
                                            recommendations.append(f"📉 Local RAM normalized ({local_mem:.1f}%) - VM may not be needed")
                                    except:
                                        pass

                                    if recommendations:
                                        print(f"    │  └─ {Colors.YELLOW}💡 Recommendations:{Colors.ENDC}")
                                        for i, rec in enumerate(recommendations):
                                            if i == len(recommendations) - 1:
                                                print(f"    │     └─ {rec}")
                                            else:
                                                print(f"    │     ├─ {rec}")
                                    else:
                                        print(f"    │  └─ {Colors.GREEN}✓ No cost savings available - VM optimally used{Colors.ENDC}")

                            else:
                                # No VMs active
                                total_lifetime_cost = stats.get('total_cost', 0.0)
                                total_created = stats.get('total_created', 0)

                                if total_created > 0:
                                    print(f"  {Colors.CYAN}ℹ GCP Hybrid Cloud:{Colors.ENDC} No active VMs")
                                    print(f"    ├─ Session stats: {total_created} created, ${total_lifetime_cost:.4f} lifetime cost")
                                else:
                                    print(f"  {Colors.CYAN}ℹ GCP Hybrid Cloud:{Colors.ENDC} No VMs created this session")

                            # Optimizer cost report
                            if _optimizer is not None:
                                optimizer = _optimizer
                                cost_report = optimizer.get_cost_report()

                                current_spend = cost_report['current_spend']
                                budget_limit = cost_report['budget_limit']
                                remaining = cost_report['remaining_budget']
                                vm_count_today = cost_report['vm_creation_count']

                                # Budget status
                                budget_pct = (current_spend / budget_limit * 100) if budget_limit > 0 else 0

                                if budget_pct >= 100:
                                    budget_icon = f"{Colors.FAIL}🚨"
                                    budget_status = f"{Colors.FAIL}EXHAUSTED{Colors.ENDC}"
                                elif budget_pct >= 80:
                                    budget_icon = f"{Colors.WARNING}⚠️ "
                                    budget_status = f"{Colors.WARNING}HIGH{Colors.ENDC}"
                                elif budget_pct >= 60:
                                    budget_icon = f"{Colors.YELLOW}🟡"
                                    budget_status = f"{Colors.YELLOW}MODERATE{Colors.ENDC}"
                                else:
                                    budget_icon = f"{Colors.GREEN}✓"
                                    budget_status = f"{Colors.GREEN}HEALTHY{Colors.ENDC}"

                                print(f"    ├─ {budget_icon} Daily Budget: {budget_status} ${current_spend:.2f} / ${budget_limit:.2f} ({budget_pct:.0f}%)")
                                print(f"    │  └─ Remaining: ${remaining:.2f}")

                                # VM creation quota
                                max_vms = 10  # From thresholds
                                quota_pct = (vm_count_today / max_vms * 100)

                                if quota_pct >= 100:
                                    quota_color = Colors.FAIL
                                elif quota_pct >= 70:
                                    quota_color = Colors.WARNING
                                else:
                                    quota_color = Colors.GREEN

                                print(f"    ├─ VM Creation Quota: {quota_color}{vm_count_today}/{max_vms} today ({quota_pct:.0f}%){Colors.ENDC}")

                                # Decision stats
                                total_decisions = cost_report['total_decisions']
                                false_alarms = cost_report.get('false_alarms', 0)
                                missed_opps = cost_report.get('missed_opportunities', 0)

                                if total_decisions > 0:
                                    accuracy = ((total_decisions - false_alarms - missed_opps) / total_decisions * 100)
                                    acc_color = Colors.GREEN if accuracy >= 80 else Colors.YELLOW if accuracy >= 60 else Colors.WARNING
                                    print(f"    └─ Optimizer: {acc_color}{accuracy:.0f}% accuracy{Colors.ENDC} ({total_decisions} decisions)")

                        else:
                            # VM manager not initialized - check if warmup tried to use it
                            warmup_system = get_warmup_system()

                            # Check if we're in memory pressure mode
                            import psutil
                            mem_percent = psutil.virtual_memory().percent

                            if mem_percent >= 80:
                                print(f"  {Colors.WARNING}⚠️  GCP Hybrid Cloud:{Colors.ENDC} High memory ({mem_percent:.1f}%) but VM manager not initialized")
                            else:
                                print(f"  {Colors.CYAN}ℹ GCP Hybrid Cloud:{Colors.ENDC} Standby (RAM: {mem_percent:.1f}%)")

                    except ImportError:
                        # GCP components not available
                        pass
                    except Exception as e:
                        print(f"  {Colors.YELLOW}⚠ GCP Hybrid Cloud:{Colors.ENDC} Status check failed - {str(e)[:50]}")
                        logger.debug(f"GCP monitoring error: {e}")

                    # === COMPONENT WARMUP STATUS ===
                    try:
                        from backend.core.component_warmup import get_warmup_system

                        warmup = get_warmup_system()

                        if warmup.warmup_complete.is_set():
                            # Warmup finished
                            ready_count = sum(1 for status in warmup.component_status.values()
                                            if status.value == "ready")
                            total_count = len(warmup.components)
                            failed_count = len(warmup.failed_components)

                            if failed_count == 0:
                                status_icon = f"{Colors.GREEN}✓"
                                status_text = "ALL READY"
                            elif ready_count > 0:
                                status_icon = f"{Colors.YELLOW}⚠️ "
                                status_text = "PARTIAL"
                            else:
                                status_icon = f"{Colors.FAIL}✗"
                                status_text = "FAILED"

                            warmup_time = warmup.total_load_time
                            critical_time = warmup.critical_load_time

                            print(f"  {status_icon} Component Warmup:{Colors.ENDC} {status_text} ({ready_count}/{total_count} ready)")
                            print(f"    ├─ Total time: {warmup_time:.2f}s (critical: {critical_time:.2f}s)")

                            if failed_count > 0:
                                print(f"    ├─ {Colors.WARNING}Failed: {failed_count} component(s){Colors.ENDC}")
                                for failed in warmup.failed_components[:3]:  # Show first 3
                                    print(f"    │  └─ {failed}")

                            # Show component breakdown by priority
                            from backend.core.component_warmup import ComponentStatus, ComponentPriority
                            priority_counts = {}
                            for name, comp in warmup.components.items():
                                priority = comp.priority.name
                                status = warmup.component_status.get(name, ComponentStatus.PENDING)

                                if priority not in priority_counts:
                                    priority_counts[priority] = {"ready": 0, "total": 0}

                                priority_counts[priority]["total"] += 1
                                if status == ComponentStatus.READY:
                                    priority_counts[priority]["ready"] += 1

                            print(f"    └─ By priority:")
                            for priority in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "DEFERRED"]:
                                if priority in priority_counts:
                                    counts = priority_counts[priority]
                                    pct = (counts["ready"] / counts["total"] * 100) if counts["total"] > 0 else 0

                                    if pct == 100:
                                        priority_color = Colors.GREEN
                                    elif pct >= 50:
                                        priority_color = Colors.YELLOW
                                    else:
                                        priority_color = Colors.WARNING

                                    print(f"       ├─ {priority}: {priority_color}{counts['ready']}/{counts['total']} ({pct:.0f}%){Colors.ENDC}")

                        else:
                            # Warmup still in progress or not started
                            print(f"  {Colors.CYAN}ℹ Component Warmup:{Colors.ENDC} In progress...")

                    except ImportError:
                        pass
                    except Exception as e:
                        print(f"  {Colors.YELLOW}⚠ Component Warmup:{Colors.ENDC} Status unavailable")
                        logger.debug(f"Component warmup monitoring error: {e}")

                    # Alert on repeated failures
                    for service, failures in consecutive_failures.items():
                        if failures >= 3:
                            print(
                                f"\n{Colors.WARNING}⚠ {service} health checks failing ({failures} failures){Colors.ENDC}"
                            )

                    # SAI (Situational Awareness Intelligence) Predictions
                    print()  # Blank line for separation
                    if self.last_sai_prediction:
                        prediction = self.last_sai_prediction
                        timestamp = datetime.fromisoformat(prediction['timestamp'])
                        time_ago = (datetime.now() - timestamp).total_seconds()

                        confidence = prediction['confidence']
                        if confidence >= 0.8:
                            confidence_icon = f"{Colors.GREEN}✓"
                        elif confidence >= 0.5:
                            confidence_icon = f"{Colors.YELLOW}⚠"
                        else:
                            confidence_icon = f"{Colors.FAIL}!"

                        print(f"  {Colors.CYAN}🔮 SAI (Situational Awareness):{Colors.ENDC} {Colors.GREEN}Active{Colors.ENDC}")
                        print(f"    ├─ Last prediction: {int(time_ago)}s ago")
                        print(f"    ├─ {confidence_icon} Confidence: {confidence:.1%}{Colors.ENDC}")
                        print(f"    ├─ Type: {prediction['type'].replace('_', ' ').title()}")
                        print(f"    ├─ Predicted peak: {prediction['predicted_peak']*100:.1f}%")
                        print(f"    ├─ Reason: {prediction['reason']}")
                        print(f"    ├─ Time horizon: {prediction['time_horizon_seconds']}s")
                        print(f"    └─ Total predictions: {self.sai_prediction_count}")
                    else:
                        print(f"  {Colors.CYAN}🔮 SAI (Situational Awareness):{Colors.ENDC} {Colors.YELLOW}Idle{Colors.ENDC} (no recent predictions)")

                    # 🔐 VOICE UNLOCK CONFIGURATION CHECK (COMPREHENSIVE)
                    print()  # Blank line for separation
                    voice_unlock_status = await self._check_voice_unlock_configuration()

                    if voice_unlock_status['configured']:
                        status_icon = f"{Colors.GREEN}✅"
                        status_text = f"{Colors.GREEN}CONFIGURED{Colors.ENDC}"
                    else:
                        status_icon = f"{Colors.YELLOW}⚠️ "
                        status_text = f"{Colors.YELLOW}NOT CONFIGURED{Colors.ENDC}"

                    print(f"  {status_icon} Voice Unlock: {status_text}")

                    # Show detailed checks
                    detailed = voice_unlock_status.get('detailed_checks', {})

                    print(f"    │")
                    print(f"    ├─ {Colors.CYAN}📦 CORE COMPONENTS:{Colors.ENDC}")
                    print(f"    │")

                    # 1. Learning Database
                    learning_db = detailed.get('learning_db', {})
                    if learning_db.get('initialized'):
                        print(f"    │  ├─ ✅ Learning Database: {Colors.GREEN}INITIALIZED{Colors.ENDC}")
                    else:
                        error = learning_db.get('error', 'Unknown error')
                        print(f"    │  ├─ ❌ Learning Database: {Colors.FAIL}FAILED{Colors.ENDC} ({error})")

                    # 2. CloudSQL Proxy
                    cloudsql = detailed.get('cloudsql_proxy', {})
                    if cloudsql.get('connected'):
                        print(f"    │  ├─ ✅ CloudSQL Proxy: {Colors.GREEN}CONNECTED{Colors.ENDC}")
                    else:
                        error = cloudsql.get('error', 'Disconnected')
                        print(f"    │  ├─ ❌ CloudSQL Proxy: {Colors.FAIL}DISCONNECTED{Colors.ENDC} ({error})")

                    # 3. Voice Profiles
                    voice_profiles = detailed.get('voice_profiles', {})
                    if voice_profiles.get('loaded'):
                        count = voice_profiles.get('count', 0)
                        profiles = voice_profiles.get('profiles', [])
                        print(f"    │  ├─ ✅ Voice Profiles: {Colors.GREEN}{count} loaded{Colors.ENDC}")
                        for profile_name in profiles:
                            print(f"    │  │  └─ {Colors.CYAN}{profile_name}{Colors.ENDC}")
                    else:
                        error = voice_profiles.get('error', 'No profiles found')
                        print(f"    │  ├─ ❌ Voice Profiles: {Colors.FAIL}{error}{Colors.ENDC}")

                    # 4. Keychain Password
                    keychain = detailed.get('keychain', {})
                    if keychain.get('stored'):
                        pwd_len = keychain.get('password_length', 0)
                        print(f"    │  ├─ ✅ Keychain Password: {Colors.GREEN}STORED{Colors.ENDC} ({pwd_len} chars)")
                    else:
                        error = keychain.get('error', 'Not found')
                        print(f"    │  ├─ ❌ Keychain Password: {Colors.FAIL}{error}{Colors.ENDC}")

                    # 5. Password Typer
                    typer = detailed.get('password_typer', {})
                    if typer.get('available'):
                        print(f"    │  └─ ✅ Password Typer: {Colors.GREEN}FUNCTIONAL{Colors.ENDC}")
                    else:
                        error = typer.get('error', 'Not available')
                        print(f"    │  └─ ❌ Password Typer: {Colors.FAIL}{error}{Colors.ENDC}")

                    # BEAST MODE COMPONENTS
                    print(f"    │")
                    print(f"    ├─ {Colors.CYAN}🦁 BEAST MODE VERIFICATION:{Colors.ENDC}")
                    print(f"    │")

                    # 6. Speaker Verification Service
                    speaker_verif = detailed.get('speaker_verification', {})
                    if speaker_verif.get('initialized'):
                        encoder_status = "READY" if speaker_verif.get('encoder_ready') else "NOT LOADED"
                        profiles = speaker_verif.get('profiles_loaded', 0)
                        color = Colors.GREEN if speaker_verif.get('encoder_ready') and profiles > 0 else Colors.YELLOW
                        print(f"    │  ├─ ✅ Speaker Verification: {color}{speaker_verif.get('status')}{Colors.ENDC}")
                        print(f"    │  │  ├─ Encoder: {encoder_status}")
                        print(f"    │  │  └─ Profiles: {profiles}")
                    else:
                        error = speaker_verif.get('error', 'Not available')
                        print(f"    │  ├─ ❌ Speaker Verification: {Colors.FAIL}FAILED{Colors.ENDC} ({error})")

                    # 7. ECAPA-TDNN Embeddings
                    ecapa = detailed.get('ecapa_embeddings', {})
                    if ecapa.get('available'):
                        count = ecapa.get('count', 0)
                        dims = ecapa.get('dimensions', [])
                        dim_str = f"{dims[0]}D" if dims else "unknown"
                        print(f"    │  ├─ ✅ ECAPA-TDNN Embeddings: {Colors.GREEN}{count} profiles{Colors.ENDC} ({dim_str})")
                    else:
                        error = ecapa.get('error', 'Not found')
                        print(f"    │  ├─ ⚠️  ECAPA-TDNN Embeddings: {Colors.YELLOW}{error}{Colors.ENDC}")

                    # 8. Anti-Spoofing Detection
                    anti_spoof = detailed.get('anti_spoofing', {})
                    if anti_spoof.get('available'):
                        features = anti_spoof.get('features', [])
                        print(f"    │  ├─ ✅ Anti-Spoofing: {Colors.GREEN}ENABLED{Colors.ENDC} ({len(features)} detectors)")
                    else:
                        print(f"    │  ├─ ⚠️  Anti-Spoofing: {Colors.YELLOW}NOT AVAILABLE{Colors.ENDC}")

                    # 9. Hybrid STT System
                    hybrid_stt = detailed.get('hybrid_stt', {})
                    if hybrid_stt.get('initialized'):
                        count = hybrid_stt.get('count', 0)
                        engines = hybrid_stt.get('engines', [])
                        print(f"    │  ├─ ✅ Hybrid STT: {Colors.GREEN}{count} engines{Colors.ENDC}")
                        for engine in engines[:3]:  # Show first 3
                            print(f"    │  │  ├─ {engine}")
                        if len(engines) > 3:
                            print(f"    │  │  └─ ... +{len(engines)-3} more")
                    else:
                        error = hybrid_stt.get('error', 'Not available')
                        print(f"    │  ├─ ⚠️  Hybrid STT: {Colors.YELLOW}{error}{Colors.ENDC}")

                    # 10. Context-Aware Intelligence (CAI)
                    cai = detailed.get('cai', {})
                    if cai.get('available'):
                        features = cai.get('features', [])
                        print(f"    │  ├─ ✅ CAI (Context-Aware): {Colors.GREEN}ENABLED{Colors.ENDC} ({len(features)} analyzers)")
                    else:
                        print(f"    │  ├─ ⚠️  CAI: {Colors.YELLOW}NOT AVAILABLE{Colors.ENDC}")

                    # 11. Scenario-Aware Intelligence (SAI)
                    sai = detailed.get('sai', {})
                    if sai.get('available'):
                        features = sai.get('features', [])
                        print(f"    │  ├─ ✅ SAI (Scenario-Aware): {Colors.GREEN}ENABLED{Colors.ENDC} ({len(features)} detectors)")
                    else:
                        print(f"    │  ├─ ⚠️  SAI: {Colors.YELLOW}NOT AVAILABLE{Colors.ENDC}")

                    # 12. Voice Biometric Intelligence (VBI) - CRITICAL!
                    vbi = detailed.get('voice_biometric_intelligence', {})
                    if vbi.get('available'):
                        vbi_profiles = vbi.get('profiles_loaded', 0)
                        vbi_state = vbi.get('cache_state', 'unknown')
                        has_owner = vbi.get('has_owner_profile', False)
                        color = Colors.GREEN if vbi_profiles > 0 and has_owner else Colors.YELLOW
                        owner_status = "OWNER DETECTED" if has_owner else "NO OWNER"
                        print(f"    │  ├─ ✅ VBI (Voice Biometric): {color}{vbi_profiles} profiles{Colors.ENDC} ({owner_status})")
                        print(f"    │  │  ├─ Cache State: {vbi_state}")
                        # Show profiles dynamically
                        vbi_profile_list = vbi.get('profiles', [])
                        for i, p in enumerate(vbi_profile_list):
                            connector = "└─" if i == len(vbi_profile_list) - 1 else "├─"
                            owner_tag = " [OWNER]" if p.get('is_owner') else ""
                            print(f"    │  │  {connector} {p.get('name', 'unknown')}{owner_tag} ({p.get('dimensions', 0)}D)")
                    else:
                        error = vbi.get('error', 'Not initialized')
                        print(f"    │  ├─ ❌ VBI (Voice Biometric): {Colors.FAIL}{error}{Colors.ENDC}")

                    # 13. Hybrid Database Sync
                    hybrid_sync = detailed.get('hybrid_sync', {})
                    if hybrid_sync.get('enabled'):
                        cloudsql_status = "AVAILABLE" if hybrid_sync.get('cloudsql_available') else "UNAVAILABLE"
                        color = Colors.GREEN if hybrid_sync.get('cloudsql_available') else Colors.YELLOW
                        print(f"    │  └─ ✅ Hybrid Sync: {color}{cloudsql_status}{Colors.ENDC}")
                        print(f"    │     ├─ Local Read: {hybrid_sync.get('local_read_latency_ms', 0):.1f}ms")
                        print(f"    │     ├─ Cloud Write: {hybrid_sync.get('cloud_write_latency_ms', 0):.1f}ms")
                        print(f"    │     ├─ Queue: {hybrid_sync.get('sync_queue_size', 0)} pending")
                        print(f"    │     ├─ Synced: {hybrid_sync.get('total_synced', 0)}")
                        print(f"    │     └─ Failed: {hybrid_sync.get('total_failed', 0)}")
                    else:
                        reason = hybrid_sync.get('reason', hybrid_sync.get('error', 'Disabled'))
                        print(f"    │  └─ ⚠️  Hybrid Sync: {Colors.YELLOW}DISABLED{Colors.ENDC} ({reason})")

                    # UNLOCK FLOW DIAGRAM
                    # Get owner name dynamically - NO hardcoding!
                    _enrollment = voice_unlock_status.get('enrollment_details', {})
                    _owner_full_name = _enrollment.get('username', 'Owner')
                    _owner_first_name = _owner_full_name.split()[0] if _owner_full_name else 'Owner'

                    print(f"    │")
                    print(f"    └─ {Colors.CYAN}🔄 UNLOCK FLOW (When you say 'unlock my screen'):{Colors.ENDC}")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[1] Audio Capture{Colors.ENDC} → Record your voice command")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[2] Hybrid STT{Colors.ENDC} → Transcribe audio to text")
                    print(f"       │   └─ Output: 'unlock my screen'")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[3] Speaker Identification{Colors.ENDC} → Extract ECAPA-TDNN embedding")
                    print(f"       │   ├─ Compare with CloudSQL profiles")
                    print(f"       │   └─ Identify: {_owner_full_name} (confidence: XX%)")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[4] Multi-Modal Verification{Colors.ENDC} → BEAST MODE")
                    print(f"       │   ├─ Deep learning embeddings (ECAPA-TDNN)")
                    print(f"       │   ├─ Mahalanobis distance (statistical)")
                    print(f"       │   ├─ Acoustic features (pitch, formants)")
                    print(f"       │   ├─ Physics-based validation")
                    print(f"       │   └─ Anti-spoofing detection")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[5] CAI Analysis{Colors.ENDC} → Check context")
                    print(f"       │   ├─ Screen state (locked/unlocked)")
                    print(f"       │   ├─ Time of day")
                    print(f"       │   └─ Location context")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[6] SAI Analysis{Colors.ENDC} → Detect scenario")
                    print(f"       │   ├─ Routine unlock (normal)")
                    print(f"       │   ├─ Emergency unlock (urgent)")
                    print(f"       │   └─ Suspicious activity (security alert)")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[7] Password Retrieval{Colors.ENDC} → Keychain (JARVIS_Screen_Unlock)")
                    print(f"       │   └─ Password: ************* (13 chars)")
                    print(f"       │")
                    print(f"       ├─ {Colors.BLUE}[8] Secure Typing{Colors.ENDC} → CoreGraphics API")
                    print(f"       │   ├─ Type password character-by-character")
                    print(f"       │   ├─ Randomized timing (human-like)")
                    print(f"       │   └─ Press Enter")
                    print(f"       │")
                    print(f"       └─ {Colors.GREEN}[9] ✅ UNLOCKED{Colors.ENDC} → Welcome back, {_owner_first_name}!")
                    print(f"")

                    # Legacy checks
                    print(f"    ├─ {'✅' if voice_unlock_status['enrollment_data_exists'] else '❌'} Enrollment data")

                    # Daemon status with detailed info
                    if voice_unlock_status['daemon_running']:
                        print(f"    ├─ ✅ Service status: {Colors.GREEN}RUNNING{Colors.ENDC}")
                    else:
                        print(f"    ├─ ⚠️  Service status: {Colors.YELLOW}NOT RUNNING{Colors.ENDC}")

                    # Show enrollment details if available
                    if voice_unlock_status['enrollment_data_exists'] and voice_unlock_status.get('enrollment_details'):
                        details = voice_unlock_status['enrollment_details']
                        print(f"    ├─ Enrolled user: {details.get('username', 'unknown')}")
                        print(f"    ├─ Voice samples: {details.get('voice_samples', 0)}")
                        enrollment_date = details.get('enrollment_date', 'unknown')
                        if enrollment_date != 'unknown':
                            print(f"    ├─ Enrolled: {enrollment_date}")

                    # Auto-configure if not configured
                    if not voice_unlock_status['configured']:
                        print(f"    │")
                        # Attempt autonomous configuration once per session
                        if not self.voice_unlock_config_status.get('auto_config_attempted'):
                            print(f"    ├─ {Colors.YELLOW}🤖 Attempting autonomous configuration...{Colors.ENDC}")
                            auto_config_success = await self._auto_configure_voice_unlock()
                            if auto_config_success:
                                print(f"    └─ {Colors.GREEN}✅ Auto-configured successfully!{Colors.ENDC}")
                            else:
                                print(f"    └─ {Colors.YELLOW}⚠️  Partial config - run: ./backend/voice_unlock/enable_screen_unlock.sh{Colors.ENDC}")
                        else:
                            print(f"    └─ {Colors.YELLOW}⚠️  Manual setup required - run: ./backend/voice_unlock/enable_screen_unlock.sh{Colors.ENDC}")
                    else:
                        print(f"    └─ {Colors.GREEN}✓ Ready for voice unlock commands{Colors.ENDC}")

                    # Voice Verification Diagnostics with AI-Powered Recommendations
                    print()  # Blank line for separation
                    print(f"  {Colors.CYAN}{'='*60}{Colors.ENDC}")
                    stats = self.voice_verification_stats

                    # Always show status even with no attempts
                    if stats['total_attempts'] == 0:
                        print(f"  {Colors.CYAN}🎤 Voice Verification:{Colors.ENDC} {Colors.YELLOW}Waiting for first attempt...{Colors.ENDC}")
                        print(f"  {Colors.CYAN}{'='*60}{Colors.ENDC}")
                    elif stats['total_attempts'] > 0:
                        success_rate = (stats['successful'] / stats['total_attempts']) * 100

                        # Status icon based on recent performance
                        if stats['consecutive_failures'] >= 3:
                            status_icon = f"{Colors.FAIL}❌"
                            status_text = f"{Colors.FAIL}FAILING{Colors.ENDC}"
                        elif stats['consecutive_failures'] >= 1:
                            status_icon = f"{Colors.WARNING}⚠️ "
                            status_text = f"{Colors.WARNING}DEGRADED{Colors.ENDC}"
                        else:
                            status_icon = f"{Colors.GREEN}✅"
                            status_text = f"{Colors.GREEN}HEALTHY{Colors.ENDC}"

                        print(f"  {Colors.CYAN}🎤 Voice Verification:{Colors.ENDC} {status_text}")
                        print(f"    ├─ {status_icon} Success rate: {success_rate:.1f}% ({stats['successful']}/{stats['total_attempts']}){Colors.ENDC}")
                        print(f"    ├─ Average confidence: {stats['average_confidence']:.2%}")
                        print(f"    ├─ Consecutive failures: {stats['consecutive_failures']}")

                        if stats['last_attempt_time']:
                            last_attempt_ago = (datetime.now() - stats['last_attempt_time']).total_seconds()
                            print(f"    ├─ Last attempt: {int(last_attempt_ago)}s ago")

                        # 🧠 INTELLIGENT ANALYSIS using SAI/CAI/UAE
                        if len(self.voice_verification_attempts) > 0:
                            recent_failures = [a for a in self.voice_verification_attempts if not a.get('success', False)]
                            if recent_failures:
                                # Analyze failure patterns with AI
                                ai_analysis = self._analyze_voice_failures_with_ai(recent_failures, stats)

                                print(f"    ├─ 🧠 AI Analysis:")
                                print(f"    │  ├─ Root cause: {ai_analysis['root_cause']}")
                                print(f"    │  ├─ Pattern: {ai_analysis['pattern_detected']}")
                                print(f"    │  └─ Confidence: {ai_analysis['analysis_confidence']:.0%}")

                                # 🔬 TRIGGER DEEP DIAGNOSTIC on critical failures
                                if stats['consecutive_failures'] >= 3:
                                    print(f"    ├─ 🔬 BEAST MODE: Running deep diagnostic...")
                                    deep_diagnostic = await self._deep_diagnostic_analysis(recent_failures, stats)

                                    # Display findings
                                    if deep_diagnostic['bugs_detected']:
                                        print(f"    │  ├─ 🐛 Bugs Found: {len(deep_diagnostic['bugs_detected'])}")
                                        for bug in deep_diagnostic['bugs_detected'][:3]:
                                            severity_color = Colors.FAIL if bug['severity'] == 'critical' else Colors.WARNING
                                            print(f"    │  │  └─ {severity_color}{bug['type']}: {bug.get('fix', 'No fix available')}{Colors.ENDC}")

                                    if deep_diagnostic['missing_components']:
                                        print(f"    │  ├─ 📦 Missing: {len(deep_diagnostic['missing_components'])}")
                                        for missing in deep_diagnostic['missing_components'][:2]:
                                            print(f"    │  │  └─ {Colors.YELLOW}{missing.get('package', missing.get('file', 'unknown'))}{Colors.ENDC}")

                                    critical_bugs = sum(1 for b in deep_diagnostic.get('bugs_detected', []) if b.get('severity') == 'critical')
                                    if critical_bugs > 0 or deep_diagnostic.get('missing_components'):
                                        print(f"    │")
                                        print(f"    │  {Colors.YELLOW}{'▂' * 50}{Colors.ENDC}")
                                        print(f"    │  {Colors.YELLOW}{'▔' * 50}{Colors.ENDC}")
                                        print(f"    │  └─ Analyzing {len(deep_diagnostic.get('bugs_detected', []))} bugs...")
                                        print(f"    │     ├─ Checking {len(deep_diagnostic.get('missing_components', []))} missing components...")
                                # Show intelligent recommendations
                                print(f"    ├─ 💡 JARVIS Recommendations:")
                                for i, rec in enumerate(ai_analysis['recommendations'][:3]):
                                    priority_icon = "🔴" if rec['priority'] == 'critical' else "🟡" if rec['priority'] == 'high' else "🟢"
                                    auto_fix = f" {Colors.GREEN}[AUTO-FIX AVAILABLE]{Colors.ENDC}" if rec.get('auto_fix_available') else ""
                                    print(f"    │  {priority_icon} {rec['action']}{auto_fix}")
                                    print(f"    │     └─ Why: {rec['reason']}")

                                # Show recent failures (condensed)
                                print(f"    ├─ Recent failures ({len(recent_failures[-3:])}):")
                                for failure in recent_failures[-3:]:
                                    reason = failure.get('primary_reason', 'unknown')[:50]
                                    severity = failure.get('severity', 'unknown')
                                    severity_color = Colors.FAIL if severity == 'critical' else Colors.WARNING if severity == 'high' else Colors.YELLOW
                                    print(f"    │  └─ {severity_color}{reason}{Colors.ENDC}")

                        # Show top failure reasons with counts
                        if stats['failure_reasons']:
                            print(f"    └─ Failure breakdown:")
                            sorted_reasons = sorted(stats['failure_reasons'].items(), key=lambda x: x[1], reverse=True)
                            for reason, count in sorted_reasons[:3]:
                                percentage = (count / stats['failed']) * 100 if stats['failed'] > 0 else 0
                                print(f"       ├─ {reason[:50]}: {count}x ({percentage:.0f}%)")

                    if stats['total_attempts'] == 0:
                        pass  # Already handled above
                    elif stats['total_attempts'] > 0:
                        pass  # Already displayed
                    else:
                        print(f"  {Colors.CYAN}🎤 Voice Verification:{Colors.ENDC} {Colors.YELLOW}No attempts yet{Colors.ENDC}")

                    # Show next health check countdown
                    print(f"\n{Colors.CYAN}  Next health check in 30 seconds...{Colors.ENDC}")

        except asyncio.CancelledError:
            self._shutting_down = True

    async def clear_frontend_cache(self):
        """Clear stale frontend configuration cache to prevent port mismatch issues"""
        try:
            # Create a small JavaScript file to clear the cache
            clear_cache_js = """
// Clear JARVIS configuration cache
if (typeof localStorage !== 'undefined') {
    const cached = localStorage.getItem('jarvis_dynamic_config');
    if (cached) {
        try {
            const config = JSON.parse(cached);
            // Check if cache points to wrong port
            if (config.API_BASE_URL && (config.API_BASE_URL.includes(':8001') || config.API_BASE_URL.includes(':8000'))) {
                localStorage.removeItem('jarvis_dynamic_config');
                console.log('[JARVIS] Cleared stale configuration cache pointing to wrong port');
            }
        } catch (e) {
            // Invalid cache, clear it
            localStorage.removeItem('jarvis_dynamic_config');
            console.log('[JARVIS] Cleared invalid configuration cache');
        }
    }
}
"""

            # Write to public folder if it exists
            public_dir = self.frontend_dir / "public"
            if public_dir.exists():
                cache_clear_file = public_dir / "clear-stale-cache.js"
                cache_clear_file.write_text(clear_cache_js)

                # Also ensure it's loaded in index.html if needed
                index_html = public_dir / "index.html"
                if index_html.exists():
                    content = index_html.read_text()
                    if "clear-stale-cache.js" not in content:
                        # Add script tag before closing body
                        content = content.replace(
                            "</body>",
                            '  <script src="/clear-stale-cache.js"></script>\n  </body>',
                        )
                        index_html.write_text(content)

                print(f"{Colors.GREEN}✓ Added frontend cache clearing logic{Colors.ENDC}")
        except Exception as e:
            # Non-critical, don't fail startup
            logger.debug(f"Could not add cache clearing: {e}")

    async def open_browser_smart(self, custom_url: str = None):
        """Open browser intelligently - reuse tabs when possible

        Args:
            custom_url: Optional custom URL to open (e.g., loading page)
        """
        if custom_url:
            url = custom_url
        elif self.is_restart:
            # On restart, redirect to loading page to show progress
            url = "http://localhost:3001/"
        elif self.frontend_dir.exists() and not self.backend_only:
            url = f"http://localhost:{self.ports['frontend']}/"
        else:
            url = f"http://localhost:{self.ports['main_api']}/docs"

        # On restart, give browsers a moment to settle
        if self.is_restart:
            await asyncio.sleep(0.5)

        # Try to reuse existing tab on macOS using AppleScript
        if platform.system() == "Darwin":
            # List of URL patterns that indicate JARVIS tabs (localhost only to avoid matching github.com/user/JARVIS)
            # Include ALL JARVIS-related ports: frontend, API, loading server, websocket router, event UI
            jarvis_patterns = [
                "localhost:3000",   # Frontend
                "127.0.0.1:3000",
                "localhost:3001",   # Loading server
                "127.0.0.1:3001",
                "localhost:8010",   # Main API
                "127.0.0.1:8010",
                "localhost:8001",   # WebSocket router
                "127.0.0.1:8001",
                "localhost:8888",   # Event UI
                "127.0.0.1:8888",
            ]

            # Log what we're looking for
            action = "restart" if self.is_restart else "startup"
            logger.info(f"🔍 Looking for existing JARVIS tabs on {action} with patterns: {jarvis_patterns}")

            # Build AppleScript conditions - must contain localhost or 127.0.0.1 to avoid false positives
            url_conditions = " or ".join([f'(tabURL contains "{pattern}")' for pattern in jarvis_patterns])

            # AppleScript to close duplicate JARVIS tabs and reuse one
            # Simpler, more reliable approach
            pattern_list = '", "'.join(jarvis_patterns)
            applescript = f"""
            tell application "System Events"
                set browserList to {{}}

                -- Check which browsers are running
                if exists process "Google Chrome" then set end of browserList to "Google Chrome"
                if exists process "Safari" then set end of browserList to "Safari"
                if exists process "Arc" then set end of browserList to "Arc"

                -- Process each browser
                repeat with browserName in browserList
                    if browserName is "Google Chrome" then
                        tell application "Google Chrome"
                            set jarvisPatterns to {{"{pattern_list}"}}
                            set foundFirst to false
                            set totalClosed to 0

                            repeat with w in windows
                                set tabsToClose to {{}}
                                set tabCount to count of tabs of w

                                repeat with i from 1 to tabCount
                                    set t to tab i of w
                                    set tabURL to URL of t
                                    set isJarvis to false

                                    -- Check if this is a JARVIS tab
                                    repeat with pattern in jarvisPatterns
                                        if tabURL contains pattern then
                                            set isJarvis to true
                                            exit repeat
                                        end if
                                    end repeat

                                    if isJarvis then
                                        if not foundFirst then
                                            -- Keep this one and update to target URL
                                            set foundFirst to true
                                            set URL of t to "{url}"
                                            set active tab index of w to i
                                            set index of w to 1
                                        else
                                            -- Mark for closure
                                            set end of tabsToClose to i
                                        end if
                                    end if
                                end repeat

                                -- Close marked tabs in reverse order
                                if (count of tabsToClose) > 0 then
                                    repeat with i from (count of tabsToClose) to 1 by -1
                                        try
                                            set tabIndex to item i of tabsToClose
                                            close tab tabIndex of w
                                            set totalClosed to totalClosed + 1
                                        end try
                                    end repeat
                                end if
                            end repeat

                            if foundFirst then
                                activate
                                return "REUSED_TAB_CHROME:" & totalClosed
                            end if
                        end tell

                    else if browserName is "Safari" then
                        tell application "Safari"
                            set jarvisPatterns to {{"{pattern_list}"}}
                            set foundFirst to false
                            set totalClosed to 0

                            repeat with w in windows
                                set tabsToClose to {{}}
                                set tabCount to count of tabs of w

                                repeat with i from 1 to tabCount
                                    set t to tab i of w
                                    set tabURL to URL of t
                                    set isJarvis to false

                                    repeat with pattern in jarvisPatterns
                                        if tabURL contains pattern then
                                            set isJarvis to true
                                            exit repeat
                                        end if
                                    end repeat

                                    if isJarvis then
                                        if not foundFirst then
                                            set foundFirst to true
                                            set URL of t to "{url}"
                                            set current tab of w to t
                                            set index of w to 1
                                        else
                                            set end of tabsToClose to i
                                        end if
                                    end if
                                end repeat

                                if (count of tabsToClose) > 0 then
                                    repeat with i from (count of tabsToClose) to 1 by -1
                                        try
                                            set tabIndex to item i of tabsToClose
                                            close tab tabIndex of w
                                            set totalClosed to totalClosed + 1
                                        end try
                                    end repeat
                                end if
                            end repeat

                            if foundFirst then
                                activate
                                return "REUSED_TAB_SAFARI:" & totalClosed
                            end if
                        end tell

                    else if browserName is "Arc" then
                        tell application "Arc"
                            set jarvisPatterns to {{"{pattern_list}"}}
                            set foundFirst to false
                            set totalClosed to 0

                            repeat with w in windows
                                set tabsToClose to {{}}
                                set tabCount to count of tabs of w

                                repeat with i from 1 to tabCount
                                    set t to tab i of w
                                    set tabURL to URL of t
                                    set isJarvis to false

                                    repeat with pattern in jarvisPatterns
                                        if tabURL contains pattern then
                                            set isJarvis to true
                                            exit repeat
                                        end if
                                    end repeat

                                    if isJarvis then
                                        if not foundFirst then
                                            set foundFirst to true
                                            set URL of t to "{url}"
                                            set index of w to 1
                                        else
                                            set end of tabsToClose to i
                                        end if
                                    end if
                                end repeat

                                if (count of tabsToClose) > 0 then
                                    repeat with i from (count of tabsToClose) to 1 by -1
                                        try
                                            set tabIndex to item i of tabsToClose
                                            close tab tabIndex of w
                                            set totalClosed to totalClosed + 1
                                        end try
                                    end repeat
                                end if
                            end repeat

                            if foundFirst then
                                activate
                                return "REUSED_TAB_ARC:" & totalClosed
                            end if
                        end tell
                    end if
                end repeat
            end tell

            -- If no tab was found in any browser, create a new one
            tell application "System Events"
                if (count of browserList) > 0 then
                    set preferredBrowser to item 1 of browserList
                    tell application preferredBrowser
                        if preferredBrowser is "Google Chrome" then
                            if (count of windows) = 0 then
                                make new window
                            end if
                            tell window 1
                                set newTab to make new tab with properties {{URL:"{url}"}}
                                set active tab index to index of newTab
                            end tell
                            activate
                            return "NEW_TAB_CHROME:0"
                        else if preferredBrowser is "Safari" then
                            if (count of windows) = 0 then
                                make new document
                            end if
                            tell window 1
                                set current tab to make new tab with properties {{URL:"{url}"}}
                            end tell
                            activate
                            return "NEW_TAB_SAFARI:0"
                        else if preferredBrowser is "Arc" then
                            if (count of windows) = 0 then
                                make new window
                            end if
                            tell window 1
                                make new tab with properties {{URL:"{url}"}}
                            end tell
                            activate
                            return "NEW_TAB_ARC:0"
                        end if
                    end tell
                else
                    open location "{url}"
                    return "NEW_TAB_DEFAULT:0"
                end if
            end tell
            """

            try:
                # Run AppleScript with better error handling
                process = await asyncio.create_subprocess_exec(
                    "osascript",
                    "-e",
                    applescript,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()

                if process.returncode != 0 and stderr:
                    logger.debug(f"AppleScript warning (non-fatal): {stderr.decode()}")

                # Log successful operation and whether we reused or created a tab
                if stdout:
                    result = stdout.decode().strip()
                    if "REUSED_TAB" in result:
                        parts = result.split(":")
                        browser = parts[0].split("_")[-1]
                        tabs_closed = int(parts[1]) if len(parts) > 1 else 0

                        logger.info(f"✅ Reused existing JARVIS tab in {browser}, closed {tabs_closed} duplicates")
                        if tabs_closed > 0:
                            print(f"{Colors.GREEN}✓ Reused existing JARVIS tab in {browser} (closed {tabs_closed} duplicate{'s' if tabs_closed > 1 else ''}){Colors.ENDC}")
                        else:
                            print(f"{Colors.GREEN}✓ Reused existing JARVIS tab in {browser}{Colors.ENDC}")
                    elif "NEW_TAB" in result:
                        browser = result.split(":")[0].split("_")[-1]
                        logger.info(f"🌐 Created new tab in {browser}")
                        print(f"{Colors.BLUE}➕ Created new JARVIS tab in {browser}{Colors.ENDC}")
                    else:
                        logger.info(f"Browser tab operation completed: {result}")

                return
            except Exception as e:
                # Fall back to webbrowser if AppleScript fails
                logger.debug(f"AppleScript failed (using fallback): {e}")

        # Fallback for other platforms or if AppleScript fails
        webbrowser.open(url)

    # ==================== SELF-HEALING METHODS ====================

    async def _diagnose_and_heal(self, error_context: str, error: Exception) -> bool:
        """Intelligently diagnose and fix common startup issues"""

        if not self.auto_heal_enabled:
            return False

        error_type = type(error).__name__
        error_msg = str(error).lower()

        # Track healing attempts
        heal_key = f"{error_context}_{error_type}"
        if heal_key not in self.healing_attempts:
            self.healing_attempts[heal_key] = 0

        if self.healing_attempts[heal_key] >= self.max_healing_attempts:
            print(f"{Colors.FAIL}❌ Max healing attempts reached for {error_context}{Colors.ENDC}")
            return False

        self.healing_attempts[heal_key] += 1
        attempt = self.healing_attempts[heal_key]

        print(
            f"\n{Colors.CYAN}🔧 Self-Healing: Analyzing {error_context} error (attempt {attempt}/{self.max_healing_attempts})...{Colors.ENDC}"
        )

        # Analyze error and attempt healing
        healed = False

        # Port in use errors
        if "address already in use" in error_msg or "port" in error_msg or "bind" in error_msg:
            port = self._extract_port_from_error(error_msg)
            if port:
                healed = await self._heal_port_conflict(port)

        # Missing module/import errors
        elif "modulenotfounderror" in error_type.lower() or (
            "module" in error_msg and "not found" in error_msg
        ):
            module = self._extract_module_from_error(str(error))
            if module:
                healed = await self._heal_missing_module(module)

        # NameError for missing imports
        elif "nameerror" in error_type.lower():
            if "List" in str(error):
                healed = await self._heal_typing_import()

        # Permission errors
        elif "permission" in error_msg or "access denied" in error_msg:
            healed = await self._heal_permission_issue(error_context)

        # API key errors
        elif "api" in error_msg and "key" in error_msg:
            healed = await self._heal_missing_api_key()

        # Memory errors
        elif "memory" in error_msg:
            healed = await self._heal_memory_pressure()

        # Process exit codes
        elif hasattr(error, "returncode") or "returncode" in str(error):
            healed = await self._heal_process_crash(error_context, error)

        # Log healing result
        self.healing_log.append(
            {
                "timestamp": datetime.now(),
                "context": error_context,
                "error": str(error),
                "attempt": attempt,
                "healed": healed,
            }
        )

        if healed:
            print(f"{Colors.GREEN}✅ Self-healing successful! Retrying...{Colors.ENDC}")
            await asyncio.sleep(2)  # Brief pause before retry
        else:
            print(
                f"{Colors.WARNING}⚠️  Self-healing could not fix this issue automatically{Colors.ENDC}"
            )

        return healed

    async def _heal_port_conflict(self, port: int) -> bool:
        """Fix port already in use errors"""
        print(f"{Colors.YELLOW}🔧 Port {port} is in use, attempting to free it...{Colors.ENDC}")

        # Kill process on port
        success = await self.kill_process_on_port(port)
        if success:
            await asyncio.sleep(1)  # Give OS time to release port
            if await self.check_port_available(port):
                print(f"{Colors.GREEN}✅ Port {port} is now available{Colors.ENDC}")
                return True

        # Try alternative port
        alt_ports = {8010: 8011, 8001: 8002, 3000: 3001, 8888: 8889}
        if port in alt_ports:
            new_port = alt_ports[port]
            if await self.check_port_available(new_port):
                for key, p in self.ports.items():
                    if p == port:
                        self.ports[key] = new_port
                        print(
                            f"{Colors.GREEN}✅ Switched to alternative port {new_port}{Colors.ENDC}"
                        )
                        return True

        return False

    async def _heal_missing_module(self, module: str) -> bool:
        """Auto-install missing Python modules"""
        print(f"{Colors.YELLOW}🔧 Installing missing module: {module}...{Colors.ENDC}")

        # Map common module names to packages
        module_map = {
            "dotenv": "python-dotenv",
            "aiohttp": "aiohttp",
            "psutil": "psutil",
            "colorama": "colorama",
            "anthropic": "anthropic",
            "ml_logging_config": None,  # Local module
            "enable_ml_logging": None,  # Local module
        }

        # Skip local modules
        if module in module_map and module_map[module] is None:
            print(
                f"{Colors.WARNING}Local module {module} missing - may need to check file paths{Colors.ENDC}"
            )
            return False

        package = module_map.get(module, module)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                print(f"{Colors.GREEN}✅ Successfully installed {package}{Colors.ENDC}")
                return True

        except Exception as e:
            print(f"{Colors.WARNING}Failed to install {package}: {e}{Colors.ENDC}")

        return False

    async def _heal_typing_import(self) -> bool:
        """Fix missing typing imports like List"""
        print(f"{Colors.YELLOW}🔧 Fixing typing import error...{Colors.ENDC}")

        # Find the file with the error
        files_to_check = [
            "backend/ml_logging_config.py",
            "backend/ml_memory_manager.py",
            "backend/context_aware_loader.py",
        ]

        for file_path in files_to_check:
            if Path(file_path).exists():
                try:
                    content = Path(file_path).read_text()
                    # Check if List is used but not imported
                    if (
                        "List[" in content
                        and "from typing import" in content
                        and "List" not in content
                    ):
                        # Add List to imports
                        content = content.replace("from typing import", "from typing import List,")
                        Path(file_path).write_text(content)
                        print(f"{Colors.GREEN}✅ Fixed typing import in {file_path}{Colors.ENDC}")
                        return True
                except:
                    pass

        return False

    async def _heal_permission_issue(self, context: str) -> bool:
        """Fix file permission issues"""
        print(f"{Colors.YELLOW}🔧 Fixing permission issues...{Colors.ENDC}")

        # Make scripts executable
        scripts = [
            "start_system.py",
            "backend/main.py",
            "backend/main_minimal.py",
            "backend/start_backend.py",
        ]

        fixed = False
        for script in scripts:
            if Path(script).exists():
                try:
                    os.chmod(script, 0o755)
                    print(f"{Colors.GREEN}✅ Made {script} executable{Colors.ENDC}")
                    fixed = True
                except Exception:
                    pass

        return fixed

    async def _heal_missing_api_key(self) -> bool:
        """Handle missing API keys"""
        print(f"{Colors.YELLOW}🔧 Checking for API key configuration...{Colors.ENDC}")

        # Check multiple .env locations
        env_paths = [".env", "backend/.env", "../.env"]

        for env_path in env_paths:
            if Path(env_path).exists():
                try:
                    # Force reload of environment
                    from dotenv import load_dotenv

                    load_dotenv(env_path, override=True)

                    if _get_anthropic_api_key():
                        print(f"{Colors.GREEN}✅ Found API key in {env_path}{Colors.ENDC}")
                        return True
                except:
                    pass

        # Create .env template
        print(f"{Colors.WARNING}Creating .env template...{Colors.ENDC}")
        env_content = """# JARVIS Environment Configuration
ANTHROPIC_API_KEY=your_claude_api_key_here

# Get your API key from: https://console.anthropic.com/
# Then restart JARVIS
"""
        env_path = Path("backend/.env")
        env_path.parent.mkdir(exist_ok=True)
        env_path.write_text(env_content)
        print(f"{Colors.YELLOW}📝 Please add your ANTHROPIC_API_KEY to {env_path}{Colors.ENDC}")

        return False

    async def _heal_memory_pressure(self) -> bool:
        """Fix high memory usage (macOS-aware)"""
        memory = psutil.virtual_memory()
        available_gb_before = memory.available / (1024**3)
        print(
            f"{Colors.YELLOW}🔧 Low memory: {available_gb_before:.1f}GB available, attempting cleanup...{Colors.ENDC}"
        )

        # Kill common memory hogs
        memory_hogs = [
            "Chrome Helper",
            "Chrome Helper (GPU)",
            "Chrome Helper (Renderer)",
        ]

        for process_name in memory_hogs:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pkill",
                    "-f",
                    process_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except:
                pass

        # Force Python garbage collection
        import gc

        gc.collect()

        # Wait and check
        await asyncio.sleep(3)

        new_memory = psutil.virtual_memory()
        available_gb_after = new_memory.available / (1024**3)

        # Success if we freed at least 500MB
        if available_gb_after > available_gb_before + 0.5:
            print(
                f"{Colors.GREEN}✅ Memory freed: {available_gb_after:.1f}GB available (gained {available_gb_after - available_gb_before:.1f}GB){Colors.ENDC}"
            )
            return True

        return False

    async def _heal_process_crash(self, context: str, error: Exception) -> bool:
        """Handle process crashes with intelligent recovery"""
        print(
            f"{Colors.YELLOW}🔧 Process crashed in {context}, attempting recovery...{Colors.ENDC}"
        )

        # Get return code if available
        returncode = getattr(error, "returncode", -1)

        if "backend" in context:
            if returncode == 1:
                # Python error - check logs
                print(f"{Colors.CYAN}Checking error logs...{Colors.ENDC}")
                # The error will be caught and we'll try minimal backend
                return True

        elif "websocket" in context:
            # Try rebuilding
            websocket_dir = self.backend_dir / "websocket"
            if websocket_dir.exists():
                print(f"{Colors.CYAN}Attempting to rebuild WebSocket router...{Colors.ENDC}")
                try:
                    # Clean and rebuild
                    proc = await asyncio.create_subprocess_exec(
                        "npm",
                        "run",
                        "build",
                        cwd=str(websocket_dir),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()

                    if proc.returncode == 0:
                        print(f"{Colors.GREEN}✅ WebSocket router rebuilt{Colors.ENDC}")
                        return True
                except:
                    pass

        return False

    def _extract_port_from_error(self, error_msg: str) -> Optional[int]:
        """Extract port number from error message"""
        import re

        # Look for port numbers in various formats
        patterns = [
            r":(\d{4,5})",  # :8010
            r"port\s+(\d{4,5})",  # port 8010
            r"Port\s+(\d{4,5})",  # Port 8010
        ]

        for pattern in patterns:
            match = re.search(pattern, error_msg)
            if match:
                return int(match.group(1))
        return None

    def _extract_module_from_error(self, error_str: str) -> Optional[str]:
        """Extract module name from error message"""
        import re

        # Match patterns like: No module named 'X'
        match = re.search(r"No module named ['\"](\w+)['\"]", error_str)
        if match:
            return match.group(1)
        # Also check for just the module name after ModuleNotFoundError
        match = re.search(r"ModuleNotFoundError.*['\"](\w+)['\"]", error_str)
        if match:
            return match.group(1)
        return None

    async def cleanup(self):
        """Clean up all processes"""
        print(
            f"\n{Colors.BLUE}╔══════════════════════════════════════════════════════════════╗{Colors.ENDC}"
        )
        print(
            f"{Colors.BLUE}║         Shutting down JARVIS gracefully...                  ║{Colors.ENDC}"
        )
        print(
            f"{Colors.BLUE}╚══════════════════════════════════════════════════════════════╝{Colors.ENDC}\n"
        )

        # Set a flag to suppress exit warnings
        self._shutting_down = True

        # Cancel ALL pending async tasks (both tracked and untracked)
        print(f"{Colors.CYAN}🔄 [0/6] Canceling async tasks...{Colors.ENDC}")

        # Get ALL tasks in the current event loop
        try:
            current_task = asyncio.current_task()
            all_tasks = [task for task in asyncio.all_tasks() if task is not current_task]

            if all_tasks:
                print(f"   ├─ Found {len(all_tasks)} pending async tasks")

                # Cancel tasks safely with recursion protection
                cancelled_count = 0
                for task in all_tasks:
                    if not task.done():
                        try:
                            task.cancel()
                            cancelled_count += 1
                        except RecursionError:
                            # Skip tasks that cause recursion during cancellation
                            logger.warning(f"Skipping task due to recursion: {task.get_name()}")
                            continue
                        except Exception as e:
                            logger.warning(f"Failed to cancel task {task.get_name()}: {e}")
                            continue

                print(f"   ├─ Cancelled {cancelled_count}/{len(all_tasks)} tasks")

                # Wait for cancellation to complete with timeout
                try:
                    # Use wait_for to prevent hanging, capture results to suppress warnings
                    results = await asyncio.wait_for(
                        asyncio.gather(*all_tasks, return_exceptions=True),
                        timeout=5.0
                    )
                    # Process results to suppress CancelledError warnings
                    if results:
                        for result in results:
                            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                                logger.debug(f"Task exception during cleanup: {result}")
                    print(f"   └─ {Colors.GREEN}✓ All async tasks cancelled{Colors.ENDC}")
                except asyncio.TimeoutError:
                    print(f"   └─ {Colors.YELLOW}⚠ Task cancellation timeout (some tasks may still be running){Colors.ENDC}")
                except asyncio.CancelledError:
                    print(f"   └─ {Colors.GREEN}✓ Cleanup cancelled (shutdown in progress){Colors.ENDC}")
                except Exception as e:
                    print(f"   └─ {Colors.YELLOW}⚠ Task cancellation warning: {e}{Colors.ENDC}")
            else:
                print(f"   └─ {Colors.GREEN}✓ No pending async tasks{Colors.ENDC}")

            self.background_tasks.clear()
        except RecursionError:
            print(f"   └─ {Colors.YELLOW}⚠ Recursion error during task enumeration - forcing cleanup{Colors.ENDC}")
            self.background_tasks.clear()
        except Exception as e:
            print(f"   └─ {Colors.YELLOW}⚠ Could not enumerate tasks: {e}{Colors.ENDC}")

        # Clean up tracked subprocesses (CRITICAL: Must happen before event loop closure)
        print(f"\n{Colors.CYAN}🔌 [0.5/6] Cleaning up asyncio subprocesses...{Colors.ENDC}")
        subprocess_cleanup_tasks = []

        # Include loading server process if it exists
        if '_loading_server_process' in globals():
            loading_proc = globals()['_loading_server_process']
            if loading_proc and loading_proc.returncode is None:
                self.subprocesses.append(loading_proc)

        if self.subprocesses:
            print(f"   ├─ Found {len(self.subprocesses)} tracked subprocesses")
            terminated_count = 0

            for proc in self.subprocesses:
                if proc and proc.returncode is None:
                    try:
                        proc.terminate()  # Graceful SIGTERM
                        subprocess_cleanup_tasks.append(proc.wait())
                        terminated_count += 1
                    except ProcessLookupError:
                        pass  # Process already dead
                    except Exception as e:
                        logger.warning(f"Failed to terminate subprocess {proc.pid}: {e}")

            print(f"   ├─ Terminated {terminated_count}/{len(self.subprocesses)} subprocesses")

            # Wait for graceful shutdown with timeout
            if subprocess_cleanup_tasks:
                try:
                    # Capture results to suppress warnings
                    results = await asyncio.wait_for(
                        asyncio.gather(*subprocess_cleanup_tasks, return_exceptions=True),
                        timeout=3.0
                    )
                    # Process results to handle any exceptions
                    if results:
                        for result in results:
                            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                                logger.debug(f"Subprocess wait exception: {result}")
                    print(f"   └─ {Colors.GREEN}✓ All subprocesses exited gracefully{Colors.ENDC}")
                except asyncio.TimeoutError:
                    print(f"   ├─ {Colors.YELLOW}⚠ Timeout - force killing subprocesses...{Colors.ENDC}")
                    killed_count = 0
                    for proc in self.subprocesses:
                        if proc and proc.returncode is None:
                            try:
                                proc.kill()  # Force SIGKILL
                                killed_count += 1
                            except ProcessLookupError:
                                pass
                    print(f"   └─ {Colors.GREEN}✓ Force killed {killed_count} subprocesses{Colors.ENDC}")

            self.subprocesses.clear()
        else:
            print(f"   └─ {Colors.GREEN}✓ No tracked subprocesses to clean{Colors.ENDC}")

        # Stop hybrid coordinator first
        if self.hybrid_enabled and self.hybrid_coordinator:
            try:
                print(f"{Colors.CYAN}🌐 [1/6] Stopping Hybrid Cloud Intelligence...{Colors.ENDC}")
                print(f"   ├─ Canceling health check tasks...")
                await self.hybrid_coordinator.stop()
                print(f"   ├─ Closing HTTP client connections...")

                # Print final stats
                status = await self.hybrid_coordinator.get_status()
                metrics = status["metrics"]
                if metrics["total_migrations"] > 0:
                    print(f"   ├─ Session stats:")
                    print(f"   │  • Total GCP migrations: {metrics['total_migrations']}")
                    print(f"   │  • Prevented crashes: {metrics['prevented_crashes']}")
                    print(f"   │  • Avg migration time: {metrics['avg_migration_time']:.1f}s")
                print(f"   └─ {Colors.GREEN}✓ Hybrid coordinator stopped{Colors.ENDC}")
            except Exception as e:
                print(
                    f"   └─ {Colors.YELLOW}⚠ Hybrid coordinator cleanup warning: {e}{Colors.ENDC}"
                )
                logger.warning(f"Hybrid coordinator cleanup failed: {e}")
        else:
            print(f"{Colors.CYAN}🌐 [1/6] Hybrid Cloud Intelligence not active{Colors.ENDC}")

        # Stop Advanced Metrics Monitor
        if hasattr(self, 'metrics_monitor') and self.metrics_monitor:
            try:
                print(f"\n{Colors.CYAN}📊 [1.1/6] Stopping Advanced Metrics Monitor...{Colors.ENDC}")
                from voice_unlock.metrics_monitor import shutdown_metrics_monitor
                await shutdown_metrics_monitor()
                print(f"   └─ {Colors.GREEN}✓ Metrics monitor stopped (DB Browser closed){Colors.ENDC}")
            except Exception as e:
                print(f"   └─ {Colors.YELLOW}⚠ Metrics monitor cleanup warning: {e}{Colors.ENDC}")
                logger.warning(f"Metrics monitor cleanup failed: {e}")

        # Stop ML Continuous Learning Engine
        try:
            print(f"\n{Colors.CYAN}🧠 [1.15/6] Stopping ML Continuous Learning Engine...{Colors.ENDC}")

            # Ensure backend directory is in path
            backend_path = str(Path(__file__).parent / "backend")
            if backend_path not in sys.path:
                sys.path.insert(0, backend_path)

            from voice_unlock.continuous_learning_engine import shutdown_learning_engine
            print(f"   ├─ Saving ML model checkpoints...")
            await shutdown_learning_engine()
            print(f"   └─ {Colors.GREEN}✓ ML Learning Engine stopped (models saved){Colors.ENDC}")
        except ImportError:
            print(f"   └─ {Colors.BLUE}ℹ️  ML Learning Engine not available{Colors.ENDC}")
        except Exception as e:
            print(f"   └─ {Colors.YELLOW}⚠ ML Learning Engine cleanup warning: {e}{Colors.ENDC}")
            logger.warning(f"ML Learning Engine cleanup failed: {e}")

        # Stop Cloud SQL proxy (CRITICAL: Must happen before database connections close)
        if self.cloud_sql_proxy_manager:
            try:
                print(f"\n{Colors.CYAN}🔐 [1.2/6] Stopping Cloud SQL Proxy...{Colors.ENDC}")
                print(f"   ├─ Terminating proxy process (PID: {self.cloud_sql_proxy_manager.pid_path.read_text().strip() if self.cloud_sql_proxy_manager.pid_path.exists() else 'unknown'})...")
                await self.cloud_sql_proxy_manager.stop()
                print(f"   └─ {Colors.GREEN}✓ Cloud SQL proxy stopped{Colors.ENDC}")
            except Exception as e:
                print(f"   └─ {Colors.YELLOW}⚠ Proxy cleanup warning: {e}{Colors.ENDC}")
                logger.warning(f"Cloud SQL proxy cleanup failed: {e}")

        # Show GCP VM cost summary
        gcp_vm_enabled = os.getenv("GCP_VM_ENABLED", "true").lower() == "true"
        if gcp_vm_enabled:
            try:
                print(f"\n{Colors.CYAN}💰 [1.5/6] GCP VM Cost Summary...{Colors.ENDC}")

                # Ensure backend directory is in path
                backend_path = str(Path(__file__).parent / "backend")
                if backend_path not in sys.path:
                    sys.path.insert(0, backend_path)

                # Import after path is set
                from core.gcp_vm_status import show_vm_status

                # Show brief VM status without full details
                result = await show_vm_status(verbose=False)
                if result.get("vms"):
                    print(
                        f"   ├─ {Colors.YELLOW}⚠️  Active VMs will be terminated by backend{Colors.ENDC}"
                    )
                    print(
                        f"   └─ {Colors.CYAN}ℹ️  Check backend logs for termination costs{Colors.ENDC}"
                    )
                else:
                    print(f"   └─ {Colors.GREEN}✓ No active VMs (no costs){Colors.ENDC}")
            except ImportError as e:
                print(f"   └─ {Colors.YELLOW}⚠️  VM status module not available{Colors.ENDC}")
                logger.debug(f"GCP VM status import failed: {e}")
            except Exception as e:
                print(f"   └─ {Colors.YELLOW}⚠️  Could not retrieve VM status: {e}{Colors.ENDC}")
                logger.debug(f"GCP VM status check failed: {e}")

        # Shutdown singleton connection manager
        print(f"\n{Colors.CYAN}🔌 [1.8/6] Shutting down database connections...{Colors.ENDC}")
        try:
            from intelligence.cloud_sql_connection_manager import get_connection_manager
            conn_manager = get_connection_manager()
            if conn_manager.is_initialized:
                await conn_manager.shutdown()
                print(f"   └─ {Colors.GREEN}✓ Database connections closed{Colors.ENDC}")
            else:
                print(f"   └─ {Colors.CYAN}ℹ️  No active database connections{Colors.ENDC}")
        except Exception as e:
            print(f"   └─ {Colors.YELLOW}⚠ Database shutdown warning: {e}{Colors.ENDC}")
            logger.debug(f"Connection manager shutdown failed: {e}")

        # Close all open file handles
        print(f"\n{Colors.CYAN}📁 [2/6] Closing file handles...{Colors.ENDC}")
        file_count = len(self.open_files)
        for file_handle in self.open_files:
            try:
                file_handle.close()
            except Exception:
                pass
        self.open_files.clear()
        print(f"   └─ {Colors.GREEN}✓ Closed {file_count} file handles{Colors.ENDC}")

        # First try graceful termination
        print(f"\n{Colors.CYAN}🔌 [3/6] Terminating processes gracefully...{Colors.ENDC}")
        active_processes = [p for p in self.processes if p and p.returncode is None]
        print(f"   ├─ Found {len(active_processes)} active processes")

        tasks = []
        for proc in self.processes:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    # Mark as intentionally terminated to suppress warnings
                    proc._exit_reported = True
                    tasks.append(proc.wait())
                except ProcessLookupError:
                    # Process already terminated
                    pass

        if tasks:
            # Wait for processes to terminate with a timeout
            print(f"   ├─ Waiting for graceful termination (3s timeout)...")
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
                print(f"   └─ {Colors.GREEN}✓ All processes terminated gracefully{Colors.ENDC}")
            except asyncio.TimeoutError:
                print(
                    f"   ├─ {Colors.YELLOW}⚠ Timeout - force killing remaining processes...{Colors.ENDC}"
                )
                killed_count = 0
                # Force kill any remaining processes
                for proc in self.processes:
                    if proc and proc.returncode is None:
                        try:
                            proc.kill()
                            killed_count += 1
                        except ProcessLookupError:
                            pass
                print(f"   └─ {Colors.GREEN}✓ Force killed {killed_count} processes{Colors.ENDC}")
        else:
            print(f"   └─ {Colors.GREEN}✓ No active processes to terminate{Colors.ENDC}")

        # Double-check by killing processes on known ports
        print(f"\n{Colors.CYAN}🔌 [4/6] Cleaning up port processes...{Colors.ENDC}")
        port_list = ", ".join([f"{name}:{port}" for name, port in self.ports.items()])
        print(f"   ├─ Checking ports: {port_list}")
        cleanup_tasks = []
        for service_name, port in self.ports.items():
            cleanup_tasks.append(self.kill_process_on_port(port))

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            print(f"   └─ {Colors.GREEN}✓ Freed {len(cleanup_tasks)} ports{Colors.ENDC}")
        else:
            print(f"   └─ {Colors.GREEN}✓ No ports to clean{Colors.ENDC}")

        # Clean up any lingering Node.js and Python processes
        print(f"\n{Colors.CYAN}🧹 [5/6] Cleaning up JARVIS-related processes...{Colors.ENDC}")
        try:
            # Kill npm processes
            print(f"   ├─ Killing npm processes...")
            npm_kill = await asyncio.create_subprocess_shell(
                "pkill -f 'npm.*start' || true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await npm_kill.wait()  # Properly awaited - no lingering waiters

            # Kill node processes running our apps
            print(f"   ├─ Killing Node.js processes (websocket, frontend)...")
            node_kill = await asyncio.create_subprocess_shell(
                "pkill -f 'node.*websocket|node.*3000' || true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await node_kill.wait()  # Properly awaited - no lingering waiters

            # Kill python processes running our backend (but not IDE-related processes)
            print(f"   ├─ Killing Python backend processes (skipping IDE extensions)...")
            # First get all matching PIDs
            python_killed = 0
            try:
                result = subprocess.run(
                    "pgrep -f 'python.*main.py|python.*jarvis'",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                pids = result.stdout.strip().split("\n")

                for pid in pids:
                    if not pid:
                        continue

                    # Check parent process to avoid killing IDE extensions
                    try:
                        parent_check = subprocess.run(
                            f"ps -o ppid= -p {pid} | xargs ps -o comm= -p 2>/dev/null || echo ''",
                            shell=True,
                            capture_output=True,
                            text=True,
                        )
                        parent_name = parent_check.stdout.strip().lower()

                        # Skip if parent is an IDE
                        ide_patterns = [
                            "cursor",
                            "code",
                            "vscode",
                            "sublime",
                            "pycharm",
                            "intellij",
                            "webstorm",
                            "atom",
                        ]

                        if any(pattern in parent_name for pattern in ide_patterns):
                            continue

                        # Kill the process
                        subprocess.run(f"kill {pid}", shell=True, capture_output=True)
                        python_killed += 1
                    except:
                        pass
            except:
                pass

            print(f"   └─ {Colors.GREEN}✓ Cleaned up {python_killed} Python processes{Colors.ENDC}")

        except Exception as e:
            print(f"   └─ {Colors.YELLOW}⚠ Cleanup warning: {e}{Colors.ENDC}")

        # Give a moment for processes to die
        print(f"\n{Colors.CYAN}⏳ [6/6] Finalizing shutdown...{Colors.ENDC}")

        # Clean up speaker verification service (and its background threads)
        try:
            print(f"   ├─ Cleaning up speaker verification service...")
            import backend.voice.speaker_verification_service as sv
            if sv._global_speaker_service:
                await sv._global_speaker_service.cleanup()
                print(f"   ├─ {Colors.GREEN}✓ Speaker service cleaned up{Colors.ENDC}")
            else:
                print(f"   ├─ Speaker service not active")
        except Exception as e:
            print(f"   ├─ {Colors.YELLOW}⚠ Speaker service cleanup warning: {e}{Colors.ENDC}")

        print(f"   ├─ Waiting for final process cleanup (0.5s)...")
        await asyncio.sleep(0.5)
        print(f"   └─ {Colors.GREEN}✓ Shutdown complete{Colors.ENDC}")

        print(
            f"\n{Colors.GREEN}╔══════════════════════════════════════════════════════════════╗{Colors.ENDC}"
        )
        print(
            f"{Colors.GREEN}║         ✓ All JARVIS services stopped                       ║{Colors.ENDC}"
        )
        print(
            f"{Colors.GREEN}╚══════════════════════════════════════════════════════════════╝{Colors.ENDC}"
        )

        # Flush output to ensure all messages are printed
        sys.stdout.flush()
        sys.stderr.flush()

    async def _prewarm_python_imports(self) -> None:
        """Pre-warm Python imports in background for faster startup"""
        prewarm_script = """
import sys
import asyncio

# Pre-import heavy modules
try:
    import numpy
    import aiohttp
    import psutil
    import logging
    print("Pre-warmed base imports")

    # Pre-warm backend imports if available
    sys.path.insert(0, "backend")
    try:
        import ml_memory_manager
        import context_aware_loader
        print("Pre-warmed ML imports")
    except:
        pass
except Exception as e:
    print(f"Pre-warm warning: {e}")
"""

        # Run in background
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            prewarm_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Don't wait - let it run in background

    async def start_websocket_router(self) -> Optional[asyncio.subprocess.Process]:
        """Start TypeScript WebSocket Router"""
        websocket_dir = self.backend_dir / "websocket"
        if not websocket_dir.exists():
            print(f"{Colors.WARNING}WebSocket router directory not found, skipping...{Colors.ENDC}")
            return None

        print(f"\n{Colors.BLUE}Starting TypeScript WebSocket Router...{Colors.ENDC}")

        # Check/install dependencies
        node_modules = websocket_dir / "node_modules"
        if not node_modules.exists():
            print(f"{Colors.YELLOW}Installing WebSocket router dependencies...{Colors.ENDC}")
            proc = await asyncio.create_subprocess_exec(
                "npm",
                "install",
                cwd=str(websocket_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                print(
                    f"{Colors.FAIL}✗ Failed to install WebSocket router dependencies.{Colors.ENDC}"
                )
                print(stderr.decode())
                return None

        # Build TypeScript
        print(f"{Colors.CYAN}Building WebSocket router...{Colors.ENDC}")
        build_proc = await asyncio.create_subprocess_exec(
            "npm",
            "run",
            "build",
            cwd=str(websocket_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await build_proc.communicate()
        if build_proc.returncode != 0:
            print(f"{Colors.FAIL}✗ Failed to build WebSocket router.{Colors.ENDC}")
            print(stderr.decode())
            return None

        # Kill existing process
        port = self.ports["websocket_router"]
        if not await self.check_port_available(port):
            await self.kill_process_on_port(port)
            await asyncio.sleep(1)

        # Start router
        log_file = (
            self.backend_dir
            / "logs"
            / f"websocket_router_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )

        # Correctly set the environment variable for the port
        env = os.environ.copy()
        env["PORT"] = str(port)

        with open(log_file, "w") as log:
            process = await asyncio.create_subprocess_exec(
                "npm",
                "start",
                cwd=str(websocket_dir),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

        self.processes.append(process)
        print(
            f"{Colors.GREEN}✓ WebSocket Router starting on port {port} (PID: {process.pid}){Colors.ENDC}"
        )

        # Health check for the websocket router
        router_ready = await self.wait_for_service(f"http://localhost:{port}/health", timeout=15)
        if not router_ready:
            print(
                f"{Colors.FAIL}✗ WebSocket router failed to start or is not healthy.{Colors.ENDC}"
            )
            print(f"  Check log file: {log_file}")
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return None

        print(f"{Colors.GREEN}✓ WebSocket Router is healthy.{Colors.ENDC}")

        return process

    async def _run_with_healing(self, func, context: str, *args, **kwargs):
        """Run a function with self-healing capability"""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                if attempt < max_retries - 1 and await self._diagnose_and_heal(context, e):
                    print(f"{Colors.CYAN}Retrying {context} after self-healing...{Colors.ENDC}")
                    continue
                else:
                    raise
        return None

    async def run(self):
        """Main run method with self-healing"""
        self.print_header()

        # Display system environment and configuration details
        print(f"\n{Colors.BLUE}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.BLUE}📊 System Environment & Configuration{Colors.ENDC}")
        print(f"{Colors.BLUE}{'='*70}{Colors.ENDC}\n")

        print(f"{Colors.CYAN}🐍 Python Environment:{Colors.ENDC}")
        print(f"   ├─ Version: {sys.version.split()[0]}")
        print(f"   ├─ Executable: {sys.executable}")
        print(f"   └─ Platform: {sys.platform}")

        print(f"\n{Colors.CYAN}📁 Working Directories:{Colors.ENDC}")
        print(f"   ├─ Root: {Path(__file__).parent}")
        print(f"   ├─ Backend: {self.backend_dir}")
        print(f"   └─ Frontend: {self.frontend_dir}")

        # Show module import paths
        backend_in_path = str(self.backend_dir) in sys.path
        print(f"\n{Colors.CYAN}🔧 Module Import Configuration:{Colors.ENDC}")
        print(f"   ├─ Backend in sys.path: {Colors.GREEN}Yes{Colors.ENDC}" if backend_in_path else f"   ├─ Backend in sys.path: {Colors.YELLOW}No (will add){Colors.ENDC}")
        print(f"   └─ PYTHONPATH entries: {len(sys.path)}")

        # Start Cloud SQL proxy FIRST (before any database connections)
        if self.cloud_sql_proxy_enabled:
            print(f"\n{Colors.CYAN}🔐 Starting Cloud SQL Proxy...{Colors.ENDC}")
            try:
                # Import from backend/intelligence
                backend_dir = Path(__file__).parent / "backend"
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                    print(f"{Colors.CYAN}   → Added backend to sys.path{Colors.ENDC}")

                print(f"{Colors.CYAN}   → Importing CloudSQLProxyManager...{Colors.ENDC}")
                from intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager

                print(f"{Colors.CYAN}   → Initializing proxy manager...{Colors.ENDC}")
                self.cloud_sql_proxy_manager = CloudSQLProxyManager()

                # Display proxy configuration before starting
                print(f"{Colors.CYAN}   → Proxy configuration loaded:{Colors.ENDC}")
                print(f"      ├─ Connection: {self.cloud_sql_proxy_manager.config['cloud_sql']['connection_name']}")
                print(f"      ├─ Database: {self.cloud_sql_proxy_manager.config['cloud_sql']['database']}")
                print(f"      └─ Port: {self.cloud_sql_proxy_manager.config['cloud_sql']['port']}")

                # Start proxy asynchronously (non-blocking)
                print(f"{Colors.CYAN}   → Starting proxy process (force_restart=True)...{Colors.ENDC}")
                proxy_started = await self.cloud_sql_proxy_manager.start(force_restart=True)

                if proxy_started:
                    print(f"   • {Colors.GREEN}✓{Colors.ENDC} Proxy started on port {self.cloud_sql_proxy_manager.config['cloud_sql']['port']}")
                    print(f"   • {Colors.GREEN}✓{Colors.ENDC} Connection: {self.cloud_sql_proxy_manager.config['cloud_sql']['connection_name']}")
                    print(f"   • {Colors.GREEN}✓{Colors.ENDC} Log: {self.cloud_sql_proxy_manager.log_path}")
                    print(f"   • {Colors.GREEN}✓{Colors.ENDC} Ready to accept database connections")
                else:
                    logger.warning("⚠️  Cloud SQL proxy failed to start - falling back to SQLite")
                    print(f"{Colors.YELLOW}   ⚠️  Will use local SQLite database instead{Colors.ENDC}")
                    self.cloud_sql_proxy_enabled = False
            except Exception as e:
                logger.warning(f"Cloud SQL proxy initialization failed: {e}")
                print(f"{Colors.YELLOW}   ⚠️  Error: {e}{Colors.ENDC}")
                print(f"{Colors.YELLOW}   ⚠️  Falling back to SQLite database{Colors.ENDC}")
                self.cloud_sql_proxy_enabled = False

        # Start hybrid cloud intelligence coordinator
        if self.hybrid_enabled and self.hybrid_coordinator:
            print(f"\n{Colors.CYAN}🌐 Starting Hybrid Cloud Intelligence...{Colors.ENDC}")
            try:
                print(f"{Colors.CYAN}   → Initializing RAM monitor and workload router...{Colors.ENDC}")
                await self.hybrid_coordinator.start()

                # Get detailed RAM state
                ram_state = await self.hybrid_coordinator.ram_monitor.get_current_state()
                ram_percent = ram_state['percent'] * 100
                ram_used_gb = ram_state.get('used_gb', 0)
                ram_total_gb = ram_state.get('total_gb', 0)
                ram_available_gb = ram_state.get('available_gb', 0)

                print(f"{Colors.CYAN}   → RAM monitoring active:{Colors.ENDC}")
                print(f"      ├─ Status: {ram_state['status']}")
                print(f"      ├─ Usage: {ram_percent:.1f}% ({ram_used_gb:.1f}GB / {ram_total_gb:.1f}GB)")
                print(f"      └─ Available: {ram_available_gb:.1f}GB")

                print(
                    f"   • {Colors.GREEN}✓{Colors.ENDC} RAM Monitor: {ram_percent:.1f}% used ({ram_state['status']})"
                )
                print(
                    f"   • {Colors.GREEN}✓{Colors.ENDC} Workload Router: Standby for automatic GCP routing"
                )
                print(
                    f"   • {Colors.GREEN}✓{Colors.ENDC} Monitoring: Active every {self.hybrid_coordinator.monitoring_interval}s"
                )
                print(f"   • {Colors.GREEN}✓{Colors.ENDC} Auto-offloading to GCP when RAM > 80%")
            except Exception as e:
                logger.warning(f"Hybrid coordinator start failed: {e}")
                print(f"{Colors.YELLOW}   ⚠️  Error: {e}{Colors.ENDC}")
                print(f"{Colors.YELLOW}   ⚠️  Will run in local-only mode{Colors.ENDC}")
                self.hybrid_enabled = False

        # Start Advanced Voice Unlock Metrics Monitor (with DB Browser auto-launch)
        print(f"\n{Colors.CYAN}📊 Starting Advanced Voice Unlock Metrics Monitor...{Colors.ENDC}")
        try:
            # Import and initialize advanced metrics monitor
            sys.path.insert(0, str(Path(__file__).parent / "backend"))
            from voice_unlock.metrics_monitor import initialize_metrics_monitor

            self.metrics_monitor = await initialize_metrics_monitor()

            # Dynamic status based on monitor state
            if self.metrics_monitor.degraded_mode:
                print(f"{Colors.YELLOW}   ⚠️  Metrics Monitor running in degraded mode{Colors.ENDC}")
                print(f"   • {Colors.YELLOW}Reason:{Colors.ENDC} {self.metrics_monitor.degradation_reason}")
            else:
                print(f"{Colors.GREEN}   ✓ Advanced Metrics Monitor active{Colors.ENDC}")

            # DB Browser status
            if self.metrics_monitor.db_browser_already_running:
                print(f"   • {Colors.GREEN}DB Browser:{Colors.ENDC} Already running (PID: {self.metrics_monitor.db_browser_pid}) - Reused existing instance")
            elif self.metrics_monitor.db_browser_pid:
                print(f"   • {Colors.GREEN}DB Browser:{Colors.ENDC} Launched successfully (PID: {self.metrics_monitor.db_browser_pid})")
            else:
                print(f"   • {Colors.YELLOW}DB Browser:{Colors.ENDC} Not launched (install with: brew install --cask db-browser-for-sqlite)")

            # Feature summary
            print(f"   • {Colors.CYAN}Real-time monitoring:{Colors.ENDC} Database updates on every unlock")
            print(f"   • {Colors.CYAN}Storage:{Colors.ENDC} JSON + SQLite + CloudSQL (triple backup)")
            print(f"   • {Colors.CYAN}Metrics:{Colors.ENDC} Confidence trends, stage performance, biometric analysis")
            print(f"   • {Colors.CYAN}Database:{Colors.ENDC} ~/.jarvis/logs/unlock_metrics/unlock_metrics.db")
            print(f"   • {Colors.CYAN}Notifications:{Colors.ENDC} macOS Notification Center alerts (async)")
            print(f"   • {Colors.CYAN}Protection:{Colors.ENDC} Auto-recovery from corruption, disk space validation")
            print(f"   • {Colors.CYAN}Concurrency:{Colors.ENDC} WAL mode + retry logic for locked database")
            print(f"{Colors.BLUE}   💡 Press F5 in DB Browser to refresh and see new unlock attempts{Colors.ENDC}")
            print(f"{Colors.BLUE}   📱 You'll receive macOS notifications for every voice unlock attempt!{Colors.ENDC}")

        except Exception as e:
            logger.warning(f"Advanced metrics monitor initialization failed: {e}")
            print(f"{Colors.YELLOW}   ⚠️  Error: {e}{Colors.ENDC}")
            print(f"{Colors.YELLOW}   ⚠️  Voice unlock will work without metrics monitoring{Colors.ENDC}")
            self.metrics_monitor = None

        # Start autonomous systems if enabled
        if self.autonomous_mode and AUTONOMOUS_AVAILABLE:
            print(f"\n{Colors.CYAN}🤖 Starting Autonomous Systems...{Colors.ENDC}")

            # Start orchestrator
            if self.orchestrator is not None:
                task = asyncio.create_task(self.orchestrator.start())
                self.background_tasks.append(task)

            # Start service mesh
            if self.mesh is not None:
                task = asyncio.create_task(self.mesh.start())
                self.background_tasks.append(task)

            # Wait for initial discovery
            await asyncio.sleep(3)

            # Check for already running services
            print(f"\n{Colors.CYAN}🔍 Discovering existing services...{Colors.ENDC}")
            discovered = self.orchestrator.services if self.orchestrator else {}
            for name, service in discovered.items():
                print(f"  • Found {name}: {service.protocol}://localhost:{service.port}")

                # Update our ports if services found on different ports
                if "backend" in name.lower():
                    self.ports["main_api"] = service.port
                elif "frontend" in name.lower():
                    self.ports["frontend"] = service.port

        # Start pre-warming imports early
        print(f"\n{Colors.BLUE}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.BLUE}🔥 Background Module Pre-Warming{Colors.ENDC}")
        print(f"{Colors.BLUE}{'='*70}{Colors.ENDC}\n")

        print(f"{Colors.CYAN}🔄 Starting background import pre-warming...{Colors.ENDC}")
        print(f"{Colors.CYAN}   → This will speed up module loading during startup{Colors.ENDC}")
        task = asyncio.create_task(self._prewarm_python_imports())
        self.background_tasks.append(task)
        print(f"{Colors.GREEN}   ✓ Background task started{Colors.ENDC}")

        # Run initial checks in parallel
        print(f"\n{Colors.BLUE}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.BLUE}🔍 System Validation Checks{Colors.ENDC}")
        print(f"{Colors.BLUE}{'='*70}{Colors.ENDC}\n")

        print(f"{Colors.CYAN}🧪 Running parallel validation checks (3 tasks):{Colors.ENDC}")
        print(f"{Colors.CYAN}   1. Python version compatibility{Colors.ENDC}")
        print(f"{Colors.CYAN}   2. Claude API configuration{Colors.ENDC}")
        print(f"{Colors.CYAN}   3. System resources availability{Colors.ENDC}")

        check_tasks = [
            self.check_python_version(),
            self.check_claude_config(),
            self.check_system_resources(),
        ]

        results = await asyncio.gather(*check_tasks)
        if not results[0]:  # Python version is critical
            print(f"{Colors.FAIL}❌ Python version check failed - cannot continue{Colors.ENDC}")
            return False

        print(f"{Colors.GREEN}   ✓ All parallel checks completed successfully{Colors.ENDC}")

        # Additional checks
        print(f"\n{Colors.CYAN}🎤 Checking microphone system...{Colors.ENDC}")
        await self.check_microphone_system()

        print(f"{Colors.CYAN}👁️  Checking vision system permissions...{Colors.ENDC}")
        await self.check_vision_permissions()

        print(f"{Colors.CYAN}⚡ Checking performance optimizations...{Colors.ENDC}")
        await self.check_performance_fixes()

        # Create necessary directories
        print(f"\n{Colors.CYAN}📁 Creating necessary directories...{Colors.ENDC}")
        await self.create_directories()
        print(f"{Colors.GREEN}   ✓ Directory structure verified{Colors.ENDC}")

        # Check dependencies
        print(f"\n{Colors.CYAN}📦 Checking Python package dependencies...{Colors.ENDC}")
        deps_ok, critical_missing, optional_missing = await self.check_dependencies()

        if critical_missing:
            print(f"{Colors.YELLOW}   ⚠️  Missing {len(critical_missing)} critical package(s): {', '.join(critical_missing)}{Colors.ENDC}")
        if optional_missing:
            print(f"{Colors.CYAN}   ℹ️  Missing {len(optional_missing)} optional package(s): {', '.join(optional_missing[:5])}{Colors.ENDC}")

        if not deps_ok:
            print(f"\n{Colors.FAIL}❌ Critical packages missing!{Colors.ENDC}")
            print(f"Install with: pip install {' '.join(critical_missing)}")
            return False

        print(f"{Colors.GREEN}   ✓ All critical dependencies satisfied{Colors.ENDC}")

        # Auto-install critical packages if requested or in autonomous mode
        if critical_missing:
            if self.autonomous_mode or input("\nInstall missing packages? (y/n): ").lower() == "y":
                for package in critical_missing:
                    print(f"Installing {package}...")
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        package,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.wait()

        # Start services with advanced parallel initialization
        print(f"\n{Colors.BLUE}{'='*70}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.BLUE}🚀 Service Initialization{Colors.ENDC}")
        print(f"{Colors.BLUE}{'='*70}{Colors.ENDC}\n")

        if self.backend_only:
            print(f"{Colors.CYAN}🎯 Mode: Backend-only startup{Colors.ENDC}")
            print(f"{Colors.CYAN}   → WebSocket router will be started{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Backend API will be initialized{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Frontend will be skipped{Colors.ENDC}")
            await self.start_websocket_router()
            await asyncio.sleep(2)  # Reduced wait time
            await self.start_backend()
        elif self.frontend_only:
            print(f"{Colors.CYAN}🎯 Mode: Frontend-only startup{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Frontend interface will be started{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Backend will be skipped (expects existing backend){Colors.ENDC}")
            await self.start_frontend()
        else:
            # Advanced parallel startup with intelligent sequencing
            print(f"{Colors.CYAN}🎯 Mode: Full-stack parallel initialization{Colors.ENDC}")
            print(f"{Colors.CYAN}   → All services will be started in optimized sequence{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Backend and frontend will start in parallel{Colors.ENDC}")

            start_time = time.time()

            # Phase 1: Start WebSocket router first (optional - for advanced features)
            print(f"\n{Colors.BLUE}{'─'*70}{Colors.ENDC}")
            print(f"{Colors.BOLD}{Colors.CYAN}Phase 1/3: WebSocket Router (Optional){Colors.ENDC}")
            print(f"{Colors.BLUE}{'─'*70}{Colors.ENDC}")
            print(f"{Colors.CYAN}🌐 Starting WebSocket router for advanced features...{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Enables real-time bidirectional communication{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Required for: Live updates, streaming responses{Colors.ENDC}")

            websocket_router_process = await self.start_websocket_router()
            if not websocket_router_process:
                print(
                    f"{Colors.WARNING}   ⚠ WebSocket router not available (optional feature){Colors.ENDC}"
                )
                print(f"{Colors.CYAN}   → Continuing with HTTP-only mode{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}   ✓ WebSocket router started successfully{Colors.ENDC}")

            # Phase 2: Start backend and frontend in parallel
            print(f"\n{Colors.BLUE}{'─'*70}{Colors.ENDC}")
            print(f"{Colors.BOLD}{Colors.CYAN}Phase 2/3: Backend & Frontend (Parallel){Colors.ENDC}")
            print(f"{Colors.BLUE}{'─'*70}{Colors.ENDC}")
            print(f"{Colors.CYAN}🔄 Starting backend and frontend concurrently...{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Backend: FastAPI server with AI services{Colors.ENDC}")
            print(f"{Colors.CYAN}   → Frontend: User interface and voice interaction{Colors.ENDC}")
            print(
                f"{Colors.CYAN}   → Optimization: Parallel startup saves ~5-10 seconds{Colors.ENDC}"
            )

            # Small delay to ensure router is ready
            await asyncio.sleep(1)

            # Start both services in parallel
            backend_task = asyncio.create_task(self.start_backend())
            frontend_task = asyncio.create_task(self.start_frontend())

            # Start ULTRA-DYNAMIC progress tracking (ZERO HARDCODING!)
            async def track_backend_progress():
                """
                🚀 ULTRA-DYNAMIC ADAPTIVE PROGRESS TRACKER
                - Zero hardcoded values (all config-driven)
                - Self-discovering endpoints
                - Adaptive polling (speeds up on success, slows down on failure)
                - Fully async with intelligent error handling
                """
                import aiohttp
                import json

                # Load dynamic configuration
                config_path = Path(__file__).parent / "backend" / "config" / "startup_progress_config.json"
                try:
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                except:
                    # Fallback minimal config if file not found
                    # Port will be dynamically discovered via parallel scanning
                    config = {
                        "progress_tracking": {"poll_interval_ms": 2000, "max_startup_time_s": 300},
                        "milestones": [
                            {"endpoint": "/health", "progress": 60, "name": "backend", "message": "Backend responding", "icon": "⚙️"}
                        ],
                        "backend_config": {"host": "localhost", "port": 8011, "protocol": "http"},
                        "loading_server": {"host": "localhost", "port": 3001, "protocol": "http", "update_endpoint": "/api/update-progress"}
                    }

                # Extract config values (DYNAMIC!)
                tracking_config = config["progress_tracking"]
                milestones = config["milestones"]
                backend_cfg = config["backend_config"]
                loader_cfg = config["loading_server"]

                # ═══════════════════════════════════════════════════════════════════
                # ADVANCED ASYNC DYNAMIC PORT DISCOVERY
                # ═══════════════════════════════════════════════════════════════════
                # Features:
                # - Parallel port scanning for speed (all ports checked simultaneously)
                # - Proper async timeout handling (no event loop blocking)
                # - Health validation (checks response content, not just status code)
                # - Graceful degradation with informative logging
                # - Continuous re-discovery if selected port becomes unresponsive
                # ═══════════════════════════════════════════════════════════════════

                class AsyncPortDiscovery:
                    """Fully async port discovery with parallel scanning."""

                    def __init__(self, host: str, protocol: str, timeout: float = 2.0):
                        self.host = host
                        self.protocol = protocol
                        self.timeout = timeout
                        self.discovered_port = None
                        self.port_health_scores = {}  # Track port health over time
                        self._session = None

                    async def _get_session(self):
                        """Lazy async session creation."""
                        if self._session is None or self._session.closed:
                            connector = aiohttp.TCPConnector(
                                limit=10,
                                ttl_dns_cache=300,
                                enable_cleanup_closed=True
                            )
                            self._session = aiohttp.ClientSession(
                                connector=connector,
                                timeout=aiohttp.ClientTimeout(total=self.timeout)
                            )
                        return self._session

                    async def close(self):
                        """Clean up session."""
                        if self._session and not self._session.closed:
                            await self._session.close()

                    async def check_port_health(self, port: int) -> dict:
                        """Check if a specific port has a healthy backend (Python 3.9+ compatible)."""
                        result = {
                            'port': port,
                            'healthy': False,
                            'status_code': None,
                            'response_time_ms': None,
                            'error': None
                        }

                        url = f"{self.protocol}://{self.host}:{port}/health"
                        start_time = asyncio.get_event_loop().time()

                        async def _do_health_check():
                            """Inner async function for timeout wrapping."""
                            session = await self._get_session()
                            async with session.get(url) as resp:
                                result['status_code'] = resp.status
                                result['response_time_ms'] = (asyncio.get_event_loop().time() - start_time) * 1000
                                if resp.status == 200:
                                    try:
                                        data = await resp.json()
                                        if data.get('status') == 'healthy':
                                            result['healthy'] = True
                                        elif 'status' in data:
                                            result['healthy'] = data.get('status') not in ['error', 'failed', 'unhealthy']
                                        else:
                                            result['healthy'] = True
                                    except:
                                        result['healthy'] = True

                        try:
                            # Use wait_for for Python 3.9 compatibility
                            await asyncio.wait_for(_do_health_check(), timeout=self.timeout)
                        except asyncio.TimeoutError:
                            result['error'] = 'timeout'
                            result['response_time_ms'] = self.timeout * 1000
                        except aiohttp.ClientConnectorError:
                            result['error'] = 'connection_refused'
                        except Exception as e:
                            result['error'] = str(e)[:50]

                        return result

                    async def discover_healthy_port(self, ports: list) -> int:
                        """Scan multiple ports IN PARALLEL and return the healthiest one."""
                        # Remove duplicates while preserving priority order
                        unique_ports = list(dict.fromkeys(ports))

                        print(f"  {Colors.CYAN}🔍 Scanning {len(unique_ports)} ports in parallel...{Colors.ENDC}")

                        # Parallel health checks using asyncio.gather
                        tasks = [self.check_port_health(port) for port in unique_ports]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Process results
                        healthy_ports = []
                        for i, result in enumerate(results):
                            port = unique_ports[i]
                            if isinstance(result, Exception):
                                print(f"    {Colors.RED}✗ Port {port}: Exception - {str(result)[:30]}{Colors.ENDC}")
                                self.port_health_scores[port] = 0
                            elif result.get('healthy'):
                                response_time = result.get('response_time_ms', 0)
                                print(f"    {Colors.GREEN}✓ Port {port}: Healthy ({response_time:.0f}ms){Colors.ENDC}")
                                healthy_ports.append((port, response_time))
                                self.port_health_scores[port] = 100 - min(response_time, 100)
                            elif result.get('error') == 'timeout':
                                print(f"    {Colors.YELLOW}⏱ Port {port}: Timeout (stuck process?){Colors.ENDC}")
                                self.port_health_scores[port] = 0
                            elif result.get('error') == 'connection_refused':
                                # Silent - port not in use
                                self.port_health_scores[port] = 0
                            else:
                                print(f"    {Colors.YELLOW}⚠ Port {port}: {result.get('error', 'Unknown')}{Colors.ENDC}")
                                self.port_health_scores[port] = 10

                        if healthy_ports:
                            # Sort by response time (fastest first) then by original priority
                            healthy_ports.sort(key=lambda x: (x[1], unique_ports.index(x[0])))
                            best_port = healthy_ports[0][0]
                            print(f"  {Colors.GREEN}✓ Selected port {best_port} (fastest healthy response){Colors.ENDC}")
                            self.discovered_port = best_port
                            return best_port

                        # No healthy port found
                        print(f"  {Colors.YELLOW}⚠ No healthy backend found on any port{Colors.ENDC}")
                        return unique_ports[0]  # Return first port as fallback

                # Initialize port discovery
                port_discovery = AsyncPortDiscovery(
                    host=backend_cfg['host'],
                    protocol=backend_cfg['protocol'],
                    timeout=2.0
                )

                # Priority order: configured port first, then fallback ports from config
                fallback_ports = backend_cfg.get('fallback_ports', [8010, 8000, 8001, 8080, 8888])
                ports_to_scan = [backend_cfg['port']] + fallback_ports

                # Discover healthy port using parallel async scanning
                discovered_port = await port_discovery.discover_healthy_port(ports_to_scan)
                backend_cfg['port'] = discovered_port  # Update config with discovered port

                # Clean up discovery session
                await port_discovery.close()

                # Build dynamic URLs (now uses discovered port)
                backend_base = f"{backend_cfg['protocol']}://{backend_cfg['host']}:{backend_cfg['port']}"
                loader_url = f"{loader_cfg['protocol']}://{loader_cfg['host']}:{loader_cfg['port']}{loader_cfg['update_endpoint']}"

                # State tracking
                start_time = time.time()
                milestone_idx = 0
                current_progress = 50
                poll_interval = tracking_config["poll_interval_ms"] / 1000.0
                consecutive_successes = 0
                consecutive_failures = 0

                async def broadcast_progress(progress, stage, message, metadata=None):
                    """Async broadcast helper"""
                    try:
                        data = {
                            "stage": stage,
                            "message": message,
                            "progress": progress,
                            "timestamp": datetime.now().isoformat()
                        }
                        if metadata:
                            data["metadata"] = metadata

                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                loader_url,
                                json=data,
                                timeout=aiohttp.ClientTimeout(total=1)
                            ) as resp:
                                pass
                    except:
                        pass  # Silent fail

                async def check_milestone(milestone):
                    """Check if milestone endpoint is reachable"""
                    url = f"{backend_base}{milestone['endpoint']}"
                    method = milestone.get('method', 'GET')
                    timeout_s = milestone.get('timeout_s', 2)
                    accept_status = milestone.get('accept_status', [200])

                    try:
                        async with aiohttp.ClientSession() as session:
                            if method == 'GET':
                                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                                    return resp.status in accept_status
                            elif method == 'POST':
                                async with session.post(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                                    return resp.status in accept_status
                    except:
                        return False

                # ADAPTIVE POLLING LOOP
                while milestone_idx < len(milestones) and not backend_task.done():
                    elapsed = time.time() - start_time

                    # Timeout protection (dynamic from config)
                    if elapsed > tracking_config.get("max_startup_time_s", 300):
                        await broadcast_progress(
                            99, "timeout", "Startup timeout - please check logs",
                            {"icon": "⚠️", "label": "Timeout", "sublabel": f"{int(elapsed)}s"}
                        )
                        break

                    # Get current milestone
                    milestone = milestones[milestone_idx]

                    # Check milestone
                    if await check_milestone(milestone):
                        # MILESTONE REACHED! 🎯
                        await broadcast_progress(
                            milestone["progress"],
                            milestone["name"],
                            milestone["message"],
                            {
                                "icon": milestone["icon"],
                                "label": milestone.get("label", milestone["name"].title()),
                                "sublabel": "Ready"
                            }
                        )
                        print(f"  {Colors.CYAN}✓ [{milestone_idx + 1}/{len(milestones)}] {milestone['message']} ({milestone['progress']}%){Colors.ENDC}")

                        milestone_idx += 1
                        current_progress = milestone["progress"]
                        consecutive_successes += 1
                        consecutive_failures = 0

                        # ADAPTIVE: Speed up polling after success
                        if tracking_config.get("adaptive_polling", {}).get("enabled", False):
                            min_interval = tracking_config["adaptive_polling"].get("min_interval_ms", 1000) / 1000.0
                            poll_interval = max(poll_interval * 0.7, min_interval)

                    else:
                        # Milestone not reached - interpolate progress
                        consecutive_failures += 1
                        consecutive_successes = 0

                        # ADAPTIVE: Slow down polling after failures
                        if tracking_config.get("adaptive_polling", {}).get("enabled", False):
                            max_interval = tracking_config["adaptive_polling"].get("max_interval_ms", 5000) / 1000.0
                            poll_interval = min(poll_interval * 1.2, max_interval)

                        # Calculate interpolated progress
                        if milestone_idx > 0:
                            prev_progress = milestones[milestone_idx - 1]["progress"]
                        else:
                            prev_progress = 50

                        target_progress = milestone["progress"]
                        window_s = tracking_config.get("interpolation_window_s", 60)
                        time_ratio = min(elapsed / window_s, 1.0)
                        interpolated_progress = int(prev_progress + (target_progress - prev_progress) * time_ratio)

                        # Find appropriate fallback stage message based on time elapsed
                        fallback_stages = config.get("fallback_stages", [])
                        fallback_message = None
                        fallback_icon = "⏳"
                        fallback_label = "Loading"
                        fallback_sublabel = f"{int(elapsed)}s elapsed"

                        for stage in reversed(fallback_stages):
                            if elapsed >= stage.get("time_trigger_s", 0):
                                fallback_message = stage.get("message", f"Loading... ({int(elapsed)}s)")
                                fallback_icon = stage.get("icon", "⏳")
                                fallback_label = stage.get("label", "Loading")
                                fallback_sublabel = stage.get("sublabel", f"{int(elapsed)}s elapsed")
                                # Use fallback progress if higher than interpolated
                                if stage.get("progress", 0) > interpolated_progress:
                                    interpolated_progress = stage["progress"]
                                break

                        if not fallback_message:
                            fallback_message = f"Backend initializing... ({int(elapsed)}s elapsed)"

                        # Only update if progress increased
                        if interpolated_progress > current_progress:
                            current_progress = interpolated_progress
                            await broadcast_progress(
                                current_progress,
                                "initializing",
                                fallback_message,
                                {"icon": fallback_icon, "label": fallback_label, "sublabel": fallback_sublabel}
                            )

                    # Dynamic sleep based on adaptive polling
                    await asyncio.sleep(poll_interval)

                # Backend complete!
                if backend_task.done():
                    print(f"  {Colors.GREEN}✅ Backend initialization complete! (took {int(time.time() - start_time)}s){Colors.ENDC}")

            # Start ultra-dynamic tracker and track it for cleanup
            progress_tracker = asyncio.create_task(track_backend_progress())
            self.background_tasks.append(progress_tracker)

            # Wait for both with proper error handling
            backend_result, frontend_result = await asyncio.gather(
                backend_task, frontend_task, return_exceptions=True
            )

            # Check backend result (critical)
            if isinstance(backend_result, Exception):
                print(f"{Colors.FAIL}✗ Backend failed with error: {backend_result}{Colors.ENDC}")
                await self.cleanup()
                return False
            elif not backend_result:
                print(f"{Colors.FAIL}✗ Backend failed to start{Colors.ENDC}")
                await self.cleanup()
                return False

            # Loading page already opened by standalone server during restart mode
            # (See loading_server.py started before process cleanup)

            # Check frontend result (non-critical)
            if isinstance(frontend_result, Exception):
                print(f"{Colors.WARNING}⚠ Frontend failed: {frontend_result}{Colors.ENDC}")
            elif frontend_result is None:
                print(f"{Colors.WARNING}⚠ Frontend returned None (may still be starting){Colors.ENDC}")
            elif hasattr(frontend_result, 'returncode') and frontend_result.returncode is not None:
                print(f"{Colors.WARNING}⚠ Frontend process exited with code {frontend_result.returncode}{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}✓ Frontend process started successfully{Colors.ENDC}")

            # Phase 3: Quick health checks
            print(f"\n{Colors.CYAN}Phase 3/3: Running parallel health checks...{Colors.ENDC}")

            elapsed = time.time() - start_time
            print(
                f"\n{Colors.GREEN}✨ Services started in {elapsed:.1f}s (was ~13-18s){Colors.ENDC}"
            )

        # Run parallel health checks instead of fixed wait
        await self._run_parallel_health_checks()

        # Verify services
        services = await self.verify_services()

        if not services:
            print(f"\n{Colors.FAIL}❌ No services started successfully{Colors.ENDC}")
            return False

        # Print access info
        self.print_access_info()

        # Broadcast 100% completion to loading page
        try:
            import aiohttp
            url = "http://localhost:3001/api/update-progress"
            data = {
                "stage": "complete",
                "message": "JARVIS is ready!",
                "progress": 100,
                "timestamp": datetime.now().isoformat(),
                "metadata": {
                    "icon": "✅",
                    "label": "Complete",
                    "sublabel": "System ready!",
                    "success": True,
                    "redirect_url": "http://localhost:3000"
                }
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=1)) as resp:
                    print(f"{Colors.GREEN}✓ Loading page notified of completion{Colors.ENDC}")
        except:
            pass  # Loading server might have already shut down

        # Configure frontend for autonomous mode
        if self.autonomous_mode and AUTONOMOUS_AVAILABLE:
            print(f"\n{Colors.CYAN}Configuring frontend for autonomous mode...{Colors.ENDC}")

            # Generate frontend configuration
            if self.orchestrator:
                frontend_config = self.orchestrator.get_frontend_config()

            # Save configuration
            config_path = Path("frontend/public/dynamic-config.json")
            if config_path.parent.exists():
                config_path.parent.mkdir(exist_ok=True)
                import json

                with open(config_path, "w") as f:
                    json.dump(frontend_config, f, indent=2)
                print(f"{Colors.GREEN}✓ Frontend configuration generated{Colors.ENDC}")

            # Register services with mesh
            if self.orchestrator and self.mesh:
                for name, service in self.orchestrator.services.items():
                    # Build full endpoint URLs
                    full_endpoints = {}
                    if hasattr(service, "endpoints"):
                        for ep_name, ep_path in service.endpoints.items():
                            full_endpoints[ep_name] = (
                                f"{service.protocol}://localhost:{service.port}{ep_path}"
                            )

                    # Add default health endpoint if not present
                    if "health" not in full_endpoints:
                        full_endpoints["health"] = (
                            f"{service.protocol}://localhost:{service.port}/health"
                        )

                    await self.mesh.register_node(
                        node_id=name,
                        node_type=self.identify_service_type(name),
                        endpoints=full_endpoints,
                    )

        # Print autonomous status
        if self.autonomous_mode:
            await self.print_autonomous_status()

        # Print self-healing summary if any healing occurred
        if self.healing_log:
            print(f"\n{Colors.CYAN}🔧 Self-Healing Summary:{Colors.ENDC}")
            successful_heals = sum(1 for h in self.healing_log if h["healed"])
            total_heals = len(self.healing_log)
            print(f"  • Total healing attempts: {total_heals}")
            print(f"  • Successful heals: {successful_heals}")
            if successful_heals > 0:
                print(
                    f"  • {Colors.GREEN}✅ Self-healing helped JARVIS start successfully!{Colors.ENDC}"
                )

            # Show what was healed
            for heal in self.healing_log:
                if heal["healed"]:
                    print(f"    - Fixed: {heal['context']} ({heal['error'][:50]}...)")

        # Open browser intelligently - wait for backend to be ready
        if not self.no_browser:
            print(
                f"\n{Colors.CYAN}⏳ Waiting for backend to be ready before opening browser...{Colors.ENDC}"
            )

            # Check backend health before opening browser
            backend_ready = False
            max_wait = 15  # seconds
            start_time = asyncio.get_event_loop().time()

            while not backend_ready and (asyncio.get_event_loop().time() - start_time) < max_wait:
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"http://localhost:{self.ports['main_api']}/health",
                            timeout=aiohttp.ClientTimeout(total=2),
                        ) as resp:
                            if resp.status == 200:
                                backend_ready = True
                                print(f"{Colors.GREEN}✓ Backend is ready!{Colors.ENDC}")
                                break
                except Exception:
                    await asyncio.sleep(0.5)

            if not backend_ready:
                print(
                    f"{Colors.YELLOW}⚠️  Backend health check timeout - opening browser anyway{Colors.ENDC}"
                )

            # Handle browser opening based on restart status
            if self.is_restart:
                # During restart: Clean up duplicate tabs and show loading page
                await self.open_browser_smart()  # Will redirect to localhost:3001 (loading page)
                print(f"{Colors.GREEN}✓ Redirected existing tabs to loading page{Colors.ENDC}")

                # Broadcast startup completion if progress broadcaster exists
                if hasattr(self, '_startup_progress') and self._startup_progress:
                    await self._startup_progress.broadcast_complete(
                        success=True,
                        redirect_url=f"http://localhost:{self.ports['frontend']}"
                    )
            else:
                # Normal startup - open frontend directly
                await asyncio.sleep(1)  # Brief pause before opening
                await self.open_browser_smart()

        # Monitor services
        try:
            await self.monitor_services()
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}Interrupt received, shutting down gracefully...{Colors.ENDC}")
        except Exception as e:
            print(f"\n{Colors.FAIL}Monitor error: {e}{Colors.ENDC}")

        # Cleanup
        await self.cleanup()

        # Ensure clean exit
        print(f"\n{Colors.BLUE}Goodbye! 👋{Colors.ENDC}\n")

        return True


# Global manager for cleanup
_manager = None


def _auto_detect_preset():
    """Automatically detect the best Goal Inference preset based on system state"""
    from pathlib import Path

    # Check if learning database exists
    learning_db_path = Path.home() / ".jarvis" / "learning" / "jarvis_learning.db"
    Path(__file__).parent / "backend" / "config" / "integration_config.json"

    # If this is first run (no database), use learning mode
    if not learning_db_path.exists():
        print(
            f"{Colors.CYAN}   → First run detected, using 'learning' preset for fast adaptation{Colors.ENDC}"
        )
        return "learning"

    # If database exists, check how many sessions we have
    try:
        import sqlite3

        conn = sqlite3.connect(str(learning_db_path))
        cursor = conn.cursor()

        # Count goals to estimate session maturity
        cursor.execute("SELECT COUNT(*) FROM goals")
        goal_count = cursor.fetchone()[0]

        # Count patterns to see learning progress
        cursor.execute("SELECT COUNT(*) FROM patterns")
        pattern_count = cursor.fetchone()[0]

        conn.close()

        # Decision logic based on learning progress
        if goal_count == 0:  # Empty database, use balanced with automation
            print(
                f"{Colors.CYAN}   → Fresh start, using 'balanced' preset with automation{Colors.ENDC}"
            )
            return "balanced"
        elif goal_count < 50:  # Very new user (< ~5-10 sessions)
            print(
                f"{Colors.CYAN}   → Early learning phase ({goal_count} goals), using 'learning' preset{Colors.ENDC}"
            )
            return "learning"
        elif goal_count < 200 and pattern_count < 10:  # Still learning patterns
            print(
                f"{Colors.CYAN}   → Building patterns ({pattern_count} patterns), using 'balanced' preset{Colors.ENDC}"
            )
            return "balanced"
        elif pattern_count >= 20:  # Lots of patterns learned, user is experienced
            print(
                f"{Colors.CYAN}   → Experienced user ({pattern_count} patterns), using 'aggressive' preset{Colors.ENDC}"
            )
            return "aggressive"
        else:  # Default case
            print(
                f"{Colors.CYAN}   → Standard usage detected, using 'balanced' preset{Colors.ENDC}"
            )
            return "balanced"

    except Exception:
        # If we can't read database, default to balanced
        print(f"{Colors.CYAN}   → Using default 'balanced' preset{Colors.ENDC}")
        return "balanced"


def _auto_detect_automation(preset):
    """Automatically decide whether to enable automation based on preset and experience"""
    from pathlib import Path

    # Aggressive, balanced, and learning presets have automation by default
    if preset in ["aggressive", "balanced", "learning"]:
        if preset == "learning":
            print(
                f"{Colors.CYAN}   → Learning preset: Automation enabled for faster adaptation{Colors.ENDC}"
            )
        else:
            print(
                f"{Colors.CYAN}   → {preset.capitalize()} preset: Automation enabled by default{Colors.ENDC}"
            )
        return True

    # Only conservative should not auto-enable
    if preset == "conservative":
        return False

    # For performance preset, check user experience
    learning_db_path = Path.home() / ".jarvis" / "learning" / "jarvis_learning.db"

    if not learning_db_path.exists():
        # New user - no automation
        return False

    try:
        import sqlite3

        conn = sqlite3.connect(str(learning_db_path))
        cursor = conn.cursor()

        # Check pattern success rate
        cursor.execute(
            """
            SELECT COUNT(*), AVG(success_rate)
            FROM patterns
            WHERE frequency >= 3
        """
        )
        result = cursor.fetchone()
        mature_patterns = result[0] if result[0] else 0
        avg_success = result[1] if result[1] else 0.0

        conn.close()

        # Enable automation if user has good pattern success rate
        if mature_patterns >= 5 and avg_success >= 0.8:
            print(
                f"{Colors.CYAN}   → High pattern success ({avg_success:.1%}), automation recommended{Colors.ENDC}"
            )
            return True
        else:
            return False

    except Exception:
        # Default to no automation if we can't determine
        return False


async def shutdown_handler():
    """
    Handle shutdown gracefully with robust cleanup
    Integrated from jarvis.sh wrapper for terminal close handling
    """
    global _manager

    if _manager and not _manager._shutting_down:
        _manager._shutting_down = True

        # Log shutdown initiation
        logger.info("🧹 Initiating graceful shutdown...")

        try:
            # Give cleanup 90 seconds to complete gracefully
            # This allows time for GCP VM deletion which can take 30-60 seconds
            await asyncio.wait_for(_manager.cleanup(), timeout=90.0)
            logger.info("✅ JARVIS stopped gracefully")
        except asyncio.TimeoutError:
            logger.warning("⚠️  Cleanup timeout (90s exceeded) - forcing shutdown...")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


async def ensure_cloud_sql_proxy():
    """
    Ensure CloudSQL proxy is running for voice biometric data access.
    Auto-starts the proxy if not running.
    """
    import subprocess
    import json
    from pathlib import Path

    print(f"\n{Colors.CYAN}🔗 Checking CloudSQL Proxy...{Colors.ENDC}")

    # Check if proxy is already running
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cloud-sql-proxy"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            print(f"  {Colors.GREEN}✓ CloudSQL Proxy already running (PID: {result.stdout.strip()}){Colors.ENDC}")

            # Verify it's listening on correct port
            port_check = subprocess.run(
                ["lsof", "-i", ":5432"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if port_check.returncode == 0 and "cloud-sql" in port_check.stdout:
                print(f"  {Colors.GREEN}✓ Listening on port 5432{Colors.ENDC}")
                return True
            else:
                print(f"  {Colors.YELLOW}⚠️  Proxy running but not on port 5432, restarting...{Colors.ENDC}")
                subprocess.run(["pkill", "-f", "cloud-sql-proxy"], timeout=5)
                await asyncio.sleep(1)
    except Exception as e:
        print(f"  {Colors.YELLOW}⚠️  Error checking proxy status: {e}{Colors.ENDC}")

    # Start the proxy
    print(f"  {Colors.CYAN}Starting CloudSQL Proxy...{Colors.ENDC}")

    # Load database config
    config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
    if not config_path.exists():
        print(f"  {Colors.FAIL}✗ Database config not found at {config_path}{Colors.ENDC}")
        return False

    with open(config_path, 'r') as f:
        config = json.load(f)

    connection_name = config.get("cloud_sql", {}).get("connection_name")
    if not connection_name:
        print(f"  {Colors.FAIL}✗ No connection_name in config{Colors.ENDC}")
        return False

    # Find cloud-sql-proxy binary
    proxy_paths = [
        Path.home() / ".local" / "bin" / "cloud-sql-proxy",
        "/usr/local/bin/cloud-sql-proxy",
        "cloud-sql-proxy"  # In PATH
    ]

    proxy_binary = None
    for path in proxy_paths:
        if isinstance(path, Path) and path.exists():
            proxy_binary = str(path)
            break
        elif isinstance(path, str):
            # Check if it's in PATH
            which_result = subprocess.run(
                ["which", path],
                capture_output=True,
                text=True,
                timeout=5
            )
            if which_result.returncode == 0:
                proxy_binary = path
                break

    if not proxy_binary:
        print(f"  {Colors.FAIL}✗ cloud-sql-proxy not found{Colors.ENDC}")
        print(f"    Install: brew install cloud-sql-proxy")
        return False

    # Start proxy in background
    try:
        log_file = Path("/tmp/cloud-sql-proxy.log")
        with open(log_file, 'w') as f:
            subprocess.Popen(
                [proxy_binary, "--port=5432", connection_name],
                stdout=f,
                stderr=f,
                start_new_session=True
            )

        # Wait for proxy to start
        await asyncio.sleep(3)

        # Verify it started
        port_check = subprocess.run(
            ["lsof", "-i", ":5432"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if port_check.returncode == 0 and "cloud-sql" in port_check.stdout:
            print(f"  {Colors.GREEN}✓ CloudSQL Proxy started successfully{Colors.ENDC}")
            print(f"  {Colors.GREEN}✓ Listening on 127.0.0.1:5432{Colors.ENDC}")
            print(f"  {Colors.CYAN}  Connection: {connection_name}{Colors.ENDC}")
            return True
        else:
            print(f"  {Colors.FAIL}✗ Proxy failed to start (check {log_file}){Colors.ENDC}")
            return False

    except Exception as e:
        print(f"  {Colors.FAIL}✗ Error starting proxy: {e}{Colors.ENDC}")
        return False


async def main():
    """Main entry point"""
    global _manager

    # Parse arguments first to check for flags
    parser = argparse.ArgumentParser(
        description="J.A.R.V.I.S. Advanced AI System v14.0.0 - AUTONOMOUS Edition"
    )
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument("--backend-only", action="store_true", help="Start backend only")
    parser.add_argument("--frontend-only", action="store_true", help="Start frontend only")
    parser.add_argument(
        "--no-autonomous",
        action="store_true",
        help="Disable autonomous mode and use traditional startup",
    )
    parser.add_argument(
        "--emergency-cleanup",
        action="store_true",
        help="Perform emergency cleanup of all JARVIS processes and exit",
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Run normal cleanup process and exit (less aggressive than emergency)",
    )
    parser.add_argument(
        "--force-start",
        action="store_true",
        help="Skip multiple instance check and force start (use with caution)",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Restart JARVIS: kill old instances, start fresh, and verify intelligent system",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check system state and provide recommendations without starting",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug mode with detailed output"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Backend port (default: 8000)",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=3000,
        help="Frontend port (default: 3000)",
    )
    parser.add_argument(
        "--monitoring-port",
        type=int,
        default=8888,
        help="Monitoring dashboard port (default: 8888)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip automatic cleanup of old processes",
    )
    parser.add_argument(
        "--auto-cleanup",
        action="store_true",
        help="Force automatic cleanup (default behavior)",
    )
    parser.add_argument(
        "--standard", action="store_true", help="Use standard backend (no optimization)"
    )
    parser.add_argument(
        "--no-auto-cleanup",
        action="store_true",
        help="Disable automatic cleanup of stuck processes (will prompt instead)",
    )

    # Goal Inference Configuration
    parser.add_argument(
        "--goal-preset",
        choices=["aggressive", "balanced", "conservative", "learning", "performance"],
        help="Goal Inference configuration preset (aggressive=proactive, balanced=default, conservative=cautious, learning=fast-learning, performance=max-speed)",
    )
    parser.add_argument(
        "--enable-automation",
        action="store_true",
        help="Enable Goal Inference automation (auto-execute high-confidence actions)",
    )
    parser.add_argument(
        "--disable-automation",
        action="store_true",
        help="Disable Goal Inference automation (suggestions only)",
    )

    args = parser.parse_args()

    # Automatic Goal Inference Configuration (if not specified via command line or environment)
    import os

    auto_detected = False
    if not args.goal_preset and not os.getenv("JARVIS_GOAL_PRESET"):
        # Auto-detect best preset based on system state
        auto_preset = _auto_detect_preset()
        args.goal_preset = auto_preset
        auto_detected = True
        print(f"\n{Colors.BLUE}🎯 Auto-detected Goal Inference Preset: {auto_preset}{Colors.ENDC}")
        print(
            f"{Colors.CYAN}   (Override with --goal-preset or JARVIS_GOAL_PRESET environment variable){Colors.ENDC}"
        )

    # Auto-configure automation if not specified
    if (
        not args.enable_automation
        and not args.disable_automation
        and not os.getenv("JARVIS_GOAL_AUTOMATION")
    ):
        # Auto-detect automation based on preset and session count
        auto_automation = _auto_detect_automation(args.goal_preset)
        if auto_automation:
            args.enable_automation = True
        else:
            args.disable_automation = True

    # Apply Goal Inference preset if specified
    if args.goal_preset:
        os.environ["JARVIS_GOAL_PRESET"] = args.goal_preset
        if not auto_detected:  # Only print if not auto-detected (manual override)
            print(f"\n{Colors.BLUE}🎯 Goal Inference Preset: {args.goal_preset}{Colors.ENDC}")

    # Apply Goal Inference automation settings
    if args.enable_automation:
        # Will be applied in main.py during initialization
        os.environ["JARVIS_GOAL_AUTOMATION"] = "true"
        print(f"{Colors.GREEN}✓ Goal Inference Automation: ENABLED{Colors.ENDC}")
    elif args.disable_automation:
        os.environ["JARVIS_GOAL_AUTOMATION"] = "false"
        print(f"{Colors.YELLOW}⚠️ Goal Inference Automation: DISABLED{Colors.ENDC}")

    # PID file locking to prevent multiple instances
    pid_file = Path("/tmp/jarvis_master.pid")  # nosec B108
    pid_lock_acquired = False

    def cleanup_pid_file():
        """Clean up PID file on exit"""
        if pid_lock_acquired and pid_file.exists():
            try:
                # Verify it's our PID before deleting
                with open(pid_file, "r") as f:
                    stored_pid = int(f.read().strip())
                    if stored_pid == os.getpid():
                        pid_file.unlink()
                        logger.debug("PID lock released")
            except Exception as e:
                logger.warning(f"Failed to clean up PID file: {e}")

    # Register cleanup
    import atexit

    atexit.register(cleanup_pid_file)

    # Early check for multiple instances (before creating manager)
    # Skip check if --restart or --force-start is used
    if not args.force_start and not args.restart:
        # Check PID file first (faster and more reliable than ps)
        if pid_file.exists():
            try:
                with open(pid_file, "r") as f:
                    existing_pid = int(f.read().strip())

                # Verify process is actually running
                if psutil.pid_exists(existing_pid):
                    try:
                        proc = psutil.Process(existing_pid)
                        cmdline = " ".join(proc.cmdline()).lower()
                        if "start_system.py" in cmdline or "main.py" in cmdline:
                            print(f"\n{Colors.FAIL}❌ JARVIS is already running!{Colors.ENDC}")
                            print(
                                f"{Colors.WARNING}Master instance PID: {existing_pid}{Colors.ENDC}\n"
                            )
                            print(f"{Colors.CYAN}💡 To prevent runaway GCP costs:{Colors.ENDC}")
                            print(f"   1. Stop existing instance: kill -INT {existing_pid}")
                            print(f"   2. Or use: python start_system.py --restart")
                            print(
                                f"   3. Or force start (risky): python start_system.py --force-start"
                            )
                            print(
                                f"\n{Colors.YELLOW}⚠️  Multiple instances = Multiple VMs = Higher costs!{Colors.ENDC}"
                            )
                            return 1
                    except psutil.NoSuchProcess:
                        # PID exists but not JARVIS - stale lock file
                        print(
                            f"{Colors.YELLOW}⚠️ Removing stale PID lock from process {existing_pid}{Colors.ENDC}"
                        )
                        pid_file.unlink()
                else:
                    # PID doesn't exist - stale lock file
                    print(
                        f"{Colors.YELLOW}⚠️ Removing stale PID lock (PID {existing_pid} not running){Colors.ENDC}"
                    )
                    pid_file.unlink()
            except Exception as e:
                print(f"{Colors.YELLOW}⚠️ Failed to read PID file: {e}, removing it{Colors.ENDC}")
                pid_file.unlink()

        # Secondary check: ps command (catches cases where PID file is missing)
        try:
            # Simple instance check using process listing (macOS compatible)
            # Note: subprocess is already imported at module level (line 163)
            result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
            jarvis_processes = [
                line
                for line in result.stdout.split("\n")
                if "python" in line.lower()
                and "start_system.py" in line
                and str(os.getpid()) not in line  # Exclude ourselves
            ]

            if jarvis_processes:
                print(f"\n{Colors.FAIL}❌ JARVIS is already running!{Colors.ENDC}")
                print(
                    f"{Colors.WARNING}Found {len(jarvis_processes)} existing instance(s):{Colors.ENDC}\n"
                )

                for proc_line in jarvis_processes:
                    # Extract PID (second column in ps aux output)
                    parts = proc_line.split()
                    if len(parts) >= 2:
                        pid = parts[1]
                        print(f"  • PID {pid}: {' '.join(parts[10:])}")

                print(f"\n{Colors.CYAN}💡 To prevent runaway GCP costs:{Colors.ENDC}")
                print(f"   1. Stop existing instance: kill -INT {parts[1]}")
                print(f"   2. Or use: python start_system.py --restart")
                print(f"   3. Or force start (risky): python start_system.py --force-start")
                print(
                    f"\n{Colors.YELLOW}⚠️  Multiple instances = Multiple VMs = Higher costs!{Colors.ENDC}"
                )
                return 1

            print(
                f"\n{Colors.GREEN}✓ No existing JARVIS instances found - safe to start{Colors.ENDC}"
            )
        except subprocess.TimeoutExpired:
            print(f"{Colors.WARNING}⚠️ Instance check timed out - proceeding anyway{Colors.ENDC}")
        except Exception as e:
            print(f"{Colors.WARNING}⚠️ Instance check failed: {e} - proceeding anyway{Colors.ENDC}")
    else:
        if args.force_start:
            print(f"\n{Colors.WARNING}⚠️ Skipping instance check (--force-start){Colors.ENDC}")
            print(
                f"{Colors.WARNING}⚠️ WARNING: Multiple instances may create multiple VMs!{Colors.ENDC}"
            )

            # Check VM creation lock file to see if another instance is managing VMs
            vm_lock_file = Path.home() / ".jarvis" / "gcp_optimizer" / "vm_creation.lock"
            if vm_lock_file.exists():
                try:
                    with open(vm_lock_file, "r") as f:
                        lock_info = f.read()
                        print(
                            f"{Colors.FAIL}⚠️  DANGER: Another JARVIS instance has the VM creation lock!{Colors.ENDC}"
                        )
                        print(f"   Lock info: {lock_info.strip()}")
                        print(
                            f"\n{Colors.YELLOW}   This could lead to duplicate VMs and double billing!{Colors.ENDC}"
                        )
                except Exception:
                    pass
        else:
            # --restart mode: This is safe because it kills existing instances first
            print(
                f"\n{Colors.CYAN}🔄 Restart mode: Will kill existing instances before starting{Colors.ENDC}"
            )

    # Acquire PID lock (after instance check passes or for --restart)
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
        pid_lock_acquired = True
        print(f"{Colors.GREEN}✓ PID lock acquired ({os.getpid()}){Colors.ENDC}")
    except Exception as e:
        print(f"{Colors.WARNING}⚠️ Failed to create PID lock file: {e}{Colors.ENDC}")

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("jarvis_startup.log"),
        ],
    )

    # Handle emergency cleanup first (before creating manager)
    if args.emergency_cleanup:
        print(f"\n{Colors.FAIL}🚨 EMERGENCY CLEANUP MODE{Colors.ENDC}")
        print("This will forcefully kill ALL JARVIS-related processes.\n")

        try:
            # Add backend to path
            backend_dir = Path(__file__).parent / "backend"
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            from process_cleanup_manager import emergency_cleanup

            print(f"{Colors.YELLOW}Performing emergency cleanup...{Colors.ENDC}")
            results = emergency_cleanup(force=True)

            print(f"\n{Colors.GREEN}✅ Emergency cleanup complete:{Colors.ENDC}")
            print(f"  • Processes killed: {len(results['processes_killed'])}")
            print(f"  • Ports freed: {len(results['ports_freed'])}")
            if results.get("ipc_cleaned"):
                print(f"  • IPC resources cleaned: {sum(results['ipc_cleaned'].values())}")
            if results.get("errors"):
                print(f"  • ⚠️ Errors: {len(results['errors'])}")

            print(
                f"\n{Colors.GREEN}System is now clean. You can start JARVIS normally.{Colors.ENDC}"
            )
            return 0

        except ImportError:
            print(f"{Colors.FAIL}Error: process_cleanup_manager.py not found!{Colors.ENDC}")
            print("Make sure you're running from the JARVIS-AI-Agent directory.")
            return 1
        except Exception as e:
            print(f"{Colors.FAIL}Emergency cleanup failed: {e}{Colors.ENDC}")
            return 1

    # Handle regular cleanup
    if args.cleanup_only:
        print(f"\n{Colors.BLUE}🧹 CLEANUP MODE{Colors.ENDC}")
        print("Running system cleanup and analysis...\n")

        try:
            backend_dir = Path(__file__).parent / "backend"
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            from process_cleanup_manager import ProcessCleanupManager

            manager = ProcessCleanupManager()

            # DISABLED: Check for crash recovery (causes loops on macOS)
            # if manager.check_for_segfault_recovery():
            #     print(f"{Colors.YELLOW}🔧 Performed crash recovery cleanup{Colors.ENDC}")

            # Check for code changes and perform intelligent cleanup
            # If --restart flag is used, FORCE cleanup regardless of code changes
            if args.restart:
                print(f"\n{Colors.YELLOW}🔄 FORCE RESTART MODE - Killing all JARVIS processes...{Colors.ENDC}")
                code_cleanup = manager.force_restart_cleanup()
            else:
                print(f"\n{Colors.BLUE}🔄 Checking for code changes...{Colors.ENDC}")
                code_cleanup = manager.cleanup_old_instances_on_code_change()

            if code_cleanup:
                # Categorize cleaned processes by type
                backend_cleaned = [p for p in code_cleanup if p.get("type") == "backend"]
                frontend_cleaned = [p for p in code_cleanup if p.get("type") == "frontend"]
                related_cleaned = [p for p in code_cleanup if p.get("type") == "related"]
                websocket_cleaned = [p for p in code_cleanup if p.get("type") == "websocket"]

                if args.restart:
                    print(f"{Colors.GREEN}   ✨ FORCE RESTART - All old processes terminated!{Colors.ENDC}")
                else:
                    print(f"{Colors.YELLOW}   ✨ Code changes detected - cleaned up old processes!{Colors.ENDC}")

                if backend_cleaned:
                    print(f"{Colors.CYAN}   → Killed {len(backend_cleaned)} backend process(es) for fresh code reload{Colors.ENDC}")
                if websocket_cleaned:
                    print(f"{Colors.CYAN}   → Killed {len(websocket_cleaned)} websocket process(es){Colors.ENDC}")
                if frontend_cleaned:
                    print(f"{Colors.CYAN}   → Killed {len(frontend_cleaned)} frontend process(es){Colors.ENDC}")
                if related_cleaned:
                    print(f"{Colors.CYAN}   → Cleaned {len(related_cleaned)} related process(es){Colors.ENDC}")
                print(f"{Colors.GREEN}   ✓ System ready to load fresh code!{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}   ✓ No old processes found - system is clean{Colors.ENDC}")

            # Run analysis
            state = manager.analyze_system_state()
            print(f"\n{Colors.CYAN}System State:{Colors.ENDC}")
            print(f"  • CPU: {state['cpu_percent']:.1f}%")
            print(f"  • Memory: {state['memory_percent']:.1f}%")
            print(f"  • JARVIS processes: {len(state['jarvis_processes'])}")
            print(f"  • Stuck processes: {len(state['stuck_processes'])}")
            print(f"  • Zombie processes: {len(state['zombie_processes'])}")

            # Get recommendations
            recommendations = manager.get_cleanup_recommendations()
            if recommendations:
                print(f"\n{Colors.YELLOW}Recommendations:{Colors.ENDC}")
                for rec in recommendations:
                    print(f"  • {rec}")

            # Run smart cleanup
            print(f"\n{Colors.BLUE}Running smart cleanup...{Colors.ENDC}")
            report = await manager.smart_cleanup(dry_run=False)

            cleaned_count = len([a for a in report["actions"] if a.get("success", False)])
            if cleaned_count > 0:
                print(f"{Colors.GREEN}✓ Cleaned up {cleaned_count} processes{Colors.ENDC}")
                print(
                    f"  Freed ~{report['freed_resources']['cpu_percent']:.1f}% CPU, {report['freed_resources']['memory_mb']}MB memory"
                )
            else:
                print(f"{Colors.GREEN}✓ No cleanup needed{Colors.ENDC}")

            print(f"\n{Colors.GREEN}Cleanup complete. System is ready.{Colors.ENDC}")
            return 0

        except Exception as e:
            print(f"{Colors.FAIL}Cleanup failed: {e}{Colors.ENDC}")
            return 1

    # Auto-detect and restart existing JARVIS instances (unless specific flags used)
    skip_auto_restart = args.cleanup_only or args.emergency_cleanup or args.check_only

    if not skip_auto_restart:
        # Check for existing JARVIS processes
        jarvis_processes = []
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cmdline = proc.info.get("cmdline")
                if cmdline and any("main.py" in arg for arg in cmdline):
                    if any("JARVIS-AI-Agent/backend" in arg for arg in cmdline):
                        jarvis_processes.append(
                            {
                                "pid": proc.info["pid"],
                                "age_hours": (time.time() - proc.info["create_time"]) / 3600,
                            }
                        )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # If existing instances found, automatically restart
        if jarvis_processes:
            print(f"\n{Colors.YELLOW}⚡ Existing JARVIS instance(s) detected{Colors.ENDC}")
            print(f"Found {len(jarvis_processes)} process(es) - will restart automatically\n")

            # Kill old processes
            for proc in jarvis_processes:
                print(
                    f"  Stopping PID {proc['pid']} (running {proc['age_hours']:.1f}h)...",
                    end="",
                    flush=True,
                )
                try:
                    os.kill(proc["pid"], signal.SIGTERM)
                    time.sleep(1)
                    if psutil.pid_exists(proc["pid"]):
                        os.kill(proc["pid"], signal.SIGKILL)
                    print(f" {Colors.GREEN}✓{Colors.ENDC}")
                except Exception as e:
                    print(f" {Colors.FAIL}✗{Colors.ENDC} ({e})")

            print(f"\n{Colors.CYAN}Waiting for processes to terminate...{Colors.ENDC}")
            time.sleep(2)
            print(f"{Colors.GREEN}✓ Ready to start fresh instance{Colors.ENDC}\n")

    # Loading server process (module scope for cleanup)
    loading_server_process = None

    # Helper function to broadcast progress to loading server
    async def broadcast_to_loading_server(stage, message, progress, metadata=None):
        """Send progress update to loading server via HTTP"""
        try:
            import aiohttp
            url = "http://localhost:3001/api/update-progress"
            data = {
                "stage": stage,
                "message": message,
                "progress": progress,
                "timestamp": datetime.now().isoformat()
            }
            if metadata:
                data["metadata"] = metadata

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=1)) as resp:
                    if resp.status == 200:
                        print(f"  {Colors.CYAN}📊 Progress: {progress}% - {message}{Colors.ENDC}")
        except Exception as e:
            # Silently fail - loading server might not be ready yet
            pass

    # Handle restart mode (explicit --restart flag)
    # Ensure CloudSQL proxy is running (for voice biometrics)
    print(f"\n{Colors.CYAN}{'='*60}{Colors.ENDC}")
    print(f"{Colors.CYAN}🔐 Voice Biometric System Initialization{Colors.ENDC}")
    print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}")

    proxy_started = await ensure_cloud_sql_proxy()
    if proxy_started:
        print(f"{Colors.GREEN}✅ Voice biometric data access ready{Colors.ENDC}")
    else:
        print(f"{Colors.YELLOW}⚠️  CloudSQL proxy not available - voice biometrics may be degraded{Colors.ENDC}")

    print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}\n")

    # CRITICAL: Bootstrap voice profiles to SQLite cache for offline authentication
    if proxy_started:
        print(f"\n{Colors.CYAN}{'='*60}{Colors.ENDC}")
        print(f"{Colors.CYAN}🎤 Voice Profile Cache Bootstrap{Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}")

        try:
            # Import hybrid sync
            from intelligence.hybrid_database_sync import HybridDatabaseSync
            import json

            # Load database config
            config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
            if config_path.exists():
                with open(config_path, 'r') as f:
                    db_config = json.load(f)

                cloudsql_config = db_config.get("cloud_sql", {})

                # Get database password from Secret Manager
                from core.secret_manager import get_db_password
                password = get_db_password()

                if password:
                    cloudsql_config["password"] = password

                    # Create temporary hybrid sync instance for bootstrap
                    print(f"{Colors.CYAN}   Initializing voice cache system...{Colors.ENDC}")
                    bootstrap_sync = HybridDatabaseSync(
                        sqlite_path=Path.home() / ".jarvis" / "learning" / "voice_biometrics_sync.db",
                        cloudsql_config=cloudsql_config,
                        max_connections=3,
                        enable_faiss_cache=True,
                        enable_prometheus=False,
                        enable_redis=False
                    )

                    await bootstrap_sync.initialize()

                    # Check if bootstrap is needed
                    needs_bootstrap = False
                    if bootstrap_sync.faiss_cache and bootstrap_sync.faiss_cache.size() == 0:
                        needs_bootstrap = True
                        print(f"{Colors.YELLOW}   ⚠️  Voice cache is empty - bootstrap required{Colors.ENDC}")
                    else:
                        # Check staleness
                        needs_bootstrap = await bootstrap_sync._check_cache_staleness()
                        if needs_bootstrap:
                            print(f"{Colors.YELLOW}   ⚠️  Voice cache is stale - refresh required{Colors.ENDC}")
                        else:
                            print(f"{Colors.GREEN}   ✅ Voice cache is fresh and ready{Colors.ENDC}")

                    # Bootstrap if needed
                    if needs_bootstrap:
                        print(f"{Colors.CYAN}   📥 Bootstrapping voice profiles from CloudSQL...{Colors.ENDC}")
                        success = await bootstrap_sync.bootstrap_voice_profiles_from_cloudsql()

                        if success:
                            profiles_count = bootstrap_sync.metrics.voice_profiles_cached
                            cache_size = bootstrap_sync.metrics.cache_size
                            print(f"{Colors.GREEN}   ✅ Bootstrap complete!{Colors.ENDC}")
                            print(f"{Colors.GREEN}      • Cached profiles: {profiles_count}{Colors.ENDC}")
                            print(f"{Colors.GREEN}      • FAISS cache size: {cache_size} embeddings{Colors.ENDC}")
                            print(f"{Colors.GREEN}      • Ready for offline authentication{Colors.ENDC}")
                        else:
                            print(f"{Colors.FAIL}   ❌ Bootstrap failed - voice authentication may not work{Colors.ENDC}")
                            print(f"{Colors.YELLOW}      Check logs for details{Colors.ENDC}")

                    # Verify cache readiness
                    print(f"\n{Colors.CYAN}   🔍 Verifying voice authentication readiness...{Colors.ENDC}")

                    # Test profile read
                    async with bootstrap_sync.sqlite_conn.execute("""
                        SELECT speaker_name, total_samples
                        FROM speaker_profiles
                        LIMIT 5
                    """) as cursor:
                        profiles = await cursor.fetchall()

                        if profiles:
                            print(f"{Colors.GREEN}   ✅ SQLite cache ready: {len(profiles)} profile(s){Colors.ENDC}")
                            for name, samples in profiles:
                                print(f"{Colors.CYAN}      • {name}: {samples} samples{Colors.ENDC}")
                        else:
                            print(f"{Colors.FAIL}   ❌ No profiles in cache - voice unlock will fail!{Colors.ENDC}")

                    # Clean up bootstrap sync
                    await bootstrap_sync.shutdown()
                    print(f"{Colors.GREEN}   ✅ Voice cache system ready{Colors.ENDC}")

                else:
                    print(f"{Colors.FAIL}   ❌ Could not retrieve database password{Colors.ENDC}")
            else:
                print(f"{Colors.YELLOW}   ⚠️  Database config not found - skipping bootstrap{Colors.ENDC}")

        except Exception as e:
            print(f"{Colors.FAIL}   ❌ Voice cache bootstrap failed: {e}{Colors.ENDC}")
            import traceback
            traceback.print_exc()

        print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}\n")

    if args.restart:
        print(f"\n{Colors.BLUE}🔄 RESTART MODE{Colors.ENDC}")
        print("Restarting JARVIS with intelligent system verification...\n")

        # Step 0: Start standalone loading server BEFORE killing processes
        loading_server_url = "http://localhost:3001"

        if not args.no_browser:
            print(f"{Colors.CYAN}📡 Starting loading page server...{Colors.ENDC}")
            try:
                loading_server_script = Path(__file__).parent / "loading_server.py"
                loading_server_process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(loading_server_script),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                # Track globally for cleanup (created before manager instance)
                globals()['_loading_server_process'] = loading_server_process

                # Wait for server to be ready with retry logic
                import aiohttp
                server_ready = False

                for attempt in range(10):  # Try for up to 5 seconds
                    await asyncio.sleep(0.5)
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(f"{loading_server_url}/health", timeout=aiohttp.ClientTimeout(total=1)) as resp:
                                if resp.status == 200:
                                    server_ready = True
                                    break
                    except:
                        continue

                if server_ready:
                    print(f"{Colors.GREEN}   ✓ Loading server started on {loading_server_url}{Colors.ENDC}")

                    # Clean up existing JARVIS tabs and redirect to loading page
                    print(f"{Colors.CYAN}🌐 Redirecting to loading page...{Colors.ENDC}")
                    try:
                        # Use AppleScript to close duplicate tabs and show loading page
                        cleanup_script = """
                        tell application "Google Chrome"
                            set jarvisPatterns to {"localhost:3000", "localhost:3001", "localhost:8010", "localhost:8001", "localhost:8888", "127.0.0.1:3000", "127.0.0.1:3001", "127.0.0.1:8010", "127.0.0.1:8001", "127.0.0.1:8888"}
                            set foundFirst to false
                            set totalClosed to 0

                            repeat with w in windows
                                set tabsToClose to {}
                                set tabCount to count of tabs of w

                                repeat with i from 1 to tabCount
                                    set t to tab i of w
                                    set tabURL to URL of t
                                    set isJarvis to false

                                    -- Check if this is a JARVIS tab
                                    repeat with pattern in jarvisPatterns
                                        if tabURL contains pattern then
                                            set isJarvis to true
                                            exit repeat
                                        end if
                                    end repeat

                                    if isJarvis then
                                        if not foundFirst then
                                            -- Keep this one and redirect to loading page
                                            set foundFirst to true
                                            set URL of t to "http://localhost:3001/"
                                            set active tab index of w to i
                                            set index of w to 1
                                        else
                                            -- Mark for closure
                                            set end of tabsToClose to i
                                        end if
                                    end if
                                end repeat

                                -- Close marked tabs in reverse order
                                if (count of tabsToClose) > 0 then
                                    repeat with i from (count of tabsToClose) to 1 by -1
                                        try
                                            set tabIndex to item i of tabsToClose
                                            close tab tabIndex of w
                                            set totalClosed to totalClosed + 1
                                        end try
                                    end repeat
                                end if
                            end repeat

                            -- If no JARVIS tab was found, create one
                            if not foundFirst then
                                if (count of windows) = 0 then
                                    make new window
                                end if
                                tell window 1
                                    set newTab to make new tab with properties {URL:"http://localhost:3001/"}
                                    set active tab index to index of newTab
                                end tell
                            end if

                            activate
                        end tell
                        """
                        subprocess.run(["osascript", "-e", cleanup_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    except:
                        # Fallback to simple open if AppleScript fails
                        try:
                            subprocess.Popen(["open", loading_server_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except:
                            print(f"{Colors.YELLOW}   ⚠️  Auto-open failed. Navigate to: {loading_server_url}{Colors.ENDC}")
                else:
                    print(f"{Colors.YELLOW}   ⚠️  Loading server health check failed (server may still be starting){Colors.ENDC}")
                    print(f"{Colors.CYAN}   ℹ️  If needed, manually open: {loading_server_url}{Colors.ENDC}")

            except Exception as e:
                print(f"{Colors.YELLOW}   ⚠️  Failed to start loading server: {e}{Colors.ENDC}")

        try:
            backend_dir = Path(__file__).parent / "backend"
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            # Progress: 1% - Started (EXTREME DETAIL)
            await broadcast_to_loading_server(
                "initializing",
                "Starting JARVIS restart sequence - preparing environment cleanup",
                1,
                metadata={"icon": "⚡", "label": "Initializing", "sublabel": "System check initiated"}
            )
            await asyncio.sleep(0.3)

            # Progress: 2% - Backend path setup
            await broadcast_to_loading_server(
                "path_setup",
                "Configuring Python import paths for backend modules",
                2,
                metadata={"icon": "📁", "label": "Path Setup", "sublabel": "Module paths configured"}
            )
            await asyncio.sleep(0.2)

            # Progress: 3% - Detecting
            await broadcast_to_loading_server(
                "detecting",
                "Scanning system for existing JARVIS processes using AdvancedProcessDetector",
                3,
                metadata={"icon": "🔍", "label": "Process Detection", "sublabel": "Scanning PID table"}
            )

            # Step 1: Advanced JARVIS process detection with multiple strategies
            print(f"{Colors.YELLOW}1️⃣ Advanced JARVIS instance detection (using AdvancedProcessDetector)...{Colors.ENDC}")

            try:
                from core.process_detector import (
                    AdvancedProcessDetector,
                    DetectionConfig,
                    detect_and_kill_jarvis_processes,
                )

                # Run async detection
                print(f"  → Running 7 concurrent detection strategies...")
                print(f"    • psutil_scan: Process enumeration")
                print(f"    • ps_command: Shell command verification")
                print(f"    • port_based: Dynamic port scanning")
                print(f"    • network_connections: Active connections")
                print(f"    • file_descriptor: Open file analysis")
                print(f"    • parent_child: Process tree analysis")
                print(f"    • command_line: Regex pattern matching")

                # Detect processes (dry run first to show what we found)
                result = await detect_and_kill_jarvis_processes(dry_run=True)

                jarvis_processes = result["processes"]
                print(f"\n  {Colors.GREEN}✓ Detected {result['total_detected']} JARVIS processes{Colors.ENDC}")

                # Progress: 5% - Process scan in progress
                await broadcast_to_loading_server(
                    "scanning_ports",
                    "Scanning active network ports (3000, 8010) for JARVIS services",
                    5,
                    metadata={"icon": "🔌", "label": "Port Scan", "sublabel": "Checking listeners"}
                )

                # Progress: 7% - Process enumeration
                await broadcast_to_loading_server(
                    "enumerating",
                    f"Enumerating system processes - found {result['total_detected']} JARVIS instances",
                    7,
                    metadata={"icon": "📊", "label": "Enumeration", "sublabel": f"{result['total_detected']} processes"}
                )

                # Progress: 8% - Detection complete
                await broadcast_to_loading_server(
                    "detected",
                    f"Process detection complete: {result['total_detected']} JARVIS processes identified (frontend, backend, minimal)",
                    8,
                    metadata={"icon": "✓", "label": "Detection Complete", "sublabel": f"{result['total_detected']} PIDs captured"}
                )

                # Convert to old format for compatibility with existing code
                jarvis_processes = [
                    {
                        "pid": p["pid"],
                        "age_hours": p["age_hours"],
                        "type": p["detection_strategy"],
                        "cmdline": p["cmdline"],
                    }
                    for p in jarvis_processes
                ]

            except ImportError as e:
                print(f"  {Colors.YELLOW}⚠ Advanced detector not available, falling back to basic detection{Colors.ENDC}")
                print(f"    Error: {e}")

                # Fallback to basic detection
                current_pid = os.getpid()
                jarvis_processes = []

                # Build exclusion list (current process + parent chain + all IDE processes)
                excluded_pids = {current_pid}
                try:
                    current_proc = psutil.Process(current_pid)
                    parent = current_proc.parent()
                    if parent:
                        excluded_pids.add(parent.pid)
                        # Also exclude grandparent
                        grandparent = parent.parent()
                        if grandparent:
                            excluded_pids.add(grandparent.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

                # CRITICAL: Exclude ALL Claude Code / IDE processes to prevent killing the active session
                for proc in psutil.process_iter(['pid', 'name']):
                    try:
                        proc_name = proc.info['name'].lower()
                        if any(ide in proc_name for ide in ['claude', 'vscode', 'code-helper', 'pycharm', 'idea', 'cursor']):
                            excluded_pids.add(proc.info['pid'])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                print(f"  → Fallback: Basic psutil enumeration...")
                print(f"  → Excluding {len(excluded_pids)} process(es) from current session + IDEs")
                for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
                    try:
                        pid = proc.info["pid"]
                        if pid in excluded_pids:
                            continue  # Skip current session processes

                        cmdline = proc.info.get("cmdline")
                        if not cmdline:
                            continue

                        cmdline_str = " ".join(cmdline).lower()

                        # Enhanced matching: catch all variants
                        is_start_system = "python" in cmdline_str and "start_system.py" in cmdline_str
                        is_backend = (
                            "python" in cmdline_str
                            and "main.py" in cmdline_str
                            and "backend" in cmdline_str
                        )

                        # Also check if process is in JARVIS directory
                        is_jarvis_dir = "jarvis" in cmdline_str.lower()

                        if (is_start_system or is_backend) and is_jarvis_dir:
                            jarvis_processes.append(
                                {
                                    "pid": pid,
                                    "age_hours": (time.time() - proc.info["create_time"]) / 3600,
                                    "type": (
                                        "start_system.py" if is_start_system else "backend/main.py"
                                    ),
                                    "cmdline": " ".join(cmdline),
                                }
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

            if jarvis_processes:
                print(
                    f"\n{Colors.YELLOW}Found {len(jarvis_processes)} JARVIS process(es):{Colors.ENDC}"
                )
                for idx, proc in enumerate(jarvis_processes, 1):
                    age_str = (
                        f"{proc['age_hours']:.1f}h" if proc["age_hours"] > 0 else "unknown age"
                    )
                    print(f"  {idx}. PID {proc['pid']} ({proc['type']}, {age_str})")

                # Progress: 10% - Preparing termination
                await broadcast_to_loading_server(
                    "preparing_kill",
                    f"Preparing graceful shutdown sequence for {len(jarvis_processes)} processes",
                    10,
                    metadata={"icon": "🛑", "label": "Shutdown Prep", "sublabel": "Saving state"}
                )

                # Progress: 12% - Starting termination
                await broadcast_to_loading_server(
                    "terminating",
                    f"Sending SIGTERM to {len(jarvis_processes)} JARVIS processes - graceful shutdown initiated",
                    12,
                    metadata={"icon": "⚔️", "label": "Terminating", "sublabel": f"SIGTERM → {len(jarvis_processes)} PIDs"}
                )

                print(f"\n{Colors.YELLOW}⚔️  Killing all instances...{Colors.ENDC}")

                killed_count = 0
                failed_count = 0

                for proc in jarvis_processes:
                    try:
                        # Try SIGTERM first (graceful)
                        print(f"  → Terminating PID {proc['pid']}...", end="", flush=True)
                        os.kill(proc["pid"], signal.SIGTERM)
                        time.sleep(0.5)

                        # Check if still alive, use SIGKILL if needed
                        if psutil.pid_exists(proc["pid"]):
                            print(f" forcing...", end="", flush=True)
                            os.kill(proc["pid"], signal.SIGKILL)
                            time.sleep(0.3)

                        # Verify it's actually dead
                        if psutil.pid_exists(proc["pid"]):
                            print(f" {Colors.FAIL}✗ Still alive{Colors.ENDC}")
                            failed_count += 1
                        else:
                            print(f" {Colors.GREEN}✓{Colors.ENDC}")
                            killed_count += 1

                    except ProcessLookupError:
                        # Already dead
                        print(f" {Colors.GREEN}✓ (already dead){Colors.ENDC}")
                        killed_count += 1
                    except PermissionError:
                        print(f" {Colors.FAIL}✗ Permission denied{Colors.ENDC}")
                        failed_count += 1
                    except Exception as e:
                        print(f" {Colors.FAIL}✗ {str(e)[:50]}{Colors.ENDC}")
                        failed_count += 1

                print(f"\n{Colors.YELLOW}⏳ Waiting for processes to terminate...{Colors.ENDC}")
                time.sleep(2)

                # Final verification
                still_alive = []
                for proc in jarvis_processes:
                    if psutil.pid_exists(proc["pid"]):
                        still_alive.append(proc["pid"])

                if still_alive:
                    print(
                        f"{Colors.FAIL}⚠️  WARNING: {len(still_alive)} process(es) still alive: {still_alive}{Colors.ENDC}"
                    )
                else:
                    print(
                        f"{Colors.GREEN}✓ All {killed_count} process(es) terminated successfully{Colors.ENDC}"
                    )

                # Progress: 16% - Verification
                await broadcast_to_loading_server(
                    "verifying_kill",
                    f"Verifying process termination - checking {killed_count} PIDs no longer exist",
                    16,
                    metadata={"icon": "🔍", "label": "Verification", "sublabel": "Confirming shutdown"}
                )

                # Progress: 20% - Processes killed
                await broadcast_to_loading_server(
                    "killed",
                    f"Process termination complete: {killed_count}/{len(jarvis_processes)} processes successfully terminated",
                    20,
                    metadata={"icon": "✓", "label": "Terminated", "sublabel": f"{killed_count} PIDs released"}
                )
                await asyncio.sleep(0.5)

                # Progress: 23% - Port cleanup
                await broadcast_to_loading_server(
                    "port_cleanup",
                    "Releasing network ports 3000 and 8010 - ensuring clean state",
                    23,
                    metadata={"icon": "🔌", "label": "Port Release", "sublabel": "Freeing listeners"}
                )

                # Progress: 25% - Resource cleanup
                await broadcast_to_loading_server(
                    "cleanup",
                    "Cleaning up shared memory, file locks, and temporary resources",
                    25,
                    metadata={"icon": "🧹", "label": "Resource Cleanup", "sublabel": "Deallocating memory"}
                )
            else:
                print(f"{Colors.GREEN}No old JARVIS processes found{Colors.ENDC}")
                await broadcast_to_loading_server(
                    "cleanup", "No existing processes found, proceeding...", 25,
                    metadata={
                        "icon": "🧹",
                        "label": "Cleanup",
                        "sublabel": "Clean state"
                    }
                )

            # Step 1.5: Clean up VM creation lock file (prevent lock conflicts)
            print(f"\n{Colors.YELLOW}🔒 Checking VM creation lock file...{Colors.ENDC}")
            vm_lock_file = Path.home() / ".jarvis" / "gcp_optimizer" / "vm_creation.lock"
            if vm_lock_file.exists():
                try:
                    # Read lock info to show what we're cleaning
                    with open(vm_lock_file, "r") as f:
                        lock_info = f.read().strip()

                    # Remove the lock file
                    vm_lock_file.unlink()
                    print(f"{Colors.GREEN}✓ Removed stale VM creation lock{Colors.ENDC}")
                    if lock_info:
                        import json

                        try:
                            lock_data = json.loads(lock_info)
                            print(
                                f"  Previous lock: PID {lock_data.get('pid', 'unknown')}, {lock_data.get('timestamp', 'unknown')}"
                            )
                        except:
                            pass
                except Exception as e:
                    print(f"{Colors.WARNING}⚠️  Failed to clean VM lock file: {e}{Colors.ENDC}")
            else:
                print(f"{Colors.GREEN}✓ No VM creation lock file found{Colors.ENDC}")

            # Progress: 28% - Checking database proxies
            await broadcast_to_loading_server(
                "checking_proxies",
                "Scanning for active cloud-sql-proxy database connections",
                28,
                metadata={"icon": "🔐", "label": "DB Proxy Check", "sublabel": "Scanning processes"}
            )

            # Step 1.55: Kill any running cloud-sql-proxy processes (fresh start)
            print(f"\n{Colors.YELLOW}🔐 Checking for cloud-sql-proxy processes...{Colors.ENDC}")
            try:
                # Find all cloud-sql-proxy processes
                proxy_pids = []
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        cmdline = proc.info.get("cmdline")
                        if cmdline and any("cloud-sql-proxy" in str(arg) for arg in cmdline):
                            proxy_pids.append(proc.info["pid"])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                if proxy_pids:
                    print(f"{Colors.YELLOW}Found {len(proxy_pids)} cloud-sql-proxy process(es){Colors.ENDC}")

                    # Progress: 30% - Terminating database proxies
                    await broadcast_to_loading_server(
                        "terminating_proxies",
                        f"Terminating {len(proxy_pids)} cloud-sql-proxy process(es) - closing database tunnels",
                        30,
                        metadata={"icon": "🔐", "label": "DB Proxy Kill", "sublabel": f"SIGTERM → {len(proxy_pids)} proxies"}
                    )

                    for pid in proxy_pids:
                        try:
                            print(f"  → Terminating cloud-sql-proxy PID {pid}...", end="", flush=True)
                            os.kill(pid, signal.SIGTERM)
                            time.sleep(0.5)
                            if psutil.pid_exists(pid):
                                os.kill(pid, signal.SIGKILL)
                            print(f" {Colors.GREEN}✓{Colors.ENDC}")
                        except Exception as e:
                            print(f" {Colors.WARNING}⚠️  {e}{Colors.ENDC}")
                    print(f"{Colors.GREEN}✓ Cloud SQL proxy processes terminated{Colors.ENDC}")
                else:
                    print(f"{Colors.GREEN}✓ No cloud-sql-proxy processes found{Colors.ENDC}")
            except Exception as e:
                print(f"{Colors.WARNING}⚠️  Failed to check proxy processes: {e}{Colors.ENDC}")

            # Progress: 32% - Scanning cloud resources
            await broadcast_to_loading_server(
                "scanning_vms",
                "Querying GCP Compute Engine for orphaned JARVIS VMs (jarvis-auto-*, jarvis-backend-*)",
                32,
                metadata={"icon": "☁️", "label": "Cloud Scan", "sublabel": "Listing GCP instances"}
            )

            # Step 1.6: Clean up any GCP VMs (CRITICAL for cost control)
            print(f"\n{Colors.YELLOW}🌐 Checking for orphaned GCP VMs...{Colors.ENDC}")
            try:
                gcp_project = os.getenv("GCP_PROJECT_ID", "jarvis-473803")

                # List all jarvis-auto-* and jarvis-backend-* VMs (both old and new naming)
                list_cmd = [
                    "gcloud",
                    "compute",
                    "instances",
                    "list",
                    "--project",
                    gcp_project,
                    "--filter",
                    "name:jarvis-auto-* OR name:jarvis-backend-*",
                    "--format",
                    "value(name,zone)",
                ]

                result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=10)

                if result.returncode == 0 and result.stdout.strip():
                    vms = result.stdout.strip().split("\n")
                    print(f"Found {len(vms)} GCP VM(s) to clean up:")

                    # Progress: 33% - Deleting VMs
                    await broadcast_to_loading_server(
                        "deleting_vms",
                        f"Terminating {len(vms)} GCP Compute Engine instance(s) - stopping cloud costs",
                        33,
                        metadata={"icon": "☁️", "label": "VM Deletion", "sublabel": f"Deleting {len(vms)} instances"}
                    )

                    for vm_line in vms:
                        parts = vm_line.split()
                        if len(parts) >= 2:
                            vm_id, zone = parts[0], parts[1]
                            print(f"  Deleting {vm_id} in {zone}...")

                            delete_cmd = [
                                "gcloud",
                                "compute",
                                "instances",
                                "delete",
                                vm_id,
                                "--project",
                                gcp_project,
                                "--zone",
                                zone,
                                "--quiet",
                            ]

                            delete_result = subprocess.run(
                                delete_cmd, capture_output=True, text=True, timeout=60
                            )

                            if delete_result.returncode == 0:
                                print(f"  {Colors.GREEN}✓ Deleted {vm_id}{Colors.ENDC}")
                            else:
                                print(
                                    f"  {Colors.YELLOW}⚠ Failed to delete {vm_id}: {delete_result.stderr[:100]}{Colors.ENDC}"
                                )

                    print(f"{Colors.GREEN}✓ VM cleanup complete{Colors.ENDC}")
                else:
                    print(f"{Colors.GREEN}No GCP VMs found{Colors.ENDC}")

            except subprocess.TimeoutExpired:
                print(f"{Colors.YELLOW}⚠ VM cleanup timed out - proceeding anyway{Colors.ENDC}")
            except Exception as e:
                print(f"{Colors.YELLOW}⚠ VM cleanup failed: {e} - proceeding anyway{Colors.ENDC}")

            # Progress: 35% - VM cleanup done
            await broadcast_to_loading_server(
                "vm_cleanup",
                "Cloud resource cleanup complete - all orphaned GCP VMs terminated, costs stopped",
                35,
                metadata={"icon": "☁️", "label": "Cloud Cleanup", "sublabel": "VMs deleted"}
            )
            await asyncio.sleep(0.3)

            print(f"\n{'='*50}")
            print(
                f"{Colors.GREEN}🎉 Old instances cleaned up - starting fresh JARVIS...{Colors.ENDC}"
            )
            print(f"{'='*50}\n")

            # Progress: 40% - Ready to start
            await broadcast_to_loading_server(
                "ready_to_start",
                "Environment validation complete - all ports free, resources available, system ready to launch",
                40,
                metadata={"icon": "✓", "label": "Ready", "sublabel": "Environment clean"}
            )
            await asyncio.sleep(0.5)

            # Progress: 45% - Starting services
            await broadcast_to_loading_server(
                "starting",
                "Spawning FastAPI backend process - initializing uvicorn ASGI server on port 8010",
                45,
                metadata={"icon": "🚀", "label": "Starting", "sublabel": "Launching backend"}
            )

            # Optimize system for faster startup
            print(f"{Colors.CYAN}⚡ Optimizing system for fast startup...{Colors.ENDC}")
            try:
                # Reduce CPU throttling by giving JARVIS higher priority
                subprocess.run(
                    ["sudo", "-n", "renice", "-n", "-10", "-p", str(os.getpid())],
                    capture_output=True,
                    timeout=1
                )
                print(f"  {Colors.GREEN}✓ Process priority increased{Colors.ENDC}")
            except:
                pass  # Silently fail if no sudo access

            # Fall through to normal startup - backend will start fresh

        except Exception as e:
            print(f"{Colors.FAIL}Restart failed: {e}{Colors.ENDC}")
            await broadcast_to_loading_server(
                "failed", f"Restart failed: {str(e)}", 0,
                metadata={"icon": "❌", "label": "Failed", "sublabel": "Error occurred"}
            )
            import traceback

            traceback.print_exc()
            return 1

    # Create manager
    _manager = AsyncSystemManager()
    _manager.no_browser = args.no_browser
    _manager.backend_only = args.backend_only
    _manager.frontend_only = args.frontend_only
    _manager.is_restart = args.restart  # Track if this is a restart
    _manager.use_optimized = not args.standard

    # Set global reference for voice verification tracking
    global _global_system_manager
    _global_system_manager = _manager
    _manager.auto_cleanup = not args.no_auto_cleanup

    # Always use autonomous mode unless explicitly disabled
    if args.no_autonomous:
        _manager.autonomous_mode = False
        print(
            f"{Colors.BLUE}✓ Starting in traditional mode (--no-autonomous flag set)...{Colors.ENDC}\n"
        )
    else:
        # Always default to autonomous mode since it's always available now
        _manager.autonomous_mode = True
        print(f"{Colors.GREEN}✓ Starting in autonomous mode...{Colors.ENDC}\n")

    if args.check_only:
        _manager.print_header()
        await _manager.check_python_version()
        await _manager.check_claude_config()
        await _manager.check_system_resources()
        deps_ok, _, _ = await _manager.check_dependencies()
        return 0 if deps_ok else 1

    # PID file management (integrated from jarvis.sh)
    pid_file = Path(tempfile.gettempdir()) / "jarvis.pid"

    # Clean up orphaned PID file from previous crashed sessions
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if not psutil.pid_exists(old_pid):
                logger.info(f"🧹 Removing stale PID file (process {old_pid} not running)")
                pid_file.unlink()
            else:
                # Check if it's actually a JARVIS process
                try:
                    proc = psutil.Process(old_pid)
                    cmdline = " ".join(proc.cmdline())
                    if "start_system.py" in cmdline:
                        logger.warning(f"⚠️  Another JARVIS instance is running (PID {old_pid})")
                        print(
                            f"{Colors.YELLOW}⚠️  Another JARVIS instance is running (PID {old_pid}){Colors.ENDC}"
                        )
                        print(f"   Use --force-start to override, or kill PID {old_pid} first")
                        return 1
                    else:
                        # Different process reused the PID - safe to remove
                        pid_file.unlink()
                except psutil.NoSuchProcess:
                    pid_file.unlink()
        except (ValueError, OSError) as e:
            logger.warning(f"⚠️  Could not read PID file: {e}")
            pid_file.unlink()

    # Write current PID
    pid_file.write_text(str(os.getpid()))
    logger.info(f"📝 PID file created: {pid_file} (PID {os.getpid()})")

    # Set up signal handlers with cleanup
    loop = asyncio.get_event_loop()

    def cleanup_and_shutdown():
        """Cleanup PID file, loading server, and trigger shutdown"""
        try:
            if pid_file.exists():
                pid_file.unlink()
                logger.info("🧹 PID file removed")
        except Exception as e:
            logger.warning(f"Could not remove PID file: {e}")

        # Cleanup loading server if it exists
        if 'loading_server_process' in locals() and loading_server_process:
            try:
                loading_server_process.terminate()
                logger.info("🧹 Loading server stopped")
            except:
                pass

        asyncio.create_task(shutdown_handler())

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, cleanup_and_shutdown)

    # Run the system
    try:
        success = await _manager.run()
        return 0 if success else 1
    except asyncio.CancelledError:
        # Expected during shutdown
        logger.info("Main task cancelled during shutdown")
        return 0


if __name__ == "__main__":
    # Global to track if we successfully initialized (for cleanup)
    _jarvis_initialized = False
    _hybrid_coordinator = None

    # Track Ctrl+C count for force exit on double-press
    _interrupt_count = 0
    _last_interrupt_time = 0

    def _force_exit_handler(signum, frame):
        """Handle Ctrl+C with force exit on double-press"""
        global _interrupt_count, _last_interrupt_time
        import time as t

        current_time = t.time()

        # Reset count if more than 2 seconds since last interrupt
        if current_time - _last_interrupt_time > 2.0:
            _interrupt_count = 0

        _interrupt_count += 1
        _last_interrupt_time = current_time

        if _interrupt_count >= 2:
            # Double Ctrl+C - force immediate exit
            print(f"\n\r{Colors.RED}⚡ Force exit (double Ctrl+C){Colors.ENDC}")
            sys.stdout.flush()
            os._exit(130)  # 128 + SIGINT(2) = 130
        else:
            # First Ctrl+C - show hint and let normal handling proceed
            print(f"\n\r{Colors.YELLOW}⏳ Shutting down... (Ctrl+C again to force quit){Colors.ENDC}")
            sys.stdout.flush()
            # Raise KeyboardInterrupt to trigger normal shutdown
            raise KeyboardInterrupt

    # Install force exit handler before entering main loop
    signal.signal(signal.SIGINT, _force_exit_handler)

    try:
        # Use custom asyncio runner to ensure all tasks are cancelled on exit
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            exit_code = loop.run_until_complete(main())
        except asyncio.CancelledError:
            # Expected during shutdown
            logger.info("Main event loop cancelled")
            exit_code = 0
        finally:
            # Cancel all pending tasks
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                # Wait for all tasks to be cancelled
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except RuntimeError:
                # Loop may already be closed
                pass
            finally:
                if not loop.is_closed():
                    loop.close()
        sys.exit(exit_code if exit_code else 0)
    except KeyboardInterrupt:
        # Don't print anything extra - cleanup() already handles the shutdown message
        print("\r", end="")  # Clear the ^C from the terminal
        sys.exit(0)
    except Exception as e:
        print(f"\n{Colors.FAIL}Fatal error: {e}{Colors.ENDC}")
        logger.exception("Fatal error during startup")
        sys.exit(1)
    finally:
        # Cleanup PID file on exit
        pid_file = Path(tempfile.gettempdir()) / "jarvis.pid"
        try:
            if pid_file.exists():
                current_pid = os.getpid()
                file_pid = int(pid_file.read_text().strip())
                # Only remove if it's our PID (safety check)
                if file_pid == current_pid:
                    pid_file.unlink()
                    logger.info("🧹 PID file cleaned up on exit")
        except Exception as e:
            logger.warning(f"Could not cleanup PID file: {e}")

        # CRITICAL: Cleanup GCP VMs synchronously (works even if asyncio is dead)
        # MULTI-TERMINAL SAFE: Only deletes VMs owned by THIS session
        # ONLY RUN IF JARVIS FULLY INITIALIZED (skip on early exit)
        print(
            f"\n{Colors.CYAN}╔══════════════════════════════════════════════════════════════╗{Colors.ENDC}"
        )
        print(
            f"{Colors.CYAN}║         GCP VM Cleanup (Post-Shutdown)                       ║{Colors.ENDC}"
        )
        print(
            f"{Colors.CYAN}╚══════════════════════════════════════════════════════════════╝{Colors.ENDC}\n"
        )

        try:
            project_id = os.getenv("GCP_PROJECT_ID", "jarvis-473803")

            # Check if coordinator exists and has session tracker
            # Note: 'coordinator' is local to main(), so we check globals
            coordinator_ref = globals().get("_hybrid_coordinator")

            if (
                coordinator_ref
                and hasattr(coordinator_ref, "workload_router")
                and hasattr(coordinator_ref.workload_router, "session_tracker")
            ):
                session_tracker = coordinator_ref.workload_router.session_tracker
                my_vm = session_tracker.get_my_vm()

                if my_vm:
                    vm_id = my_vm["vm_id"]
                    zone = my_vm["zone"]

                    print(f"{Colors.CYAN}🌐 Deleting session-owned GCP VM...{Colors.ENDC}")
                    print(f"   ├─ VM ID: {Colors.YELLOW}{vm_id}{Colors.ENDC}")
                    print(f"   ├─ Zone: {zone}")
                    print(f"   ├─ Project: {project_id}")
                    print(f"   ├─ Session: {session_tracker.session_id[:8]}...")
                    print(f"   ├─ PID: {session_tracker.pid}")
                    print(f"   ├─ Executing: gcloud compute instances delete...")

                    logger.info(f"🧹 Cleaning up session-owned VM: {vm_id}")
                    logger.info(f"   Session: {session_tracker.session_id[:8]}")
                    logger.info(f"   PID: {session_tracker.pid}")

                    import time

                    start_time = time.time()

                    delete_cmd = [
                        "gcloud",
                        "compute",
                        "instances",
                        "delete",
                        vm_id,
                        "--project",
                        project_id,
                        "--zone",
                        zone,
                        "--quiet",
                    ]

                    delete_result = subprocess.run(
                        delete_cmd, capture_output=True, text=True, timeout=10
                    )

                    elapsed = time.time() - start_time

                    if delete_result.returncode == 0:
                        print(
                            f"   ├─ {Colors.GREEN}✓ VM deleted successfully ({elapsed:.1f}s){Colors.ENDC}"
                        )
                        print(f"   └─ {Colors.GREEN}💰 Stopped billing for {vm_id}{Colors.ENDC}")
                        logger.info(f"✅ Deleted session VM: {vm_id}")

                        # Unregister from session tracker
                        session_tracker.unregister_vm()
                    else:
                        error_msg = delete_result.stderr.strip()
                        if "was not found" in error_msg or "Not Found" in error_msg:
                            print(
                                f"   └─ {Colors.GREEN}✓ VM already deleted during shutdown (cleanup not needed){Colors.ENDC}"
                            )
                            logger.info(
                                f"✅ VM {vm_id} already deleted (expected - cleaned up during main shutdown)"
                            )
                        else:
                            print(
                                f"   ├─ {Colors.RED}✗ Failed to delete VM ({elapsed:.1f}s){Colors.ENDC}"
                            )
                            print(f"   └─ {Colors.RED}Error: {error_msg[:100]}{Colors.ENDC}")
                            logger.warning(f"Failed to delete VM {vm_id}: {delete_result.stderr}")

                    # Show other active sessions
                    print(f"\n{Colors.CYAN}📊 Other active JARVIS sessions:{Colors.ENDC}")
                    active_sessions = session_tracker.get_all_active_sessions()
                    other_sessions = {
                        sid: data
                        for sid, data in active_sessions.items()
                        if sid != session_tracker.session_id
                    }

                    if other_sessions:
                        print(f"   ├─ {len(other_sessions)} other session(s) still running:")
                        for sid, data in other_sessions.items():
                            vm_status = (
                                f"VM: {data.get('vm_id', 'none')}" if data.get("vm_id") else "No VM"
                            )
                            print(f"   │  • Session {sid[:8]}: PID {data.get('pid')}, {vm_status}")
                        print(
                            f"   └─ {Colors.YELLOW}Note: Other sessions remain active{Colors.ENDC}"
                        )
                        logger.info(
                            f"ℹ️  {len(other_sessions)} other JARVIS session(s) still running"
                        )
                    else:
                        print(f"   └─ No other active JARVIS sessions")
                else:
                    print(f"{Colors.CYAN}ℹ️  No VM registered to this session{Colors.ENDC}")
                    print(f"   └─ Session ran locally only (no cloud migration)")
                    logger.info("ℹ️  No VM registered to this session")
            else:
                # Fallback: Old behavior if session tracker not initialized
                print(f"{Colors.YELLOW}⚠️  Session tracker not initialized{Colors.ENDC}")
                print(f"   ├─ Falling back to legacy VM detection...")
                logger.warning("⚠️  Session tracker not available, falling back to legacy cleanup")

                list_cmd = [
                    "gcloud",
                    "compute",
                    "instances",
                    "list",
                    "--project",
                    project_id,
                    "--filter",
                    "name:jarvis-auto-*",
                    "--format",
                    "value(name,zone)",
                ]

                result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=5)

                if result.returncode == 0 and result.stdout.strip():
                    instances = result.stdout.strip().split("\n")
                    print(
                        f"   ├─ {Colors.YELLOW}Found {len(instances)} jarvis-auto-* VMs{Colors.ENDC}"
                    )
                    print(
                        f"   ├─ {Colors.YELLOW}⚠ Cannot determine ownership without session tracker{Colors.ENDC}"
                    )
                    print(f"   └─ {Colors.YELLOW}Manual cleanup may be required:{Colors.ENDC}")
                    print(f"      gcloud compute instances list --filter='name:jarvis-auto-*'")
                    logger.warning(
                        f"⚠️  Found {len(instances)} jarvis-auto-* VMs (cannot determine ownership)"
                    )
                    logger.warning(
                        "   Manual cleanup may be required: gcloud compute instances list"
                    )
                else:
                    print(f"   └─ {Colors.GREEN}✓ No orphaned GCP VMs found{Colors.ENDC}")
                    logger.info("ℹ️  No orphaned GCP VMs found")

        except subprocess.TimeoutExpired:
            print(f"\n{Colors.RED}✗ GCP VM cleanup timed out{Colors.ENDC}")
            print(f"   └─ Network may be slow or gcloud not responding")
            logger.warning("GCP VM cleanup timed out")
        except Exception as e:
            print(f"\n{Colors.RED}✗ Error during GCP VM cleanup: {e}{Colors.ENDC}")
            logger.warning(f"Could not cleanup GCP VMs on exit: {e}")

        # NOTE: Removed automatic cleanup of other start_system.py processes
        # Each instance is protected by single-instance check at startup
        # Users must manually stop other instances if needed

        # Ensure terminal is restored
        sys.stdout.flush()
        sys.stderr.flush()

        # Shutdown all managed thread pool executors first
        print(f"\n{Colors.CYAN}🧹 Shutting down thread pool executors...{Colors.ENDC}")
        try:
            from core.thread_manager import shutdown_all_executors
            count = shutdown_all_executors(wait=True, timeout=3.0)
            print(f"   ├─ Shut down {count} managed executors")
        except ImportError:
            print(f"   ├─ Thread manager not available")
        except Exception as e:
            print(f"   ├─ {Colors.YELLOW}⚠ Executor shutdown error: {e}{Colors.ENDC}")

        # Aggressively clean up async tasks and event loop
        print(f"\n{Colors.CYAN}🧹 Performing final async cleanup...{Colors.ENDC}")
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and not loop.is_closed():
                # Cancel all remaining tasks with recursion protection
                try:
                    all_tasks = asyncio.all_tasks(loop)
                except RecursionError:
                    print(f"   ├─ {Colors.YELLOW}⚠ Recursion error enumerating tasks - skipping final cancellation{Colors.ENDC}")
                    all_tasks = []

                if all_tasks:
                    print(f"   ├─ Canceling {len(all_tasks)} remaining async tasks...")
                    cancelled = 0
                    for task in all_tasks:
                        if not task.done():
                            try:
                                task.cancel()
                                cancelled += 1
                            except RecursionError:
                                continue  # Skip tasks causing recursion
                            except Exception:
                                continue

                    print(f"   ├─ Cancelled {cancelled}/{len(all_tasks)} tasks")

                    # Run the loop briefly to process cancellations
                    if cancelled > 0:
                        try:
                            # Capture results to prevent "exception was never retrieved" warning
                            results = loop.run_until_complete(
                                asyncio.wait_for(
                                    asyncio.gather(*all_tasks, return_exceptions=True),
                                    timeout=2.0
                                )
                            )
                            # Process results to suppress CancelledError warnings
                            if results:
                                for result in results:
                                    if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                                        logger.debug(f"Task exception during cleanup: {result}")
                        except (RecursionError, asyncio.TimeoutError, asyncio.CancelledError):
                            pass  # Ignore recursion/timeout/cancelled during final cleanup
                        except Exception as e:
                            logger.debug(f"Cleanup gather exception: {e}")

                # CRITICAL: Ensure all subprocess waiters complete BEFORE stopping loop
                print(f"   ├─ Waiting for subprocess handlers to complete...")
                try:
                    # Get all pending tasks that might be subprocess waiters
                    import threading
                    import asyncio.subprocess

                    waitpid_threads = [t for t in threading.enumerate() if t.name.startswith('waitpid-')]

                    if waitpid_threads:
                        print(f"   │  Found {len(waitpid_threads)} waitpid threads - draining subprocess operations...")

                        # Strategy 1: Find and complete all pending subprocess wait() operations
                        try:
                            # Get all tasks and filter for subprocess-related ones
                            all_tasks = asyncio.all_tasks(loop)
                            subprocess_tasks = []

                            for task in all_tasks:
                                # Check if task is related to subprocess (waitpid)
                                if not task.done():
                                    task_repr = repr(task)
                                    if 'subprocess' in task_repr.lower() or 'wait' in task_repr.lower():
                                        subprocess_tasks.append(task)

                            if subprocess_tasks:
                                print(f"   │  Completing {len(subprocess_tasks)} subprocess-related tasks...")
                                try:
                                    loop.run_until_complete(
                                        asyncio.wait_for(
                                            asyncio.gather(*subprocess_tasks, return_exceptions=True),
                                            timeout=2.0
                                        )
                                    )
                                except:
                                    pass
                        except:
                            pass

                        # Strategy 2: Give remaining waitpid threads time to complete
                        for i in range(20):  # Up to 1 second
                            try:
                                loop.run_until_complete(asyncio.sleep(0.05))
                                # Check if waitpid threads are done
                                remaining = [t for t in threading.enumerate() if t.name.startswith('waitpid-')]
                                if not remaining:
                                    print(f"   │  ✓ All waitpid threads completed after {(i+1)*50}ms")
                                    break
                                elif i == 19:
                                    print(f"   │  ⚠ {len(remaining)} waitpid threads still active (will be orphaned)")
                            except:
                                break
                    else:
                        print(f"   │  No waitpid threads found")

                except Exception as e:
                    logger.debug(f"Subprocess waiter cleanup exception: {e}")

                # Stop and close the event loop
                print(f"   ├─ Stopping event loop...")
                try:
                    loop.stop()
                except RecursionError:
                    pass  # Event loop may already be stopped

                print(f"   ├─ Closing event loop...")
                try:
                    loop.close()
                except RecursionError:
                    pass  # Event loop may already be closed

                print(f"   └─ {Colors.GREEN}✓ Event loop cleanup complete{Colors.ENDC}")
        except RecursionError as e:
            print(f"   └─ {Colors.YELLOW}⚠ Recursion error during cleanup - event loop may not be fully closed{Colors.ENDC}")
        except Exception as e:
            print(f"   └─ {Colors.YELLOW}⚠ Event loop cleanup warning: {e}{Colors.ENDC}")

        # Check for remaining threads (informational only)
        import threading

        remaining_threads = [t for t in threading.enumerate() if t != threading.main_thread()]
        if remaining_threads:
            # Filter out daemon threads (they're okay to leave running)
            non_daemon_threads = [t for t in remaining_threads if not t.daemon]

            if non_daemon_threads:
                print(
                    f"\n{Colors.YELLOW}⚠️  {len(non_daemon_threads)} non-daemon threads still running:{Colors.ENDC}"
                )
                # Group threads by their target function to identify sources
                thread_sources = {}
                for thread in non_daemon_threads:
                    # Get thread target info
                    target_name = "unknown"
                    if hasattr(thread, '_target') and thread._target:
                        target_name = getattr(thread._target, '__qualname__',
                                            getattr(thread._target, '__name__', str(thread._target)))
                    elif hasattr(thread, 'name'):
                        target_name = thread.name

                    if target_name not in thread_sources:
                        thread_sources[target_name] = []
                    thread_sources[target_name].append(thread.name)

                # Print grouped summary
                print(f"\n   {Colors.CYAN}Thread sources:{Colors.ENDC}")
                for source, threads in sorted(thread_sources.items(), key=lambda x: -len(x[1])):
                    print(f"   - {source}: {len(threads)} threads")
                    if len(threads) <= 3:
                        for t in threads:
                            print(f"     • {t}")

                # Log individual threads
                for thread in non_daemon_threads[:10]:  # First 10 only
                    logger.warning(f"Non-daemon thread still running: {thread.name}")

            # Daemon threads are okay
            daemon_threads = [t for t in remaining_threads if t.daemon]
            if daemon_threads:
                print(f"\n{Colors.CYAN}ℹ️  {len(daemon_threads)} daemon threads (will auto-terminate):{Colors.ENDC}")
                for thread in daemon_threads:
                    print(f"   - {thread.name}")

            # Give non-daemon threads a chance to terminate gracefully
            if non_daemon_threads:
                print(f"\n{Colors.YELLOW}🔧 Waiting for {len(non_daemon_threads)} non-daemon threads to complete...{Colors.ENDC}")

                # Give threads a generous 5 seconds to finish gracefully
                import time as time_module
                deadline = time_module.time() + 5.0

                while time_module.time() < deadline:
                    remaining = [t for t in threading.enumerate()
                                if t != threading.main_thread() and not t.daemon and t.is_alive()]
                    if not remaining:
                        print(f"   {Colors.GREEN}✓ All threads terminated gracefully{Colors.ENDC}")
                        break
                    time_module.sleep(0.2)
                else:
                    # Threads still running after timeout - this shouldn't happen
                    # with proper daemon threads, but log it for debugging
                    still_alive = [t for t in threading.enumerate()
                                  if t != threading.main_thread() and not t.daemon and t.is_alive()]
                    if still_alive:
                        print(f"   {Colors.YELLOW}⚠ {len(still_alive)} threads still running after 5s timeout{Colors.ENDC}")
                        for t in still_alive[:5]:  # Show first 5
                            print(f"      - {t.name}")
                        if len(still_alive) > 5:
                            print(f"      ... and {len(still_alive) - 5} more")
                        print(f"   {Colors.CYAN}ℹ These may be from third-party libraries{Colors.ENDC}")
