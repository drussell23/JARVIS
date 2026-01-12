#!/usr/bin/env python3
"""
AirPlay Discovery Service - Bonjour/mDNS Implementation
=======================================================

Production-grade async Bonjour/mDNS discovery for AirPlay devices.

Features:
- Zero-configuration device discovery
- mDNS/Bonjour service browser
- Automatic device tracking and caching
- Network change detection
- Service filtering and validation
- Async/await throughout
- Comprehensive error handling

Author: Derek Russell
Date: 2025-10-16
Version: 2.0
"""

import asyncio
import logging
import json
import socket
import time
from typing import Dict, List, Optional, Set, Callable, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import re

logger = logging.getLogger(__name__)

try:
    from zeroconf import ServiceBrowser, Zeroconf, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("[AIRPLAY DISCOVERY] zeroconf not available - install with: pip install zeroconf")


@dataclass
class AirPlayDevice:
    """Discovered AirPlay device with metadata and connection information.
    
    Represents an AirPlay-capable device found on the network via mDNS/Bonjour
    discovery. Contains all necessary information for establishing connections
    and tracking device availability.
    
    Attributes:
        id: Unique device identifier (MD5 hash of name + IP)
        name: Human-readable device name
        hostname: Network hostname
        ip_address: IPv4 address
        port: Service port number
        service_type: mDNS service type (_airplay._tcp or _raop._tcp)
        txt_records: Raw TXT record data from mDNS
        mac_address: MAC address if available
        model: Device model identifier
        features: Supported feature flags
        discovered_at: Initial discovery timestamp
        last_seen: Most recent activity timestamp
        is_available: Current availability status
    """
    id: str
    name: str
    hostname: str
    ip_address: str
    port: int
    service_type: str
    txt_records: Dict[str, Any]
    mac_address: Optional[str] = None
    model: Optional[str] = None
    features: Optional[str] = None
    discovered_at: datetime = None
    last_seen: datetime = None
    is_available: bool = True

    def __post_init__(self):
        """Initialize timestamps if not provided."""
        if self.discovered_at is None:
            self.discovered_at = datetime.now()
        if self.last_seen is None:
            self.last_seen = datetime.now()

    def to_dict(self) -> Dict:
        """Convert device to dictionary representation.
        
        Returns:
            Dictionary with all device attributes, timestamps as ISO strings.
        """
        data = asdict(self)
        data['discovered_at'] = self.discovered_at.isoformat()
        data['last_seen'] = self.last_seen.isoformat()
        return data

    def update_last_seen(self):
        """Update last seen timestamp and mark device as available."""
        self.last_seen = datetime.now()
        self.is_available = True


