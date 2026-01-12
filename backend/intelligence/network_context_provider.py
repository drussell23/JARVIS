#!/usr/bin/env python3
"""
Network Context Provider for JARVIS Voice Authentication
==========================================================

Provides network-based contextual intelligence for multi-factor authentication:
- WiFi SSID detection and familiarity scoring
- Network location classification (home, office, public, unknown)
- Connection quality and stability monitoring
- Historical network pattern learning

This enables behavioral verification like:
- "Last successful unlock from same WiFi network ✓"
- "Unknown network - requires additional verification"
- "Trusted network - confidence boost applied"

Author: Derek J. Russell
Version: 1.0.0
"""

import asyncio
import logging
import os
import subprocess
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict
import re

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================
class NetworkContextConfig:
    """Dynamic configuration for network context provider."""

    def __init__(self):
        # Network familiarity thresholds
        self.trusted_network_threshold = int(os.getenv('NETWORK_TRUSTED_THRESHOLD', '5'))  # 5+ successful unlocks
        self.known_network_threshold = int(os.getenv('NETWORK_KNOWN_THRESHOLD', '2'))  # 2+ successful unlocks

        # Confidence scores
        self.trusted_network_confidence = float(os.getenv('NETWORK_TRUSTED_CONFIDENCE', '0.95'))
        self.known_network_confidence = float(os.getenv('NETWORK_KNOWN_CONFIDENCE', '0.85'))
        self.unknown_network_confidence = float(os.getenv('NETWORK_UNKNOWN_CONFIDENCE', '0.50'))

        # Pattern tracking
        self.max_network_history = int(os.getenv('NETWORK_MAX_HISTORY', '100'))
        self.network_decay_days = int(os.getenv('NETWORK_DECAY_DAYS', '90'))  # Forget networks after 90 days

        # Cache settings
        self.cache_duration_seconds = int(os.getenv('NETWORK_CACHE_DURATION', '30'))  # 30 second cache

        # Database path
        self.db_path = Path(os.getenv(
            'NETWORK_CONTEXT_DB',
            os.path.expanduser('~/.jarvis/network_context.json')
        ))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_config = NetworkContextConfig()


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class NetworkInfo:
    """Current network information."""
    ssid: str
    bssid: Optional[str] = None
    interface: str = "en0"
    ip_address: Optional[str] = None
    router_ip: Optional[str] = None
    signal_strength: Optional[int] = None  # RSSI in dBm
    connection_type: str = "wifi"  # wifi, ethernet, vpn, cellular
    is_connected: bool = True
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'ssid': self.ssid,
            'bssid': self.bssid,
            'interface': self.interface,
            'ip_address': self.ip_address,
            'router_ip': self.router_ip,
            'signal_strength': self.signal_strength,
            'connection_type': self.connection_type,
            'is_connected': self.is_connected,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class NetworkPattern:
    """Historical pattern for a network."""
    ssid: str
    ssid_hash: str  # Privacy: hashed SSID
    total_connections: int = 0
    successful_unlocks: int = 0
    failed_unlocks: int = 0
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    typical_signal_strength: Optional[int] = None
    location_type: str = "unknown"  # home, office, public, unknown
    trust_level: str = "unknown"  # trusted, known, unknown
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'ssid_hash': self.ssid_hash,
            'total_connections': self.total_connections,
            'successful_unlocks': self.successful_unlocks,
            'failed_unlocks': self.failed_unlocks,
            'first_seen': self.first_seen.isoformat(),
            'last_seen': self.last_seen.isoformat(),
            'typical_signal_strength': self.typical_signal_strength,
            'location_type': self.location_type,
            'trust_level': self.trust_level,
            'confidence': self.confidence
        }

    @staticmethod
    def from_dict(ssid: str, data: Dict[str, Any]) -> 'NetworkPattern':
        """Create from dictionary."""
        return NetworkPattern(
            ssid=ssid,
            ssid_hash=data['ssid_hash'],
            total_connections=data.get('total_connections', 0),
            successful_unlocks=data.get('successful_unlocks', 0),
            failed_unlocks=data.get('failed_unlocks', 0),
            first_seen=datetime.fromisoformat(data['first_seen']) if 'first_seen' in data else datetime.now(),
            last_seen=datetime.fromisoformat(data['last_seen']) if 'last_seen' in data else datetime.now(),
            typical_signal_strength=data.get('typical_signal_strength'),
            location_type=data.get('location_type', 'unknown'),
            trust_level=data.get('trust_level', 'unknown'),
            confidence=data.get('confidence', 0.0)
        )


@dataclass
class NetworkContext:
    """Complete network context for authentication."""
    current_network: Optional[NetworkInfo]
    network_pattern: Optional[NetworkPattern]
    is_trusted: bool
    is_known: bool
    confidence: float
    reasoning: str
    connection_stable: bool = True
    time_on_network_minutes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'current_network': self.current_network.to_dict() if self.current_network else None,
            'network_pattern': self.network_pattern.to_dict() if self.network_pattern else None,
            'is_trusted': self.is_trusted,
            'is_known': self.is_known,
            'confidence': self.confidence,
            'reasoning': self.reasoning,
            'connection_stable': self.connection_stable,
            'time_on_network_minutes': self.time_on_network_minutes
        }


