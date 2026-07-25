"""
Streaming Audio Processor with Chunk Management
Optimized for low memory usage and real-time processing
"""

import asyncio
import logging
import numpy as np
from typing import Callable, Optional, List, Tuple, Any
from dataclasses import dataclass
from collections import deque
import threading
import queue
import time
from concurrent.futures import ThreadPoolExecutor

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import DaemonThreadPoolExecutor
    DAEMON_EXECUTOR_AVAILABLE = True
except ImportError:
    DAEMON_EXECUTOR_AVAILABLE = False

from .optimization_config import StreamingConfig, OPTIMIZATION_CONFIG

logger = logging.getLogger(__name__)

@dataclass
class AudioChunk:
    """Single audio chunk with metadata"""
    data: np.ndarray
    timestamp: float
    sequence_number: int
    is_speech: bool = False
    features: Optional[dict] = None

class StreamingAudioProcessor:
    """
    Processes audio in streaming chunks for memory efficiency
    """
    
    def __init__(self, 
                 process_callback: Callable[[np.ndarray], Any],
                 config: StreamingConfig = None,
                 sample_rate: int = 16000):
        
        self.config = config or OPTIMIZATION_CONFIG.streaming
        self.sample_rate = sample_rate
        self.process_callback = process_callback
        
        # Chunk management
        self.chunk_size = self.config.chunk_size_samples
        self.chunk_overlap = self.config.chunk_overlap_samples
        self.chunk_duration = self.chunk_size / sample_rate
        
        # Buffers
        self.input_queue = queue.Queue(maxsize=self.config.input_buffer_chunks)
        self.output_queue = queue.Queue(maxsize=self.config.output_buffer_chunks)
        self.overlap_buffer = deque(maxlen=self.chunk_overlap)
        
        # Processing state
        self.sequence_number = 0
        self.chunks_processed = 0
        self.total_latency = 0.0
        self.dropped_chunks = 0
        
        # Threading (use daemon threads for clean shutdown)
        if DAEMON_EXECUTOR_AVAILABLE:
            self.executor = DaemonThreadPoolExecutor(
                max_workers=self.config.worker_threads,
                thread_name_prefix='StreamingProcessor'
            )
        else:
            self.executor = ThreadPoolExecutor(max_workers=self.config.worker_threads)
        self.processing_thread = None
        self.running = False
        
        # Performance monitoring
        self.last_chunk_time = time.time()
        self.processing_times = deque(maxlen=100)
        
        logger.info(f"Streaming processor initialized: "
                   f"chunk_size={self.chunk_size}, "
                   f"overlap={self.chunk_overlap}, "
                   f"workers={self.config.worker_threads}")
    
    def start(self):
        """Start the streaming processor"""
        if self.running:
            return
        
        self.running = True
        self.processing_thread = threading.Thread(
            target=self._processing_loop,
            daemon=True
        )
        self.processing_thread.start()
        logger.info("Streaming processor started")
    
    def stop(self):
        """Stop the streaming processor"""
        self.running = False

        # Wait for processing thread
        if self.processing_thread and self.processing_thread.is_alive():
            logger.info("Waiting for processing thread to stop...")
            self.processing_thread.join(timeout=2.0)
            if self.processing_thread.is_alive():
                logger.warning("Processing thread did not stop within timeout")
                self.processing_thread.daemon = True
            self.processing_thread = None

        # Shutdown executor and wait for worker threads
        if self.executor:
            logger.info(f"Shutting down thread pool executor ({self.config.worker_threads} workers)...")
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None

        logger.info("Streaming processor stopped")
    
    def feed_audio(self, audio_data: np.ndarray) -> bool:
        """
        Feed audio data to the processor
        Returns False if buffer is full (data dropped)
        """
        # ── Zero-copy UI broadcast tap ────────────────────────────────────
        # FIRST statement in the ingest path, BEFORE any dtype conversion or
        # chunking, so the visualizer sees exactly what the mic delivered and
        # adds nothing to the STT critical path.
        #
        # macOS CoreAudio refuses a second handle on a captured device, so the
        # UI cannot open its own stream — it must observe this one. Safety is
        # structural: offer() stores a read-only VIEW (O(1), no copy) into a
        # single-slot latest-wins mailbox using a NON-BLOCKING lock, performs no
        # arithmetic, and swallows every exception. RMS happens on the consumer
        # side. A no-op when nothing is subscribed.
        try:
            from backend.voice.audio_broadcast_tap import offer_to_default_tap
            offer_to_default_tap(audio_data, sample_rate=self.sample_rate)
        except Exception:  # noqa: BLE001 — the tap can NEVER affect transcription
            pass

        # Convert to float32 if needed
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)
        
        # Split into chunks
        chunks = self._create_chunks(audio_data)
        
        # Queue chunks for processing
        dropped = False
        for chunk in chunks:
            try:
                if self.config.low_latency_mode:
                    # In low latency mode, drop old chunks if queue is full
                    if self.input_queue.full():
                        try:
                            self.input_queue.get_nowait()
                            self.dropped_chunks += 1
                            dropped = True
                        except queue.Empty:
                            pass
                
                self.input_queue.put(chunk, timeout=0.01)
            except queue.Full:
                self.dropped_chunks += 1
                dropped = True
                
                if self.dropped_chunks % 10 == 0:
                    logger.warning(f"Dropped {self.dropped_chunks} chunks due to full buffer")
        
        return not dropped
    
    async def feed_audio_async(self, audio_data: np.ndarray) -> bool:
        """Async version of feed_audio"""
        return await asyncio.to_thread(self.feed_audio, audio_data)
    
    def get_results(self, timeout: float = 0.1) -> List[Any]:
        """Get processed results"""
        results = []
        deadline = time.time() + timeout
        
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                
                result = self.output_queue.get(timeout=min(remaining, 0.01))
                results.append(result)
            except queue.Empty:
                break
        
        return results
    
    async def get_results_async(self, timeout: float = 0.1) -> List[Any]:
        """Async version of get_results"""
        return await asyncio.to_thread(self.get_results, timeout)
    
    def _create_chunks(self, audio_data: np.ndarray) -> List[AudioChunk]:
        """Split audio into overlapping chunks"""
        chunks = []
        
        # Add overlap from previous audio
        if len(self.overlap_buffer) > 0:
            audio_data = np.concatenate([
                np.array(list(self.overlap_buffer)),
                audio_data
            ])
        
        # Create chunks
        hop_size = self.chunk_size - self.chunk_overlap
        
        for i in range(0, len(audio_data) - self.chunk_size + 1, hop_size):
            chunk_data = audio_data[i:i + self.chunk_size]
            
            chunk = AudioChunk(
                data=chunk_data,
                timestamp=time.time(),
                sequence_number=self.sequence_number
            )
            
            self.sequence_number += 1
            chunks.append(chunk)
        
        # Save remainder for overlap
        remainder_start = len(chunks) * hop_size
        if remainder_start < len(audio_data):
            self.overlap_buffer.extend(audio_data[remainder_start:])
        
        return chunks
    
    def _processing_loop(self):
        """Main processing loop"""
        batch = []
        last_batch_time = time.time()
        
        while self.running:
            try:
                # Get chunk from queue
                timeout = self.config.batch_timeout_ms / 1000.0
                chunk = self.input_queue.get(timeout=timeout)
                batch.append(chunk)
                
                # Process batch if full or timeout
                should_process = (
                    len(batch) >= self.config.max_chunks_in_flight or
                    time.time() - last_batch_time > timeout
                )
                
                if should_process and batch:
                    # Submit batch for processing
                    self.executor.submit(self._process_batch, batch.copy())
                    batch.clear()
                    last_batch_time = time.time()
                    
            except queue.Empty:
                # Process any remaining batch
                if batch:
                    self.executor.submit(self._process_batch, batch.copy())
                    batch.clear()
                    last_batch_time = time.time()
            
            except Exception as e:
                logger.error(f"Error in processing loop: {e}")
    
    def _process_batch(self, batch: List[AudioChunk]):
        """Process a batch of chunks"""
        start_time = time.time()
        
        try:
            for chunk in batch:
                # Process chunk
                result = self._process_chunk(chunk)
                
                # Queue result
                if result is not None:
                    try:
                        self.output_queue.put(result, timeout=0.1)
                    except queue.Full:
                        logger.warning("Output queue full, dropping result")
                
                self.chunks_processed += 1
            
            # Track processing time
            processing_time = time.time() - start_time
            self.processing_times.append(processing_time)
            self.total_latency += processing_time
            
        except Exception as e:
            logger.error(f"Error processing batch: {e}")
    
    def _process_chunk(self, chunk: AudioChunk) -> Optional[Any]:
        """Process a single chunk"""
        try:
            # Run callback
            result = self.process_callback(chunk.data)
            
            # Add chunk metadata to result if dict
            if isinstance(result, dict):
                result['chunk_timestamp'] = chunk.timestamp
                result['chunk_sequence'] = chunk.sequence_number
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing chunk {chunk.sequence_number}: {e}")
            return None
    
    def get_stats(self) -> dict:
        """Get processing statistics"""
        avg_processing_time = (
            np.mean(self.processing_times) if self.processing_times else 0
        )
        
        return {
            "chunks_processed": self.chunks_processed,
            "dropped_chunks": self.dropped_chunks,
            "average_latency_ms": avg_processing_time * 1000,
            "total_latency_seconds": self.total_latency,
            "input_queue_size": self.input_queue.qsize(),
            "output_queue_size": self.output_queue.qsize(),
            "chunk_duration_ms": self.chunk_duration * 1000,
            "realtime_factor": avg_processing_time / self.chunk_duration if self.chunk_duration > 0 else 0
        }