class AirPlayDiscoveryService:
    """
    AirPlay Discovery Service using Bonjour/mDNS protocol.

    Provides asynchronous discovery and monitoring of AirPlay devices on the local
    network. Uses zeroconf library for mDNS service browsing and maintains a cache
    of discovered devices with automatic cleanup of stale entries.
    
    The service supports multiple AirPlay service types (_airplay._tcp, _raop._tcp)
    and provides event callbacks for device lifecycle management.
    
    Attributes:
        config: Configuration dictionary loaded from JSON
        discovered_devices: Cache of discovered devices by ID
        is_discovering: Current discovery state
        discovery_task: Background discovery task
        zeroconf: AsyncZeroconf instance
        browser: AsyncServiceBrowser instance
        service_types: List of mDNS service types to monitor
        callbacks: Event callback registry
        stats: Discovery statistics and metrics
        cache_enabled: Whether device caching is enabled
        cache_ttl: Cache time-to-live in seconds
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize AirPlay discovery service.
        
        Args:
            config_path: Path to configuration JSON file. If None, uses default
                        location at backend/config/airplay_config.json
                        
        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If config file contains invalid JSON
        """
        self.config = self._load_config(config_path)

        # Discovery state
        self.discovered_devices: Dict[str, AirPlayDevice] = {}
        self.is_discovering = False
        self.discovery_task: Optional[asyncio.Task] = None

        # Zeroconf components
        self.zeroconf: Optional[AsyncZeroconf] = None
        self.browser: Optional[AsyncServiceBrowser] = None

        # Service types to discover
        self.service_types = [
            self.config['discovery']['service_type'],
            self.config['raop']['service_type']
        ]

        # Callbacks
        self.callbacks: Dict[str, List[Callable]] = {
            'device_discovered': [],
            'device_updated': [],
            'device_lost': [],
            'error': []
        }

        # Statistics
        self.stats = {
            'total_discovered': 0,
            'total_lost': 0,
            'discovery_errors': 0,
            'last_scan': None,
            'scan_duration': 0.0
        }

        # Cache
        self.cache_enabled = self.config['advanced']['enable_mdns_cache']
        self.cache_ttl = self.config['discovery']['cache_ttl_seconds']

        logger.info("[AIRPLAY DISCOVERY] Initialized")

    def _load_config(self, config_path: Optional[str]) -> Dict:
        """Load configuration from JSON file.
        
        Args:
            config_path: Path to configuration file
            
        Returns:
            Configuration dictionary
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If config file contains invalid JSON
        """
        if config_path is None:
            config_path = Path(__file__).parent.parent / 'config' / 'airplay_config.json'

        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"[AIRPLAY DISCOVERY] Config not found: {config_path}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"[AIRPLAY DISCOVERY] Invalid config JSON: {e}")
            raise

    def register_callback(self, event: str, callback: Callable):
        """Register event callback for device lifecycle events.
        
        Args:
            event: Event type ('device_discovered', 'device_updated', 
                  'device_lost', 'error')
            callback: Callback function (sync or async)
            
        Example:
            >>> async def on_device_found(device):
            ...     print(f"Found: {device.name}")
            >>> discovery.register_callback('device_discovered', on_device_found)
        """
        if event in self.callbacks:
            self.callbacks[event].append(callback)
            logger.debug(f"[AIRPLAY DISCOVERY] Registered callback for {event}")

    async def _emit_event(self, event: str, **kwargs):
        """Emit event to all registered callbacks.
        
        Args:
            event: Event type to emit
            **kwargs: Event data passed to callbacks
        """
        if event in self.callbacks:
            for callback in self.callbacks[event]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(**kwargs)
                    else:
                        callback(**kwargs)
                except Exception as e:
                    logger.error(f"[AIRPLAY DISCOVERY] Callback error for {event}: {e}")

    async def start(self):
        """Start the discovery service.
        
        Initializes zeroconf and begins monitoring for AirPlay devices.
        Creates background task for continuous discovery and device cleanup.
        
        Raises:
            Exception: If zeroconf library is not available or discovery fails
        """
        if self.is_discovering:
            logger.warning("[AIRPLAY DISCOVERY] Already discovering")
            return

        if not ZEROCONF_AVAILABLE:
            logger.error("[AIRPLAY DISCOVERY] zeroconf library not available")
            await self._emit_event('error', error=Exception("zeroconf not available"))
            return

        if not self.config['discovery']['enabled']:
            logger.warning("[AIRPLAY DISCOVERY] Discovery disabled in config")
            return

        self.is_discovering = True
        self.discovery_task = asyncio.create_task(self._discovery_loop())
        logger.info("[AIRPLAY DISCOVERY] Started")

    async def stop(self):
        """Stop the discovery service.
        
        Cancels background tasks and cleans up zeroconf resources.
        """
        self.is_discovering = False

        if self.discovery_task:
            self.discovery_task.cancel()
            try:
                await self.discovery_task
            except asyncio.CancelledError:
                pass

        # Clean up zeroconf
        if self.browser:
            await self.browser.async_cancel()

        if self.zeroconf:
            await self.zeroconf.async_close()

        logger.info("[AIRPLAY DISCOVERY] Stopped")

    async def _discovery_loop(self):
        """Main discovery loop running in background task.
        
        Initializes zeroconf service browsers and runs periodic cleanup
        of stale devices. Handles discovery errors and emits error events.
        
        Raises:
            asyncio.CancelledError: When discovery is cancelled
            Exception: For other discovery errors
        """
        logger.info("[AIRPLAY DISCOVERY] Discovery loop starting")

        try:
            # Initialize zeroconf
            self.zeroconf = AsyncZeroconf()

            # Start service browsers for each service type
            browsers = []
            for service_type in self.service_types:
                logger.info(f"[AIRPLAY DISCOVERY] Browsing for {service_type}")
                browser = AsyncServiceBrowser(
                    self.zeroconf.zeroconf,
                    service_type,
                    handlers=[self._on_service_state_change]
                )
                browsers.append(browser)

            # Store browsers
            self.browser = browsers[0] if browsers else None

            # Periodic cleanup and monitoring
            scan_interval = self.config['discovery']['scan_interval_seconds']

            while self.is_discovering:
                await asyncio.sleep(scan_interval)
                await self._cleanup_stale_devices()

        except asyncio.CancelledError:
            logger.info("[AIRPLAY DISCOVERY] Discovery cancelled")
        except Exception as e:
            logger.error(f"[AIRPLAY DISCOVERY] Discovery error: {e}", exc_info=True)
            await self._emit_event('error', error=e)
            self.stats['discovery_errors'] += 1

    def _on_service_state_change(self, zeroconf, service_type, name, state_change):
        """Handle mDNS service state changes (sync callback).
        
        Args:
            zeroconf: Zeroconf instance
            service_type: mDNS service type
            name: Service name
            state_change: ServiceStateChange enum value
        """
        asyncio.create_task(self._handle_service_state_change(zeroconf, service_type, name, state_change))

    async def _handle_service_state_change(self, zeroconf, service_type, name, state_change):
        """Async handler for mDNS service state changes.
        
        Routes service events to appropriate handlers based on state change type.
        
        Args:
            zeroconf: Zeroconf instance
            service_type: mDNS service type
            name: Service name
            state_change: ServiceStateChange enum value
        """
        try:
            from zeroconf import ServiceStateChange

            if state_change == ServiceStateChange.Added:
                await self._handle_service_added(zeroconf, service_type, name)
            elif state_change == ServiceStateChange.Removed:
                await self._handle_service_removed(name)
            elif state_change == ServiceStateChange.Updated:
                await self._handle_service_updated(zeroconf, service_type, name)

        except Exception as e:
            logger.error(f"[AIRPLAY DISCOVERY] Error handling state change: {e}")

    async def _handle_service_added(self, zeroconf, service_type, name):
        """Handle new mDNS service discovery.
        
        Requests service info, parses device details, and adds to device cache.
        Emits device_discovered or device_updated events.
        
        Args:
            zeroconf: Zeroconf instance
            service_type: mDNS service type
            name: Service name
        """
        try:
            logger.info(f"[AIRPLAY DISCOVERY] Service added: {name}")

            # Get service info
            info = AsyncServiceInfo(service_type, name)
            await info.async_request(zeroconf, 3000)

            if not info:
                logger.warning(f"[AIRPLAY DISCOVERY] Could not get info for {name}")
                return

            # Parse service info
            device = self._parse_service_info(info, service_type)

            if not device:
                return

            # Filter out self if configured
            if self.config['discovery']['exclude_self']:
                if self._is_self_device(device):
                    logger.debug(f"[AIRPLAY DISCOVERY] Excluding self device: {device.name}")
                    return

            # Add to discovered devices
            device_id = device.id
            is_new = device_id not in self.discovered_devices

            self.discovered_devices[device_id] = device

            if is_new:
                self.stats['total_discovered'] += 1
                logger.info(f"[AIRPLAY DISCOVERY] ✅ Discovered: {device.name} at {device.ip_address}:{device.port}")
                await self._emit_event('device_discovered', device=device)
            else:
                logger.debug(f"[AIRPLAY DISCOVERY] Updated: {device.name}")
                self.discovered_devices[device_id].update_last_seen()
                await self._emit_event('device_updated', device=device)

        except Exception as e:
            logger.error(f"[AIRPLAY DISCOVERY] Error handling service added: {e}")

    async def _handle_service_removed(self, name):
        """Handle mDNS service removal.
        
        Marks matching device as unavailable and emits device_lost event.
        
        Args:
            name: Service name that was removed
        """
        try:
            logger.info(f"[AIRPLAY DISCOVERY] Service removed: {name}")

            # Find device by name
            device_id = None
            for dev_id, device in self.discovered_devices.items():
                if device.name in name or name in device.name:
                    device_id = dev_id
                    break

            if device_id:
                device = self.discovered_devices[device_id]
                device.is_available = False
                self.stats['total_lost'] += 1
                logger.info(f"[AIRPLAY DISCOVERY] ❌ Lost: {device.name}")
                await self._emit_event('device_lost', device=device)

        except Exception as e:
            logger.error(f"[AIRPLAY DISCOVERY] Error handling service removed: {e}")

    async def _handle_service_updated(self, zeroconf, service_type, name):
        """Handle mDNS service update.
        
        Treats service updates as add/update events to refresh device info.
        
        Args:
            zeroconf: Zeroconf instance
            service_type: mDNS service type
            name: Service name
        """
        # Treat as added/updated
        await self._handle_service_added(zeroconf, service_type, name)

    def _parse_service_info(self, info: ServiceInfo, service_type: str) -> Optional[AirPlayDevice]:
        """Parse mDNS ServiceInfo into AirPlayDevice object.
        
        Extracts device information from mDNS service info including IP address,
        port, TXT records, and device metadata.
        
        Args:
            info: ServiceInfo from zeroconf
            service_type: mDNS service type
            
        Returns:
            AirPlayDevice instance or None if parsing fails
        """
        try:
            # Get IP address
            addresses = info.parsed_addresses()
            if not addresses:
                logger.warning(f"[AIRPLAY DISCOVERY] No addresses for {info.name}")
                return None

            ip_address = addresses[0]

            # Get port
            port = info.port

            # Get hostname
            hostname = info.server if info.server else info.name

            # Parse TXT records
            txt_records = {}
            if info.properties:
                for key, value in info.properties.items():
                    try:
                        txt_records[key.decode('utf-8')] = value.decode('utf-8')
                    except Exception:
                        txt_records[key.decode('utf-8')] = value

            # Extract device name
            device_name = txt_records.get('fn', info.name.split('.')[0])

            # Extract model
            model = txt_records.get('model', txt_records.get('am', None))

            # Extract features
            features = txt_records.get('features', None)

            # Generate device ID
            device_id = self._generate_device_id(device_name, ip_address)

            # Create device
            device = AirPlayDevice(
                id=device_id,
                name=device_name,
                hostname=hostname,
                ip_address=ip_address,
                port=port,
                service_type=service_type,
                txt_records=txt_records,
                model=model,
                features=features
            )

            return device

        except Exception as e:
            logger.error(f"[AIRPLAY DISCOVERY] Error parsing service info: {e}")
            return None

    def _generate_device_id(self, name: str, ip: str) -> str:
        """Generate unique device identifier.
        
        Creates MD5 hash of device name and IP address for consistent
        device identification across discovery sessions.
        
        Args:
            name: Device name
            ip: IP address
            
        Returns:
            12-character hex string device ID
        """
        import hashlib
        unique_str = f"{name}_{ip}"
        return hashlib.md5(unique_str.encode()).hexdigest()[:12]

    def _is_self_device(self, device: AirPlayDevice) -> bool:
        """Check if discovered device is this Mac.
        
        Prevents discovery of the local machine's own AirPlay service
        by checking IP address and hostname patterns.
        
        Args:
            device: Device to check
            
        Returns:
            True if device appears to be this machine
        """
        try:
            # Check if IP is localhost
            if device.ip_address in ['127.0.0.1', 'localhost', '::1']:
                return True

            # Check if device name matches this Mac's name
            import socket
            hostname = socket.gethostname()
            if device.name.lower() in hostname.lower() or hostname.lower() in device.name.lower():
                return True

            return False

        except Exception as e:
            logger.debug(f"[AIRPLAY DISCOVERY] Error checking self device: {e}")
            return False

    async def _cleanup_stale_devices(self):
        """Remove devices that haven't been seen recently.
        
        Checks all cached devices against cache TTL and removes stale entries.
        Emits device_lost events for removed devices.
        """
        try:
            now = datetime.now()
            stale_threshold = timedelta(seconds=self.cache_ttl)

            stale_ids = []
            for device_id, device in self.discovered_devices.items():
                if now - device.last_seen > stale_threshold:
                    stale_ids.append(device_id)

            for device_id in stale_ids:
                device = self.discovered_devices.pop(device_id)
                device.is_available = False
                logger.info(f"[AIRPLAY DISCOVERY] Removed stale device: {device.name}")
                await self._emit_event('device_lost', device=device)

        except Exception as e:
            logger.error(f"[AIRPLAY DISCOVERY] Error cleaning stale devices: {e}")

    async def discover_devices(self, timeout: Optional[float] = None) -> List[AirPlayDevice]:
        """
        Discover AirPlay devices on network with timeout.

        Performs one-time discovery scan for the specified duration.
        If discovery service is not running, starts it temporarily.

        Args:
            timeout: Discovery timeout in seconds. Uses config default if None.

        Returns:
            List of available AirPlay devices found during scan.
            
        Example:
            >>> devices = await discovery.discover_devices(10.0)
            >>> for device in devices:
            ...     print(f"{device.name} at {device.ip_address}")
        """
        start_time = time.time()

        if timeout is None:
            timeout = self.config['discovery']['timeout_seconds']

        logger.info(f"[AIRPLAY DISCOVERY] Starting discovery (timeout: {timeout}s)")

        # Start discovery if not already running
        was_running = self.is_discovering
        if not was_running:
            await self.start()

        # Wait for discovery
        await asyncio.sleep(timeout)

        # Stop discovery if we started it
        if not was_running:
            await self.stop()

        # Return available devices
        devices = [d for d in self.discovered_devices.values() if d.is_available]

        duration = time.time() - start_time
        self.stats['last_scan'] = datetime.now().isoformat()
        self.stats['scan_duration'] = duration

        logger.info(f"[AIRPLAY DISCOVERY] Discovery complete: found {len(devices)} devices in {duration:.2f}s")

        return devices

    def get_device_by_name(self, name: str) -> Optional[AirPlayDevice]:
        """Get device by name with fuzzy matching.
        
        Searches for device by exact name match first, then partial matches.
        Search is case-insensitive.
        
        Args:
            name: Device name to search for
            
        Returns:
            Matching AirPlayDevice or None if not found
            
        Example:
            >>> device = discovery.get_device_by_name("Living Room TV")
            >>> if device:
            ...     print(f"Found at {device.ip_address}")
        """
        name_lower = name.lower()

        for device in self.discovered_devices.values():
            if not device.is_available:
                continue

            # Exact match
            if device.name.lower() == name_lower:
                return device

            # Partial match
            if name_lower in device.name.lower() or device.name.lower() in name_lower:
                return device

        return None

    def get_device_by_ip(self, ip_address: str) -> Optional[AirPlayDevice]:
        """Get device by IP address.
        
        Args:
            ip_address: IPv4 address to search for
            
        Returns:
            Matching AirPlayDevice or None if not found
        """
        for device in self.discovered_devices.values():
            if device.is_available and device.ip_address == ip_address:
                return device
        return None

    def get_all_devices(self, available_only: bool = True) -> List[AirPlayDevice]:
        """Get all discovered devices.
        
        Args:
            available_only: If True, only return currently available devices
            
        Returns:
            List of AirPlayDevice objects
        """
        devices = list(self.discovered_devices.values())
        if available_only:
            devices = [d for d in devices if d.is_available]
        return devices

    def get_stats(self) -> Dict[str, Any]:
        """Get discovery service statistics and metrics.
        
        Returns:
            Dictionary containing discovery stats including device counts,
            error counts, timing information, and current service state.
        """
        return {
            **self.stats,
            'total_devices': len(self.discovered_devices),
            'available_devices': len([d for d in self.discovered_devices.values() if d.is_available]),
            'is_discovering': self.is_discovering
        }


