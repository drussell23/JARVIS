"""Versioned pickle envelope with integrity checking and migration support.

v310.0: All pickle cache files must use this envelope for safe deserialization.
Protocol: MAGIC(8) + pickle(envelope_dict) with hash + version + migration chain.
Atomic write: tmp + fsync + os.replace.
"""
import hashlib
import os
import pickle
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("jarvis.cache_envelope")

ENVELOPE_MAGIC = b"JCACHE01"

_MIGRATIONS: Dict[Tuple[int, int], Callable[[Any], Any]] = {}


def register_migration(from_v: int, to_v: int, fn: Callable[[Any], Any]) -> None:
    """Register a data migration handler between versions."""
    _MIGRATIONS[(from_v, to_v)] = fn


def save_versioned(path: Path, data: Any, version: int) -> None:
    """Save data wrapped in version envelope with integrity hash."""
    payload_bytes = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    envelope = {
        "magic": ENVELOPE_MAGIC.decode(),
        "schema_version": version,
        "payload_hash": payload_hash,
        "data": data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, ENVELOPE_MAGIC)
        os.write(fd, pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def load_versioned(path: Path, expected_version: int) -> Optional[Any]:
    """Load data, returning None on version mismatch, corruption, or unknown version."""
    try:
        with open(path, "rb") as f:
            magic = f.read(len(ENVELOPE_MAGIC))
            if magic != ENVELOPE_MAGIC:
                _quarantine(path, "missing_magic")
                return None
            envelope = pickle.load(f)

        if not isinstance(envelope, dict):
            _quarantine(path, "invalid_envelope_type")
            return None

        file_version = envelope.get("schema_version")
        if file_version is None:
            _quarantine(path, "missing_version")
            return None

        if isinstance(file_version, int) and file_version > expected_version:
            _quarantine(path, f"unknown_major_version_{file_version}_vs_{expected_version}")
            logger.warning(
                "Cache at %s: unknown version %d > expected %d. Quarantined.",
                path, file_version, expected_version,
            )
            return None

        if file_version == expected_version:
            data = envelope.get("data")
            stored_hash = envelope.get("payload_hash")
            if stored_hash:
                actual_hash = hashlib.sha256(
                    pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
                ).hexdigest()
                if actual_hash != stored_hash:
                    _quarantine(path, "payload_hash_mismatch")
                    return None
            return data

        # Try migration chain
        current_data = envelope.get("data")
        current_version = file_version
        while current_version < expected_version:
            next_version = current_version + 1
            migration = _MIGRATIONS.get((current_version, next_version))
            if migration is None:
                _quarantine(path, f"no_migration_{current_version}_to_{next_version}")
                return None
            try:
                current_data = migration(current_data)
                current_version = next_version
            except Exception as e:
                _quarantine(path, f"migration_failed_{current_version}_to_{next_version}")
                logger.warning("Migration failed for %s: %s", path, e)
                return None

        save_versioned(path, current_data, expected_version)
        logger.info("Migrated cache %s from v%d to v%d", path, file_version, expected_version)
        return current_data

    except FileNotFoundError:
        return None
    except Exception as e:
        _quarantine(path, f"load_exception_{type(e).__name__}")
        logger.warning("Cache load failed at %s: %s", path, e)
        return None


def _quarantine(path: Path, reason: str) -> None:
    """Move corrupted cache to quarantine with reason suffix."""
    quarantine_path = path.with_suffix(f".quarantine.{reason}")
    try:
        if path.exists():
            shutil.move(str(path), str(quarantine_path))
            logger.info("Quarantined cache %s -> %s", path, quarantine_path)
    except OSError as e:
        logger.debug("Quarantine failed for %s: %s", path, e)
