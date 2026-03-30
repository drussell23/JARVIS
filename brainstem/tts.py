"""Lightweight TTS via macOS say + afplay. No orchestrator dependency."""
import asyncio
import logging
import os
import tempfile

logger = logging.getLogger("jarvis.brainstem.tts")


async def speak(text: str, voice: str = "Daniel", rate: int = 175, timeout: float = 15.0) -> bool:
    if not text or not text.strip():
        return False
    if len(text) > 500:
        text = text[:497] + "..."
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False, dir=tempfile.gettempdir()) as tmp:
            tmp_path = tmp.name
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-r", str(rate), "-o", tmp_path, text,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("[TTS] say timed out after %.0fs", timeout)
            return False
        if proc.returncode != 0:
            logger.warning("[TTS] say failed with exit code %d", proc.returncode)
            return False
        play_proc = await asyncio.create_subprocess_exec(
            "afplay", tmp_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(play_proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            play_proc.kill()
            return False
        return play_proc.returncode == 0
    except Exception as e:
        logger.error("[TTS] Failed: %s", e)
        return False
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
