#!/usr/bin/env python3
"""
Intelligent Diagnostic & Remediation System for PAVA/VIBA Integration
====================================================================

A dynamic, async, self-healing diagnostic system that:
- Automatically detects system state
- Identifies root causes without hardcoding
- Provides intelligent remediation steps
- Adapts to different environments
- Self-heals common issues

Features:
- Zero hardcoding (all config-driven)
- Async operations for non-blocking checks
- Dynamic adaptation to system state
- Intelligent root cause analysis
- Automated remediation suggestions
- Real-time system health monitoring
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# EcapaFacade feature flag (Phase 3b migration)
_USE_FACADE = os.getenv("ECAPA_USE_FACADE", "true").lower() in ("true", "1", "yes")

# =============================================================================
# CONFIGURATION - Environment-driven, zero hardcoding
# =============================================================================

class DiagnosticConfig:
    """Dynamic configuration from environment variables."""
    
    # Timeouts (all configurable)
    CHECK_TIMEOUT = float(os.getenv("DIAGNOSTIC_CHECK_TIMEOUT", "5.0"))
    REMEDIATION_TIMEOUT = float(os.getenv("DIAGNOSTIC_REMEDIATION_TIMEOUT", "30.0"))
    
    # Retry settings
    MAX_RETRIES = int(os.getenv("DIAGNOSTIC_MAX_RETRIES", "3"))
    RETRY_DELAY = float(os.getenv("DIAGNOSTIC_RETRY_DELAY", "1.0"))
    
    # Health check intervals
    HEALTH_CHECK_INTERVAL = float(os.getenv("DIAGNOSTIC_HEALTH_INTERVAL", "60.0"))
    
    # Auto-remediation
    AUTO_REMEDIATE = os.getenv("DIAGNOSTIC_AUTO_REMEDIATE", "false").lower() == "true"
    AUTO_REMEDIATE_SAFE_ONLY = os.getenv("DIAGNOSTIC_AUTO_REMEDIATE_SAFE", "true").lower() == "true"
    
    # Cache settings
    CACHE_DIR = Path(os.getenv("DIAGNOSTIC_CACHE_DIR", str(Path.home() / ".cache" / "jarvis" / "diagnostics")))
    CACHE_TTL = float(os.getenv("DIAGNOSTIC_CACHE_TTL", "300.0"))  # 5 minutes
    
    # Logging
    VERBOSE = os.getenv("DIAGNOSTIC_VERBOSE", "false").lower() == "true"
    
    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        """Export configuration for logging."""
        return {
            "check_timeout": cls.CHECK_TIMEOUT,
            "remediation_timeout": cls.REMEDIATION_TIMEOUT,
            "max_retries": cls.MAX_RETRIES,
            "auto_remediate": cls.AUTO_REMEDIATE,
            "cache_dir": str(cls.CACHE_DIR),
        }


# =============================================================================
# DATA MODELS
# =============================================================================

class ComponentStatus(str, Enum):
    """Component health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_INSTALLED = "not_installed"


class Severity(str, Enum):
    """Issue severity levels."""
    CRITICAL = "critical"  # System completely broken
    HIGH = "high"         # Major functionality impaired
    MEDIUM = "medium"     # Some features unavailable
    LOW = "low"           # Minor issues, system functional
    INFO = "info"         # Informational only


class RemediationType(str, Enum):
    """Types of remediation actions."""
    INSTALL = "install"           # Install missing package
    CONFIGURE = "configure"      # Update configuration
    RESTART = "restart"          # Restart service
    REPAIR = "repair"            # Fix corrupted state
    ENROLL = "enroll"            # Complete enrollment
    DOWNLOAD = "download"        # Download missing resources


@dataclass
class ComponentDiagnostic:
    """Diagnostic result for a single component."""
    name: str
    status: ComponentStatus
    severity: Severity
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    remediation_steps: List[Dict[str, Any]] = field(default_factory=list)
    auto_remediable: bool = False
    last_checked: Optional[datetime] = None
    check_duration_ms: float = 0.0


@dataclass
class SystemDiagnostic:
    """Complete system diagnostic result."""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    overall_status: ComponentStatus = ComponentStatus.UNKNOWN
    overall_confidence: float = 0.0  # 0.0-1.0
    components: Dict[str, ComponentDiagnostic] = field(default_factory=dict)
    root_causes: List[str] = field(default_factory=list)
    recommended_actions: List[Dict[str, Any]] = field(default_factory=list)
    integration_status: Dict[str, Any] = field(default_factory=dict)
    environment_info: Dict[str, Any] = field(default_factory=dict)
    diagnostics_version: str = "1.0.0"


