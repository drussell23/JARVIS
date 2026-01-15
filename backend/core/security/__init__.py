"""
Unified Security System v1.0
=============================

Enterprise-grade security for the JARVIS Trinity ecosystem.
Handles all aspects of security across JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning).

Implements 5 critical security patterns:
1. Cross-Repo Authentication - mTLS + JWT tokens
2. Cross-Repo Authorization - Role-based access control (RBAC)
3. Secure Communication - AES-256-GCM encrypted IPC
4. Audit Logging - Tamper-evident comprehensive audit trail
5. Secret Management - Vault-like secure secret storage

Author: Trinity Security System
Version: 1.0.0
"""

from backend.core.security.unified_engine import (
    # Configuration
    SecurityConfig,
    # Enums
    AuthMethod,
    TokenType,
    Permission,
    Role,
    ComponentIdentity,
    AuditAction,
    SecurityLevel,
    EncryptionAlgorithm,
    # Data Structures
    TokenPayload,
    AuthToken,
    APIKey,
    RoleDefinition,
    Principal,
    AuditEntry,
    Secret,
    EncryptedMessage,
    # Managers
    JWTTokenManager,
    APIKeyManager,
    RBACManager,
    EncryptionManager,
    AuditLogger,
    SecureSecretManager,
    # Engine
    UnifiedSecurityEngine,
    # Global Functions
    get_security_engine,
    initialize_security,
    shutdown_security,
)

from backend.core.security.cross_repo_bridge import (
    # Configuration
    CrossRepoSecurityConfig,
    # Enums
    SecurityEventType,
    TrustLevel,
    SecurityPolicyType,
    # Data Structures
    SecurityEvent,
    RepoTrustInfo,
    SecurityPolicy,
    IntrusionAlert,
    # Components
    SecurityEventBus,
    IntrusionDetector,
    # Bridge
    CrossRepoSecurityBridge,
    # Global Functions
    get_cross_repo_security_bridge,
    initialize_cross_repo_security,
    shutdown_cross_repo_security,
)

from backend.core.security.supervisor_integration import (
    # Enums
    SecuritySystemState,
    SecurityComponentState,
    # Configuration
    SecuritySupervisorConfig,
    # Data Structures
    SecurityComponentHealth,
    SecurityInitResult,
    SecurityHealthReport,
    # Coordinator
    SecuritySupervisorCoordinator,
    # Global Functions
    get_security_supervisor,
    initialize_security_supervisor,
    shutdown_security_supervisor,
    get_security_status,
    get_security_health,
    # Context Manager
    SecurityContext,
)

__all__ = [
    # =========================================================================
    # UNIFIED ENGINE
    # =========================================================================
    # Configuration
    "SecurityConfig",
    # Enums
    "AuthMethod",
    "TokenType",
    "Permission",
    "Role",
    "ComponentIdentity",
    "AuditAction",
    "SecurityLevel",
    "EncryptionAlgorithm",
    # Data Structures
    "TokenPayload",
    "AuthToken",
    "APIKey",
    "RoleDefinition",
    "Principal",
    "AuditEntry",
    "Secret",
    "EncryptedMessage",
    # Managers
    "JWTTokenManager",
    "APIKeyManager",
    "RBACManager",
    "EncryptionManager",
    "AuditLogger",
    "SecureSecretManager",
    # Engine
    "UnifiedSecurityEngine",
    # Global Functions
    "get_security_engine",
    "initialize_security",
    "shutdown_security",
    # =========================================================================
    # CROSS-REPO BRIDGE
    # =========================================================================
    # Configuration
    "CrossRepoSecurityConfig",
    # Enums
    "SecurityEventType",
    "TrustLevel",
    "SecurityPolicyType",
    # Data Structures
    "SecurityEvent",
    "RepoTrustInfo",
    "SecurityPolicy",
    "IntrusionAlert",
    # Components
    "SecurityEventBus",
    "IntrusionDetector",
    # Bridge
    "CrossRepoSecurityBridge",
    # Global Functions
    "get_cross_repo_security_bridge",
    "initialize_cross_repo_security",
    "shutdown_cross_repo_security",
    # =========================================================================
    # SUPERVISOR INTEGRATION
    # =========================================================================
    # Enums
    "SecuritySystemState",
    "SecurityComponentState",
    # Configuration
    "SecuritySupervisorConfig",
    # Data Structures
    "SecurityComponentHealth",
    "SecurityInitResult",
    "SecurityHealthReport",
    # Coordinator
    "SecuritySupervisorCoordinator",
    # Global Functions
    "get_security_supervisor",
    "initialize_security_supervisor",
    "shutdown_security_supervisor",
    "get_security_status",
    "get_security_health",
    # Context Manager
    "SecurityContext",
]
