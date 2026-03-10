"""
boot_handshake.py — Real implementations of HandshakeEngine, PolicyLoader, and
RuntimeInventoryProvider for the JARVIS governance boot sequence.

Classes
-------
ConcreteHandshakeEngine   - Pure-synchronous HandshakeEngine implementation.
YamlPolicyLoader          - Loads a schema_version 1.0.0 YAML policy manifest.
JprimeRuntimeInventoryProvider - Fetches /v1/brains from j-prime via HTTP.
run_boot_handshake        - Convenience async function that wires the three together.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from backend.core.ouroboros.governance.contracts.inventory_handshake_contract import (
    BrainDescriptor,
    HandshakeDiff,
    HandshakeEngine,
    HandshakeMode,
    HandshakeResult,
    PolicyManifest,
    RuntimeInventory,
)

__all__ = [
    "ConcreteHandshakeEngine",
    "YamlPolicyLoader",
    "JprimeRuntimeInventoryProvider",
    "run_boot_handshake",
]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_semver(version: str) -> tuple[int, ...]:
    """Split a semver string into a 3-tuple of ints, zero-padding as needed.

    Raises ValueError if any part is not an integer.
    """
    parts = version.split(".")
    # Pad to exactly 3 parts
    while len(parts) < 3:
        parts.append("0")
    return tuple(int(p) for p in parts[:3])


# ---------------------------------------------------------------------------
# 1. ConcreteHandshakeEngine
# ---------------------------------------------------------------------------

class ConcreteHandshakeEngine(HandshakeEngine):
    """Synchronous implementation of the HandshakeEngine contract."""

    # ------------------------------------------------------------------
    # validate_schema
    # ------------------------------------------------------------------

    def validate_schema(
        self, policy: PolicyManifest, runtime: RuntimeInventory
    ) -> None:
        """Raise ValueError if either schema_version is absent or empty."""
        if not policy.schema_version:
            raise ValueError(
                "CONTRACT_SCHEMA_INVALID: policy.schema_version is empty or None"
            )
        if not runtime.schema_version:
            raise ValueError(
                "CONTRACT_SCHEMA_INVALID: runtime.schema_version is empty or None"
            )

    # ------------------------------------------------------------------
    # validate_contract_versions
    # ------------------------------------------------------------------

    def validate_contract_versions(
        self, policy: PolicyManifest, runtime: RuntimeInventory
    ) -> None:
        """Raise ValueError if runtime.contract_version falls outside [min, max]."""
        try:
            runtime_v = _parse_semver(runtime.contract_version)
            min_v = _parse_semver(policy.min_runtime_contract_version)
            max_v = _parse_semver(policy.max_runtime_contract_version)
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                f"CONTRACT_SCHEMA_INVALID: unparseable version string — {exc}"
            ) from exc

        if runtime_v < min_v or runtime_v > max_v:
            raise ValueError(
                f"CONTRACT_VERSION_INCOMPATIBLE: runtime={runtime.contract_version} "
                f"not in [{policy.min_runtime_contract_version}, "
                f"{policy.max_runtime_contract_version}]"
            )

    # ------------------------------------------------------------------
    # diff
    # ------------------------------------------------------------------

    def diff(
        self, policy: PolicyManifest, runtime: RuntimeInventory
    ) -> HandshakeDiff:
        """Compute the structural difference between policy expectations and
        the current runtime inventory."""
        routable_ready: frozenset[str] = frozenset(
            b.brain_id
            for b in runtime.brains.values()
            if b.routable and b.health_state == "ready"
        )

        phantom_required = frozenset(policy.required_brains - routable_ready)
        optional_missing = frozenset(policy.optional_brains - routable_ready)
        unexpected_runtime = frozenset(routable_ready - policy.allowed_brains)

        capability_mismatch: set[str] = set()
        for brain_id, req_caps in policy.required_capabilities.items():
            if brain_id in runtime.brains:
                actual_caps = runtime.brains[brain_id].capabilities
                if not req_caps.issubset(actual_caps):
                    capability_mismatch.add(brain_id)

        return HandshakeDiff(
            phantom_required=phantom_required,
            optional_missing=optional_missing,
            unexpected_runtime=unexpected_runtime,
            capability_mismatch=frozenset(capability_mismatch),
        )

    # ------------------------------------------------------------------
    # evaluate
    # ------------------------------------------------------------------

    def evaluate(
        self, policy: PolicyManifest, runtime: RuntimeInventory
    ) -> HandshakeResult:
        """Run the full handshake sequence and return a HandshakeResult.

        Steps (all synchronous):
          1. validate_schema   — aborts on invalid schema
          2. validate_contract_versions — aborts on version incompatibility
          3. diff              — compute structural diff
          4. Build result according to policy mode
        """
        # Phase 1 & 2 — may raise
        self.validate_schema(policy, runtime)
        self.validate_contract_versions(policy, runtime)

        # Phase 3 — diff
        d = self.diff(policy, runtime)

        # Recompute routable_ready set for active intersection
        routable_ready: frozenset[str] = frozenset(
            b.brain_id
            for b in runtime.brains.values()
            if b.routable and b.health_state == "ready"
        )
        active = frozenset(policy.allowed_brains & routable_ready)

        # Phase 4 — gate logic
        reason_codes: list[str] = []
        accepted = True
        degraded = False

        if d.phantom_required:
            reason_codes.append("CONTRACT_REQUIRED_BRAIN_MISSING")
            if policy.mode == HandshakeMode.HARD_FAIL:
                accepted = False
            else:
                degraded = True

        if d.capability_mismatch:
            reason_codes.append("CONTRACT_CAPABILITY_MISMATCH")
            accepted = False

        if d.unexpected_runtime:
            # Log-only — does not gate boot
            reason_codes.append("CONTRACT_UNEXPECTED_RUNTIME_BRAIN")
            _log.warning(
                "Boot handshake: unexpected runtime brains detected (not allowlisted) "
                "— they will NOT be added to the active set: %s",
                d.unexpected_runtime,
            )

        return HandshakeResult(
            accepted=accepted,
            degraded=degraded,
            reason_codes=reason_codes,
            active_brain_set=active,
            diff=d,
        )


# ---------------------------------------------------------------------------
# 2. YamlPolicyLoader
# ---------------------------------------------------------------------------

class YamlPolicyLoader:
    """Load a schema_version 1.0.0 YAML brain-selection policy manifest.

    The expected YAML structure (schema_version 1.0.0):

    schema_version: "1.0.0"
    contract_version: "1.0.0"

    handshake:
      fail_mode: "hard_fail"   # or "degraded"
      compatibility:
        min_runtime_contract_version: "1.0.0"
        max_runtime_contract_version: "1.0.999"

    brains:
      required:
        - brain_id: phi3_lightweight
          required_capabilities: [chat, trivial_ops]
      optional:
        - brain_id: mistral_7b_fallback

    allowlist:
      allowed_brain_ids: [phi3_lightweight, qwen_coder, mistral_7b_fallback]
    """

    def __init__(self, policy_path: Path) -> None:
        self._policy_path = Path(policy_path)

    async def load_policy(self) -> PolicyManifest:
        """Read and parse the YAML policy file; returns a PolicyManifest."""
        try:
            import yaml  # type: ignore[import]
        except ImportError as exc:
            raise ImportError("PyYAML required: pip install pyyaml") from exc

        loop = asyncio.get_event_loop()
        raw_text = await loop.run_in_executor(
            None, self._policy_path.read_text, "utf-8"
        )
        data: dict = yaml.safe_load(raw_text)

        schema_version: str = str(data["schema_version"])
        contract_version: str = str(data["contract_version"])

        handshake_block: dict = data.get("handshake", {})
        compat_block: dict = handshake_block.get("compatibility", {})
        min_ver: str = str(compat_block["min_runtime_contract_version"])
        max_ver: str = str(compat_block["max_runtime_contract_version"])

        fail_mode_raw: str = handshake_block.get("fail_mode", "hard_fail")
        mode = (
            HandshakeMode.DEGRADED
            if fail_mode_raw == "degraded"
            else HandshakeMode.HARD_FAIL
        )

        brains_block: dict = data.get("brains", {})

        required_entries: list[dict] = brains_block.get("required", []) or []
        required_brains: frozenset[str] = frozenset(
            e["brain_id"] for e in required_entries
        )

        optional_entries: list[dict] = brains_block.get("optional", []) or []
        optional_brains: frozenset[str] = frozenset(
            e["brain_id"] for e in optional_entries
        )

        allowlist_block: dict = data.get("allowlist", {})
        allowed_brain_ids: list[str] = allowlist_block.get("allowed_brain_ids", []) or []
        allowed_brains: frozenset[str] = frozenset(allowed_brain_ids)

        # Build required_capabilities from required[] entries
        required_capabilities: dict[str, frozenset[str]] = {}
        for entry in required_entries:
            brain_id = entry["brain_id"]
            caps = entry.get("required_capabilities") or []
            if caps:
                required_capabilities[brain_id] = frozenset(caps)

        return PolicyManifest(
            schema_version=schema_version,
            contract_version=contract_version,
            min_runtime_contract_version=min_ver,
            max_runtime_contract_version=max_ver,
            required_brains=required_brains,
            optional_brains=optional_brains,
            allowed_brains=allowed_brains,
            required_capabilities=required_capabilities,
            mode=mode,
        )


# ---------------------------------------------------------------------------
# 3. JprimeRuntimeInventoryProvider
# ---------------------------------------------------------------------------

class JprimeRuntimeInventoryProvider:
    """Fetch the live runtime brain inventory from j-prime's /v1/brains endpoint.

    On any network / timeout error the method raises
    ``RuntimeError("RUNTIME_INVENTORY_STALE: ...")``.
    """

    def __init__(self, endpoint: str, timeout_s: float = 5.0) -> None:
        # Normalise: strip trailing slash so we can always append /v1/brains cleanly
        self._endpoint = endpoint.rstrip("/")
        self._timeout_s = timeout_s

    async def fetch_runtime_inventory(self) -> RuntimeInventory:
        """GET {endpoint}/v1/brains and parse the response into a RuntimeInventory."""
        url = f"{self._endpoint}/v1/brains"
        _log.debug("JprimeRuntimeInventoryProvider: fetching %s", url)

        raw: dict = await self._get_json(url)
        return self._parse_inventory(raw)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str) -> dict:
        """Perform the HTTP GET, preferring aiohttp; falling back to urllib."""
        try:
            import aiohttp  # type: ignore[import]
            return await self._fetch_aiohttp(url, aiohttp)
        except ImportError:
            _log.debug(
                "aiohttp not available; falling back to urllib for %s", url
            )
            return await self._fetch_urllib(url)

    async def _fetch_aiohttp(self, url: str, aiohttp_mod) -> dict:
        """Use aiohttp for the GET request."""
        timeout = aiohttp_mod.ClientTimeout(total=self._timeout_s)
        try:
            async with aiohttp_mod.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        except Exception as exc:
            raise RuntimeError(
                f"RUNTIME_INVENTORY_STALE: GET {url} failed via aiohttp — {exc}"
            ) from exc

    async def _fetch_urllib(self, url: str) -> dict:
        """Fall back to urllib.request executed in the default thread executor."""
        import urllib.request
        import urllib.error

        def _blocking_get() -> dict:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    body = resp.read()
                    return json.loads(body)
            except (urllib.error.URLError, OSError) as exc:
                raise RuntimeError(
                    f"RUNTIME_INVENTORY_STALE: GET {url} failed via urllib — {exc}"
                ) from exc

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _blocking_get)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"RUNTIME_INVENTORY_STALE: GET {url} unexpected error — {exc}"
            ) from exc

    @staticmethod
    def _parse_inventory(data: dict) -> RuntimeInventory:
        """Convert the raw /v1/brains JSON dict into a RuntimeInventory dataclass."""
        schema_version: str = str(data.get("schema_version", ""))
        contract_version: str = str(data.get("contract_version", ""))
        generated_at_epoch_s: int = int(data.get("generated_at_epoch_s", 0))

        raw_brains: dict = data.get("brains", {})
        brains: dict[str, BrainDescriptor] = {}
        for brain_id, b in raw_brains.items():
            brains[brain_id] = BrainDescriptor(
                brain_id=str(b.get("brain_id", brain_id)),
                provider=str(b.get("provider", "")),
                # Response sends a list; contract stores a frozenset
                capabilities=frozenset(b.get("capabilities") or []),
                routable=bool(b.get("routable", False)),
                health_state=str(b.get("health_state", "unknown")),
                version=str(b.get("version", "")),
                contract_version=str(b.get("contract_version", "")),
            )

        return RuntimeInventory(
            schema_version=schema_version,
            contract_version=contract_version,
            generated_at_epoch_s=generated_at_epoch_s,
            brains=brains,
        )


# ---------------------------------------------------------------------------
# 4. run_boot_handshake — convenience function
# ---------------------------------------------------------------------------

async def run_boot_handshake(
    policy_path: Path,
    jprime_endpoint: str,
    *,
    timeout_s: float = 5.0,
    logger: Optional[logging.Logger] = None,
) -> HandshakeResult:
    """Load policy, fetch runtime inventory, run handshake.

    Raises RuntimeError on hard fail.
    Returns HandshakeResult on success or degraded.
    """
    _logger = logger or _log

    loader = YamlPolicyLoader(policy_path)
    provider = JprimeRuntimeInventoryProvider(jprime_endpoint, timeout_s=timeout_s)
    engine = ConcreteHandshakeEngine()

    _logger.debug("run_boot_handshake: loading policy from %s", policy_path)
    policy = await loader.load_policy()

    _logger.debug(
        "run_boot_handshake: fetching runtime inventory from %s", jprime_endpoint
    )
    inventory = await provider.fetch_runtime_inventory()

    _logger.debug("run_boot_handshake: evaluating handshake")
    result = engine.evaluate(policy, inventory)

    if not result.accepted:
        raise RuntimeError(
            f"Boot handshake HARD FAIL: reason_codes={result.reason_codes} "
            f"phantom_required={result.diff.phantom_required}"
        )

    if result.degraded:
        _logger.warning(
            "Boot handshake DEGRADED: reason_codes=%s missing_optional=%s",
            result.reason_codes,
            result.diff.optional_missing,
        )
    else:
        _logger.info(
            "Boot handshake ACCEPTED: active_brain_set=%s",
            result.active_brain_set,
        )

    return result