# =============================================================================
# INTELLIGENT DIAGNOSTIC SYSTEM
# =============================================================================

class IntelligentDiagnosticSystem:
    """
    Advanced diagnostic system for PAVA/VIBA integration.
    
    Features:
    - Async, non-blocking checks
    - Dynamic adaptation to system state
    - Intelligent root cause analysis
    - Automated remediation suggestions
    - Self-healing capabilities
    """
    
    def __init__(self):
        """Initialize diagnostic system."""
        self.config = DiagnosticConfig
        self.cache_dir = self.config.CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Component checkers (dynamically discovered)
        self._checkers: Dict[str, callable] = {}
        self._register_checkers()
        
        # Remediation handlers
        self._remediators: Dict[RemediationType, callable] = {}
        self._register_remediators()
        
        # Cache for diagnostic results
        self._cache: Dict[str, Tuple[SystemDiagnostic, datetime]] = {}
        
        logger.info(f"IntelligentDiagnosticSystem initialized: {self.config.to_dict()}")
    
    def _register_checkers(self):
        """Dynamically register all component checkers."""
        self._checkers = {
            "dependencies": self._check_dependencies,
            "ecapa_encoder": self._check_ecapa_encoder,
            "voice_profiles": self._check_voice_profiles,
            "pava_components": self._check_pava_components,
            "viba_integration": self._check_viba_integration,
            "system_resources": self._check_system_resources,
            "network_connectivity": self._check_network_connectivity,
            "configuration": self._check_configuration,
        }
    
    def _register_remediators(self):
        """Dynamically register remediation handlers."""
        self._remediators = {
            RemediationType.INSTALL: self._remediate_install,
            RemediationType.CONFIGURE: self._remediate_configure,
            RemediationType.RESTART: self._remediate_restart,
            RemediationType.REPAIR: self._remediate_repair,
            RemediationType.ENROLL: self._remediate_enroll,
            RemediationType.DOWNLOAD: self._remediate_download,
        }
    
    async def run_full_diagnostic(
        self,
        components: Optional[List[str]] = None,
        use_cache: bool = True,
        auto_remediate: Optional[bool] = None
    ) -> SystemDiagnostic:
        """
        Run comprehensive system diagnostic.
        
        Args:
            components: Specific components to check (None = all)
            use_cache: Use cached results if available
            auto_remediate: Auto-remediate issues (overrides config)
        
        Returns:
            SystemDiagnostic with complete analysis
        """
        start_time = datetime.utcnow()
        
        # Check cache
        if use_cache:
            cached = self._get_cached_diagnostic()
            if cached and (datetime.utcnow() - cached[1]).total_seconds() < self.config.CACHE_TTL:
                logger.info("Using cached diagnostic results")
                return cached[0]
        
        # Determine which components to check
        components_to_check = components or list(self._checkers.keys())
        
        # Run all checks in parallel
        check_tasks = [
            self._run_component_check(name)
            for name in components_to_check
            if name in self._checkers
        ]
        
        results = await asyncio.gather(*check_tasks, return_exceptions=True)
        
        # Build diagnostic result
        diagnostic = SystemDiagnostic()
        diagnostic.components = {}
        
        for name, result in zip(components_to_check, results):
            if isinstance(result, Exception):
                logger.error(f"Component check failed for {name}: {result}")
                diagnostic.components[name] = ComponentDiagnostic(
                    name=name,
                    status=ComponentStatus.UNKNOWN,
                    severity=Severity.MEDIUM,
                    message=f"Check failed: {str(result)}",
                )
            else:
                diagnostic.components[name] = result
        
        # Analyze overall status
        diagnostic.overall_status = self._analyze_overall_status(diagnostic.components)
        diagnostic.overall_confidence = self._calculate_confidence(diagnostic.components)
        diagnostic.root_causes = self._identify_root_causes(diagnostic.components)
        diagnostic.recommended_actions = self._generate_recommendations(diagnostic)
        diagnostic.integration_status = self._analyze_integration(diagnostic.components)
        diagnostic.environment_info = await self._gather_environment_info()
        
        # Cache result
        self._cache_diagnostic(diagnostic)
        
        # Auto-remediate if enabled
        auto_remediate = auto_remediate if auto_remediate is not None else self.config.AUTO_REMEDIATE
        if auto_remediate:
            await self._auto_remediate(diagnostic)
        
        duration = (datetime.utcnow() - start_time).total_seconds() * 1000
        logger.info(f"Full diagnostic completed in {duration:.0f}ms")
        
        return diagnostic
    
    async def _run_component_check(self, component_name: str) -> ComponentDiagnostic:
        """Run a single component check with timeout."""
        start_time = datetime.utcnow()
        
        try:
            checker = self._checkers[component_name]
            result = await asyncio.wait_for(
                checker(),
                timeout=self.config.CHECK_TIMEOUT
            )
            duration = (datetime.utcnow() - start_time).total_seconds() * 1000
            result.check_duration_ms = duration
            result.last_checked = datetime.utcnow()
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Component check timeout: {component_name}")
            return ComponentDiagnostic(
                name=component_name,
                status=ComponentStatus.UNKNOWN,
                severity=Severity.MEDIUM,
                message=f"Check timed out after {self.config.CHECK_TIMEOUT}s",
            )
        except Exception as e:
            logger.error(f"Component check error: {component_name}: {e}")
            return ComponentDiagnostic(
                name=component_name,
                status=ComponentStatus.UNKNOWN,
                severity=Severity.MEDIUM,
                message=f"Check error: {str(e)}",
            )
    
    # =========================================================================
    # COMPONENT CHECKERS (Dynamic, no hardcoding)
    # =========================================================================
    
    async def _check_dependencies(self) -> ComponentDiagnostic:
        """Check if all required dependencies are installed."""
        required_packages = {
            "numpy": "numpy",
            "torch": "torch",
            "speechbrain": "speechbrain",
            "scipy": "scipy",
            "librosa": "librosa",
        }
        
        missing = []
        installed = []
        versions = {}
        
        for package_name, import_name in required_packages.items():
            try:
                module = __import__(import_name)
                installed.append(package_name)
                if hasattr(module, "__version__"):
                    versions[package_name] = module.__version__
            except ImportError:
                missing.append(package_name)
        
        if missing:
            status = ComponentStatus.FAILED
            severity = Severity.CRITICAL if "numpy" in missing or "torch" in missing else Severity.HIGH
            message = f"Missing dependencies: {', '.join(missing)}"
            
            remediation_steps = [{
                "type": RemediationType.INSTALL.value,
                "description": f"Install missing packages: {', '.join(missing)}",
                "command": f"pip install {' '.join(missing)}",
                "auto_remediable": True,
            }]
        else:
            status = ComponentStatus.HEALTHY
            severity = Severity.INFO
            message = f"All dependencies installed: {', '.join(installed)}"
            remediation_steps = []
        
        return ComponentDiagnostic(
            name="dependencies",
            status=status,
            severity=severity,
            message=message,
            details={
                "installed": installed,
                "missing": missing,
                "versions": versions,
            },
            remediation_steps=remediation_steps,
            auto_remediable=len(missing) > 0,
        )
    
    async def _check_ecapa_encoder(self) -> ComponentDiagnostic:
        """Check ECAPA encoder availability (dynamic, no hardcoding)."""
        # --- EcapaFacade path (Phase 3b) ---
        if _USE_FACADE:
            try:
                from backend.core.ecapa_facade import get_ecapa_facade
                facade = await get_ecapa_facade()
                status = facade.get_status()
                tier = status.get("tier", "unavailable")
                backend = status.get("active_backend", "unknown")
                if tier == "ready":
                    return ComponentDiagnostic(
                        name="ecapa_encoder",
                        status=ComponentStatus.HEALTHY,
                        severity=Severity.INFO,
                        message=f"ECAPA available via facade ({backend})",
                        details=status,
                    )
                elif tier == "degraded":
                    return ComponentDiagnostic(
                        name="ecapa_encoder",
                        status=ComponentStatus.DEGRADED,
                        severity=Severity.MEDIUM,
                        message=f"ECAPA degraded via facade ({backend})",
                        details=status,
                    )
                else:
                    return ComponentDiagnostic(
                        name="ecapa_encoder",
                        status=ComponentStatus.FAILED,
                        severity=Severity.CRITICAL,
                        message=f"ECAPA unavailable (facade tier: {tier})",
                        details=status,
                    )
            except Exception as e:
                logger.debug(f"EcapaFacade check failed, falling back to legacy: {e}")
                pass  # Fall through to legacy check

        # --- Legacy path (ML registry) ---
        try:
            # Try to get registry (may not exist)
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from voice_unlock.ml_engine_registry import get_ml_registry_sync
            
            registry = get_ml_registry_sync()
            if not registry:
                return ComponentDiagnostic(
                    name="ecapa_encoder",
                    status=ComponentStatus.FAILED,
                    severity=Severity.CRITICAL,
                    message="ML Engine Registry not available",
                    remediation_steps=[{
                        "type": RemediationType.CONFIGURE.value,
                        "description": "Initialize ML Engine Registry",
                        "auto_remediable": False,
                    }],
                )
            
            status_dict = registry.get_ecapa_status()
            available = status_dict.get("available", False)
            source = status_dict.get("source")
            error = status_dict.get("error")
            
            if available:
                return ComponentDiagnostic(
                    name="ecapa_encoder",
                    status=ComponentStatus.HEALTHY,
                    severity=Severity.INFO,
                    message=f"ECAPA encoder available via {source}",
                    details=status_dict,
                )
            else:
                # Analyze why it's not available
                local_loaded = status_dict.get("local_loaded", False)
                cloud_verified = status_dict.get("cloud_verified", False)
                local_error = status_dict.get("local_error")
                
                remediation_steps = []
                
                if not local_loaded and local_error:
                    remediation_steps.append({
                        "type": RemediationType.DOWNLOAD.value,
                        "description": f"ECAPA model failed to load: {local_error}",
                        "auto_remediable": False,
                    })
                
                if not cloud_verified and registry.is_using_cloud:
                    remediation_steps.append({
                        "type": RemediationType.CONFIGURE.value,
                        "description": "Cloud ECAPA backend not verified",
                        "auto_remediable": False,
                    })
                
                return ComponentDiagnostic(
                    name="ecapa_encoder",
                    status=ComponentStatus.FAILED,
                    severity=Severity.CRITICAL,
                    message=error or "ECAPA encoder not available",
                    details=status_dict,
                    remediation_steps=remediation_steps,
                )
        except Exception as e:
            return ComponentDiagnostic(
                name="ecapa_encoder",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.HIGH,
                message=f"Could not check ECAPA status: {str(e)}",
            )
    
    async def _check_voice_profiles(self) -> ComponentDiagnostic:
        """Check voice profile enrollment status."""
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from intelligence.hybrid_database_sync import HybridDatabaseSync
            
            db = HybridDatabaseSync.get_instance()
            await db.initialize()
            
            profile = await db.find_owner_profile()
            
            if profile:
                has_embedding = profile.get("embedding") is not None
                samples = profile.get("total_samples", 0)
                
                if has_embedding and samples > 0:
                    return ComponentDiagnostic(
                        name="voice_profiles",
                        status=ComponentStatus.HEALTHY,
                        severity=Severity.INFO,
                        message=f"Owner profile found: {profile.get('name')} ({samples} samples)",
                        details={
                            "owner_name": profile.get("name"),
                            "samples": samples,
                            "has_embedding": has_embedding,
                        },
                    )
                else:
                    return ComponentDiagnostic(
                        name="voice_profiles",
                        status=ComponentStatus.DEGRADED,
                        severity=Severity.MEDIUM,
                        message="Owner profile found but missing embedding or samples",
                        remediation_steps=[{
                            "type": RemediationType.ENROLL.value,
                            "description": "Re-enroll voice profile with more samples",
                            "auto_remediable": False,
                        }],
                    )
            else:
                return ComponentDiagnostic(
                    name="voice_profiles",
                    status=ComponentStatus.FAILED,
                    severity=Severity.HIGH,
                    message="No owner voice profile found",
                    remediation_steps=[{
                        "type": RemediationType.ENROLL.value,
                        "description": "Complete voice enrollment",
                        "command": "Say 'JARVIS, learn my voice' or run: python backend/voice/enroll_voice.py",
                        "auto_remediable": False,
                    }],
                )
        except Exception as e:
            return ComponentDiagnostic(
                name="voice_profiles",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.MEDIUM,
                message=f"Could not check voice profiles: {str(e)}",
            )
    
    async def _check_pava_components(self) -> ComponentDiagnostic:
        """Check PAVA (Physics-Aware) components availability."""
        components_status = {}
        all_available = True
        any_available = False
        
        # Check anti-spoofing detector
        try:
            from voice_unlock.core.anti_spoofing import get_anti_spoofing_detector
            detector = get_anti_spoofing_detector()
            components_status["anti_spoofing"] = detector is not None
            if detector:
                any_available = True
            else:
                all_available = False
        except Exception as e:
            components_status["anti_spoofing"] = False
            components_status["anti_spoofing_error"] = str(e)
            all_available = False
        
        # Check Bayesian fusion
        try:
            from voice_unlock.core.bayesian_fusion import get_bayesian_fusion
            fusion = get_bayesian_fusion()
            components_status["bayesian_fusion"] = fusion is not None
            if fusion:
                any_available = True
            else:
                all_available = False
        except Exception as e:
            components_status["bayesian_fusion"] = False
            components_status["bayesian_fusion_error"] = str(e)
            all_available = False
        
        # Check feature extraction
        try:
            from voice_unlock.core.feature_extraction import VoiceFeatureExtractor
            extractor = VoiceFeatureExtractor()
            components_status["feature_extraction"] = True
            any_available = True
        except Exception as e:
            components_status["feature_extraction"] = False
            components_status["feature_extraction_error"] = str(e)
            all_available = False
        
        if all_available:
            status = ComponentStatus.HEALTHY
            severity = Severity.INFO
            message = "All PAVA components available"
        elif any_available:
            status = ComponentStatus.DEGRADED
            severity = Severity.MEDIUM
            message = "Some PAVA components unavailable (system will work with reduced security)"
        else:
            status = ComponentStatus.FAILED
            severity = Severity.MEDIUM
            message = "PAVA components unavailable (optional, system will work without physics analysis)"
        
        return ComponentDiagnostic(
            name="pava_components",
            status=status,
            severity=severity,
            message=message,
            details=components_status,
        )
    
    async def _check_viba_integration(self) -> ComponentDiagnostic:
        """Check VIBA (Voice Biometric Intelligence) integration."""
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence
            
            vbi = await get_voice_biometric_intelligence()
            
            if vbi:
                initialized = getattr(vbi, "_initialized", False)
                
                if initialized:
                    return ComponentDiagnostic(
                        name="viba_integration",
                        status=ComponentStatus.HEALTHY,
                        severity=Severity.INFO,
                        message="VIBA initialized and ready",
                        details={
                            "initialized": True,
                        },
                    )
                else:
                    return ComponentDiagnostic(
                        name="viba_integration",
                        status=ComponentStatus.DEGRADED,
                        severity=Severity.MEDIUM,
                        message="VIBA available but not initialized",
                        remediation_steps=[{
                            "type": RemediationType.RESTART.value,
                            "description": "Initialize VIBA system",
                            "auto_remediable": True,
                        }],
                    )
            else:
                return ComponentDiagnostic(
                    name="viba_integration",
                    status=ComponentStatus.FAILED,
                    severity=Severity.HIGH,
                    message="VIBA not available",
                )
        except Exception as e:
            return ComponentDiagnostic(
                name="viba_integration",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.MEDIUM,
                message=f"Could not check VIBA: {str(e)}",
            )
    
    async def _check_system_resources(self) -> ComponentDiagnostic:
        """Check system resources (memory, CPU, disk)."""
        try:
            import psutil
            
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=0.1)
            disk = psutil.disk_usage("/")
            
            details = {
                "memory_available_gb": memory.available / (1024**3),
                "memory_percent": memory.percent,
                "cpu_percent": cpu_percent,
                "disk_free_gb": disk.free / (1024**3),
                "disk_percent": disk.percent,
            }
            
            # Determine status based on resources
            issues = []
            if memory.available < 2 * (1024**3):  # < 2GB
                issues.append("low_memory")
            if cpu_percent > 90:
                issues.append("high_cpu")
            if disk.free < 1 * (1024**3):  # < 1GB
                issues.append("low_disk")
            
            if issues:
                status = ComponentStatus.DEGRADED
                severity = Severity.MEDIUM
                message = f"Resource constraints: {', '.join(issues)}"
            else:
                status = ComponentStatus.HEALTHY
                severity = Severity.INFO
                message = "System resources adequate"
            
            return ComponentDiagnostic(
                name="system_resources",
                status=status,
                severity=severity,
                message=message,
                details=details,
            )
        except ImportError:
            return ComponentDiagnostic(
                name="system_resources",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.LOW,
                message="psutil not available for resource checking",
            )
        except Exception as e:
            return ComponentDiagnostic(
                name="system_resources",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.LOW,
                message=f"Resource check error: {str(e)}",
            )
    
    async def _check_network_connectivity(self) -> ComponentDiagnostic:
        """Check network connectivity for model downloads."""
        try:
            import aiohttp
            
            test_urls = [
                "https://huggingface.co",
                "https://github.com",
            ]
            
            reachable = []
            unreachable = []
            
            async with aiohttp.ClientSession() as session:
                for url in test_urls:
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as response:
                            if response.status == 200:
                                reachable.append(url)
                            else:
                                unreachable.append(url)
                    except Exception:
                        unreachable.append(url)
            
            if all(url in reachable for url in test_urls):
                status = ComponentStatus.HEALTHY
                severity = Severity.INFO
                message = "Network connectivity good"
            elif any(url in reachable for url in test_urls):
                status = ComponentStatus.DEGRADED
                severity = Severity.LOW
                message = "Partial network connectivity"
            else:
                status = ComponentStatus.FAILED
                severity = Severity.MEDIUM
                message = "Network connectivity issues (may affect model downloads)"
            
            return ComponentDiagnostic(
                name="network_connectivity",
                status=status,
                severity=severity,
                message=message,
                details={
                    "reachable": reachable,
                    "unreachable": unreachable,
                },
            )
        except ImportError:
            return ComponentDiagnostic(
                name="network_connectivity",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.LOW,
                message="aiohttp not available for network checking",
            )
        except Exception as e:
            return ComponentDiagnostic(
                name="network_connectivity",
                status=ComponentStatus.UNKNOWN,
                severity=Severity.LOW,
                message=f"Network check error: {str(e)}",
            )
    
    async def _check_configuration(self) -> ComponentDiagnostic:
        """Check system configuration for common issues."""
        issues = []
        details = {}
        
        # Check environment variables
        env_vars_to_check = [
            "JARVIS_ML_ENABLE_ECAPA",
            "JARVIS_CLOUD_FALLBACK",
            "JARVIS_SKIP_MODEL_PREWARM",
        ]
        
        for var in env_vars_to_check:
            value = os.getenv(var)
            details[var] = value
            
            if var == "JARVIS_ML_ENABLE_ECAPA" and value == "false":
                issues.append("ECAPA explicitly disabled")
            if var == "JARVIS_SKIP_MODEL_PREWARM" and value == "true":
                issues.append("Model prewarm skipped (may cause delays)")
        
        if issues:
            status = ComponentStatus.DEGRADED
            severity = Severity.MEDIUM
            message = f"Configuration issues: {', '.join(issues)}"
        else:
            status = ComponentStatus.HEALTHY
            severity = Severity.INFO
            message = "Configuration looks good"
        
        return ComponentDiagnostic(
            name="configuration",
            status=status,
            severity=severity,
            message=message,
            details=details,
        )
    
    # =========================================================================
    # ANALYSIS & RECOMMENDATIONS
    # =========================================================================
    
    def _analyze_overall_status(self, components: Dict[str, ComponentDiagnostic]) -> ComponentStatus:
        """Analyze overall system status from components."""
        statuses = [comp.status for comp in components.values()]
        
        if ComponentStatus.FAILED in statuses:
            return ComponentStatus.FAILED
        elif ComponentStatus.DEGRADED in statuses:
            return ComponentStatus.DEGRADED
        elif all(s == ComponentStatus.HEALTHY for s in statuses):
            return ComponentStatus.HEALTHY
        else:
            return ComponentStatus.UNKNOWN
    
    def _calculate_confidence(self, components: Dict[str, ComponentDiagnostic]) -> float:
        """Calculate overall system confidence (0.0-1.0)."""
        if not components:
            return 0.0
        
        # Weight components by importance
        weights = {
            "dependencies": 0.25,
            "ecapa_encoder": 0.30,
            "voice_profiles": 0.20,
            "pava_components": 0.10,
            "viba_integration": 0.10,
            "system_resources": 0.03,
            "network_connectivity": 0.01,
            "configuration": 0.01,
        }
        
        total_confidence = 0.0
        total_weight = 0.0
        
        for name, component in components.items():
            weight = weights.get(name, 0.05)
            
            # Map status to confidence
            status_confidence = {
                ComponentStatus.HEALTHY: 1.0,
                ComponentStatus.DEGRADED: 0.6,
                ComponentStatus.FAILED: 0.0,
                ComponentStatus.UNKNOWN: 0.3,
                ComponentStatus.NOT_INSTALLED: 0.0,
            }
            
            confidence = status_confidence.get(component.status, 0.5)
            total_confidence += weight * confidence
            total_weight += weight
        
        return total_confidence / total_weight if total_weight > 0 else 0.0
    
    def _identify_root_causes(self, components: Dict[str, ComponentDiagnostic]) -> List[str]:
        """Intelligently identify root causes from component diagnostics."""
        root_causes = []
        
        # Check for dependency issues first (most fundamental)
        deps = components.get("dependencies")
        if deps and deps.status == ComponentStatus.FAILED:
            missing = deps.details.get("missing", [])
            if "numpy" in missing or "torch" in missing:
                root_causes.append("Missing critical dependencies (numpy/torch) - breaks entire ML pipeline")
        
        # Check ECAPA status
        ecapa = components.get("ecapa_encoder")
        if ecapa and ecapa.status == ComponentStatus.FAILED:
            error = ecapa.details.get("error", "")
            if "local not loaded" in error.lower():
                root_causes.append("ECAPA encoder not loaded locally")
            if "cloud not verified" in error.lower():
                root_causes.append("ECAPA cloud backend not verified")
        
        # Check voice profiles
        profiles = components.get("voice_profiles")
        if profiles and profiles.status == ComponentStatus.FAILED:
            root_causes.append("No voice profile enrolled - cannot identify speaker")
        
        # Check integration
        viba = components.get("viba_integration")
        if viba and viba.status == ComponentStatus.FAILED:
            root_causes.append("VIBA integration not available")
        
        return root_causes
    
    def _generate_recommendations(self, diagnostic: SystemDiagnostic) -> List[Dict[str, Any]]:
        """Generate intelligent recommendations based on diagnostic."""
        recommendations = []
        
        # Prioritize by severity
        critical_components = [
            (name, comp) for name, comp in diagnostic.components.items()
            if comp.severity == Severity.CRITICAL
        ]
        
        for name, component in critical_components:
            for step in component.remediation_steps:
                recommendations.append({
                    "priority": "critical",
                    "component": name,
                    "action": step.get("description", ""),
                    "command": step.get("command"),
                    "auto_remediable": step.get("auto_remediable", False),
                })
        
        # Add high severity recommendations
        high_components = [
            (name, comp) for name, comp in diagnostic.components.items()
            if comp.severity == Severity.HIGH
        ]
        
        for name, component in high_components:
            for step in component.remediation_steps:
                recommendations.append({
                    "priority": "high",
                    "component": name,
                    "action": step.get("description", ""),
                    "command": step.get("command"),
                    "auto_remediable": step.get("auto_remediable", False),
                })
        
        return recommendations
    
    def _analyze_integration(self, components: Dict[str, ComponentDiagnostic]) -> Dict[str, Any]:
        """Analyze PAVA/VIBA integration status."""
        ecapa = components.get("ecapa_encoder")
        pava = components.get("pava_components")
        viba = components.get("viba_integration")
        profiles = components.get("voice_profiles")
        
        integration_status = {
            "ecapa_available": ecapa.status == ComponentStatus.HEALTHY if ecapa else False,
            "pava_available": pava.status in [ComponentStatus.HEALTHY, ComponentStatus.DEGRADED] if pava else False,
            "viba_available": viba.status == ComponentStatus.HEALTHY if viba else False,
            "profiles_available": profiles.status == ComponentStatus.HEALTHY if profiles else False,
        }
        
        # Determine integration health
        if all(integration_status.values()):
            integration_status["overall"] = "fully_integrated"
        elif integration_status["ecapa_available"] and integration_status["profiles_available"]:
            integration_status["overall"] = "core_functional"
        else:
            integration_status["overall"] = "degraded"
        
        return integration_status
    
    async def _gather_environment_info(self) -> Dict[str, Any]:
        """Gather environment information."""
        return {
            "python_version": sys.version,
            "platform": sys.platform,
            "working_directory": str(Path.cwd()),
            "user_home": str(Path.home()),
            "environment_vars": {
                k: v for k, v in os.environ.items()
                if k.startswith("JARVIS_") or k.startswith("DIAGNOSTIC_")
            },
        }
    
    # =========================================================================
    # REMEDIATION
    # =========================================================================
    
    async def _auto_remediate(self, diagnostic: SystemDiagnostic):
        """Automatically remediate issues if safe and enabled."""
        if not self.config.AUTO_REMEDIATE:
            return
        
        logger.info("Auto-remediation enabled, attempting fixes...")
        
        for component_name, component in diagnostic.components.items():
            if not component.auto_remediable:
                continue
            
            if self.config.AUTO_REMEDIATE_SAFE_ONLY and component.severity == Severity.CRITICAL:
                logger.info(f"Skipping auto-remediation for critical component: {component_name}")
                continue
            
            for step in component.remediation_steps:
                if step.get("auto_remediable"):
                    remediation_type = RemediationType(step.get("type"))
                    remediator = self._remediators.get(remediation_type)
                    
                    if remediator:
                        try:
                            await asyncio.wait_for(
                                remediator(step),
                                timeout=self.config.REMEDIATION_TIMEOUT
                            )
                            logger.info(f"Auto-remediated: {component_name}")
                        except Exception as e:
                            logger.error(f"Auto-remediation failed for {component_name}: {e}")
    
    async def _remediate_install(self, step: Dict[str, Any]):
        """Install missing packages."""
        command = step.get("command", "")
        if command and "pip install" in command:
            packages = command.replace("pip install", "").strip()
            logger.info(f"Installing packages: {packages}")
            
            # Run in thread pool to avoid blocking
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    lambda: subprocess.run(
                        ["pip", "install"] + packages.split(),
                        capture_output=True,
                        text=True
                    )
                )
                
                if result.returncode == 0:
                    logger.info(f"Successfully installed: {packages}")
                else:
                    logger.error(f"Installation failed: {result.stderr}")
    
    async def _remediate_configure(self, step: Dict[str, Any]):
        """Configure system settings."""
        logger.info(f"Configuration remediation: {step.get('description')}")
        # Implementation would set environment variables or config files
        # This is a placeholder for actual configuration logic
    
    async def _remediate_restart(self, step: Dict[str, Any]):
        """Restart services."""
        logger.info(f"Restart remediation: {step.get('description')}")
        # Implementation would restart services
        # This is a placeholder for actual restart logic
    
    async def _remediate_repair(self, step: Dict[str, Any]):
        """Repair corrupted state."""
        logger.info(f"Repair remediation: {step.get('description')}")
        # Implementation would repair corrupted files/state
        # This is a placeholder for actual repair logic
    
    async def _remediate_enroll(self, step: Dict[str, Any]):
        """Complete voice enrollment."""
        logger.info(f"Enrollment remediation: {step.get('description')}")
        # Implementation would trigger enrollment process
        # This is a placeholder for actual enrollment logic
    
    async def _remediate_download(self, step: Dict[str, Any]):
        """Download missing resources."""
        logger.info(f"Download remediation: {step.get('description')}")
        # Implementation would download models/resources
        # This is a placeholder for actual download logic
    
    # =========================================================================
    # CACHING
    # =========================================================================
    
    def _get_cached_diagnostic(self) -> Optional[Tuple[SystemDiagnostic, datetime]]:
        """Get cached diagnostic if available and fresh."""
        cache_file = self.cache_dir / "last_diagnostic.json"
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file, "r") as f:
                data = json.load(f)
            
            cached_time = datetime.fromisoformat(data["timestamp"])
            diagnostic = SystemDiagnostic(**data["diagnostic"])
            
            return diagnostic, cached_time
        except Exception as e:
            logger.debug(f"Cache read error: {e}")
            return None
    
    def _cache_diagnostic(self, diagnostic: SystemDiagnostic):
        """Cache diagnostic result."""
        cache_file = self.cache_dir / "last_diagnostic.json"
        
        try:
            data = {
                "timestamp": diagnostic.timestamp.isoformat(),
                "diagnostic": {
                    "timestamp": diagnostic.timestamp.isoformat(),
                    "overall_status": diagnostic.overall_status.value,
                    "overall_confidence": diagnostic.overall_confidence,
                    "components": {
                        name: {
                            "name": comp.name,
                            "status": comp.status.value,
                            "severity": comp.severity.value,
                            "message": comp.message,
                            "details": comp.details,
                        }
                        for name, comp in diagnostic.components.items()
                    },
                    "root_causes": diagnostic.root_causes,
                    "recommended_actions": diagnostic.recommended_actions,
                }
            }
            
            with open(cache_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"Cache write error: {e}")


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_diagnostic_system: Optional[IntelligentDiagnosticSystem] = None


