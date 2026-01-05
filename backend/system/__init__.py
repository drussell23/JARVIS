"""
JARVIS System Module

Contains system-level utilities and integrations:
- app_library.py: v67.0 CEREBRO PROTOCOL - Dynamic App Resolution via macOS Spotlight
- phantom_hardware_manager.py: v68.0 PHANTOM HARDWARE - Software-Defined Ghost Display
- cryostasis_manager.py: v69.0 CRYOSTASIS - Process Suspension for Resource Governance
- reactor_bridge.py: PROJECT TRINITY - Cross-Repo Communication Bridge
"""

# v67.0: App Library (Cerebro Protocol)
from .app_library import (
    AppLibrary,
    get_app_library,
    resolve_app_name,
    is_app_installed,
    is_app_running,
)

# v68.0: Phantom Hardware Manager
from .phantom_hardware_manager import (
    PhantomHardwareManager,
    get_phantom_manager,
    ensure_ghost_display,
    get_phantom_status,
)

# v69.0: Cryostasis Manager
from .cryostasis_manager import (
    CryostasisManager,
    get_cryostasis_manager,
    freeze_app,
    thaw_app,
    is_app_frozen,
    ensure_app_thawed,
)

# PROJECT TRINITY: Reactor Core Bridge
from .reactor_bridge import (
    ReactorCoreBridge,
    get_reactor_bridge,
    connect_to_reactor,
    publish_command,
    TrinityCommand,
    TrinityIntent,
    TrinitySource,
    HeartbeatPayload,
)

# PROJECT TRINITY: Command Handlers
from .trinity_handlers import (
    register_trinity_handlers,
)

# PROJECT TRINITY: Auto-Initializer
from .trinity_initializer import (
    initialize_trinity,
    shutdown_trinity,
    trinity_context,
    is_trinity_initialized,
    get_trinity_status,
    JARVIS_INSTANCE_ID,
)

__all__ = [
    # v67.0 Cerebro
    'AppLibrary',
    'get_app_library',
    'resolve_app_name',
    'is_app_installed',
    'is_app_running',
    # v68.0 Phantom Hardware
    'PhantomHardwareManager',
    'get_phantom_manager',
    'ensure_ghost_display',
    'get_phantom_status',
    # v69.0 Cryostasis
    'CryostasisManager',
    'get_cryostasis_manager',
    'freeze_app',
    'thaw_app',
    'is_app_frozen',
    'ensure_app_thawed',
    # PROJECT TRINITY
    'ReactorCoreBridge',
    'get_reactor_bridge',
    'connect_to_reactor',
    'publish_command',
    'TrinityCommand',
    'TrinityIntent',
    'TrinitySource',
    'HeartbeatPayload',
    'register_trinity_handlers',
    # Trinity Initializer
    'initialize_trinity',
    'shutdown_trinity',
    'trinity_context',
    'is_trinity_initialized',
    'get_trinity_status',
    'JARVIS_INSTANCE_ID',
]
