"""Gmail label management for email triage.

Creates jarvis/* labels if they don't exist and applies labels to messages.
All operations are idempotent — safe to call repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from autonomy.email_triage.config import TriageConfig

logger = logging.getLogger("jarvis.email_triage.labels")


async def ensure_labels_exist(
    gmail_service: Any,
    config: TriageConfig,
) -> Dict[str, str]:
    """Create jarvis/* labels if they don't exist.

    Args:
        gmail_service: Authenticated Gmail API service.
        config: Triage config with label names.

    Returns:
        Dict mapping label name → label ID.
    """
    loop = asyncio.get_event_loop()
    needed = [
        config.label_tier1,
        config.label_tier2,
        config.label_tier3,
        config.label_tier4,
    ]
    label_map: Dict[str, str] = {}

    # Fetch existing labels
    result = await loop.run_in_executor(
        None,
        lambda: gmail_service.users().labels().list(userId="me").execute(),
    )
    existing = {l["name"]: l["id"] for l in result.get("labels", [])}

    for name in needed:
        if name in existing:
            label_map[name] = existing[name]
        else:
            created = await loop.run_in_executor(
                None,
                lambda n=name: gmail_service.users().labels().create(
                    userId="me",
                    body={
                        "name": n,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                ).execute(),
            )
            label_map[name] = created["id"]
            logger.info("Created Gmail label: %s (id=%s)", name, created["id"])

    return label_map


async def apply_label(
    gmail_service: Any,
    message_id: str,
    label_name: str,
    label_map: Dict[str, str],
) -> None:
    """Apply a label to a Gmail message. Idempotent.

    Args:
        gmail_service: Authenticated Gmail API service.
        message_id: Gmail message ID.
        label_name: Label name (e.g., "jarvis/tier1_critical").
        label_map: Dict from ensure_labels_exist().
    """
    label_id = label_map.get(label_name)
    if not label_id:
        logger.warning("Label '%s' not found in label map", label_name)
        return

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: gmail_service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute(),
        )
    except Exception as e:
        logger.warning("Failed to apply label %s to %s: %s", label_name, message_id, e)