class StreamingVADProcessor(StreamingAudioProcessor):
    """
    Streaming processor with integrated Voice Activity Detection
    """
    
    def __init__(self, 
                 process_callback: Callable[[np.ndarray], Any],
                 vad_callback: Callable[[np.ndarray], bool],
                 config: StreamingConfig = None,
                 sample_rate: int = 16000):
        
        super().__init__(process_callback, config, sample_rate)
        self.vad_callback = vad_callback
        
        # VAD state
        self.speech_chunks = deque(maxlen=10)
        self.silence_chunks = 0
        self.in_speech = False
    
    def _process_chunk(self, chunk: AudioChunk) -> Optional[Any]:
        """Process chunk with VAD filtering"""
        # Check if chunk contains speech
        chunk.is_speech = self.vad_callback(chunk.data)
        
        # Update speech state
        if chunk.is_speech:
            self.speech_chunks.append(chunk)
            self.silence_chunks = 0
            self.in_speech = True
        else:
            self.silence_chunks += 1
            
            # End of speech detection
            if self.in_speech and self.silence_chunks > 3:
                self.in_speech = False
                
                # Process accumulated speech
                if len(self.speech_chunks) > 2:
                    # Concatenate speech chunks
                    speech_data = np.concatenate([c.data for c in self.speech_chunks])
                    result = self.process_callback(speech_data)
                    
                    self.speech_chunks.clear()
                    return result
        
        return None