# =============================================================================
# NETWORK CONTEXT PROVIDER
# =============================================================================
class NetworkContextProvider:
    """
    Provides network-based contextual intelligence for voice authentication.

    Features:
    - WiFi SSID detection with privacy-preserving hashing
    - Network familiarity scoring based on usage history
    - Trusted network identification
    - Connection stability monitoring
    - Historical pattern learning
    """

    def __init__(self):
        self.config = _config
        self._cache: Optional[Tuple[NetworkInfo, datetime]] = None
        self._patterns: Dict[str, NetworkPattern] = {}
        self._connection_history: List[Tuple[str, datetime]] = []
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self):
        """Initialize the provider and load historical patterns."""
        if self._initialized:
            return

        async with self._lock:
            try:
                await self._load_patterns()
                self._initialized = True
                logger.info("🌐 [NETWORK-CONTEXT] Initialized successfully")
            except Exception as e:
                logger.error(f"🌐 [NETWORK-CONTEXT] Initialization failed: {e}")
                self._initialized = True  # Continue even if load fails

    async def _load_patterns(self):
        """Load network patterns from disk."""
        if not self.config.db_path.exists():
            logger.debug("🌐 [NETWORK-CONTEXT] No existing pattern database")
            return

        try:
            with open(self.config.db_path, 'r') as f:
                data = json.load(f)

            for ssid_hash, pattern_data in data.get('patterns', {}).items():
                # Reconstruct pattern (ssid is unknown, only hash is stored)
                pattern = NetworkPattern.from_dict(ssid=f"network_{ssid_hash[:8]}", data=pattern_data)
                self._patterns[ssid_hash] = pattern

            logger.info(f"🌐 [NETWORK-CONTEXT] Loaded {len(self._patterns)} network patterns")

        except Exception as e:
            logger.error(f"🌐 [NETWORK-CONTEXT] Failed to load patterns: {e}")

    async def _save_patterns(self):
        """Save network patterns to disk."""
        try:
            data = {
                'patterns': {
                    ssid_hash: pattern.to_dict()
                    for ssid_hash, pattern in self._patterns.items()
                },
                'last_updated': datetime.now().isoformat()
            }

            with open(self.config.db_path, 'w') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"🌐 [NETWORK-CONTEXT] Saved {len(self._patterns)} patterns")

        except Exception as e:
            logger.error(f"🌐 [NETWORK-CONTEXT] Failed to save patterns: {e}")

    def _hash_ssid(self, ssid: str) -> str:
        """Create privacy-preserving hash of SSID."""
        return hashlib.sha256(ssid.encode()).hexdigest()

    async def get_current_network(self, use_cache: bool = True) -> Optional[NetworkInfo]:
        """
        Get current network information.

        Args:
            use_cache: Use cached value if fresh (< 30 seconds)

        Returns:
            NetworkInfo or None if not connected
        """
        if not self._initialized:
            await self.initialize()

        # Check cache
        if use_cache and self._cache:
            cached_info, cached_time = self._cache
            if (datetime.now() - cached_time).total_seconds() < self.config.cache_duration_seconds:
                return cached_info

        # Get fresh network info
        network_info = await self._detect_network()

        # Update cache
        if network_info:
            self._cache = (network_info, datetime.now())

        return network_info

    async def _detect_network(self) -> Optional[NetworkInfo]:
        """
        Detect current network using macOS networksetup command.

        Returns:
            NetworkInfo or None if not connected
        """
        try:
            # Get WiFi info using networksetup
            result = await asyncio.create_subprocess_exec(
                '/usr/sbin/networksetup', '-getairportnetwork', 'en0',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                logger.debug("🌐 [NETWORK-CONTEXT] Not connected to WiFi")
                return None

            output = stdout.decode().strip()

            # Parse SSID from output: "Current Wi-Fi Network: NetworkName"
            match = re.search(r'Current Wi-Fi Network: (.+)', output)
            if not match:
                return None

            ssid = match.group(1).strip()

            # Get additional info using airport command
            signal_strength = await self._get_signal_strength()
            ip_address = await self._get_ip_address('en0')
            router_ip = await self._get_router_ip()

            network_info = NetworkInfo(
                ssid=ssid,
                interface='en0',
                ip_address=ip_address,
                router_ip=router_ip,
                signal_strength=signal_strength,
                connection_type='wifi',
                is_connected=True,
                timestamp=datetime.now()
            )

            logger.debug(f"🌐 [NETWORK-CONTEXT] Detected network: {ssid}")
            return network_info

        except Exception as e:
            logger.error(f"🌐 [NETWORK-CONTEXT] Network detection failed: {e}")
            return None

    async def _get_signal_strength(self) -> Optional[int]:
        """Get WiFi signal strength in dBm."""
        try:
            result = await asyncio.create_subprocess_exec(
                '/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport',
                '-I',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()

            if result.returncode == 0:
                output = stdout.decode()
                match = re.search(r'agrCtlRSSI:\s*(-?\d+)', output)
                if match:
                    return int(match.group(1))

        except Exception as e:
            logger.debug(f"🌐 [NETWORK-CONTEXT] Signal strength detection failed: {e}")

        return None

    async def _get_ip_address(self, interface: str) -> Optional[str]:
        """Get IP address for interface."""
        try:
            result = await asyncio.create_subprocess_exec(
                'ifconfig', interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()

            if result.returncode == 0:
                output = stdout.decode()
                match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', output)
                if match:
                    return match.group(1)

        except Exception as e:
            logger.debug(f"🌐 [NETWORK-CONTEXT] IP address detection failed: {e}")

        return None

    async def _get_router_ip(self) -> Optional[str]:
        """Get router IP address."""
        try:
            result = await asyncio.create_subprocess_exec(
                'netstat', '-nr',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()

            if result.returncode == 0:
                output = stdout.decode()
                # Look for default gateway
                match = re.search(r'default\s+(\d+\.\d+\.\d+\.\d+)', output)
                if match:
                    return match.group(1)

        except Exception as e:
            logger.debug(f"🌐 [NETWORK-CONTEXT] Router IP detection failed: {e}")

        return None

    async def get_network_context(self) -> NetworkContext:
        """
        Get complete network context for authentication.

        Returns:
            NetworkContext with current network, pattern, and confidence
        """
        if not self._initialized:
            await self.initialize()

        current_network = await self.get_current_network()

        if not current_network:
            return NetworkContext(
                current_network=None,
                network_pattern=None,
                is_trusted=False,
                is_known=False,
                confidence=0.0,
                reasoning="Not connected to any network",
                connection_stable=False
            )

        # Get or create pattern for this network
        ssid_hash = self._hash_ssid(current_network.ssid)
        pattern = self._patterns.get(ssid_hash)

        if not pattern:
            # First time seeing this network
            pattern = NetworkPattern(
                ssid=current_network.ssid,
                ssid_hash=ssid_hash,
                first_seen=datetime.now(),
                last_seen=datetime.now()
            )
            self._patterns[ssid_hash] = pattern

        # Update pattern
        pattern.last_seen = datetime.now()
        pattern.total_connections += 1

        # Calculate trust level and confidence
        is_trusted = pattern.successful_unlocks >= self.config.trusted_network_threshold
        is_known = pattern.successful_unlocks >= self.config.known_network_threshold

        if is_trusted:
            confidence = self.config.trusted_network_confidence
            trust_level = "trusted"
            reasoning = f"Trusted network ({pattern.successful_unlocks} successful unlocks)"
        elif is_known:
            confidence = self.config.known_network_confidence
            trust_level = "known"
            reasoning = f"Known network ({pattern.successful_unlocks} successful unlocks)"
        else:
            confidence = self.config.unknown_network_confidence
            trust_level = "unknown"
            reasoning = "Unknown network - first time or insufficient history"

        pattern.trust_level = trust_level
        pattern.confidence = confidence

        # Check connection stability
        connection_stable = current_network.signal_strength is None or current_network.signal_strength > -75

        # Calculate time on network
        time_on_network = 0
        if self._connection_history:
            last_ssid, last_time = self._connection_history[-1]
            if last_ssid == current_network.ssid:
                time_on_network = int((datetime.now() - last_time).total_seconds() / 60)

        # Track connection
        self._connection_history.append((current_network.ssid, datetime.now()))
        if len(self._connection_history) > 100:
            self._connection_history.pop(0)

        return NetworkContext(
            current_network=current_network,
            network_pattern=pattern,
            is_trusted=is_trusted,
            is_known=is_known,
            confidence=confidence,
            reasoning=reasoning,
            connection_stable=connection_stable,
            time_on_network_minutes=time_on_network
        )

    async def record_unlock_attempt(self, success: bool):
        """
        Record an unlock attempt on the current network.

        Args:
            success: Whether the unlock was successful
        """
        if not self._initialized:
            await self.initialize()

        current_network = await self.get_current_network()
        if not current_network:
            return

        ssid_hash = self._hash_ssid(current_network.ssid)
        pattern = self._patterns.get(ssid_hash)

        if pattern:
            if success:
                pattern.successful_unlocks += 1
            else:
                pattern.failed_unlocks += 1

            pattern.last_seen = datetime.now()

            # Save patterns periodically
            if (pattern.successful_unlocks + pattern.failed_unlocks) % 5 == 0:
                await self._save_patterns()

            logger.debug(
                f"🌐 [NETWORK-CONTEXT] Recorded {'successful' if success else 'failed'} unlock on network "
                f"(total: {pattern.successful_unlocks}/{pattern.failed_unlocks})"
            )


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================
_provider: Optional[NetworkContextProvider] = None
_provider_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_network_context_provider() -> NetworkContextProvider:
    """Get or create the global network context provider."""
    global _provider

    if _provider is None:
        async with _provider_lock:
            if _provider is None:
                _provider = NetworkContextProvider()
                await _provider.initialize()

    return _provider
