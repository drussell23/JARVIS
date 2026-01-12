#!/usr/bin/env python3
"""
Control Center Clicker Factory
===============================

Provides the best available Control Center clicker based on what's initialized.

Priority order:
1. UAE-Enhanced Clicker (Context + Situational Awareness)
2. SAI-Enhanced Clicker (Situational Awareness only)
3. Adaptive Clicker (Multi-method detection)
4. Basic Clicker (Fallback)

Usage:
    from display.control_center_clicker_factory import get_best_clicker

    # Get best available clicker
    clicker = get_best_clicker(vision_analyzer=analyzer)

    # Use it
    async with clicker as c:
        result = await c.click("control_center")

Author: Derek J. Russell
Date: October 2025
Version: 1.0.0
"""

import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


def get_best_clicker(
    vision_analyzer=None,
    cache_ttl: int = 86400,
    enable_verification: bool = True,
    prefer_uae: bool = True,
    force_new: bool = False
):
    """
    Get the best available Control Center clicker

    Args:
        vision_analyzer: Claude Vision analyzer
        cache_ttl: Cache TTL in seconds
        enable_verification: Enable click verification
        prefer_uae: Prefer UAE if available
        force_new: Force creation of new instance (bypass singleton)

    Returns:
        Best available clicker instance
    """
    clicker = None
    clicker_type = "unknown"

    # If force_new is True, clear singletons to ensure fresh instances
    if force_new:
        logger.info("[CLICKER-FACTORY] Force new instance requested, clearing singletons")
        try:
            import backend.display.adaptive_control_center_clicker as acc
            acc._adaptive_clicker = None
        except Exception:
            pass
        try:
            import backend.display.sai_enhanced_control_center_clicker as sai
            sai._sai_clicker = None
        except Exception:
            pass
        try:
            import backend.display.uae_enhanced_control_center_clicker as uae_mod
            uae_mod._uae_clicker = None
        except Exception:
            pass

    # Try UAE-Enhanced (best option)
    if prefer_uae:
        try:
            from backend.display.uae_enhanced_control_center_clicker import get_uae_clicker
            from backend.intelligence.uae_integration import get_uae

            # Check if UAE is initialized
            uae = get_uae()
            if uae is not None:
                logger.info("[CLICKER-FACTORY] ✅ Using UAE-Enhanced Clicker")
                clicker = get_uae_clicker(
                    vision_analyzer=vision_analyzer,
                    cache_ttl=cache_ttl,
                    enable_verification=enable_verification,
                    enable_uae=True
                )
                clicker_type = "uae_enhanced"
                return clicker
            else:
                logger.info("[CLICKER-FACTORY] UAE not initialized, trying SAI...")

        except ImportError as e:
            logger.debug(f"[CLICKER-FACTORY] UAE clicker not available: {e}")
        except Exception as e:
            logger.warning(f"[CLICKER-FACTORY] Error loading UAE clicker: {e}")

    # Try SAI-Enhanced (good option)
    if clicker is None:
        try:
            from backend.display.sai_enhanced_control_center_clicker import get_sai_clicker
            from backend.vision.situational_awareness import get_sai_engine

            # Check if SAI is available
            try:
                sai_engine = get_sai_engine()
                logger.info("[CLICKER-FACTORY] ✅ Using SAI-Enhanced Clicker")
                clicker = get_sai_clicker(
                    vision_analyzer=vision_analyzer,
                    cache_ttl=cache_ttl,
                    enable_verification=enable_verification,
                    enable_sai=True
                )
                clicker_type = "sai_enhanced"
                return clicker
            except Exception:
                logger.info("[CLICKER-FACTORY] SAI not available, trying Adaptive...")

        except ImportError as e:
            logger.debug(f"[CLICKER-FACTORY] SAI clicker not available: {e}")
        except Exception as e:
            logger.warning(f"[CLICKER-FACTORY] Error loading SAI clicker: {e}")

    # Try Adaptive Clicker (fallback)
    if clicker is None:
        try:
            from backend.display.adaptive_control_center_clicker import AdaptiveControlCenterClicker

            logger.info("[CLICKER-FACTORY] ✅ Using Adaptive Clicker")
            clicker = AdaptiveControlCenterClicker(
                vision_analyzer=vision_analyzer,
                cache_ttl=cache_ttl,
                enable_verification=enable_verification
            )
            clicker_type = "adaptive"
            return clicker

        except ImportError as e:
            logger.error(f"[CLICKER-FACTORY] Adaptive clicker not available: {e}")
        except Exception as e:
            logger.error(f"[CLICKER-FACTORY] Error loading Adaptive clicker: {e}")

    # Final fallback to basic clicker
    if clicker is None:
        try:
            from backend.display.control_center_clicker import ControlCenterClicker

            logger.warning("[CLICKER-FACTORY] ⚠️  Using Basic Clicker (fallback)")
            clicker = ControlCenterClicker(vision_analyzer=vision_analyzer)
            clicker_type = "basic"
            return clicker

        except Exception as e:
            logger.error(f"[CLICKER-FACTORY] ❌ No clicker available: {e}")
            raise RuntimeError("No Control Center clicker available")

    return clicker