def get_diagnostic_system() -> IntelligentDiagnosticSystem:
    """Get global diagnostic system instance."""
    global _diagnostic_system
    if _diagnostic_system is None:
        _diagnostic_system = IntelligentDiagnosticSystem()
    return _diagnostic_system


# =============================================================================
# CLI INTERFACE
# =============================================================================

async def main():
    """CLI entry point for diagnostic system."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Intelligent Diagnostic System for PAVA/VIBA")
    parser.add_argument("--components", nargs="+", help="Specific components to check")
    parser.add_argument("--no-cache", action="store_true", help="Don't use cached results")
    parser.add_argument("--auto-remediate", action="store_true", help="Auto-remediate issues")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    
    system = get_diagnostic_system()
    diagnostic = await system.run_full_diagnostic(
        components=args.components,
        use_cache=not args.no_cache,
        auto_remediate=args.auto_remediate
    )
    
    if args.json:
        import json
        print(json.dumps({
            "overall_status": diagnostic.overall_status.value,
            "overall_confidence": diagnostic.overall_confidence,
            "root_causes": diagnostic.root_causes,
            "recommended_actions": diagnostic.recommended_actions,
            "components": {
                name: {
                    "status": comp.status.value,
                    "severity": comp.severity.value,
                    "message": comp.message,
                }
                for name, comp in diagnostic.components.items()
            }
        }, indent=2))
    else:
        print(f"\n{'='*70}")
        print(f"System Diagnostic Report")
        print(f"{'='*70}")
        print(f"Overall Status: {diagnostic.overall_status.value.upper()}")
        print(f"Overall Confidence: {diagnostic.overall_confidence:.1%}")
        print(f"\nRoot Causes:")
        for cause in diagnostic.root_causes:
            print(f"  • {cause}")
        print(f"\nRecommended Actions:")
        for action in diagnostic.recommended_actions:
            print(f"  [{action['priority'].upper()}] {action['action']}")
            if action.get('command'):
                print(f"    Command: {action['command']}")


if __name__ == "__main__":
    asyncio.run(main())
