#!/usr/bin/env python3
"""
JARVIS Supervisor Configuration
================================

Dynamic YAML-based configuration loader for the Self-Updating Lifecycle Manager.
Supports environment variable overrides, hot-reload, and sensible defaults.

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional

import yaml

logger = logging.getLogger(__name__)


class SupervisorMode(str, Enum):
    """Supervisor operation modes."""
    MANUAL = "manual"      # Only updates on explicit command
    AUTO = "auto"          # Proactive notifications + idle updates
    SCHEDULED = "scheduled"  # Cron-like scheduled updates


class UpdateSource(str, Enum):
    """Update source types."""
    GITHUB = "github"
    GITLAB = "gitlab"
    LOCAL = "local"


@dataclass
class UpdateCheckConfig:
    """Update check configuration."""
    enabled: bool = True
    interval_seconds: int = 300
    on_startup: bool = True
    rate_limit_aware: bool = True


@dataclass
class BuildStep:
    """Single build step configuration."""
    name: str
    command: str
    timeout_seconds: int = 120
    required: bool = True
    working_dir: Optional[str] = None
    condition: Optional[str] = None


@dataclass
class UpdateConfig:
    """Update configuration."""
    source_type: UpdateSource = UpdateSource.GITHUB
    remote: str = "origin"
    branch: str = "main"
    check: UpdateCheckConfig = field(default_factory=UpdateCheckConfig)
    announce_changes: bool = True
    require_confirmation: bool = True
    auto_apply_enabled: bool = False
    build_steps: list[BuildStep] = field(default_factory=list)


@dataclass
class IdleConfig:
    """Idle detection configuration."""
    enabled: bool = True
    threshold_seconds: int = 7200  # 2 hours
    min_consecutive_seconds: int = 300
    monitor_keyboard: bool = True
    monitor_mouse: bool = True
    monitor_audio: bool = False
    silent_update_enabled: bool = True
    announce_on_resume: bool = True


@dataclass
class HealthConfig:
    """Health monitoring configuration."""
    boot_stability_window: int = 60
    min_components_ready: int = 5
    max_crash_retries: int = 3
    retry_delay_seconds: int = 5
    backoff_multiplier: float = 2.0
    check_interval_seconds: int = 30
    check_timeout_seconds: int = 10


@dataclass
class RollbackConfig:
    """Rollback configuration."""
    enabled: bool = True
    max_versions: int = 5
    history_db: str = "data/supervisor/version_history.db"
    auto_on_boot_failure: bool = True
    auto_on_crash_within_seconds: int = 60
    include_pip_freeze: bool = True


@dataclass
class ZeroTouchConfig:
    """
    Zero-Touch Autonomous Update Configuration.
    
    Enables JARVIS to autonomously detect, validate, apply, and verify
    updates without human intervention - transforming it from a "tool"
    into a "living organism" that can evolve safely.
    
    Safety Features:
    1. JARVIS busy-state check before update
    2. Staging area with dry-run validation
    3. Dead Man's Switch post-update monitoring
    4. Automatic rollback on failure
    5. Immutable core protection
    """
    # Master toggle for autonomous updates
    # v4.0: Now ON by default - JARVIS is a living, self-updating system
    # Disable with environment variable or config: JARVIS_ZERO_TOUCH_ENABLED=false
    enabled: bool = True
    
    # Pre-Update Safety Checks
    check_jarvis_busy: bool = True           # Query JARVIS internal state before update
    busy_check_endpoint: str = "/health/busy"  # Endpoint to check busy state
    busy_check_timeout: float = 5.0          # Timeout for busy check
    require_idle_system: bool = True         # Require macOS idle before update
    min_idle_seconds: int = 300              # Minimum 5 minutes idle
    
    # Staging & Validation
    use_staging_area: bool = True            # Download to staging before merge
    staging_directory: str = ".jarvis_staging"
    dry_run_pip: bool = True                 # Dry-run pip install before real install
    validate_syntax: bool = True             # Python syntax validation
    validate_imports: bool = True            # Check for import errors
    max_staging_age_seconds: int = 3600      # Clean up old staging after 1 hour
    
    # Auto-Apply Triggers
    apply_security_updates: bool = True      # Always apply security fixes immediately
    apply_minor_updates: bool = True         # Apply minor version updates
    apply_major_updates: bool = False        # Major updates require confirmation
    max_commits_auto: int = 10               # Don't auto-apply if > 10 commits behind
    
    # Timing & Scheduling
    preferred_update_hours: tuple = (2, 6)   # Prefer updates between 2-6 AM
    force_idle_window: bool = False          # Only update during idle windows
    cooldown_after_update_seconds: int = 300 # Wait 5 min between updates
    
    # Notifications
    announce_before_update: bool = True      # Voice: "Applying update..."
    announce_after_update: bool = True       # Voice: "Update complete" / "Update failed"
    notify_on_auto_update: bool = True       # WebSocket notification on auto-update


@dataclass
class PrimeDirectivesConfig:
    """
    Prime Directives - Immutable Safety Constraints for Autonomous JARVIS.
    
    These are the "constitutional" constraints that JARVIS cannot override,
    even in autonomous mode. They represent the ethical and safety boundaries
    that protect both the user and the system.
    
    The Immutable Core Protection ensures JARVIS cannot:
    - Modify the Supervisor itself
    - Disable safety mechanisms
    - Act without user consent for dangerous operations
    """
    # === CORE IMMUTABLE DIRECTIVES ===
    
    # File Protection - JARVIS cannot modify these patterns
    protected_files: tuple = (
        "run_supervisor.py",                 # The God Process
        "backend/core/supervisor/*.py",      # All supervisor modules
        ".git/hooks/*",                      # Git hooks
        "*.pem", "*.key", "*.crt",           # Cryptographic keys
    )
    
    # The Supervisor is READ-ONLY to JARVIS process
    supervisor_read_only: bool = True
    
    # JARVIS cannot update its own Prime Directives
    directives_immutable: bool = True
    
    # === USER CONSENT REQUIREMENTS ===
    
    # Actions that ALWAYS require user confirmation
    confirm_before_delete_gb: float = 1.0    # Confirm before deleting > 1GB
    confirm_before_network_change: bool = True  # Confirm network config changes
    confirm_before_system_settings: bool = True  # Confirm macOS system changes
    confirm_before_credential_access: bool = True  # Confirm accessing passwords
    
    # === OPERATION LIMITS ===
    
    # Resource usage limits JARVIS cannot exceed autonomously
    max_autonomous_api_calls_per_hour: int = 1000
    max_autonomous_file_changes_per_update: int = 100
    max_autonomous_memory_gb: float = 8.0
    max_autonomous_cpu_percent: float = 80.0
    
    # Time limits
    max_autonomous_operation_seconds: int = 300  # 5 min max for any single operation
    
    # === ROLLBACK PROTECTION ===
    
    # Number of stable versions to always keep
    min_stable_versions: int = 3
    
    # Never auto-rollback if uptime > this (user is actively using)
    no_rollback_if_uptime_hours: float = 24.0
    
    # === TRANSPARENCY REQUIREMENTS ===
    
    # Always log these actions (cannot be disabled)
    always_log_updates: bool = True
    always_log_rollbacks: bool = True
    always_log_config_changes: bool = True
    always_log_file_modifications: bool = True
    
    # Voice announcement for significant actions
    announce_significant_actions: bool = True


@dataclass
class DeadManSwitchConfig:
    """
    Dead Man's Switch Configuration - Post-Update Stability Verification.
    
    The "Dead Man's Switch" is a critical safety mechanism that:
    1. Monitors JARVIS health after updates for a probation period
    2. Automatically rolls back if health checks fail
    3. Commits the update as "stable" once probation passes
    4. Uses parallel health probing for fast detection
    5. Provides intelligent, multi-signal health assessment
    
    This transforms JARVIS from a "tool" to an "organism" that can
    safely self-update without creating "Zombie JARVIS" loops.
    """
    # Core settings
    enabled: bool = True
    
    # Probation period - how long to monitor after update
    probation_seconds: int = 45  # 45 seconds of stability required
    
    # Heartbeat detection
    heartbeat_interval_seconds: float = 2.0  # Check every 2 seconds
    heartbeat_timeout_seconds: float = 5.0   # Consider dead if no response in 5s
    max_consecutive_failures: int = 3        # 3 strikes = rollback
    
    # Health thresholds
    min_health_score: float = 0.6            # Minimum 60% components healthy
    require_backend_healthy: bool = True     # Backend MUST be healthy
    require_api_responding: bool = True      # /health endpoint MUST respond
    
    # Rollback behavior
    auto_rollback_enabled: bool = True       # Automatically rollback on failure
    announce_rollback: bool = True           # Voice announcement of rollback
    max_rollback_attempts: int = 2           # Max cascading rollback attempts
    
    # Parallel probing endpoints (dynamic, no hardcoding)
    probe_backend: bool = True
    probe_frontend: bool = True
    probe_voice: bool = False                # Voice service optional
    probe_vision: bool = False               # Vision service optional
    
    # Stability commitment
    auto_commit_stable: bool = True          # Auto-mark as stable after probation
    notify_on_commit: bool = True            # Voice announcement when stable
    
    # Advanced: Intelligent analysis
    analyze_crash_patterns: bool = True      # Learn from crash patterns
    track_boot_metrics: bool = True          # Track boot time trends


@dataclass
class ChangelogConfig:
    """Changelog analysis configuration."""
    enabled: bool = True
    max_commits: int = 20
    summarization_enabled: bool = True
    summarization_provider: str = "local"
    max_length_words: int = 50


@dataclass
class NotificationConfig:
    """
    Update notification configuration.
    
    Controls how users are notified about available updates
    through multiple channels (voice, WebSocket, console).
    """
    # Channel toggles
    voice_enabled: bool = True           # TTS announcements
    websocket_enabled: bool = True       # Frontend badge/modal
    console_enabled: bool = True         # Console logging
    
    # Timing
    min_interval_seconds: int = 60       # Min time between notifications
    reminder_interval_seconds: int = 900  # Re-notify after 15 minutes
    max_reminders: int = 3               # Max reminder notifications
    
    # Priority handling
    security_immediate: bool = True      # Always notify for security
    
    # User activity awareness  
    interrupt_active_user: bool = False  # Don't interrupt active use
    active_timeout_seconds: int = 120    # Consider user "active" if recent activity
    
    # Backend integration
    backend_url: str = "http://localhost:8010"
    websocket_timeout: float = 3.0


@dataclass
class ExitCodes:
    """Exit code mappings for supervisor communication."""
    clean_shutdown: int = 0
    error_crash: int = 1
    update_request: int = 100
    rollback_request: int = 101
    restart_request: int = 102


@dataclass
class DevModeConfig:
    """
    v5.0: Developer Mode Configuration - Intelligent Polyglot Hot Reload
    
    When enabled, the supervisor watches for code changes across ALL languages
    in your codebase and automatically restarts JARVIS when files are modified.
    
    SUPPORTED LANGUAGES (auto-detected):
    - Python (.py, .pyx)        → Backend restart
    - Rust (.rs)                → Backend restart (native rebuild)
    - Swift (.swift)            → Backend restart (native rebuild)
    - JavaScript (.js)          → Frontend restart
    - TypeScript (.ts, .tsx)    → Frontend restart
    - React (.jsx)              → Frontend restart
    - CSS/SCSS/LESS             → Frontend restart
    - HTML                      → Frontend restart
    - YAML/TOML                 → Config reload
    
    Think of this as JARVIS's own "nodemon" - you run `python3 run_supervisor.py`
    once and it handles all restarts automatically as you develop.
    """
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_DEV_MODE", "true").lower() == "true"
    )
    
    # Grace period before hot reload activates (allows initial startup)
    startup_grace_period_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_RELOAD_GRACE_PERIOD", "120"))
    )
    
    # How often to check for file changes
    check_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_RELOAD_CHECK_INTERVAL", "10"))
    )
    
    # Cooldown between restarts (prevents rapid-fire restarts)
    restart_cooldown_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_RELOAD_COOLDOWN", "10"))
    )
    
    # Debounce delay for rapid file changes (seconds)
    debounce_delay_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_RELOAD_DEBOUNCE", "0.5"))
    )
    
    # Directories to exclude from watching
    exclude_directories: List[str] = field(default_factory=lambda: [
        ".git", "__pycache__", "node_modules", "venv", "env",
        ".venv", "build", "dist", "target", ".cursor", ".idea",
        ".vscode", "coverage", ".pytest_cache", ".mypy_cache",
        "logs", "cache", ".jarvis_cache", "htmlcov",
    ])
    
    # File patterns to ignore
    exclude_patterns: List[str] = field(default_factory=lambda: [
        "*.pyc", "*.pyo", "*.log", "*.tmp", "*.bak",
        "*.swp", "*.swo", "*~", ".DS_Store",
    ])
    
    # Files that require FULL restart (dependencies, not hot reload)
    cold_restart_files: List[str] = field(default_factory=lambda: [
        "requirements.txt",
        "requirements-dev.txt",
        "Cargo.toml",
        "Cargo.lock",
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        "setup.py",
        ".env",
    ])
    
    # Clear Python cache on restart
    clear_cache_on_restart: bool = True
    
    # Enable parallel file hash calculation
    parallel_hash_calculation: bool = True
    
    # Verbose logging of file changes
    verbose_logging: bool = field(
        default_factory=lambda: os.getenv("JARVIS_RELOAD_VERBOSE", "false").lower() == "true"
    )
    
    # Auto-discover file types (vs using predefined list)
    auto_discover_file_types: bool = True


@dataclass
class SupervisorConfig:
    """
    Complete supervisor configuration.
    
    Loaded from YAML with environment variable overrides.
    
    v2.0 - Zero-Touch Autonomous Updates:
    - `zero_touch`: Autonomous update configuration
    - `prime_directives`: Immutable safety constraints
    - `dead_man_switch`: Post-update stability verification
    """
    enabled: bool = True
    mode: SupervisorMode = SupervisorMode.AUTO
    log_level: str = "INFO"
    log_file: str = "logs/supervisor.log"
    
    update: UpdateConfig = field(default_factory=UpdateConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    rollback: RollbackConfig = field(default_factory=RollbackConfig)
    changelog: ChangelogConfig = field(default_factory=ChangelogConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    exit_codes: ExitCodes = field(default_factory=ExitCodes)
    dead_man_switch: DeadManSwitchConfig = field(default_factory=DeadManSwitchConfig)
    
    # v2.0: Zero-Touch Autonomous Updates
    zero_touch: ZeroTouchConfig = field(default_factory=ZeroTouchConfig)
    prime_directives: PrimeDirectivesConfig = field(default_factory=PrimeDirectivesConfig)
    
    # v5.0: Developer Mode - Hot Reload / Live Reload
    dev_mode: DevModeConfig = field(default_factory=DevModeConfig)
    
    # Runtime state
    config_path: Optional[Path] = None
    _last_modified: float = 0.0
    
    @property
    def is_zero_touch_enabled(self) -> bool:
        """Check if Zero-Touch mode is fully enabled."""
        return (
            self.zero_touch.enabled and 
            self.update.auto_apply_enabled and 
            not self.update.require_confirmation
        )
    
    @property
    def is_autonomous_mode(self) -> bool:
        """Check if JARVIS is in full autonomous mode."""
        return self.is_zero_touch_enabled and self.mode == SupervisorMode.AUTO


def _env_override(key: str, default: Any, cast_type: type = str) -> Any:
    """Get environment variable with type casting."""
    env_key = f"JARVIS_SUPERVISOR_{key.upper()}"
    value = os.environ.get(env_key)
    if value is None:
        return default
    
    try:
        if cast_type == bool:
            return value.lower() in ("true", "1", "yes", "on")
        return cast_type(value)
    except (ValueError, TypeError):
        logger.warning(f"Invalid value for {env_key}: {value}, using default")
        return default


def _parse_build_steps(raw_steps: list[dict]) -> list[BuildStep]:
    """Parse build steps from YAML."""
    steps = []
    for step_data in raw_steps:
        steps.append(BuildStep(
            name=step_data.get("name", "unknown"),
            command=step_data.get("command", ""),
            timeout_seconds=step_data.get("timeout_seconds", 120),
            required=step_data.get("required", True),
            working_dir=step_data.get("working_dir"),
            condition=step_data.get("condition"),
        ))
    return steps


def load_config(config_path: Optional[Path] = None) -> SupervisorConfig:
    """
    Load supervisor configuration from YAML file.
    
    Searches in order:
    1. Provided path
    2. JARVIS_SUPERVISOR_CONFIG env var
    3. backend/config/supervisor_config.yaml
    4. Default config
    
    Environment variables override YAML values.
    """
    # Determine config path
    if config_path is None:
        config_path = os.environ.get("JARVIS_SUPERVISOR_CONFIG")
        if config_path:
            config_path = Path(config_path)
    
    if config_path is None:
        # Search in standard locations
        possible_paths = [
            Path(__file__).parent.parent.parent / "config" / "supervisor_config.yaml",
            Path("backend/config/supervisor_config.yaml"),
            Path("config/supervisor_config.yaml"),
        ]
        for p in possible_paths:
            if p.exists():
                config_path = p
                break
    
    # Load YAML if exists
    raw_config: dict[str, Any] = {}
    if config_path and Path(config_path).exists():
        try:
            with open(config_path) as f:
                raw_config = yaml.safe_load(f) or {}
            logger.info(f"📋 Loaded supervisor config from {config_path}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load config from {config_path}: {e}")
    
    # Extract sections
    supervisor_data = raw_config.get("supervisor", {})
    update_data = raw_config.get("update", {})
    idle_data = raw_config.get("idle", {})
    health_data = raw_config.get("health", {})
    rollback_data = raw_config.get("rollback", {})
    changelog_data = raw_config.get("changelog", {})
    exit_codes_data = raw_config.get("exit_codes", {})
    
    # Build update check config
    check_data = update_data.get("check", {})
    update_check = UpdateCheckConfig(
        enabled=_env_override("update_check_enabled", check_data.get("enabled", True), bool),
        interval_seconds=_env_override("update_check_interval", check_data.get("interval_seconds", 300), int),
        on_startup=check_data.get("on_startup", True),
        rate_limit_aware=check_data.get("rate_limit_aware", True),
    )
    
    # Build update config
    source_data = update_data.get("source", {})
    notification_data = update_data.get("notification", {})
    update_config = UpdateConfig(
        source_type=UpdateSource(source_data.get("type", "github")),
        remote=source_data.get("remote", "origin"),
        branch=_env_override("update_branch", source_data.get("branch", "main")),
        check=update_check,
        announce_changes=notification_data.get("announce_changes", True),
        require_confirmation=notification_data.get("require_confirmation", True),
        auto_apply_enabled=update_data.get("auto_apply", {}).get("enabled", False),
        build_steps=_parse_build_steps(update_data.get("build_steps", [])),
    )
    
    # Build idle config
    monitor_data = idle_data.get("monitor", {})
    silent_data = idle_data.get("silent_update", {})
    idle_config = IdleConfig(
        enabled=_env_override("idle_enabled", idle_data.get("enabled", True), bool),
        threshold_seconds=_env_override("idle_threshold", idle_data.get("threshold_seconds", 7200), int),
        min_consecutive_seconds=idle_data.get("min_consecutive_seconds", 300),
        monitor_keyboard=monitor_data.get("keyboard", True),
        monitor_mouse=monitor_data.get("mouse", True),
        monitor_audio=monitor_data.get("audio_output", False),
        silent_update_enabled=silent_data.get("enabled", True),
        announce_on_resume=silent_data.get("announce_on_resume", True),
    )
    
    # Build health config
    boot_data = health_data.get("boot_stability", {})
    crash_data = health_data.get("crash_detection", {})
    checks_data = health_data.get("checks", {})
    health_config = HealthConfig(
        boot_stability_window=boot_data.get("window_seconds", 60),
        min_components_ready=boot_data.get("min_components_ready", 5),
        max_crash_retries=crash_data.get("max_retries", 3),
        retry_delay_seconds=crash_data.get("retry_delay_seconds", 5),
        backoff_multiplier=crash_data.get("backoff_multiplier", 2.0),
        check_interval_seconds=checks_data.get("interval_seconds", 30),
        check_timeout_seconds=checks_data.get("timeout_seconds", 10),
    )
    
    # Build rollback config
    auto_rollback_data = rollback_data.get("auto_rollback", {})
    rollback_config = RollbackConfig(
        enabled=_env_override("rollback_enabled", rollback_data.get("enabled", True), bool),
        max_versions=rollback_data.get("max_versions", 5),
        history_db=rollback_data.get("history_db", "data/supervisor/version_history.db"),
        auto_on_boot_failure=auto_rollback_data.get("on_boot_failure", True),
        auto_on_crash_within_seconds=auto_rollback_data.get("on_crash_within_seconds", 60),
        include_pip_freeze=rollback_data.get("snapshot", {}).get("include_pip_freeze", True),
    )
    
    # Build changelog config
    summarization_data = changelog_data.get("summarization", {})
    changelog_config = ChangelogConfig(
        enabled=changelog_data.get("enabled", True),
        max_commits=changelog_data.get("max_commits", 20),
        summarization_enabled=summarization_data.get("enabled", True),
        summarization_provider=summarization_data.get("provider", "local"),
        max_length_words=summarization_data.get("max_length_words", 50),
    )
    
    # Build exit codes
    exit_codes = ExitCodes(
        clean_shutdown=exit_codes_data.get("clean_shutdown", 0),
        error_crash=exit_codes_data.get("error_crash", 1),
        update_request=exit_codes_data.get("update_request", 100),
        rollback_request=exit_codes_data.get("rollback_request", 101),
        restart_request=exit_codes_data.get("restart_request", 102),
    )
    
    # Build Dead Man's Switch config
    dms_data = raw_config.get("dead_man_switch", {})
    dms_probes = dms_data.get("probes", {})
    dms_rollback = dms_data.get("rollback", {})
    dms_config = DeadManSwitchConfig(
        enabled=_env_override("dms_enabled", dms_data.get("enabled", True), bool),
        probation_seconds=_env_override("dms_probation", dms_data.get("probation_seconds", 45), int),
        heartbeat_interval_seconds=dms_data.get("heartbeat_interval_seconds", 2.0),
        heartbeat_timeout_seconds=dms_data.get("heartbeat_timeout_seconds", 5.0),
        max_consecutive_failures=dms_data.get("max_consecutive_failures", 3),
        min_health_score=dms_data.get("min_health_score", 0.6),
        require_backend_healthy=dms_data.get("require_backend_healthy", True),
        require_api_responding=dms_data.get("require_api_responding", True),
        auto_rollback_enabled=dms_rollback.get("auto_enabled", True),
        announce_rollback=dms_rollback.get("announce", True),
        max_rollback_attempts=dms_rollback.get("max_attempts", 2),
        probe_backend=dms_probes.get("backend", True),
        probe_frontend=dms_probes.get("frontend", True),
        probe_voice=dms_probes.get("voice", False),
        probe_vision=dms_probes.get("vision", False),
        auto_commit_stable=dms_data.get("auto_commit_stable", True),
        notify_on_commit=dms_data.get("notify_on_commit", True),
        analyze_crash_patterns=dms_data.get("analyze_crash_patterns", True),
        track_boot_metrics=dms_data.get("track_boot_metrics", True),
    )
    
    # Build Zero-Touch config
    zt_data = raw_config.get("zero_touch", {})
    zt_triggers = zt_data.get("triggers", {})
    zt_staging = zt_data.get("staging", {})
    zt_timing = zt_data.get("timing", {})
    zero_touch_config = ZeroTouchConfig(
        enabled=_env_override("zero_touch_enabled", zt_data.get("enabled", False), bool),
        check_jarvis_busy=zt_data.get("check_jarvis_busy", True),
        busy_check_endpoint=zt_data.get("busy_check_endpoint", "/health/busy"),
        busy_check_timeout=zt_data.get("busy_check_timeout", 5.0),
        require_idle_system=zt_data.get("require_idle_system", True),
        min_idle_seconds=zt_data.get("min_idle_seconds", 300),
        use_staging_area=zt_staging.get("enabled", True),
        staging_directory=zt_staging.get("directory", ".jarvis_staging"),
        dry_run_pip=zt_staging.get("dry_run_pip", True),
        validate_syntax=zt_staging.get("validate_syntax", True),
        validate_imports=zt_staging.get("validate_imports", True),
        max_staging_age_seconds=zt_staging.get("max_age_seconds", 3600),
        apply_security_updates=zt_triggers.get("security", True),
        apply_minor_updates=zt_triggers.get("minor", True),
        apply_major_updates=zt_triggers.get("major", False),
        max_commits_auto=zt_triggers.get("max_commits", 10),
        preferred_update_hours=tuple(zt_timing.get("preferred_hours", [2, 6])),
        force_idle_window=zt_timing.get("force_idle_window", False),
        cooldown_after_update_seconds=zt_timing.get("cooldown_seconds", 300),
        announce_before_update=zt_data.get("announce_before", True),
        announce_after_update=zt_data.get("announce_after", True),
        notify_on_auto_update=zt_data.get("notify_on_auto", True),
    )
    
    # Build Prime Directives config
    pd_data = raw_config.get("prime_directives", {})
    pd_consent = pd_data.get("consent", {})
    pd_limits = pd_data.get("limits", {})
    pd_transparency = pd_data.get("transparency", {})
    prime_directives_config = PrimeDirectivesConfig(
        protected_files=tuple(pd_data.get("protected_files", [
            "run_supervisor.py",
            "backend/core/supervisor/*.py",
            ".git/hooks/*",
            "*.pem", "*.key", "*.crt",
        ])),
        supervisor_read_only=pd_data.get("supervisor_read_only", True),
        directives_immutable=pd_data.get("directives_immutable", True),
        confirm_before_delete_gb=pd_consent.get("delete_gb_threshold", 1.0),
        confirm_before_network_change=pd_consent.get("network_changes", True),
        confirm_before_system_settings=pd_consent.get("system_settings", True),
        confirm_before_credential_access=pd_consent.get("credential_access", True),
        max_autonomous_api_calls_per_hour=pd_limits.get("api_calls_per_hour", 1000),
        max_autonomous_file_changes_per_update=pd_limits.get("file_changes_per_update", 100),
        max_autonomous_memory_gb=pd_limits.get("memory_gb", 8.0),
        max_autonomous_cpu_percent=pd_limits.get("cpu_percent", 80.0),
        max_autonomous_operation_seconds=pd_limits.get("operation_seconds", 300),
        min_stable_versions=pd_data.get("min_stable_versions", 3),
        no_rollback_if_uptime_hours=pd_data.get("no_rollback_uptime_hours", 24.0),
        always_log_updates=pd_transparency.get("log_updates", True),
        always_log_rollbacks=pd_transparency.get("log_rollbacks", True),
        always_log_config_changes=pd_transparency.get("log_config_changes", True),
        always_log_file_modifications=pd_transparency.get("log_file_mods", True),
        announce_significant_actions=pd_transparency.get("announce_actions", True),
    )
    
    # Build main config
    logging_data = supervisor_data.get("logging", {})
    config = SupervisorConfig(
        enabled=_env_override("enabled", supervisor_data.get("enabled", True), bool),
        mode=SupervisorMode(supervisor_data.get("mode", "auto")),
        log_level=_env_override("log_level", logging_data.get("level", "INFO")),
        log_file=logging_data.get("file", "logs/supervisor.log"),
        update=update_config,
        idle=idle_config,
        health=health_config,
        rollback=rollback_config,
        changelog=changelog_config,
        exit_codes=exit_codes,
        dead_man_switch=dms_config,
        zero_touch=zero_touch_config,
        prime_directives=prime_directives_config,
        config_path=Path(config_path) if config_path else None,
    )
    
    if config_path and Path(config_path).exists():
        config._last_modified = Path(config_path).stat().st_mtime
    
    return config


def check_config_changed(config: SupervisorConfig) -> bool:
    """Check if config file has been modified since last load."""
    if not config.config_path or not config.config_path.exists():
        return False
    return config.config_path.stat().st_mtime > config._last_modified


# Global singleton
_supervisor_config: Optional[SupervisorConfig] = None


def get_supervisor_config(reload: bool = False) -> SupervisorConfig:
    """Get singleton supervisor configuration."""
    global _supervisor_config
    
    if _supervisor_config is None or reload:
        _supervisor_config = load_config()
        
    # Check for hot-reload
    if _supervisor_config and check_config_changed(_supervisor_config):
        logger.info("📋 Config file changed, reloading...")
        _supervisor_config = load_config(_supervisor_config.config_path)
    
    return _supervisor_config


def reset_config() -> None:
    """Reset the singleton config (for testing)."""
    global _supervisor_config
    _supervisor_config = None
