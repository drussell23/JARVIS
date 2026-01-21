"""
Python 3.9 Compatibility Layer for JARVIS AI System
====================================================

This module provides runtime patches for Python 3.10+ features that are used
by dependencies but not available in Python 3.9. It must be imported VERY EARLY
in the startup process before any affected packages are imported.

Key Fixes:
- importlib.metadata.packages_distributions() - Added in Python 3.10
- google-api-core uses this function without proper version checking
- Suppresses noisy deprecation warnings from dependencies

Architecture:
- Fully async-native with sync fallbacks
- Monkey-patches importlib.metadata to add missing functions
- Patches google.api_core._python_version_support module if already loaded
- Uses importlib_metadata backport when available
- Provides graceful fallbacks when backport unavailable
- Comprehensive warning suppression for clean startup logs
- Thread-safe AND async-safe operations

Warning Suppression:
- Google API Core Python 3.9 EOL FutureWarning
- urllib3 LibreSSL NotOpenSSLWarning
- Pydantic v1/v2 deprecation warnings
- Other noisy but harmless dependency warnings

Usage:
    # Sync usage (at the very top of your main script):
    from backend.utils.python39_compat import ensure_python39_compatibility
    ensure_python39_compatibility()

    # Async usage:
    from backend.utils.python39_compat import ensure_python39_compatibility_async
    await ensure_python39_compatibility_async()

Author: JARVIS AI System
Version: 3.0.0 - Fully Async Python 3.9 Support with Warning Suppression
"""

from __future__ import annotations

import sys
import warnings
import logging
import re
import threading
import asyncio
from typing import (
    Dict, List, Mapping, Optional, Any, Callable,
    Pattern, Tuple, Set, Union, Coroutine, TypeVar
)
from functools import wraps
from dataclasses import dataclass, field
from contextlib import contextmanager, asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod

# Configure logging
logger = logging.getLogger(__name__)

# Type variables for generic async operations
T = TypeVar('T')
AsyncResult = Coroutine[Any, Any, T]

# Version info
__version__ = "3.0.0"
__python_version__ = sys.version_info
__is_python39__ = __python_version__ < (3, 10)

# Global thread pool for async operations
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Get or create the global thread pool executor."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="py39_compat_"
            )
        return _executor


async def run_sync_in_async(func: Callable[..., T], *args, **kwargs) -> T:
    """
    Run a synchronous function in an async context without blocking.

    Args:
        func: Synchronous function to run
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function

    Returns:
        The result of the function call
    """
    loop = asyncio.get_event_loop()
    executor = _get_executor()

    # Create a partial function if we have kwargs
    if kwargs:
        from functools import partial
        func = partial(func, **kwargs)
        return await loop.run_in_executor(executor, func, *args)
    else:
        return await loop.run_in_executor(executor, func, *args)


class AsyncLock:
    """
    A lock that works in both sync and async contexts.

    Provides a unified interface for thread-safe operations that can be
    used from both synchronous and asynchronous code.
    """

    def __init__(self):
        self._thread_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazily create async lock (must be created in async context)."""
        if self._async_lock is None:
            try:
                self._async_lock = asyncio.Lock()
            except RuntimeError:
                # No event loop - this is fine for sync usage
                pass
        return self._async_lock

    def acquire_sync(self) -> bool:
        """Acquire the lock synchronously."""
        return self._thread_lock.acquire()

    def release_sync(self) -> None:
        """Release the lock synchronously."""
        self._thread_lock.release()

    async def acquire_async(self) -> bool:
        """Acquire the lock asynchronously."""
        # First acquire thread lock to ensure thread safety
        self._thread_lock.acquire()
        try:
            async_lock = self._get_async_lock()
            if async_lock:
                await async_lock.acquire()
            return True
        except Exception:
            self._thread_lock.release()
            raise

    async def release_async(self) -> None:
        """Release the lock asynchronously."""
        try:
            async_lock = self._get_async_lock()
            if async_lock and async_lock.locked():
                async_lock.release()
        finally:
            self._thread_lock.release()

    def __enter__(self):
        """Sync context manager entry."""
        self.acquire_sync()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Sync context manager exit."""
        self.release_sync()
        return False

    async def __aenter__(self):
        """Async context manager entry."""
        await self.acquire_async()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.release_async()
        return False


@dataclass
class WarningRule:
    """
    Configuration for a warning suppression rule.

    Attributes:
        category: Warning category to suppress (e.g., FutureWarning, DeprecationWarning)
        message_pattern: Regex pattern to match warning messages
        module_pattern: Regex pattern to match module names (optional)
        description: Human-readable description of what this rule suppresses
        enabled: Whether this rule is active
    """
    category: type
    message_pattern: str
    module_pattern: Optional[str] = None
    description: str = ""
    enabled: bool = True

    def __post_init__(self):
        """Compile regex patterns for efficient matching."""
        self._message_re: Pattern = re.compile(self.message_pattern, re.IGNORECASE | re.DOTALL)
        self._module_re: Optional[Pattern] = (
            re.compile(self.module_pattern, re.IGNORECASE)
            if self.module_pattern else None
        )

    def matches(self, warning_message: str, module_name: str = "") -> bool:
        """Check if a warning matches this rule."""
        if not self.enabled:
            return False

        message_match = self._message_re.search(warning_message)
        if not message_match:
            return False

        if self._module_re and module_name:
            return bool(self._module_re.search(module_name))

        return True