# Singleton instance
_discovery_service: Optional[AirPlayDiscoveryService] = None


def get_airplay_discovery(config_path: Optional[str] = None) -> AirPlayDiscoveryService:
    """Get singleton AirPlay discovery service instance.
    
    Provides global access to a single discovery service instance.
    Creates new instance on first call.
    
    Args:
        config_path: Path to configuration file (only used on first call)
        
    Returns:
        AirPlayDiscoveryService singleton instance
        
    Example:
        >>> discovery = get_airplay_discovery()
        >>> await discovery.start()
    """
    global _discovery_service
    if _discovery_service is None:
        _discovery_service = AirPlayDiscoveryService(config_path)
    return _discovery_service


if __name__ == "__main__":
    # Test discovery service
    async def test():
        """Test the AirPlay discovery service functionality.
        
        Demonstrates basic usage including device discovery, event callbacks,
        and statistics reporting.
        """
        logging.basicConfig(level=logging.INFO)

        discovery = get_airplay_discovery()

        # Register callbacks
        async def on_discovered(device):
            print(f"✅ Discovered: {device.name} at {device.ip_address}:{device.port}")

        async def on_lost(device):
            print(f"❌ Lost: {device.name}")

        discovery.register_callback('device_discovered', on_discovered)
        discovery.register_callback('device_lost', on_lost)

        # Start discovery
        await discovery.start()

        print("\nDiscovering AirPlay devices... (30s)")
        await asyncio.sleep(30)

        # Get all devices
        devices = discovery.get_all_devices()
        print(f"\n\nFound {len(devices)} devices:")
        for device in devices:
            print(f"  - {device.name} ({device.ip_address}:{device.port})")

        # Get stats
        stats = discovery.get_stats()
        print(f"\nStats: {stats}")

        # Stop discovery
        await discovery.stop()

    asyncio.run(test())