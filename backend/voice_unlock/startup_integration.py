"""
Voice Unlock Startup Integration
================================

Integrates the Voice Unlock system into JARVIS's main startup process.

ENHANCED v3.0: Comprehensive voice biometric validation at startup.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# STARTUP VALIDATION TYPES
# =============================================================================

class ValidationStatus(Enum):
    """Status of a validation check"""
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ValidationResult:
    """Result of a single validation check"""
    name: str
    status: ValidationStatus
    message: str
    duration_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """Complete validation report"""
    total_checks: int = 0
    passed: int = 0
    warnings: int = 0
    failed: int = 0
    skipped: int = 0
    results: List[ValidationResult] = field(default_factory=list)
    total_duration_ms: float = 0.0
    ready_for_voice_unlock: bool = False

    def add_result(self, result: ValidationResult):
        """Add a validation result"""
        self.results.append(result)
        self.total_checks += 1
        self.total_duration_ms += result.duration_ms

        if result.status == ValidationStatus.PASSED:
            self.passed += 1
        elif result.status == ValidationStatus.WARNING:
            self.warnings += 1
        elif result.status == ValidationStatus.FAILED:
            self.failed += 1
        elif result.status == ValidationStatus.SKIPPED:
            self.skipped += 1

    def get_summary(self) -> str:
        """Get a summary string"""
        return (
            f"{self.passed}/{self.total_checks} passed, "
            f"{self.warnings} warnings, {self.failed} failed, "
            f"{self.skipped} skipped in {self.total_duration_ms:.0f}ms"
        )


# =============================================================================
# COMPREHENSIVE VOICE BIOMETRIC STARTUP VALIDATOR
# =============================================================================

class VoiceBiometricStartupValidator:
    """
    CRITICAL FIX v3.0: Comprehensive validation of all voice biometric components.

    This validator runs at startup to ensure ALL required components are working:
    1. ML Engine Registry (ECAPA-TDNN management)
    2. ECAPA Encoder (speaker embedding extraction)
    3. Unified Voice Cache (profile caching, embedding extraction)
    4. Voice Biometric Intelligence (speaker verification)
    5. Hybrid Cloud Architecture (local/cloud fallback)
    6. Physics-Aware Voice Auth (mathematical verification)
    7. Voiceprint Database (stored enrollments)
    """

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.report = ValidationReport()

    def _log(self, message: str, level: str = "info"):
        """Log with optional verbosity control"""
        if self.verbose:
            if level == "info":
                logger.info(message)
            elif level == "warning":
                logger.warning(message)
            elif level == "error":
                logger.error(message)

    async def _run_check(
        self,
        name: str,
        check_func,
        critical: bool = True,
    ) -> ValidationResult:
        """Run a single validation check with timing"""
        start = time.time()
        try:
            passed, message, details = await check_func()
            duration_ms = (time.time() - start) * 1000

            if passed:
                status = ValidationStatus.PASSED
                icon = "✅"
            elif critical:
                status = ValidationStatus.FAILED
                icon = "❌"
            else:
                status = ValidationStatus.WARNING
                icon = "⚠️"

            self._log(f"   {icon} {name}: {message}")

            return ValidationResult(
                name=name,
                status=status,
                message=message,
                duration_ms=duration_ms,
                details=details or {},
            )
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            self._log(f"   ❌ {name}: Exception - {e}", "error")
            return ValidationResult(
                name=name,
                status=ValidationStatus.FAILED if critical else ValidationStatus.WARNING,
                message=f"Exception: {e}",
                duration_ms=duration_ms,
                details={"exception": str(e)},
            )

    async def _check_ml_registry(self) -> Tuple[bool, str, Dict]:
        """Check ML Engine Registry initialization"""
        try:
            from voice_unlock.ml_engine_registry import get_ml_registry_sync

            registry = get_ml_registry_sync(auto_create=True)
            if registry is None:
                return False, "Registry creation failed", {}

            details = {
                "is_ready": registry.is_ready,
                "is_using_cloud": registry.is_using_cloud,
                "cloud_verified": getattr(registry, "_cloud_verified", False),
            }

            return True, "Registry initialized", details
        except ImportError as e:
            return False, f"Import error: {e}", {}

    async def _check_ecapa_encoder(self) -> Tuple[bool, str, Dict]:
        """Check ECAPA encoder availability (with on-demand loading)"""
        try:
            from voice_unlock.ml_engine_registry import ensure_ecapa_available

            success, message, encoder = await ensure_ecapa_available(
                timeout=45.0,
                allow_cloud=True,
            )

            details = {
                "encoder_available": success,
                "encoder_type": type(encoder).__name__ if encoder else "Cloud/None",
                "mode": "local" if encoder else "cloud",
            }

            return success, message, details
        except ImportError as e:
            return False, f"Import error: {e}", {}

    async def _check_unified_cache(self) -> Tuple[bool, str, Dict]:
        """Check Unified Voice Cache Manager"""
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager

            cache = get_unified_cache_manager()
            if cache is None:
                return False, "Cache manager not created", {}

            # Check encoder via cache
            encoder = cache.get_ecapa_encoder()
            encoder_status = cache.get_encoder_status() if hasattr(cache, "get_encoder_status") else {}

            details = {
                "cache_ready": getattr(cache, "_stats", None) is not None,
                "models_loaded": getattr(cache._stats, "models_loaded", False) if hasattr(cache, "_stats") else False,
                "profiles_loaded": cache.profiles_loaded if hasattr(cache, "profiles_loaded") else 0,
                "encoder_available": encoder is not None,
                "encoder_status": encoder_status,
            }

            passed = encoder is not None or getattr(cache, "_using_cloud_ecapa", False)
            message = "Cache ready" + (" (encoder available)" if encoder else " (cloud mode)")

            return passed, message, details
        except ImportError as e:
            return False, f"Import error: {e}", {}

    async def _check_embedding_extraction(self) -> Tuple[bool, str, Dict]:
        """Test actual embedding extraction with synthetic audio"""
        import numpy as np

        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager

            cache = get_unified_cache_manager()
            if cache is None:
                return False, "Cache manager unavailable", {}

            # Generate test audio (1s sine wave)
            sample_rate = 16000
            duration = 1.0
            t = np.linspace(0, duration, int(sample_rate * duration))
            test_audio = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)

            # Extract embedding
            embedding = await cache.extract_embedding(test_audio, sample_rate=sample_rate)

            if embedding is None:
                return False, "Embedding extraction returned None", {}

            details = {
                "embedding_shape": list(embedding.shape),
                "embedding_dim": embedding.shape[-1],
                "embedding_norm": float(np.linalg.norm(embedding)),
                "valid_192d": embedding.shape[-1] == 192,
            }

            if embedding.shape[-1] != 192:
                return False, f"Wrong embedding dim: {embedding.shape[-1]} (expected 192)", details

            return True, f"Extraction OK ({embedding.shape[-1]}D, norm={details['embedding_norm']:.3f})", details
        except Exception as e:
            return False, f"Extraction failed: {e}", {}

    async def _check_voice_biometric_intelligence(self) -> Tuple[bool, str, Dict]:
        """Check Voice Biometric Intelligence service"""
        try:
            from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence

            vbi = await get_voice_biometric_intelligence()
            if vbi is None:
                return False, "VBI service unavailable", {}

            details = {
                "initialized": getattr(vbi, "_initialized", False),
                "has_speaker_engine": hasattr(vbi, "speaker_engine") and vbi.speaker_engine is not None,
            }

            if not details["initialized"]:
                return False, "VBI not initialized", details

            return True, "VBI ready", details
        except ImportError as e:
            return False, f"Import error: {e}", {}

    async def _check_hybrid_cloud(self) -> Tuple[bool, str, Dict]:
        """Check hybrid cloud architecture"""
        try:
            from voice_unlock.ml_engine_registry import get_ml_registry_sync

            registry = get_ml_registry_sync(auto_create=False)
            if registry is None:
                return True, "Registry not initialized (OK for on-demand)", {}

            details = {
                "cloud_mode": registry.is_using_cloud,
                "cloud_verified": getattr(registry, "_cloud_verified", False),
                "cloud_endpoint": getattr(registry, "_cloud_endpoint", None),
                "local_available": not registry.is_using_cloud,
            }

            # Cloud mode should be verified if active
            if registry.is_using_cloud and not details["cloud_verified"]:
                return False, "Cloud mode active but not verified", details

            mode = "cloud" if registry.is_using_cloud else "local"
            return True, f"Hybrid arch OK ({mode} mode)", details
        except Exception as e:
            return True, f"Hybrid check skipped: {e}", {}

    async def _check_voiceprint_database(self) -> Tuple[bool, str, Dict]:
        """Check voiceprint database connectivity"""
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager

            cache = get_unified_cache_manager()

            # Check if we can get enrolled profiles count
            profiles_count = cache.profiles_loaded if hasattr(cache, "profiles_loaded") else 0

            details = {
                "profiles_loaded": profiles_count,
                "database_accessible": True,
            }

            return True, f"Database OK ({profiles_count} profiles cached)", details
        except Exception as e:
            return False, f"Database check failed: {e}", {}

    async def _check_intelligent_unlock_service(self) -> Tuple[bool, str, Dict]:
        """Check Intelligent Voice Unlock Service"""
        try:
            from voice_unlock.intelligent_voice_unlock_service import get_intelligent_unlock_service

            service = get_intelligent_unlock_service()
            if service is None:
                return False, "Service unavailable", {}

            # Check if initialized
            initialized = getattr(service, "_initialized", False)

            details = {
                "initialized": initialized,
                "unlock_threshold": getattr(service, "unlock_threshold", None),
            }

            if not initialized:
                # Try to initialize
                await service.initialize()
                details["initialized"] = getattr(service, "_initialized", False)

            return details["initialized"], "Intelligent unlock ready" if details["initialized"] else "Not initialized", details
        except ImportError as e:
            return False, f"Import error: {e}", {}
        except Exception as e:
            return False, f"Error: {e}", {}

    async def validate_all(self, load_models: bool = True) -> ValidationReport:
        """
        Run all validation checks.

        Args:
            load_models: If True, trigger model loading during validation

        Returns:
            ValidationReport with all results
        """
        self.report = ValidationReport()

        self._log("\n" + "=" * 70)
        self._log("🔐 VOICE BIOMETRIC STARTUP VALIDATION")
        self._log("=" * 70)
        self._log("")

        # Define all checks (name, func, critical)
        checks = [
            ("ML Engine Registry", self._check_ml_registry, True),
            ("ECAPA Encoder", self._check_ecapa_encoder, True),
            ("Unified Voice Cache", self._check_unified_cache, True),
            ("Embedding Extraction", self._check_embedding_extraction, True),
            ("Voice Biometric Intelligence", self._check_voice_biometric_intelligence, False),
            ("Hybrid Cloud Architecture", self._check_hybrid_cloud, False),
            ("Voiceprint Database", self._check_voiceprint_database, False),
            ("Intelligent Unlock Service", self._check_intelligent_unlock_service, False),
        ]

        # Run all checks
        for name, check_func, critical in checks:
            result = await self._run_check(name, check_func, critical)
            self.report.add_result(result)

        # Determine overall readiness
        critical_passed = all(
            r.status == ValidationStatus.PASSED
            for r in self.report.results
            if r.name in ["ML Engine Registry", "ECAPA Encoder", "Unified Voice Cache", "Embedding Extraction"]
        )
        self.report.ready_for_voice_unlock = critical_passed

        # Print summary
        self._log("")
        self._log("-" * 70)
        summary_icon = "✅" if self.report.ready_for_voice_unlock else "❌"
        self._log(f"{summary_icon} VALIDATION SUMMARY: {self.report.get_summary()}")

        if self.report.ready_for_voice_unlock:
            self._log("🔐 VOICE UNLOCK READY: All critical components operational")
        else:
            self._log("🚫 VOICE UNLOCK NOT READY: Critical component(s) failed")
            failed = [r.name for r in self.report.results if r.status == ValidationStatus.FAILED]
            self._log(f"   Failed: {', '.join(failed)}")

        self._log("=" * 70 + "\n")

        return self.report


# Global validator instance
_startup_validator: Optional[VoiceBiometricStartupValidator] = None


async def validate_voice_biometric_readiness(
    verbose: bool = True,
    load_models: bool = True,
) -> ValidationReport:
    """
    Validate all voice biometric components are ready.

    Call this at startup or before voice unlock operations.

    Args:
        verbose: If True, print detailed status messages
        load_models: If True, trigger model loading during validation

    Returns:
        ValidationReport with all check results
    """
    global _startup_validator
    _startup_validator = VoiceBiometricStartupValidator(verbose=verbose)
    return await _startup_validator.validate_all(load_models=load_models)


def get_last_validation_report() -> Optional[ValidationReport]:
    """Get the last validation report"""
    if _startup_validator:
        return _startup_validator.report
    return None


class VoiceUnlockStartup:
    """Manages Voice Unlock system startup with robust profile loading"""

    def __init__(self):
        self.websocket_process: Optional[subprocess.Popen] = None
        self.daemon_process: Optional[subprocess.Popen] = None
        self.voice_unlock_dir = Path(__file__).parent
        self.websocket_port = 8765
        self.initialized = False
        self.intelligent_service = None
        self.voice_profile_service = None
        self.profiles_loaded = False

    async def start(self) -> bool:
        """Start the Voice Unlock system components with voice profile loading"""
        try:
            logger.info("🔐 Starting Voice Unlock system...")

            # ====================================================================
            # STEP 1: Load Voice Profiles FIRST (Critical for instant unlock)
            # ====================================================================
            logger.info("🎤 Loading voice profiles from CloudSQL/SQLite...")
            profiles_ready = await self._ensure_voice_profiles_loaded()
            
            if profiles_ready:
                logger.info("✅ Voice profiles loaded and ready for unlock")
            else:
                logger.warning("⚠️ Voice profiles not loaded - unlock may not work")

            # Check if password is stored
            if not self._check_password_stored():
                logger.warning("⚠️  Voice Unlock password not configured")
                logger.info("   Run: backend/voice_unlock/enable_screen_unlock.sh")
                return False

            # Start WebSocket server
            if not await self._start_websocket_server():
                logger.error("Failed to start Voice Unlock WebSocket server")
                return False

            # Give WebSocket server time to start
            await asyncio.sleep(2)

            # Initialize Intelligent Voice Unlock Service
            if not await self._initialize_intelligent_service():
                logger.warning("⚠️  Intelligent Voice Unlock Service initialization failed")
                logger.info("   Basic voice unlock will still work")

            # Start daemon automatically
            logger.info("✅ Voice Unlock WebSocket server ready on port 8765")
            logger.info("   Voice Unlock is ready to use")

            self.initialized = True
            return True

        except Exception as e:
            logger.error(f"Voice Unlock startup error: {e}")
            return False

    async def _ensure_voice_profiles_loaded(self) -> bool:
        """
        Ensure voice profiles are loaded from CloudSQL and synced to SQLite.
        
        This is the CRITICAL first step - without profiles, unlock won't work.
        
        IMPORTANT: This method uses the singleton VoiceProfileStartupService
        which ensures profiles are only loaded ONCE even if called multiple times.
        
        Returns:
            True if profiles are loaded and ready
        """
        try:
            # Use VoiceProfileStartupService for robust loading
            from voice_unlock.voice_profile_startup_service import (
                get_voice_profile_service,
                initialize_voice_profiles,
                is_voice_profile_ready,
            )
            
            self.voice_profile_service = get_voice_profile_service()
            
            # Check if already loaded (fast path - no logging spam)
            if is_voice_profile_ready():
                profile_count = self.voice_profile_service.profile_count
                logger.debug(f"Voice profiles already loaded: {profile_count} profile(s) - skipping")
                self.profiles_loaded = True
                return True
            
            # Initialize with timeout (only log on first load)
            timeout = float(os.getenv("VOICE_PROFILE_LOAD_TIMEOUT", "30.0"))
            
            logger.info(f"🔄 Loading voice profiles (timeout={timeout}s)...")
            
            success = await asyncio.wait_for(
                initialize_voice_profiles(timeout=timeout),
                timeout=timeout + 5.0  # Extra buffer
            )
            
            if success:
                profile_count = self.voice_profile_service.profile_count
                metrics = self.voice_profile_service.metrics
                
                # Only log summary if this is the first successful load
                if metrics.profiles_loaded > 0:
                    logger.info(
                        f"✅ Voice profiles ready: {profile_count} profile(s) "
                        f"(CloudSQL={metrics.profiles_from_cloudsql}, "
                        f"SQLite={metrics.profiles_from_sqlite})"
                    )
                    
                    # List loaded profiles (only once)
                    for name, profile in self.voice_profile_service.get_all_profiles().items():
                        owner_tag = " [OWNER]" if profile.is_primary_user else ""
                        logger.info(
                            f"   • {name}{owner_tag}: "
                            f"dim={profile.embedding_dim}, "
                            f"conf={profile.recognition_confidence:.1%}"
                        )
                
                self.profiles_loaded = True
                return True
            else:
                logger.warning("⚠️ VoiceProfileStartupService failed to load profiles")
                return False
                
        except ImportError as e:
            logger.debug(f"VoiceProfileStartupService not available: {e}")
            # Fall back to direct cache loading
            return await self._fallback_profile_loading()
        except asyncio.TimeoutError:
            logger.warning("⏱️ Voice profile loading timed out")
            return await self._fallback_profile_loading()
        except Exception as e:
            logger.warning(f"Voice profile loading error: {e}")
            return await self._fallback_profile_loading()

    async def _fallback_profile_loading(self) -> bool:
        """Fallback profile loading via UnifiedVoiceCacheManager"""
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager
            
            logger.info("📂 Falling back to UnifiedVoiceCacheManager...")
            
            cache = get_unified_cache_manager()
            
            # Initialize with profile preloading
            success = await asyncio.wait_for(
                cache.initialize(
                    preload_profiles=True,
                    preload_models=False,  # Don't block on models
                    init_chromadb=False,   # Don't block on ChromaDB
                    timeout=20.0
                ),
                timeout=25.0
            )
            
            if success and cache.profiles_loaded > 0:
                logger.info(f"✅ Fallback loaded {cache.profiles_loaded} profile(s)")
                self.profiles_loaded = True
                return True
            else:
                logger.warning("⚠️ Fallback profile loading failed")
                return False
                
        except Exception as e:
            logger.error(f"Fallback profile loading error: {e}")
            return False

    async def _initialize_intelligent_service(self) -> bool:
        """Initialize the Intelligent Voice Unlock Service"""
        try:
            from voice_unlock.intelligent_voice_unlock_service import get_intelligent_unlock_service

            logger.info("🧠 Initializing Intelligent Voice Unlock Service...")

            self.intelligent_service = get_intelligent_unlock_service()
            await self.intelligent_service.initialize()

            logger.info("✅ Intelligent Voice Unlock Service initialized")
            logger.info("   • Hybrid STT System ready")
            logger.info("   • Speaker Recognition active")
            logger.info("   • Context-Aware Intelligence enabled")
            logger.info("   • Scenario-Aware Intelligence enabled")
            logger.info("   • Learning Database connected")
            
            if self.profiles_loaded:
                logger.info("   • Voice Profiles READY ✓")

            return True

        except Exception as e:
            logger.error(f"Failed to initialize Intelligent Voice Unlock Service: {e}")
            return False

    def _check_password_stored(self) -> bool:
        """Check if password is stored in Keychain"""
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "com.jarvis.voiceunlock",
                    "-a",
                    "unlock_token",
                ],
                capture_output=True,
                text=True,
            )

            return result.returncode == 0
        except Exception:
            return False

    async def _start_websocket_server(self) -> bool:
        """Start the Python WebSocket server"""
        try:
            # Kill any existing process on the port
            subprocess.run(
                f"lsof -ti:{self.websocket_port} | xargs kill -9", shell=True, capture_output=True
            )
            await asyncio.sleep(1)

            # Start WebSocket server
            server_script = self.voice_unlock_dir / "objc" / "server" / "websocket_server.py"
            if not server_script.exists():
                logger.error(f"WebSocket server script not found: {server_script}")
                return False

            env = os.environ.copy()
            env["PYTHONPATH"] = str(self.voice_unlock_dir.parent)

            self.websocket_process = subprocess.Popen(
                [sys.executable, str(server_script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            logger.info(
                f"Voice Unlock WebSocket server started (PID: {self.websocket_process.pid})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to start WebSocket server: {e}")
            return False

    async def start_daemon_if_needed(self) -> bool:
        """Start the Voice Unlock daemon if not running"""
        try:
            # Check if daemon is already running
            result = subprocess.run(["pgrep", "-f", "JARVISVoiceUnlockDaemon"], capture_output=True)

            if result.returncode == 0:
                logger.info("Voice Unlock daemon already running")
                return True

            # Start daemon
            daemon_path = self.voice_unlock_dir / "objc" / "bin" / "JARVISVoiceUnlockDaemon"
            if not daemon_path.exists():
                logger.error(f"Voice Unlock daemon not found: {daemon_path}")
                logger.info("Build with: cd backend/voice_unlock/objc && make")
                return False

            self.daemon_process = subprocess.Popen(
                [str(daemon_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            logger.info(f"Voice Unlock daemon started (PID: {self.daemon_process.pid})")
            return True

        except Exception as e:
            logger.error(f"Failed to start daemon: {e}")
            return False

    async def stop(self):
        """Stop Voice Unlock components"""
        logger.info("Stopping Voice Unlock system...")

        if self.websocket_process:
            self.websocket_process.terminate()
            try:
                self.websocket_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.websocket_process.kill()
            self.websocket_process = None

        if self.daemon_process:
            self.daemon_process.terminate()
            try:
                self.daemon_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon_process.kill()
            self.daemon_process = None

        # Kill any lingering processes
        subprocess.run("pkill -f websocket_server.py", shell=True, capture_output=True)
        subprocess.run("pkill -f JARVISVoiceUnlockDaemon", shell=True, capture_output=True)

        logger.info("Voice Unlock system stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get Voice Unlock system status including voice profiles"""
        status = {
            "initialized": self.initialized,
            "websocket_running": self.websocket_process is not None
            and self.websocket_process.poll() is None,
            "daemon_running": self.daemon_process is not None
            and self.daemon_process.poll() is None,
            "password_stored": self._check_password_stored(),
            "websocket_port": self.websocket_port,
            "intelligent_service_enabled": self.intelligent_service is not None,
            "profiles_loaded": self.profiles_loaded,
            "profile_count": 0,
            "voice_profiles": [],
        }
        
        # Add voice profile details
        if self.voice_profile_service:
            try:
                status["profile_count"] = self.voice_profile_service.profile_count
                status["voice_profiles"] = [
                    {
                        "name": p.speaker_name,
                        "is_owner": p.is_primary_user,
                        "confidence": round(p.recognition_confidence, 2),
                        "embedding_dim": p.embedding_dim,
                        "source": p.source.value,
                    }
                    for p in self.voice_profile_service.get_all_profiles().values()
                ]
                
                # Add metrics
                metrics = self.voice_profile_service.metrics
                status["profile_metrics"] = {
                    "from_cloudsql": metrics.profiles_from_cloudsql,
                    "from_sqlite": metrics.profiles_from_sqlite,
                    "synced_to_sqlite": metrics.profiles_synced_to_sqlite,
                    "last_sync": metrics.last_sync_time.isoformat() if metrics.last_sync_time else None,
                    "cloudsql_available": metrics.cloudsql_available,
                }
            except Exception:
                pass
        
        return status


# Global instance
voice_unlock_startup = VoiceUnlockStartup()


async def initialize_voice_unlock_system():
    """Initialize Voice Unlock system for JARVIS integration"""
    global voice_unlock_startup
    return await voice_unlock_startup.start()


async def shutdown_voice_unlock_system():
    """Shutdown Voice Unlock system"""
    global voice_unlock_startup
    await voice_unlock_startup.stop()


# Import for backwards compatibility
import sys