def get_clicker_info() -> dict:
    """
    Get information about available clickers

    Returns:
        Dict with clicker availability info
    """
    info = {
        'uae_available': False,
        'sai_available': False,
        'adaptive_available': False,
        'basic_available': False,
        'recommended': 'unknown'
    }

    # Check UAE
    try:
        from backend.intelligence.uae_integration import get_uae
        uae = get_uae()
        info['uae_available'] = uae is not None and uae.is_active
        if info['uae_available']:
            info['recommended'] = 'uae'
    except Exception:
        pass

    # Check SAI
    try:
        from backend.vision.situational_awareness import get_sai_engine
        sai = get_sai_engine()
        info['sai_available'] = True
        if not info['uae_available']:
            info['recommended'] = 'sai'
    except Exception:
        pass

    # Check Adaptive
    try:
        from backend.display.adaptive_control_center_clicker import AdaptiveControlCenterClicker
        info['adaptive_available'] = True
        if not info['uae_available'] and not info['sai_available']:
            info['recommended'] = 'adaptive'
    except Exception:
        pass

    # Check Basic
    try:
        from backend.display.control_center_clicker import ControlCenterClicker
        info['basic_available'] = True
        if info['recommended'] == 'unknown':
            info['recommended'] = 'basic'
    except Exception:
        pass

    return info


# ============================================================================
# Convenience Functions
# ============================================================================

async def click_control_center(vision_analyzer=None, **kwargs):
    """
    Convenience function to click Control Center using best available method

    Args:
        vision_analyzer: Vision analyzer
        **kwargs: Additional arguments

    Returns:
        Click result
    """
    clicker = get_best_clicker(vision_analyzer=vision_analyzer)

    async with clicker as c:
        result = await c.click("control_center", **kwargs)
        return result


async def connect_to_device(device_name: str, vision_analyzer=None, **kwargs):
    """
    Convenience function to connect to AirPlay device

    Args:
        device_name: Device name
        vision_analyzer: Vision analyzer
        **kwargs: Additional arguments

    Returns:
        Connection result
    """
    clicker = get_best_clicker(vision_analyzer=vision_analyzer)

    async with clicker as c:
        if hasattr(c, 'connect_to_device'):
            result = await c.connect_to_device(device_name, **kwargs)
        else:
            # Fallback for basic clicker
            logger.warning("[FACTORY] Clicker doesn't support connect_to_device, using basic flow")
            result = await c.click("control_center")

        return result


# ============================================================================
# Example Usage
# ============================================================================

async def main():
    """Demo clicker factory"""
    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 80)
    print("Control Center Clicker Factory - Demo")
    print("=" * 80)

    # Get clicker info
    print("\n📊 Available Clickers:")
    info = get_clicker_info()
    print(f"   UAE: {'✅' if info['uae_available'] else '❌'}")
    print(f"   SAI: {'✅' if info['sai_available'] else '❌'}")
    print(f"   Adaptive: {'✅' if info['adaptive_available'] else '❌'}")
    print(f"   Basic: {'✅' if info['basic_available'] else '❌'}")
    print(f"\n   Recommended: {info['recommended'].upper()}")

    # Get best clicker
    print("\n🎯 Getting best clicker...")
    try:
        clicker = get_best_clicker()
        print(f"   Got: {clicker.__class__.__name__}")

        # Use it
        print("\n🖱️  Clicking Control Center...")
        result = await click_control_center()
        print(f"   Success: {result.success if hasattr(result, 'success') else result.get('success')}")

    except Exception as e:
        print(f"   ❌ Error: {e}")

    print("\n" + "=" * 80)
    print("✅ Demo complete!")
    print("=" * 80)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