class ChunkedFeatureExtractor:
    """
    Extract features from audio chunks efficiently
    """
    
    def __init__(self, feature_functions: List[Callable], 
                 chunk_size: int = 1024):
        self.feature_functions = feature_functions
        self.chunk_size = chunk_size
        
        # Feature cache
        self.feature_cache = {}
        self.cache_size = 0
        self.max_cache_size_mb = 50
    
    def extract_features(self, audio_chunk: np.ndarray) -> dict:
        """Extract features with caching"""
        # Generate cache key
        chunk_hash = hash(audio_chunk.tobytes())
        
        # Check cache
        if chunk_hash in self.feature_cache:
            return self.feature_cache[chunk_hash]
        
        # Extract features
        features = {}
        for func in self.feature_functions:
            try:
                name = func.__name__
                features[name] = func(audio_chunk)
            except Exception as e:
                logger.error(f"Feature extraction error ({name}): {e}")
        
        # Cache features
        self._cache_features(chunk_hash, features)
        
        return features
    
    def _cache_features(self, key: int, features: dict):
        """Cache features with size limit"""
        # Estimate size
        size_mb = len(str(features)) / (1024**2)  # Rough estimate
        
        # Check if we need to clear cache
        if self.cache_size + size_mb > self.max_cache_size_mb:
            # Clear oldest half of cache
            num_to_remove = len(self.feature_cache) // 2
            for _ in range(num_to_remove):
                self.feature_cache.popitem(last=False)
            self.cache_size /= 2
        
        # Add to cache
        self.feature_cache[key] = features
        self.cache_size += size_mb

# Example usage
def example_streaming_setup():
    """Example of setting up streaming processing"""
    
    def process_audio(chunk: np.ndarray) -> dict:
        """Simple processing function"""
        return {
            "energy": np.sqrt(np.mean(chunk**2)),
            "max_amplitude": np.max(np.abs(chunk))
        }
    
    # Create processor
    processor = StreamingAudioProcessor(
        process_callback=process_audio,
        config=OPTIMIZATION_CONFIG.streaming
    )
    
    # Start processing
    processor.start()
    
    # Feed audio (in real app, this would come from microphone)
    sample_rate = 16000
    duration = 0.1  # 100ms chunks
    
    for i in range(10):
        # Generate dummy audio
        audio = np.random.randn(int(sample_rate * duration)) * 0.1
        processor.feed_audio(audio)
        
        # Get results
        results = processor.get_results()
        for result in results:
            print(f"Chunk result: {result}")
        
        time.sleep(0.05)
    
    # Get stats
    print("\nStreaming stats:", processor.get_stats())
    
    # Stop
    processor.stop()

if __name__ == "__main__":
    example_streaming_setup()