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
from typing import Any, Optional

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
class SupervisorConfig:
    """
    Complete supervisor configuration.
    
    Loaded from YAML with environment variable overrides.
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
    
    # Runtime state
    config_path: Optional[Path] = None
    _last_modified: float = 0.0


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