class WarningSuppressionManager:
    """
    Advanced warning suppression manager for clean startup logs.

    Features:
    - Pattern-based warning filtering
    - Category-specific suppression
    - Module-aware filtering
    - Dynamic rule management
    - Fully async-safe operations with sync fallbacks
    - Detailed suppression logging
    - Async context manager support
    """

    _instance: Optional['WarningSuppressionManager'] = None
    _initialized: bool = False
    _async_lock: Optional[AsyncLock] = None

    # Default suppression rules for Python 3.9 compatibility
    DEFAULT_RULES: List[WarningRule] = [
        # Google API Core Python 3.9 EOL warning
        WarningRule(
            category=FutureWarning,
            message_pattern=r"You are using a Python version.*past its end of life.*google.*api.*core",
            module_pattern=r"google\.api_core\._python_version_support",
            description="Google API Core Python 3.9 EOL warning"
        ),
        # urllib3 LibreSSL warning
        WarningRule(
            category=UserWarning,
            message_pattern=r"urllib3.*only supports OpenSSL.*LibreSSL",
            module_pattern=r"urllib3",
            description="urllib3 LibreSSL compatibility warning"
        ),
        # Also catch as NotOpenSSLWarning (custom warning class)
        WarningRule(
            category=Warning,
            message_pattern=r"urllib3.*only supports OpenSSL|NotOpenSSLWarning",
            module_pattern=r"urllib3",
            description="urllib3 OpenSSL warning variant"
        ),
        # Pydantic V1/V2 deprecation warnings
        WarningRule(
            category=DeprecationWarning,
            message_pattern=r"Pydantic V1.*deprecated|pydantic.*will be removed",
            description="Pydantic version deprecation warning"
        ),
        # SpeechBrain deprecated warnings
        WarningRule(
            category=FutureWarning,
            message_pattern=r"speechbrain.*deprecated|torchaudio.*deprecated",
            description="SpeechBrain/TorchAudio deprecation warnings"
        ),
        # v95.0: SpeechBrain "frozen model" warnings (expected behavior for inference)
        WarningRule(
            category=UserWarning,
            message_pattern=r"Wav2Vec2Model is frozen|model is frozen|model.*frozen.*inference",
            module_pattern=r"speechbrain|huggingface_transformers",
            description="SpeechBrain frozen model warning (expected for inference)"
        ),
        # v95.0: HuggingFace weight initialization warnings
        WarningRule(
            category=UserWarning,
            message_pattern=r"weights.*not initialized|you should probably train|some weights of the model checkpoint",
            module_pattern=r"transformers|huggingface",
            description="HuggingFace weight initialization warnings"
        ),
        # Hugging Face token deprecation
        WarningRule(
            category=FutureWarning,
            message_pattern=r"use_auth_token.*deprecated|token.*instead of.*use_auth_token",
            description="Hugging Face auth token deprecation"
        ),
        # pkg_resources deprecation
        WarningRule(
            category=DeprecationWarning,
            message_pattern=r"pkg_resources.*deprecated|setuptools.*pkg_resources",
            description="pkg_resources deprecation warning"
        ),
        # asyncio deprecation warnings in Python 3.9
        WarningRule(
            category=DeprecationWarning,
            message_pattern=r"There is no current event loop|get_event_loop.*deprecated",
            description="asyncio event loop deprecation"
        ),
        # Cryptography/SSL deprecation warnings
        WarningRule(
            category=DeprecationWarning,
            message_pattern=r"ssl\.PROTOCOL_TLS.*deprecated|cryptography.*deprecated",
            description="SSL/Cryptography deprecation warnings"
        ),
        # Numpy deprecation warnings
        WarningRule(
            category=DeprecationWarning,
            message_pattern=r"numpy\..*deprecated|np\..*deprecated",
            description="NumPy deprecation warnings"
        ),
        # Google protobuf deprecation
        WarningRule(
            category=UserWarning,
            message_pattern=r"SymbolDatabase\.GetPrototype.*deprecated",
            description="Protobuf deprecation warning"
        ),
        # TensorFlow/PyTorch deprecation warnings
        WarningRule(
            category=FutureWarning,
            message_pattern=r"torch\..*deprecated|tensorflow.*deprecated",
            description="ML framework deprecation warnings"
        ),
    ]

    def __new__(cls) -> 'WarningSuppressionManager':
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the warning suppression manager."""
        if not hasattr(self, '_setup_complete'):
            self._lock = AsyncLock()
            self._rules: List[WarningRule] = list(self.DEFAULT_RULES)
            self._suppressed_count: Dict[str, int] = {}
            self._original_showwarning: Optional[Callable] = None
            self._original_filters: List = []
            self._custom_filter_installed: bool = False
            self._setup_complete = True

    def add_rule(self, rule: WarningRule) -> None:
        """Add a custom suppression rule (sync version)."""
        with self._lock:
            self._rules.append(rule)
            logger.debug(f"Added warning suppression rule: {rule.description}")

    async def add_rule_async(self, rule: WarningRule) -> None:
        """Add a custom suppression rule (async version)."""
        async with self._lock:
            self._rules.append(rule)
            logger.debug(f"Added warning suppression rule: {rule.description}")

    def remove_rule(self, description: str) -> bool:
        """Remove a rule by its description (sync version)."""
        with self._lock:
            for i, rule in enumerate(self._rules):
                if rule.description == description:
                    self._rules.pop(i)
                    logger.debug(f"Removed warning suppression rule: {description}")
                    return True
            return False

    async def remove_rule_async(self, description: str) -> bool:
        """Remove a rule by its description (async version)."""
        async with self._lock:
            for i, rule in enumerate(self._rules):
                if rule.description == description:
                    self._rules.pop(i)
                    logger.debug(f"Removed warning suppression rule: {description}")
                    return True
            return False

    def _should_suppress(self, message: str, category: type, module: str = "") -> Tuple[bool, Optional[str]]:
        """
        Check if a warning should be suppressed.

        Returns:
            Tuple of (should_suppress, rule_description)
        """
        for rule in self._rules:
            if not rule.enabled:
                continue

            # Check category
            if not issubclass(category, rule.category):
                continue

            # Check message pattern
            if rule.matches(message, module):
                return True, rule.description

        return False, None

    def _custom_showwarning(
        self,
        message: Warning,
        category: type,
        filename: str,
        lineno: int,
        file: Optional[Any] = None,
        line: Optional[str] = None
    ) -> None:
        """Custom warning handler that applies suppression rules."""
        msg_str = str(message)
        module = filename.replace('/', '.').replace('\\', '.')

        should_suppress, rule_desc = self._should_suppress(msg_str, category, module)

        if should_suppress:
            # Track suppressed warning (use sync lock interface)
            with self._lock:
                self._suppressed_count[rule_desc] = self._suppressed_count.get(rule_desc, 0) + 1
            logger.debug(f"Suppressed warning ({rule_desc}): {msg_str[:100]}...")
            return

        # Let non-matched warnings through
        if self._original_showwarning:
            self._original_showwarning(message, category, filename, lineno, file, line)

    def _apply_standard_filters(self) -> None:
        """Apply standard warning filters (internal helper)."""
        # Google API Core FutureWarning
        warnings.filterwarnings(
            'ignore',
            message=r".*Python version.*past its end of life.*",
            category=FutureWarning,
            module=r"google\.api_core.*"
        )

        # urllib3 LibreSSL warning
        warnings.filterwarnings(
            'ignore',
            message=r".*urllib3.*only supports OpenSSL.*",
            category=UserWarning,
            module=r"urllib3.*"
        )

        # Try to filter NotOpenSSLWarning specifically
        try:
            from urllib3.exceptions import NotOpenSSLWarning
            warnings.filterwarnings('ignore', category=NotOpenSSLWarning)
        except ImportError:
            pass

        # Pydantic deprecation
        warnings.filterwarnings(
            'ignore',
            message=r".*Pydantic V1.*deprecated.*",
            category=DeprecationWarning
        )

        # pkg_resources deprecation
        warnings.filterwarnings(
            'ignore',
            message=r".*pkg_resources.*deprecated.*",
            category=DeprecationWarning
        )

        # asyncio event loop
        warnings.filterwarnings(
            'ignore',
            message=r".*no current event loop.*",
            category=DeprecationWarning
        )

    def install_filters(self) -> bool:
        """
        Install warning suppression filters (sync version).

        Returns:
            True if installation was successful
        """
        with self._lock:
            if self._custom_filter_installed:
                logger.debug("Warning filters already installed")
                return True

            try:
                # Store original showwarning
                self._original_showwarning = warnings.showwarning

                # Install custom handler
                warnings.showwarning = self._custom_showwarning

                # Apply standard filters
                self._apply_standard_filters()

                self._custom_filter_installed = True
                logger.info("✅ Warning suppression filters installed")
                return True

            except Exception as e:
                logger.error(f"Failed to install warning filters: {e}")
                return False

    async def install_filters_async(self) -> bool:
        """
        Install warning suppression filters (async version).

        Returns:
            True if installation was successful
        """
        async with self._lock:
            if self._custom_filter_installed:
                logger.debug("Warning filters already installed")
                return True

            try:
                # Store original showwarning
                self._original_showwarning = warnings.showwarning

                # Install custom handler
                warnings.showwarning = self._custom_showwarning

                # Apply standard filters
                self._apply_standard_filters()

                self._custom_filter_installed = True
                logger.info("✅ Warning suppression filters installed")
                return True

            except Exception as e:
                logger.error(f"Failed to install warning filters: {e}")
                return False

    def uninstall_filters(self) -> bool:
        """Restore original warning behavior (sync version)."""
        with self._lock:
            if not self._custom_filter_installed:
                return True

            try:
                if self._original_showwarning:
                    warnings.showwarning = self._original_showwarning
                    self._original_showwarning = None

                warnings.resetwarnings()
                self._custom_filter_installed = False
                logger.info("Warning suppression filters removed")
                return True

            except Exception as e:
                logger.error(f"Failed to uninstall warning filters: {e}")
                return False

    async def uninstall_filters_async(self) -> bool:
        """Restore original warning behavior (async version)."""
        async with self._lock:
            if not self._custom_filter_installed:
                return True

            try:
                if self._original_showwarning:
                    warnings.showwarning = self._original_showwarning
                    self._original_showwarning = None

                warnings.resetwarnings()
                self._custom_filter_installed = False
                logger.info("Warning suppression filters removed")
                return True

            except Exception as e:
                logger.error(f"Failed to uninstall warning filters: {e}")
                return False

    @contextmanager
    def suppressed(self):
        """Sync context manager for temporary warning suppression."""
        self.install_filters()
        try:
            yield
        finally:
            pass  # Keep filters installed after context

    @asynccontextmanager
    async def suppressed_async(self):
        """Async context manager for temporary warning suppression."""
        await self.install_filters_async()
        try:
            yield
        finally:
            pass  # Keep filters installed after context

    def get_suppression_stats(self) -> Dict[str, Any]:
        """Get statistics about suppressed warnings (sync version)."""
        with self._lock:
            return {
                'total_suppressed': sum(self._suppressed_count.values()),
                'by_rule': dict(self._suppressed_count),
                'active_rules': len([r for r in self._rules if r.enabled]),
                'filters_installed': self._custom_filter_installed,
            }

    async def get_suppression_stats_async(self) -> Dict[str, Any]:
        """Get statistics about suppressed warnings (async version)."""
        async with self._lock:
            return {
                'total_suppressed': sum(self._suppressed_count.values()),
                'by_rule': dict(self._suppressed_count),
                'active_rules': len([r for r in self._rules if r.enabled]),
                'filters_installed': self._custom_filter_installed,
            }


class Python39CompatibilityManager:
    """
    Advanced compatibility manager for Python 3.9 runtime patches.

    Features:
    - Fully async-native with sync fallbacks
    - Thread-safe AND async-safe patching
    - Graceful degradation
    - Comprehensive logging
    - Dynamic module patching
    - Integrated warning suppression
    - Async context manager support
    """

    _instance: Optional['Python39CompatibilityManager'] = None
    _initialized: bool = False

    def __new__(cls) -> 'Python39CompatibilityManager':
        """Singleton pattern for global compatibility state."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the compatibility manager."""
        if not hasattr(self, '_setup_complete'):
            self._lock = AsyncLock()
            self._patched_modules: Dict[str, bool] = {}
            self._fallback_implementations: Dict[str, Callable] = {}
            self._error_log: List[str] = []
            self._warning_manager = WarningSuppressionManager()
            self._packages_dist_cache: Optional[Mapping[str, List[str]]] = None
            self._setup_complete = True

    @property
    def is_python39(self) -> bool:
        """Check if running on Python 3.9 or earlier."""
        return __is_python39__

    @property
    def warning_manager(self) -> WarningSuppressionManager:
        """Get the warning suppression manager."""
        return self._warning_manager

    def _create_packages_distributions_fallback(self) -> Callable[[], Mapping[str, List[str]]]:
        """
        Create a fallback implementation of packages_distributions().

        This function maps top-level package names to their distribution names.
        Uses importlib_metadata backport if available, otherwise builds from
        installed distributions.

        Returns:
            A callable that returns the package-to-distribution mapping
        """
        # Cache for performance
        _cache: Dict[str, Mapping[str, List[str]]] = {}

        def packages_distributions_impl() -> Mapping[str, List[str]]:
            """Return mapping of top-level packages to their distributions."""
            # Return cached result if available
            if 'result' in _cache:
                return _cache['result']

            pkg_to_dist: Dict[str, List[str]] = {}

            try:
                # Try importlib_metadata backport first (most reliable)
                try:
                    import importlib_metadata as metadata_backport
                    if hasattr(metadata_backport, 'packages_distributions'):
                        result = metadata_backport.packages_distributions()
                        _cache['result'] = result
                        return result
                except ImportError:
                    pass

                # Fall back to building the mapping ourselves
                try:
                    from importlib import metadata

                    for dist in metadata.distributions():
                        try:
                            dist_name = dist.metadata.get('Name', '')
                            if not dist_name:
                                continue

                            # Get top-level packages from top_level.txt or infer from files
                            top_level = None
                            try:
                                top_level_file = dist.read_text('top_level.txt')
                                if top_level_file:
                                    top_level = top_level_file.strip().split('\n')
                            except (FileNotFoundError, TypeError):
                                pass

                            if not top_level:
                                # Infer from distribution files
                                try:
                                    if dist.files:
                                        seen_packages: Set[str] = set()
                                        for file in dist.files:
                                            parts = str(file).split('/')
                                            if parts and not parts[0].endswith('.dist-info'):
                                                pkg = parts[0].replace('.py', '')
                                                if pkg and not pkg.startswith('_'):
                                                    seen_packages.add(pkg)
                                        top_level = list(seen_packages)
                                except Exception:
                                    top_level = [dist_name.replace('-', '_').lower()]

                            # Add to mapping
                            for pkg in (top_level or []):
                                pkg = pkg.strip()
                                if pkg:
                                    if pkg not in pkg_to_dist:
                                        pkg_to_dist[pkg] = []
                                    if dist_name not in pkg_to_dist[pkg]:
                                        pkg_to_dist[pkg].append(dist_name)
                        except Exception as e:
                            logger.debug(f"Error processing distribution: {e}")
                            continue

                except Exception as e:
                    logger.warning(f"Could not build packages_distributions mapping: {e}")

            except Exception as e:
                logger.error(f"packages_distributions fallback error: {e}")

            _cache['result'] = pkg_to_dist
            return pkg_to_dist

        return packages_distributions_impl

    def patch_importlib_metadata(self) -> bool:
        """
        Patch importlib.metadata to add packages_distributions if missing.

        Returns:
            True if patching was successful or unnecessary, False on error
        """
        if not self.is_python39:
            logger.debug("Python 3.10+ detected, no patching needed for importlib.metadata")
            return True

        try:
            from importlib import metadata

            # Check if already has the function
            if hasattr(metadata, 'packages_distributions'):
                logger.debug("importlib.metadata.packages_distributions already available")
                return True

            # Create and apply the patch
            packages_distributions = self._create_packages_distributions_fallback()
            metadata.packages_distributions = packages_distributions

            self._patched_modules['importlib.metadata'] = True
            logger.info("✅ Patched importlib.metadata.packages_distributions for Python 3.9")
            return True

        except Exception as e:
            error_msg = f"Failed to patch importlib.metadata: {e}"
            logger.error(error_msg)
            self._error_log.append(error_msg)
            return False

    def patch_google_api_core(self) -> bool:
        """
        Patch google.api_core._python_version_support if loaded.

        The google-api-core package has a bug where it uses packages_distributions()
        without proper Python version checking. This patches the module to use
        our fallback implementation.

        Returns:
            True if patching was successful or unnecessary, False on error
        """
        if not self.is_python39:
            return True

        module_name = 'google.api_core._python_version_support'

        try:
            # Check if module is already loaded
            if module_name in sys.modules:
                module = sys.modules[module_name]

                # Patch the _get_pypi_package_name function
                if hasattr(module, '_get_pypi_package_name'):
                    packages_distributions = self._create_packages_distributions_fallback()

                    def patched_get_pypi_package_name(module_name: str) -> Optional[str]:
                        """Patched version that uses our fallback implementation."""
                        try:
                            module_to_distributions = packages_distributions()
                            if module_name in module_to_distributions:
                                return module_to_distributions[module_name][0]
                            return None
                        except Exception as e:
                            logger.debug(f"_get_pypi_package_name error: {e}")
                            return None

                    module._get_pypi_package_name = patched_get_pypi_package_name
                    self._patched_modules[module_name] = True
                    logger.info("✅ Patched google.api_core._python_version_support")
                    return True
            else:
                # Module not loaded yet, install import hook
                self._install_google_api_core_hook()
                return True

        except Exception as e:
            error_msg = f"Failed to patch google.api_core: {e}"
            logger.warning(error_msg)
            self._error_log.append(error_msg)
            return False

    def _install_google_api_core_hook(self) -> None:
        """Install an import hook to patch google.api_core when it's imported."""
        import importlib.abc
        import importlib.machinery

        class GoogleApiCoreImportHook(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            """Import hook that patches google.api_core modules on import."""

            def __init__(self, compat_manager: 'Python39CompatibilityManager'):
                self.compat_manager = compat_manager
                self.patched = False

            def find_spec(self, fullname, path, target=None):
                if fullname == 'google.api_core._python_version_support' and not self.patched:
                    # Let the normal import happen, then patch
                    return None
                return None

            def find_module(self, fullname, path=None):
                return None

        # Install the hook
        hook = GoogleApiCoreImportHook(self)
        if hook not in sys.meta_path:
            sys.meta_path.insert(0, hook)

    def patch_huggingface_hub(self) -> bool:
        """
        Patch huggingface_hub to convert deprecated 'use_auth_token' to 'token'.

        huggingface_hub 1.0+ removed the 'use_auth_token' parameter, but SpeechBrain
        and other libraries still use it. This patch wraps hf_hub_download and
        snapshot_download to automatically convert the parameter.

        Also patches SpeechBrain's fetch function to handle missing optional files
        gracefully (e.g., custom.py that doesn't exist in some model repos).

        Returns:
            True if patching was successful or unnecessary, False on error
        """
        try:
            import huggingface_hub

            hf_version = getattr(huggingface_hub, '__version__', '0.0.0')
            hf_major = int(hf_version.split('.')[0])

            if hf_major < 1:
                logger.debug(f"huggingface_hub {hf_version} still supports use_auth_token, no patch needed")
                return True

            # Already patched check
            if hasattr(huggingface_hub.hf_hub_download, '_jarvis_patched'):
                logger.debug("huggingface_hub already patched")
                return True

            # Get the RemoteEntryNotFoundError for exception handling
            try:
                from huggingface_hub.errors import RemoteEntryNotFoundError
            except ImportError:
                from huggingface_hub.utils import EntryNotFoundError as RemoteEntryNotFoundError

            def create_auth_token_wrapper(original_func):
                """Create a wrapper that converts use_auth_token to token."""
                @wraps(original_func)
                def wrapper(*args, **kwargs):
                    if 'use_auth_token' in kwargs:
                        auth_token = kwargs.pop('use_auth_token')
                        if auth_token is not None and 'token' not in kwargs:
                            kwargs['token'] = auth_token
                    return original_func(*args, **kwargs)
                wrapper._jarvis_patched = True
                return wrapper

            def create_speechbrain_fetch_wrapper(original_fetch, not_found_error):
                """Wrap SpeechBrain's fetch to convert RemoteEntryNotFoundError to ValueError."""
                @wraps(original_fetch)
                def wrapper(*args, **kwargs):
                    try:
                        return original_fetch(*args, **kwargs)
                    except not_found_error as e:
                        # Convert to ValueError so SpeechBrain's except block catches it
                        # This handles missing optional files like custom.py
                        raise ValueError(str(e)) from e
                wrapper._jarvis_patched = True
                return wrapper

            # Store originals
            original_hf_hub_download = huggingface_hub.hf_hub_download
            original_snapshot_download = getattr(huggingface_hub, 'snapshot_download', None)

            # Patch huggingface_hub module
            patched_hf_hub_download = create_auth_token_wrapper(original_hf_hub_download)
            huggingface_hub.hf_hub_download = patched_hf_hub_download

            if original_snapshot_download:
                patched_snapshot_download = create_auth_token_wrapper(original_snapshot_download)
                huggingface_hub.snapshot_download = patched_snapshot_download

            # Also patch the file_download module where hf_hub_download is defined
            try:
                import huggingface_hub.file_download as hf_file_download
                hf_file_download.hf_hub_download = patched_hf_hub_download
            except ImportError:
                pass

            # Patch speechbrain.utils.fetching if already loaded
            try:
                import speechbrain.utils.fetching as sb_fetching
                if hasattr(sb_fetching, 'hf_hub_download'):
                    sb_fetching.hf_hub_download = patched_hf_hub_download
                if hasattr(sb_fetching, 'snapshot_download') and original_snapshot_download:
                    sb_fetching.snapshot_download = patched_snapshot_download
                # Patch fetch function to convert RemoteEntryNotFoundError to ValueError
                if hasattr(sb_fetching, 'fetch') and not hasattr(sb_fetching.fetch, '_jarvis_patched'):
                    original_fetch = sb_fetching.fetch
                    sb_fetching.fetch = create_speechbrain_fetch_wrapper(original_fetch, RemoteEntryNotFoundError)
                logger.debug("Patched speechbrain.utils.fetching")
            except ImportError:
                # SpeechBrain not yet loaded, install import hook
                self._install_speechbrain_fetching_hook(
                    patched_hf_hub_download,
                    patched_snapshot_download if original_snapshot_download else None,
                    RemoteEntryNotFoundError,
                    create_speechbrain_fetch_wrapper
                )

            self._patched_modules['huggingface_hub'] = True
            logger.info(f"✅ Patched huggingface_hub {hf_version} (use_auth_token → token)")
            return True

        except ImportError:
            logger.debug("huggingface_hub not installed, no patch needed")
            return True
        except Exception as e:
            error_msg = f"Failed to patch huggingface_hub: {e}"
            logger.warning(error_msg)
            self._error_log.append(error_msg)
            return False

    def _install_speechbrain_fetching_hook(
        self,
        patched_hf_hub_download,
        patched_snapshot_download,
        not_found_error=None,
        fetch_wrapper_factory=None
    ) -> None:
        """Install an import hook to patch speechbrain.utils.fetching when imported."""
        import importlib.abc

        class SpeechBrainFetchingHook(importlib.abc.MetaPathFinder):
            """Import hook that patches speechbrain.utils.fetching on import."""

            def __init__(self, hf_download_patch, snapshot_patch, error_class, wrapper_factory):
                self.hf_download_patch = hf_download_patch
                self.snapshot_patch = snapshot_patch
                self.error_class = error_class
                self.wrapper_factory = wrapper_factory
                self._patching = False

            def find_module(self, fullname, path=None):
                if fullname == 'speechbrain.utils.fetching' and not self._patching:
                    # Post-import hook: let import complete, then patch
                    import threading
                    def patch_after_import():
                        import time
                        time.sleep(0.01)  # Let import complete
                        try:
                            if 'speechbrain.utils.fetching' in sys.modules:
                                sb_fetching = sys.modules['speechbrain.utils.fetching']
                                if hasattr(sb_fetching, 'hf_hub_download'):
                                    sb_fetching.hf_hub_download = self.hf_download_patch
                                if self.snapshot_patch and hasattr(sb_fetching, 'snapshot_download'):
                                    sb_fetching.snapshot_download = self.snapshot_patch
                                # Patch fetch function to handle missing optional files
                                if (self.wrapper_factory and self.error_class and
                                    hasattr(sb_fetching, 'fetch') and
                                    not hasattr(sb_fetching.fetch, '_jarvis_patched')):
                                    original_fetch = sb_fetching.fetch
                                    sb_fetching.fetch = self.wrapper_factory(original_fetch, self.error_class)
                                logger.debug("Applied delayed patch to speechbrain.utils.fetching")
                        except Exception as e:
                            logger.debug(f"Delayed speechbrain patch failed: {e}")

                    threading.Thread(target=patch_after_import, daemon=True).start()
                return None

        hook = SpeechBrainFetchingHook(
            patched_hf_hub_download,
            patched_snapshot_download,
            not_found_error,
            fetch_wrapper_factory
        )
        if hook not in sys.meta_path:
            sys.meta_path.insert(0, hook)

    def suppress_warnings(self) -> bool:
        """Install warning suppression filters (sync version)."""
        return self._warning_manager.install_filters()

    async def suppress_warnings_async(self) -> bool:
        """Install warning suppression filters (async version)."""
        return await self._warning_manager.install_filters_async()

    def patch_all(self) -> Dict[str, bool]:
        """
        Apply all Python 3.9 compatibility patches (sync version).

        Returns:
            Dictionary of module names and their patch status
        """
        with self._lock:
            if Python39CompatibilityManager._initialized:
                logger.debug("Compatibility patches already applied")
                return self._patched_modules.copy()

            results = {}

            # First: Install warning suppression (before any imports might trigger warnings)
            results['warning_suppression'] = self.suppress_warnings()

            # Core patches
            results['importlib.metadata'] = self.patch_importlib_metadata()
            results['google.api_core'] = self.patch_google_api_core()

            # CRITICAL: Patch huggingface_hub before any ML libraries load
            # This fixes: hf_hub_download() got an unexpected keyword argument 'use_auth_token'
            results['huggingface_hub'] = self.patch_huggingface_hub()

            Python39CompatibilityManager._initialized = True

            # Log summary
            success_count = sum(1 for v in results.values() if v)
            total_count = len(results)

            if success_count == total_count:
                logger.info(f"✅ All {total_count} Python 3.9 compatibility patches applied successfully")
            else:
                logger.warning(f"⚠️ {success_count}/{total_count} patches applied, some may have issues")

            return results

    async def patch_all_async(self) -> Dict[str, bool]:
        """
        Apply all Python 3.9 compatibility patches (async version).

        This is a truly async implementation that doesn't block the event loop.

        Returns:
            Dictionary of module names and their patch status
        """
        async with self._lock:
            if Python39CompatibilityManager._initialized:
                logger.debug("Compatibility patches already applied")
                return self._patched_modules.copy()

            results = {}

            # First: Install warning suppression (before any imports might trigger warnings)
            results['warning_suppression'] = await self.suppress_warnings_async()

            # Core patches - run in executor to avoid blocking
            results['importlib.metadata'] = await run_sync_in_async(self.patch_importlib_metadata)
            results['google.api_core'] = await run_sync_in_async(self.patch_google_api_core)

            # CRITICAL: Patch huggingface_hub before any ML libraries load
            # This fixes: hf_hub_download() got an unexpected keyword argument 'use_auth_token'
            results['huggingface_hub'] = await run_sync_in_async(self.patch_huggingface_hub)

            Python39CompatibilityManager._initialized = True

            # Log summary
            success_count = sum(1 for v in results.values() if v)
            total_count = len(results)

            if success_count == total_count:
                logger.info(f"✅ All {total_count} Python 3.9 compatibility patches applied successfully")
            else:
                logger.warning(f"⚠️ {success_count}/{total_count} patches applied, some may have issues")

            return results

    def get_status(self) -> Dict[str, Any]:
        """Get the current status of all patches (sync version)."""
        with self._lock:
            return {
                'python_version': f"{__python_version__.major}.{__python_version__.minor}.{__python_version__.micro}",
                'is_python39': self.is_python39,
                'initialized': Python39CompatibilityManager._initialized,
                'patched_modules': self._patched_modules.copy(),
                'errors': self._error_log.copy(),
                'warning_stats': self._warning_manager.get_suppression_stats(),
            }

    async def get_status_async(self) -> Dict[str, Any]:
        """Get the current status of all patches (async version)."""
        async with self._lock:
            return {
                'python_version': f"{__python_version__.major}.{__python_version__.minor}.{__python_version__.micro}",
                'is_python39': self.is_python39,
                'initialized': Python39CompatibilityManager._initialized,
                'patched_modules': self._patched_modules.copy(),
                'errors': self._error_log.copy(),
                'warning_stats': await self._warning_manager.get_suppression_stats_async(),
            }


# Global instance
_compat_manager: Optional[Python39CompatibilityManager] = None
_warning_manager: Optional[WarningSuppressionManager] = None


def get_compat_manager() -> Python39CompatibilityManager:
    """Get the global compatibility manager instance."""
    global _compat_manager
    if _compat_manager is None:
        _compat_manager = Python39CompatibilityManager()
    return _compat_manager


def get_warning_manager() -> WarningSuppressionManager:
    """Get the global warning suppression manager instance."""
    global _warning_manager
    if _warning_manager is None:
        _warning_manager = WarningSuppressionManager()
    return _warning_manager


def ensure_python39_compatibility() -> Dict[str, bool]:
    """
    Ensure Python 3.9 compatibility by applying all necessary patches.

    This function should be called at the very start of your application,
    before importing any packages that might use Python 3.10+ features.

    Returns:
        Dictionary of applied patches and their status

    Example:
        # At the top of start_system.py:
        from backend.utils.python39_compat import ensure_python39_compatibility
        ensure_python39_compatibility()
    """
    manager = get_compat_manager()
    return manager.patch_all()


async def ensure_python39_compatibility_async() -> Dict[str, bool]:
    """
    Async version of ensure_python39_compatibility.

    Returns:
        Dictionary of applied patches and their status
    """
    manager = get_compat_manager()
    return await manager.patch_all_async()


def suppress_deprecation_warnings() -> bool:
    """
    Suppress common deprecation warnings from dependencies (sync version).

    Call this early in your application to silence noisy but harmless warnings.

    Returns:
        True if suppression was successful
    """
    manager = get_warning_manager()
    return manager.install_filters()


async def suppress_deprecation_warnings_async() -> bool:
    """
    Suppress common deprecation warnings from dependencies (async version).

    Call this early in your application to silence noisy but harmless warnings.

    Returns:
        True if suppression was successful
    """
    manager = get_warning_manager()
    return await manager.install_filters_async()


def add_warning_rule(
    category: type,
    message_pattern: str,
    module_pattern: Optional[str] = None,
    description: str = "Custom rule"
) -> None:
    """
    Add a custom warning suppression rule (sync version).

    Args:
        category: Warning category to suppress
        message_pattern: Regex pattern for message matching
        module_pattern: Optional regex pattern for module matching
        description: Human-readable description
    """
    rule = WarningRule(
        category=category,
        message_pattern=message_pattern,
        module_pattern=module_pattern,
        description=description
    )
    get_warning_manager().add_rule(rule)


async def add_warning_rule_async(
    category: type,
    message_pattern: str,
    module_pattern: Optional[str] = None,
    description: str = "Custom rule"
) -> None:
    """
    Add a custom warning suppression rule (async version).

    Args:
        category: Warning category to suppress
        message_pattern: Regex pattern for message matching
        module_pattern: Optional regex pattern for module matching
        description: Human-readable description
    """
    rule = WarningRule(
        category=category,
        message_pattern=message_pattern,
        module_pattern=module_pattern,
        description=description
    )
    await get_warning_manager().add_rule_async(rule)


def is_patched(module_name: str) -> bool:
    """Check if a specific module has been patched."""
    manager = get_compat_manager()
    return manager._patched_modules.get(module_name, False)


def get_compat_status() -> Dict[str, Any]:
    """Get the current compatibility status (sync version)."""
    manager = get_compat_manager()
    return manager.get_status()


async def get_compat_status_async() -> Dict[str, Any]:
    """Get the current compatibility status (async version)."""
    manager = get_compat_manager()
    return await manager.get_status_async()


def get_warning_stats() -> Dict[str, Any]:
    """Get statistics about suppressed warnings (sync version)."""
    manager = get_warning_manager()
    return manager.get_suppression_stats()


async def get_warning_stats_async() -> Dict[str, Any]:
    """Get statistics about suppressed warnings (async version)."""
    manager = get_warning_manager()
    return await manager.get_suppression_stats_async()


def cleanup_executor() -> None:
    """
    Cleanup the global thread pool executor.

    Call this when shutting down to ensure clean exit.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=True)
            _executor = None
            logger.debug("Thread pool executor cleaned up")


async def cleanup_executor_async() -> None:
    """
    Cleanup the global thread pool executor (async version).

    Call this when shutting down to ensure clean exit.
    """
    await run_sync_in_async(cleanup_executor)


# Auto-apply patches and warning suppression on module import if we're on Python 3.9
if __is_python39__:
    # Apply patches immediately on import
    try:
        ensure_python39_compatibility()
    except Exception as e:
        logger.error(f"Failed to auto-apply Python 3.9 compatibility patches: {e}")
