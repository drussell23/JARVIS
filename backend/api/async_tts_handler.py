"""
Async TTS Handler with Caching and Concurrent Processing
Optimizes JARVIS voice response time by using async operations and smart caching
"""

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Optional, Dict, Tuple
import aiofiles
import logging
from concurrent.futures import ThreadPoolExecutor
import subprocess
from datetime import datetime, timedelta

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

logger = logging.getLogger(__name__)


class AsyncTTSHandler:
    """Handles text-to-speech generation with async operations and caching"""
    
    def __init__(self, cache_dir: Optional[Path] = None, max_cache_size: int = 100):
        """
        Initialize the async TTS handler
        
        Args:
            cache_dir: Directory for caching audio files
            max_cache_size: Maximum number of cached audio files
        """
        self.cache_dir = cache_dir or Path.home() / ".jarvis" / "tts_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_cache_size = max_cache_size
        
        # Thread pool for CPU-bound operations
        if _HAS_MANAGED_EXECUTOR:
            self.executor = ManagedThreadPoolExecutor(max_workers=4, name='async_tts')
        else:
            self.executor = ThreadPoolExecutor(max_workers=4)
        
        # Cache metadata
        self._cache_metadata: Dict[str, dict] = {}
        self._load_cache_metadata()
        
        # Pre-generate common phrases
        self._common_phrases = [
            "Hello, how can I help you today?",
            "I'm processing your request",
            "Command executed successfully",
            "I'm sorry, I didn't understand that",
            "Is there anything else I can help you with?",
            "Goodbye!",
            "Opening that for you now",
            "I've completed that task",
            "Let me check that for you"
        ]
        
        # Start background cache warming
        asyncio.create_task(self._warm_cache())
        
    def _get_cache_key(self, text: str, voice: str = "Daniel") -> str:
        """Generate a cache key for the given text and voice"""
        content = f"{voice}:{text}".encode('utf-8')
        return hashlib.sha256(content).hexdigest()[:16]
    
    def _load_cache_metadata(self):
        """Load cache metadata from disk"""
        metadata_file = self.cache_dir / "metadata.json"
        if metadata_file.exists():
            import json
            try:
                with open(metadata_file, 'r') as f:
                    self._cache_metadata = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load cache metadata: {e}")
                self._cache_metadata = {}
    
    def _save_cache_metadata(self):
        """Save cache metadata to disk"""
        metadata_file = self.cache_dir / "metadata.json"
        import json
        try:
            with open(metadata_file, 'w') as f:
                json.dump(self._cache_metadata, f)
        except Exception as e:
            logger.error(f"Failed to save cache metadata: {e}")
    
    async def _warm_cache(self):
        """Pre-generate audio for common phrases"""
        logger.info("Warming TTS cache with common phrases...")
        tasks = []
        for phrase in self._common_phrases:
            task = self.generate_speech(phrase, use_cache=True)
            tasks.append(task)
        
        # Process in batches to avoid overwhelming the system
        batch_size = 3
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i + batch_size]
            await asyncio.gather(*batch, return_exceptions=True)
        
        logger.info(f"Cache warmed with {len(self._common_phrases)} common phrases")
    
    async def _run_subprocess_async(self, cmd: list) -> subprocess.CompletedProcess:
        """Run a subprocess asynchronously"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            lambda: subprocess.run(cmd, capture_output=True, check=True)
        )
    
    async def _generate_audio_file(self, text: str, voice: str = "Daniel") -> Tuple[Path, str]:
        """
        Generate audio file using macOS say command
        Returns tuple of (file_path, content_type)
        """
        import tempfile
        
        # Create temp file for AIFF output
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
            aiff_path = Path(tmp.name)
        
        # Generate audio with say command (async) - much slower rate for smooth, natural speech
        # Default macOS speech rate is 200, we'll use 160 for smoother delivery
        say_cmd = ["say", "-v", voice, "-r", "160", "-o", str(aiff_path), text]
        await self._run_subprocess_async(say_cmd)
        
        # Try to convert to MP3 for smaller size and better compatibility
        mp3_path = aiff_path.with_suffix(".mp3")
        
        # Try ffmpeg first (fastest)
        try:
            ffmpeg_cmd = [
                "ffmpeg", "-i", str(aiff_path),
                "-acodec", "libmp3lame",
                "-ab", "96k",  # Lower bitrate for speech
                "-ar", "22050",  # Lower sample rate for speech
                "-ac", "1",  # Mono for smaller size
                str(mp3_path),
                "-y"
            ]
            await self._run_subprocess_async(ffmpeg_cmd)
            
            # Clean up AIFF file
            aiff_path.unlink()
            return mp3_path, "audio/mpeg"
            
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Try lame as fallback
            try:
                lame_cmd = ["lame", "-b", "96", "-m", "m", str(aiff_path), str(mp3_path)]
                await self._run_subprocess_async(lame_cmd)
                aiff_path.unlink()
                return mp3_path, "audio/mpeg"
            except Exception:
                # Use AIFF if conversion fails
                logger.warning("Audio conversion failed, using AIFF format")
                return aiff_path, "audio/aiff"
    
    async def _cache_audio_file(self, temp_path: Path, cache_key: str) -> Path:
        """Cache the audio file and manage cache size"""
        cache_path = self.cache_dir / f"{cache_key}.mp3"
        
        # Copy file to cache asynchronously
        async with aiofiles.open(temp_path, 'rb') as src:
            content = await src.read()
        
        async with aiofiles.open(cache_path, 'wb') as dst:
            await dst.write(content)
        
        # Update metadata
        self._cache_metadata[cache_key] = {
            'path': str(cache_path),
            'created': datetime.now().isoformat(),
            'last_used': datetime.now().isoformat(),
            'use_count': 1
        }
        
        # Clean up old cache entries if needed
        if len(self._cache_metadata) > self.max_cache_size:
            await self._cleanup_cache()
        
        self._save_cache_metadata()
        return cache_path
    
    async def _cleanup_cache(self):
        """Remove least recently used cache entries"""
        # Sort by last_used time
        sorted_entries = sorted(
            self._cache_metadata.items(),
            key=lambda x: x[1].get('last_used', ''),
            reverse=False
        )
        
        # Remove oldest entries
        to_remove = len(self._cache_metadata) - int(self.max_cache_size * 0.8)
        for key, metadata in sorted_entries[:to_remove]:
            try:
                Path(metadata['path']).unlink()
            except Exception:
                pass
            del self._cache_metadata[key]
    
    async def generate_speech(
        self,
        text: str,
        voice: str = "Daniel",
        use_cache: bool = True
    ) -> Tuple[Path, str]:
        """
        Generate speech audio file with caching
        
        Args:
            text: Text to convert to speech
            voice: Voice to use (default: Daniel)
            use_cache: Whether to use caching
            
        Returns:
            Tuple of (file_path, content_type)
        """
        cache_key = self._get_cache_key(text, voice)
        
        # Check cache first
        if use_cache and cache_key in self._cache_metadata:
            cached_info = self._cache_metadata[cache_key]
            cache_path = Path(cached_info['path'])
            
            if cache_path.exists():
                # Update usage stats
                cached_info['last_used'] = datetime.now().isoformat()
                cached_info['use_count'] = cached_info.get('use_count', 0) + 1
                self._save_cache_metadata()
                
                logger.debug(f"TTS cache hit for: {text[:50]}...")
                return cache_path, "audio/mpeg"
        
        # Generate new audio
        logger.debug(f"Generating TTS for: {text[:50]}...")
        start_time = time.time()
        
        # Generate audio file
        temp_path, content_type = await self._generate_audio_file(text, voice)
        
        # Cache the result if caching is enabled
        if use_cache:
            final_path = await self._cache_audio_file(temp_path, cache_key)
            # Clean up temp file
            if temp_path != final_path:
                temp_path.unlink()
        else:
            final_path = temp_path
        
        generation_time = time.time() - start_time
        logger.info(f"TTS generated in {generation_time:.2f}s")
        
        return final_path, content_type
    
    async def preload_text(self, text: str, voice: str = "Daniel"):
        """Preload text into cache for instant playback later"""
        await self.generate_speech(text, voice, use_cache=True)
    
    def get_cache_stats(self) -> dict:
        """Get cache statistics"""
        total_size = sum(
            Path(info['path']).stat().st_size
            for info in self._cache_metadata.values()
            if Path(info['path']).exists()
        )
        
        return {
            'cache_entries': len(self._cache_metadata),
            'cache_size_mb': total_size / (1024 * 1024),
            'max_cache_size': self.max_cache_size,
            'cache_dir': str(self.cache_dir)
        }
    
    async def clear_cache(self):
        """Clear all cached audio files"""
        for key, info in self._cache_metadata.items():
            try:
                Path(info['path']).unlink()
            except Exception:
                pass

        self._cache_metadata.clear()
        self._save_cache_metadata()
        logger.info("TTS cache cleared")


# Global instance
_tts_handler: Optional[AsyncTTSHandler] = None


def get_tts_handler() -> AsyncTTSHandler:
    """Get or create the global TTS handler"""
    global _tts_handler
    if _tts_handler is None:
        _tts_handler = AsyncTTSHandler()
    return _tts_handler


async def generate_speech_async(text: str, voice: str = "Daniel") -> Tuple[Path, str]:
    """Convenience function to generate speech using the global handler"""
    handler = get_tts_handler()
    return await handler.generate_speech(text, voice)